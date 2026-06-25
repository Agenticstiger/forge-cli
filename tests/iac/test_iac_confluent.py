# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for the Confluent Tableflow IaC plugin — contract → .tf.json.

Pure-function tests: no credentials, no network, no paid Confluent account. The
live ``tofu apply`` against real Tableflow is a separately-gated follow-up
(FLUID_IAC_LIVE_CONFLUENT=1) — see RFC-streaming-extension §15.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

import fluid_build
from fluid_build.iac import build_module, get_iac_plugin, required_providers, resolve_engine
from fluid_build.iac.providers.confluent import validate_confluent_binding
from fluid_build.providers.aws.util.warehouse import get_iceberg_warehouse

pytestmark = [pytest.mark.unit, pytest.mark.provider]

_SCHEMA = Path(fluid_build.__file__).parent / "schemas" / "fluid-schema-0.7.5.json"


def _loc(**overrides):
    loc = {
        "environment_id": "env-123",
        "kafka_cluster_id": "lkc-456",
        "bucket": "my-lake-bucket",
        "confluent_role_arn": "arn:aws:iam::111122223333:role/tableflow",
        "database": "analytics",
        "table": "orders",
        "topic": "orders",
    }
    loc.update(overrides)
    return loc


def _contract(loc=None):
    return {
        "id": "gold.orders",
        "name": "Orders",
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "confluent",
                    "format": "iceberg",
                    "location": loc if loc is not None else _loc(),
                },
            }
        ],
    }


def _emit(contract):
    return json.loads(build_module(get_iac_plugin("confluent"), contract))["resource"]


# ── resource shape ──────────────────────────────────────────────────────────


def test_emits_three_tableflow_resources():
    res = _emit(_contract())
    assert set(res) == {
        "confluent_provider_integration",
        "confluent_tableflow_topic",
        "confluent_catalog_integration",
    }
    (name,) = list(res["confluent_tableflow_topic"])
    topic = res["confluent_tableflow_topic"][name]
    assert topic["table_formats"] == ["ICEBERG"]
    assert topic["environment"] == {"id": "env-123"}
    assert topic["kafka_cluster"] == {"id": "lkc-456"}
    assert topic["byob_aws"]["bucket_name"] == "my-lake-bucket"
    # the provider_integration_id is a tofu cross-reference, not a literal
    assert (
        topic["byob_aws"]["provider_integration_id"]
        == f"${{confluent_provider_integration.{name}.id}}"
    )

    pi = res["confluent_provider_integration"][name]
    assert pi["aws"]["customer_role_arn"] == "arn:aws:iam::111122223333:role/tableflow"

    glue = res["confluent_catalog_integration"][name]["aws_glue"]
    assert glue["custom_database"] == "analytics"
    assert glue["provider_integration_id"] == f"${{confluent_provider_integration.{name}.id}}"


def test_emit_is_deterministic():
    c = _contract()
    assert build_module(get_iac_plugin("confluent"), c) == build_module(
        get_iac_plugin("confluent"), c
    )


def test_emit_is_credential_free():
    rendered = build_module(get_iac_plugin("confluent"), _contract())
    for var in get_iac_plugin("confluent").credential_env_vars:
        assert var not in rendered  # no API key/secret env-var names leak into the module


def test_no_exposure_emits_no_confluent_resources():
    # an aws-bound expose must not trigger the confluent emitter
    c = {"id": "x.y", "exposes": [{"exposeId": "t", "binding": {"platform": "aws"}}]}
    assert build_module(get_iac_plugin("confluent"), c)  # renders a valid (empty-resource) module
    assert json.loads(build_module(get_iac_plugin("confluent"), c)).get("resource") is None


# ── framework wiring ────────────────────────────────────────────────────────


def test_provider_pin_and_engine():
    pin = required_providers("confluent")["confluent"]
    assert pin["source"] == "confluentinc/confluent"
    assert resolve_engine(None, "confluent") == "opentofu"


def test_required_providers_block_in_module():
    doc = json.loads(build_module(get_iac_plugin("confluent"), _contract()))
    assert doc["terraform"]["required_providers"]["confluent"]["source"] == "confluentinc/confluent"


# ── zero-drift: same bucket + db the AWS Glue table resolves to ─────────────


def test_zero_drift_bucket_matches_aws_warehouse():
    loc = _loc()
    res = _emit(_contract(loc))
    (name,) = list(res["confluent_tableflow_topic"])
    bucket = res["confluent_tableflow_topic"][name]["byob_aws"]["bucket_name"]
    # the same bucket feeds the AWS Iceberg warehouse writer -> they cannot diverge
    assert get_iceberg_warehouse(loc, account_ref="111122223333").startswith(f"s3://{bucket}")
    assert (
        res["confluent_catalog_integration"][name]["aws_glue"]["custom_database"] == loc["database"]
    )


# ── validator (anti-no-op gate, §15 pt3) ────────────────────────────────────


def test_validator_clean_on_complete_binding():
    assert validate_confluent_binding(_contract()) == ([], [])


@pytest.mark.parametrize(
    "drop", ["environment_id", "kafka_cluster_id", "bucket", "confluent_role_arn"]
)
def test_validator_requires_each_hard_input(drop):
    loc = _loc()
    loc.pop(drop)
    errors, _ = validate_confluent_binding(_contract(loc))
    assert any(drop.split("_")[0] in e for e in errors), errors


def test_validator_rejects_non_iceberg_format():
    c = _contract()
    c["exposes"][0]["binding"]["format"] = "parquet"
    errors, _ = validate_confluent_binding(c)
    assert any("format=iceberg" in e for e in errors)


def test_validator_warns_on_missing_glue_database():
    loc = _loc()
    loc.pop("database")
    errors, warnings = validate_confluent_binding(_contract(loc))
    assert errors == []  # database is a warning, not a hard error
    assert any("Glue database" in w for w in warnings)


# ── schema: 0.7.5 accepts the confluent platform + location keys ────────────


def _schema():
    return json.loads(_SCHEMA.read_text())


def test_schema_platform_enum_has_confluent():
    schema = _schema()
    for defn in schema.get("$defs", {}).values():
        plat = (defn.get("properties") or {}).get("platform") or {}
        if "enum" in plat and "snowflake" in plat["enum"]:
            assert "confluent" in plat["enum"]
            return
    raise AssertionError("binding platform enum not found in schema")


def test_schema_binding_location_accepts_confluent_keys():
    # bindingLocation is additionalProperties:false, so the new keys must be
    # declared or a confluent contract would fail validation.
    loc_schema = _schema()["$defs"]["bindingLocation"]
    errs = list(
        Draft7Validator(loc_schema).iter_errors(
            {
                "environment_id": "env-1",
                "kafka_cluster_id": "lkc-1",
                "confluent_role_arn": "arn:aws:iam::1:role/x",
                "bucket": "b",
                "database": "d",
                "table": "t",
            }
        )
    )
    assert errs == [], [e.message for e in errs]
