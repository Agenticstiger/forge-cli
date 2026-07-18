# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Packaging-modes PR3 — the shared-bucket name-resolution fix.

PR2 shipped a known limitation: :func:`_emit_lakeformation` resolved the
binding's bucket through ``warehouse.normalize_location`` (which expands
``{{ env.* }}`` and falls back to ``{account}-fluid-data``) while
:func:`_emit_referenced_containers` keyed the ``data.aws_s3_bucket``
lookup off the **raw** contract value. A bucket name the two resolvers
disagree about therefore produced a data source under one key and a
``${data.aws_s3_bucket.<other-key>.id}`` reference under another — a
dangling reference that fails ``tofu validate``.

Two properties are pinned here:

* ``TestSharedBucketKeysAgree`` — both sides derive the identical key, for
  a resolvable ``{{ env.* }}`` bucket and for a plain literal.
* ``TestUnresolvableSharedBucketFailsClosed`` — an *unresolvable* pool
  bucket is an error, not a silent fallback to ``{account}-fluid-data``.
  Inventing a pool name would point the product at a different bucket than
  the one it declared: the same discipline as ``_require_pool_prefix`` and
  the resolver's ``pool-required``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping

import pytest

from fluid_build.iac.packaging import PackagingError
from fluid_build.iac.providers.aws import AwsIacPlugin

pytestmark = pytest.mark.unit

_REF_RE = re.compile(r"\$\{((?:data\.)?[A-Za-z0-9_]+\.[A-Za-z0-9_]+)\.")

SHARED = {"mode": "shared", "pool": "acme-pool"}


def _contract(bucket: str) -> Dict[str, Any]:
    """RFC Example 2 shape, with the pool bucket parameterised."""
    return {
        "fluidVersion": "0.7.6",
        "id": "telemetry-sdp",
        "name": "Telemetry SDP",
        "metadata": {"layer": "Bronze", "productType": "SDP"},
        "packaging": dict(SHARED),
        "exposes": [
            {
                "exposeId": "telemetry",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": {
                        "bucket": bucket,
                        "path": "telemetry/",
                        "database": "iot_pool",
                        "table": "telemetry",
                    },
                    "governance": {
                        "lakeFormation": {
                            "registerLocation": True,
                            "grants": [
                                {
                                    "principal": "arn:aws:iam::222222222222:role/consumer",
                                    "permissions": ["SELECT"],
                                }
                            ],
                        }
                    },
                },
                "contract": {"schema": [{"name": "device_id", "type": "string"}]},
            }
        ],
    }


def _declared(resources: Mapping[str, Any], data: Mapping[str, Any]) -> set:
    declared = {f"{rtype}.{name}" for rtype, block in resources.items() for name in block}
    declared |= {f"data.{dtype}.{name}" for dtype, block in data.items() for name in block}
    return declared


def _referenced(obj: Any, found: set) -> set:
    if isinstance(obj, str):
        found.update(_REF_RE.findall(obj))
    elif isinstance(obj, Mapping):
        for value in obj.values():
            _referenced(value, found)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _referenced(value, found)
    return found


class TestSharedBucketKeysAgree:
    """``emit`` and ``emit_data`` must key the pool bucket identically."""

    @pytest.mark.parametrize(
        "bucket",
        ["acme-iot-lake", "{{ env.FLUID_TEST_POOL_BUCKET }}"],
        ids=["literal", "env-template"],
    )
    def test_no_dangling_bucket_reference(self, bucket, monkeypatch):
        monkeypatch.setenv("FLUID_TEST_POOL_BUCKET", "acme-iot-lake")
        plugin = AwsIacPlugin()
        contract = _contract(bucket)
        resources = plugin.emit(contract)
        data = plugin.emit_data(contract)
        referenced = _referenced(resources, set())
        assert referenced <= _declared(
            resources, data
        ), f"dangling: {sorted(referenced - _declared(resources, data))}"

    def test_the_env_template_resolves_to_the_same_key_as_the_literal(self, monkeypatch):
        """The whole point: a templated name and its literal are one bucket."""
        monkeypatch.setenv("FLUID_TEST_POOL_BUCKET", "acme-iot-lake")
        plugin = AwsIacPlugin()
        literal = plugin.emit_data(_contract("acme-iot-lake"))["aws_s3_bucket"]
        templated = plugin.emit_data(_contract("{{ env.FLUID_TEST_POOL_BUCKET }}"))["aws_s3_bucket"]
        assert set(literal) == set(templated)
        assert literal == templated

    def test_the_data_source_looks_up_the_resolved_name_not_the_template(self, monkeypatch):
        monkeypatch.setenv("FLUID_TEST_POOL_BUCKET", "acme-iot-lake")
        data = AwsIacPlugin().emit_data(_contract("{{ env.FLUID_TEST_POOL_BUCKET }}"))
        buckets = data["aws_s3_bucket"]
        assert [body["bucket"] for body in buckets.values()] == ["acme-iot-lake"]
        assert not any("{{" in key for key in buckets)

    def test_the_bucket_policy_and_lf_arn_use_the_resolved_name(self, monkeypatch):
        monkeypatch.setenv("FLUID_TEST_POOL_BUCKET", "acme-iot-lake")
        resources = AwsIacPlugin().emit(_contract("{{ env.FLUID_TEST_POOL_BUCKET }}"))
        arns = [body["arn"] for body in resources.get("aws_lakeformation_resource", {}).values()]
        assert arns == ["arn:aws:s3:::acme-iot-lake/telemetry/"]
        policies = resources.get("aws_s3_bucket_policy", {})
        assert policies, "expected a bucket policy for the LF grant"
        assert not any("{{" in json_doc["policy"] for json_doc in policies.values())


class TestUnresolvableSharedBucketFailsClosed:
    """No inventing a pool name when the declared one cannot be resolved."""

    def test_unresolved_template_on_a_shared_bucket_raises(self, monkeypatch):
        monkeypatch.delenv("FLUID_TEST_POOL_BUCKET", raising=False)
        plugin = AwsIacPlugin()
        with pytest.raises(PackagingError) as excinfo:
            plugin.emit(_contract("{{ env.FLUID_TEST_POOL_BUCKET }}"))
        assert excinfo.value.kind == "shared-bucket-unresolved"

    def test_emit_data_fails_closed_the_same_way(self, monkeypatch):
        monkeypatch.delenv("FLUID_TEST_POOL_BUCKET", raising=False)
        with pytest.raises(PackagingError) as excinfo:
            AwsIacPlugin().emit_data(_contract("{{ env.FLUID_TEST_POOL_BUCKET }}"))
        assert excinfo.value.kind == "shared-bucket-unresolved"

    def test_the_message_names_the_binding_and_the_remedy(self, monkeypatch):
        monkeypatch.delenv("FLUID_TEST_POOL_BUCKET", raising=False)
        with pytest.raises(PackagingError) as excinfo:
            AwsIacPlugin().emit(_contract("{{ env.FLUID_TEST_POOL_BUCKET }}"))
        message = str(excinfo.value)
        assert "FLUID_TEST_POOL_BUCKET" in message
        assert "isolated" in message

    def test_an_owned_bucket_still_falls_back_to_the_account_bucket(self, monkeypatch):
        """LEGACY / isolated behaviour is untouched — the fallback is the point there."""
        monkeypatch.delenv("FLUID_TEST_POOL_BUCKET", raising=False)
        contract = _contract("{{ env.FLUID_TEST_POOL_BUCKET }}")
        contract["packaging"] = {"mode": "isolated", "pool": "acme-pool"}
        resources = AwsIacPlugin().emit(contract)
        assert resources.get("aws_s3_bucket"), "isolated mode still owns its bucket"

    def test_legacy_is_untouched(self, monkeypatch):
        monkeypatch.delenv("FLUID_TEST_POOL_BUCKET", raising=False)
        contract = _contract("{{ env.FLUID_TEST_POOL_BUCKET }}")
        contract.pop("packaging")
        resources = AwsIacPlugin().emit(contract)
        assert resources.get("aws_s3_bucket"), "legacy mode still owns its bucket"
