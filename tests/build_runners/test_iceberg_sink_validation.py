# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""PR4 — plan/validate-time checks for the Iceberg streaming sink."""

from __future__ import annotations

import pytest

from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    validate_iceberg_sink,
)

pytestmark = [pytest.mark.unit]


def _contract(*, binding=None, kc=None, outputs=None, sink_format="iceberg", with_expose=True):
    binding = binding or {
        "platform": "aws",
        "format": "iceberg",
        "location": {"database": "s", "table": "o", "bucket": "lake", "region": "us-east-1"},
    }
    build = {
        "id": "ingest",
        "pattern": "acquisition",
        "engine": "kafka-connect",
        "properties": {
            "source": {"kind": "postgres", "mode": "incremental_append"},
            "sink": {"format": sink_format},
            "kafka-connect": kc or {},
        },
    }
    if outputs is not None:
        build["outputs"] = outputs
    c = {"id": "b.x", "builds": [build], "exposes": []}
    if with_expose:
        c["exposes"] = [{"exposeId": "events", "kind": "table", "binding": binding}]
    return c


def _errs(contract):
    return validate_iceberg_sink(contract)[0]


def _warns(contract):
    return validate_iceberg_sink(contract)[1]


# ── happy path ──────────────────────────────────────────────────────────────


def test_valid_glue_iceberg_sink_is_clean():
    errors, warnings = validate_iceberg_sink(_contract())
    assert errors == []
    assert warnings == []


def test_non_iceberg_sink_is_ignored():
    assert validate_iceberg_sink(_contract(sink_format="parquet", with_expose=False)) == ([], [])


# ── build -> expose join ────────────────────────────────────────────────────


def test_iceberg_sink_without_iceberg_expose_errors():
    errs = _errs(_contract(with_expose=False))
    assert any("no expose with binding.format=iceberg" in e for e in errs)


def test_outputs_not_referencing_iceberg_expose_warns():
    warns = _warns(_contract(outputs=["something_else"]))
    assert any("don't reference the Iceberg expose" in w for w in warns)


def test_iceberg_table_alias_binding_counts_as_iceberg_expose():
    # the expose uses the iceberg_table alias -> still recognized (no error)
    binding = {
        "platform": "aws",
        "format": "iceberg_table",
        "location": {"database": "s", "table": "o", "bucket": "lake", "region": "us-east-1"},
    }
    assert _errs(_contract(binding=binding)) == []


# ── upsert / routing gates ──────────────────────────────────────────────────


def test_upsert_mode_rejected_in_v1():
    errs = _errs(_contract(kc={"streamingSink": {"upsertMode": True}}))
    assert any("upsertMode is not supported in v1" in e for e in errs)


def test_dynamic_routing_requires_route_field():
    errs = _errs(_contract(kc={"streamingSink": {"dynamicEnabled": True}}))
    assert any("requires streamingSink.routeField" in e for e in errs)


def test_dynamic_routing_with_route_field_ok():
    errs = _errs(_contract(kc={"streamingSink": {"dynamicEnabled": True, "routeField": "tbl"}}))
    assert errs == []


# ── catalog tagged-union completeness ───────────────────────────────────────


def test_rest_catalog_requires_uri_and_warehouse():
    binding = {
        "platform": "local",
        "format": "iceberg",
        "location": {"database": "default", "table": "events", "catalog": "rest"},
    }
    errs = _errs(_contract(binding=binding))
    assert any("rest catalog requires binding.location.uri" in e for e in errs)
    assert any("rest catalog requires binding.location.warehouse" in e for e in errs)


def test_rest_catalog_complete_ok():
    binding = {
        "platform": "local",
        "format": "iceberg",
        "location": {
            "database": "default",
            "table": "events",
            "catalog": "rest",
            "uri": "http://iceberg:8181",
            "warehouse": "s3://bucket/warehouse/",
        },
    }
    assert _errs(_contract(binding=binding)) == []


def test_glue_without_region_warns():
    binding = {
        "platform": "aws",
        "format": "iceberg",
        "location": {"database": "s", "table": "o", "bucket": "lake"},  # no region
    }
    warns = _warns(_contract(binding=binding))
    assert any("without binding.location.region" in w for w in warns)


# ── zero-drift cross-check (consumes same_warehouse) ────────────────────────


def test_override_warehouse_divergence_warns():
    warns = _warns(
        _contract(kc={"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": "s3://other/"}})
    )
    assert any("diverges from the binding warehouse" in w for w in warns)


def test_override_warehouse_matching_is_clean():
    # override equal to the derived warehouse (s3://lake/s/o/) -> no warning
    warns = _warns(
        _contract(kc={"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": "s3://lake/s/o/"}})
    )
    assert not any("diverges" in w for w in warns)


# ── managed Confluent Tableflow exposes are not self-managed KC sink targets ──


def test_confluent_expose_not_treated_as_kc_sink_target():
    # a confluent expose is managed Tableflow (the Confluent IaC plugin owns it),
    # so this validator must NOT claim it and demand REST/Glue catalog fields.
    binding = {
        "platform": "confluent",
        "format": "iceberg",
        "location": {
            "environment_id": "env-1",
            "kafka_cluster_id": "lkc-1",
            "bucket": "b",
            "confluent_role_arn": "arn:aws:iam::1:role/x",
            "database": "d",
            "table": "t",
        },
    }
    errs = _errs(_contract(binding=binding))
    # the only expose is confluent -> excluded -> a real iceberg KC sink build
    # correctly reports "no iceberg expose" rather than a bogus rest-catalog error
    assert any("no expose with binding.format=iceberg" in e for e in errs)
    assert not any("rest catalog requires" in e for e in errs)
