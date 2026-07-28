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

"""Schema validation for source-aligned data products (v0.7.3 acquisition pattern).

Asserts the new schema additions accept valid acquisition contracts and reject
invalid ones, that capability negotiation works, and that all 0.7.3 examples
remain backward-compatible (covered separately by test_examples_validate_unchanged).
"""

from __future__ import annotations

import copy
from typing import Any, Dict

import pytest

from fluid_build.schema_manager import FluidSchemaManager


@pytest.fixture(scope="module")
def manager() -> FluidSchemaManager:
    return FluidSchemaManager()


def _validate(mgr: FluidSchemaManager, contract: Dict[str, Any]):
    return mgr.validate_contract(contract, "0.7.3", offline_only=True)


@pytest.fixture
def minimal_acquisition_contract() -> Dict[str, Any]:
    """A minimum-viable acquisition contract (DuckDB filesystem source)."""
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.orders_csv",
        "name": "Orders CSV ingest",
        "domain": "sales",
        "metadata": {
            "layer": "Bronze",
            "owner": {"team": "data-platform", "email": "dp@co.example"},
        },
        "builds": [
            {
                "id": "ingest_orders",
                "pattern": "acquisition",
                "engine": "duckdb",
                "capabilities": ["full_refresh"],
                "properties": {
                    "source": {
                        "kind": "filesystem",
                        "connection": {"uri": "s3://landing-zone/orders/*.csv"},
                        "mode": "full_refresh",
                        "reader": {"format": "csv", "options": {"header": True}},
                    },
                    "sink": {"format": "parquet"},
                },
                "outputs": ["orders_raw"],
            }
        ],
        "exposes": [
            {
                "exposeId": "orders_raw",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "out/orders.parquet"},
                },
                "contract": {
                    "schema": [],
                    "schemaPolicy": "discover_and_freeze",
                },
            }
        ],
    }


class TestAcquisitionPatternRecognized:
    def test_minimal_acquisition_validates(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        result = _validate(manager, minimal_acquisition_contract)
        assert result.is_valid, f"Expected valid; errors={result.errors}"

    def test_pattern_enum_accepts_acquisition(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        # Already implicitly covered above; this test is explicit.
        contract = minimal_acquisition_contract
        assert contract["builds"][0]["pattern"] == "acquisition"
        result = _validate(manager, contract)
        assert result.is_valid

    @pytest.mark.parametrize(
        "engine",
        ["duckdb", "airbyte", "meltano", "dlt", "kafka-connect", "debezium"],
    )
    def test_all_six_ingestion_engines_accepted(
        self,
        manager: FluidSchemaManager,
        minimal_acquisition_contract: Dict[str, Any],
        engine: str,
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["engine"] = engine
        result = _validate(manager, contract)
        assert result.is_valid, f"engine={engine} rejected; errors={result.errors}"

    def test_existing_engines_still_accepted(
        self,
        manager: FluidSchemaManager,
        minimal_acquisition_contract: Dict[str, Any],
    ):
        # Switch to a Silver/transform shape to exercise the existing path.
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["id"] = "silver.transform"
        contract["metadata"]["layer"] = "Silver"
        contract["builds"] = [
            {
                "id": "tx",
                "pattern": "embedded-logic",
                "engine": "sql",
                "properties": {"sql": "SELECT 1"},
                "outputs": ["orders_raw"],
            }
        ]
        result = _validate(manager, contract)
        assert result.is_valid, f"Silver path broke; errors={result.errors}"


class TestAcquisitionRequiredFields:
    def test_missing_source_rejected(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        del contract["builds"][0]["properties"]["source"]
        result = _validate(manager, contract)
        assert not result.is_valid

    def test_missing_source_kind_rejected(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        del contract["builds"][0]["properties"]["source"]["kind"]
        result = _validate(manager, contract)
        assert not result.is_valid

    def test_missing_source_mode_rejected(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        del contract["builds"][0]["properties"]["source"]["mode"]
        result = _validate(manager, contract)
        assert not result.is_valid


class TestAcquisitionEnums:
    def test_invalid_mode_rejected(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["properties"]["source"]["mode"] = "fast_and_loose"
        result = _validate(manager, contract)
        assert not result.is_valid

    def test_invalid_capability_rejected(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["capabilities"] = ["invent_capability"]
        result = _validate(manager, contract)
        assert not result.is_valid

    def test_invalid_schema_policy_rejected(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["exposes"][0]["contract"]["schemaPolicy"] = "yolo"
        result = _validate(manager, contract)
        assert not result.is_valid


class TestAcquisitionDeliverySemantics:
    def test_delivery_block_with_dlq_validates(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["properties"]["delivery"] = {
            "guarantee": "exactly_once",
            "idempotencyKey": "{run_id}:{stream}:{record_pk}",
            "dlq": {
                "enabled": True,
                "sink": {"format": "parquet", "location": "s3://dlq/orders/"},
                "maxRecordsBeforeAbort": 1000,
                "alertOn": ["pii_classification_failed", "schema_violation"],
            },
        }
        result = _validate(manager, contract)
        assert result.is_valid, f"errors={result.errors}"

    def test_invalid_delivery_guarantee_rejected(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["properties"]["delivery"] = {"guarantee": "perfect"}
        result = _validate(manager, contract)
        assert not result.is_valid


class TestSchemaEvolutionBlock:
    def test_evolution_block_validates(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["properties"]["schemaEvolution"] = {
            "policy": "evolve_safe",
            "onAddedColumn": "include",
            "onRemovedColumn": "warn",
            "onTypeChange": "fail",
            "sourceFingerprint": "required",
        }
        result = _validate(manager, contract)
        assert result.is_valid, f"errors={result.errors}"

    def test_invalid_evolution_action_rejected(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["properties"]["schemaEvolution"] = {
            "policy": "evolve_safe",
            "onTypeChange": "yolo",
        }
        result = _validate(manager, contract)
        assert not result.is_valid


class TestQualityCostCatalog:
    def test_quality_gates_validates(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["properties"]["quality"] = {
            "gates": [
                {"rule": "not_null", "columns": ["id"], "severity": "error"},
                {
                    "rule": "regex",
                    "column": "email",
                    "pattern": "^[^@]+@[^@]+\\.[^@]+$",
                    "severity": "warn",
                },
            ],
            "onError": "route_to_dlq",
        }
        result = _validate(manager, contract)
        assert result.is_valid, f"errors={result.errors}"

    def test_cost_budget_validates(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["properties"]["cost"] = {
            "budget": {
                "monthly": {"rows": 10_000_000, "bytes": "50GB", "computeMinutes": 600},
                "onExceed": "abort",
            },
            "chargeback": {"team": "data-platform"},
        }
        result = _validate(manager, contract)
        assert result.is_valid, f"errors={result.errors}"

    def test_catalog_register_validates(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        # ``glue`` + ``snowflake_horizon`` were dropped from the
        # ``acquisitionCatalog.register`` enum on the OpenTofu-autogen
        # branch — those catalog backends are now absorbed by the IaC
        # plugin emit (see fluid_build/iac/providers/{aws,snowflake}.py),
        # so a contract that still requested them via the registrar
        # surface would be misleading. Test with two retained values
        # (``datahub`` + ``datamesh_manager``) to keep the validation
        # contract honest.
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["properties"]["catalog"] = {
            "register": ["datahub", "datamesh_manager"],
            "documentation": "auto",
        }
        result = _validate(manager, contract)
        assert result.is_valid, f"errors={result.errors}"

    def test_invalid_catalog_target_rejected(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["properties"]["catalog"] = {"register": ["mycustomthing"]}
        result = _validate(manager, contract)
        assert not result.is_valid


class TestImageSignatureSupplyChain:
    def test_image_signature_validates(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["engine"] = "airbyte"
        contract["builds"][0]["properties"]["airbyte"] = {
            "connector_image": "airbyte/source-faker:1.0.0",
            "image_signature": {
                "verifier": "cosign",
                "publicKey": "kms://aws/key/signing-prod",
                "slsaProvenance": "required",
            },
        }
        result = _validate(manager, contract)
        assert result.is_valid, f"errors={result.errors}"


class TestDeploymentModes:
    @pytest.mark.parametrize("mode", ["embedded", "bring-your-own", "managed"])
    def test_all_three_deployment_modes(
        self,
        manager: FluidSchemaManager,
        minimal_acquisition_contract: Dict[str, Any],
        mode: str,
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["engine"] = "airbyte"
        deploy: Dict[str, Any] = {"mode": mode}
        if mode == "managed":
            deploy["managed"] = {
                "target": "kubernetes",
                "chart": {
                    "repo": "https://airbytehq.github.io/helm-charts",
                    "name": "airbyte",
                    "version": "0.520.0",
                },
            }
        elif mode == "bring-your-own":
            deploy["server_url"] = "https://airbyte.internal:8001"
        contract["builds"][0]["properties"]["airbyte"] = {"deployment": deploy}
        result = _validate(manager, contract)
        assert result.is_valid, f"mode={mode} rejected; errors={result.errors}"

    def test_invalid_managed_target_rejected(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["engine"] = "airbyte"
        contract["builds"][0]["properties"]["airbyte"] = {
            "deployment": {"mode": "managed", "managed": {"target": "vm"}}
        }
        result = _validate(manager, contract)
        assert not result.is_valid


class TestRetentionBlock:
    def test_retention_validates(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["retention"] = {
            "runState": "P30D",
            "runLogs": "P90D",
            "lineage": "P365D",
            "dlq": "P180D",
        }
        result = _validate(manager, contract)
        assert result.is_valid, f"errors={result.errors}"

    def test_invalid_retention_format_rejected(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["retention"] = {"runState": "30 days"}
        result = _validate(manager, contract)
        assert not result.is_valid


class TestPreLandHookChain:
    def test_preland_chain_validates(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["properties"]["preLand"] = [
            "dlp_scan",
            "tokenize_pii",
            "quality_gate",
            "emit_lineage_input",
        ]
        result = _validate(manager, contract)
        assert result.is_valid, f"errors={result.errors}"

    def test_invalid_preland_hook_rejected(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["properties"]["preLand"] = ["custom_thing"]
        result = _validate(manager, contract)
        assert not result.is_valid


class TestConcurrencyLock:
    def test_concurrency_validates(
        self, manager: FluidSchemaManager, minimal_acquisition_contract: Dict[str, Any]
    ):
        contract = copy.deepcopy(minimal_acquisition_contract)
        contract["builds"][0]["properties"]["concurrency"] = {
            "lock": {"scope": "product", "timeout": "PT15M", "onContended": "abort"}
        }
        result = _validate(manager, contract)
        assert result.is_valid, f"errors={result.errors}"
