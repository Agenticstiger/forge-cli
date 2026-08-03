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

"""Schema pins for the 0.7.6 `packaging` block (RFC-packaging-modes.md PR1).

Three guarantees:

* valid isolated / shared / hybrid packaging contracts validate against the
  0.7.6 preview schema (top-level `packaging` and the per-exposure
  `binding.packaging` override);
* invalid shapes are rejected (`additionalProperties: false` discipline —
  bad mode enum, unknown container kind, bad container value, unknown block
  key, wrong types);
* the block is preview-gated: 0.7.5 (and thus every GA contract) rejects a
  `packaging` key outright, and a 0.7.6 contract WITHOUT the block still
  validates — legacy contracts are unaffected.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

import pytest

from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit


def _base_contract(version: str = "0.7.6") -> Dict[str, Any]:
    """A minimal valid contract for the given schema version."""
    return {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": "test.packaging",
        "name": "Packaging Schema Pin",
        "metadata": {
            "layer": "Gold",
            "owner": {"team": "platform", "email": "platform@example.com"},
        },
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"database": "SALES", "schema": "CDP", "table": "ORDERS"},
                },
                "contract": {"schema": [{"name": "ID", "type": "integer", "required": True}]},
            }
        ],
    }


@pytest.fixture(scope="module")
def manager() -> FluidSchemaManager:
    return FluidSchemaManager()


def _validate(manager: FluidSchemaManager, contract: Dict[str, Any], version: str):
    return manager.validate_contract(contract, schema_version=version, offline_only=True)


def _assert_valid(manager, contract, version="0.7.6"):
    result = _validate(manager, contract, version)
    msgs = [getattr(e, "message", str(e)) for e in result.errors]
    assert result.is_valid, f"expected valid against {version}, got: {msgs}"


def _assert_invalid(manager, contract, version="0.7.6"):
    result = _validate(manager, contract, version)
    assert not result.is_valid, f"expected INVALID against {version}, but it validated"


class TestValidPackagingShapes:
    def test_no_packaging_block_still_validates(self, manager):
        _assert_valid(manager, _base_contract())

    def test_isolated_mode(self, manager):
        contract = _base_contract()
        contract["packaging"] = {"mode": "isolated"}
        _assert_valid(manager, contract)

    def test_shared_mode_with_pool(self, manager):
        contract = _base_contract()
        contract["packaging"] = {"mode": "shared", "pool": "sales-domain"}
        _assert_valid(manager, contract)

    def test_hybrid_tier_rfc_example_1(self, manager):
        # RFC example 1 — Snowflake shared lake, isolated schema + warehouse.
        contract = _base_contract()
        contract["packaging"] = {
            "mode": "shared",
            "pool": "sales-domain",
            "containers": {"schema": "isolated", "warehouse": "isolated"},
        }
        _assert_valid(manager, contract)

    def test_pool_manifest(self, manager):
        contract = _base_contract()
        contract["packaging"] = {
            "mode": "shared",
            "pool": "iot-lake",
            "poolManifest": "pools/iot.yaml",
        }
        _assert_valid(manager, contract)

    def test_all_six_container_kinds_accepted(self, manager):
        contract = _base_contract()
        contract["packaging"] = {
            "mode": "isolated",
            "pool": "p",
            "containers": {
                "bucket": "shared",
                "database": "shared",
                "dataset": "shared",
                "schema": "isolated",
                "warehouse": "isolated",
                "cluster": "shared",
            },
        }
        _assert_valid(manager, contract)

    def test_binding_level_override(self, manager):
        contract = _base_contract()
        contract["exposes"][0]["binding"]["packaging"] = {"mode": "shared", "pool": "p"}
        _assert_valid(manager, contract)

    def test_containers_only_block(self, manager):
        contract = _base_contract()
        contract["packaging"] = {"containers": {"warehouse": "isolated"}}
        _assert_valid(manager, contract)


class TestInvalidPackagingShapes:
    @pytest.mark.parametrize(
        "block",
        [
            {"mode": "hybrid"},  # bad mode enum
            {"mode": "legacy"},  # LEGACY is a resolver sentinel, never a schema value
            {"containers": {"volume": "shared"}},  # unknown container kind
            {"containers": {"bucket": "owned"}},  # bad container value
            {"mode": "isolated", "tier": "gold"},  # unknown block key
            "shared",  # wrong type: string
            ["isolated"],  # wrong type: list
            {"pool": ""},  # empty pool id
            {"pool": 7},  # wrong pool type
            {"poolManifest": ""},  # empty manifest path
            {"containers": "shared"},  # containers not a mapping
        ],
        ids=[
            "bad-mode-enum",
            "legacy-not-a-schema-value",
            "unknown-container-kind",
            "bad-container-value",
            "unknown-block-key",
            "block-as-string",
            "block-as-list",
            "empty-pool",
            "pool-wrong-type",
            "empty-manifest",
            "containers-not-mapping",
        ],
    )
    def test_rejected_at_top_level(self, manager, block):
        contract = _base_contract()
        contract["packaging"] = copy.deepcopy(block)
        _assert_invalid(manager, contract)

    def test_bad_shape_rejected_at_binding_level_too(self, manager):
        contract = _base_contract()
        contract["exposes"][0]["binding"]["packaging"] = {"mode": "hybrid"}
        _assert_invalid(manager, contract)


class TestPreviewGateSeparation:
    """0.7.5 (GA) must not accept the block — it is 0.7.6-preview only."""

    def test_075_rejects_top_level_packaging(self, manager):
        contract = _base_contract(version="0.7.5")
        contract["packaging"] = {"mode": "isolated"}
        _assert_invalid(manager, contract, version="0.7.5")

    def test_075_rejects_binding_packaging(self, manager):
        contract = _base_contract(version="0.7.5")
        contract["exposes"][0]["binding"]["packaging"] = {"mode": "shared", "pool": "p"}
        _assert_invalid(manager, contract, version="0.7.5")

    def test_075_contract_without_packaging_still_valid(self, manager):
        _assert_valid(manager, _base_contract(version="0.7.5"), version="0.7.5")

    def test_075_contract_validates_as_076_unchanged(self, manager):
        # Backward compatibility: every 0.7.5 contract is a valid 0.7.6 one.
        contract = _base_contract(version="0.7.6")
        _assert_valid(manager, contract, version="0.7.6")

    def test_076_stays_preview_gated(self):
        # The preview gate is what keeps untagged contracts from silently
        # opting in (RFC §Migration & compatibility).
        assert "0.7.6" in FluidSchemaManager.PREVIEW_VERSIONS
        assert FluidSchemaManager.latest_bundled_version() != "0.7.6"
