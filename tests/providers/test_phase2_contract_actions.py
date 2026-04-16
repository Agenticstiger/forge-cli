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

# tests/providers/test_phase2_contract_actions.py
"""Tests for Phase 2: ContractHelper, ProviderAction, validate_actions."""

from __future__ import annotations

import pytest

try:
    import fluid_provider_sdk  # noqa: F401

    HAS_SDK = True
except ImportError:
    HAS_SDK = False

pytestmark = pytest.mark.skipif(not HAS_SDK, reason="fluid-provider-sdk not installed")

# ── Sample contracts ──────────────────────────────────────────────────

SAMPLE_071 = {
    "fluidVersion": "0.7.1",
    "kind": "DataProduct",
    "id": "crypto.bitcoin_prices",
    "name": "Bitcoin Prices",
    "description": "Real-time bitcoin data",
    "domain": "finance",
    "metadata": {
        "layer": "Gold",
        "owner": {"team": "data-eng", "email": "de@co.com"},
    },
    "tags": ["crypto", "real-time"],
    "labels": {"cost_center": "CC-123"},
    "sovereignty": {"jurisdiction": "EU"},
    "accessPolicy": {"grants": [{"principal": "group:analysts", "permissions": ["read"]}]},
    "builds": [
        {
            "id": "ingest",
            "pattern": "hybrid-reference",
            "engine": "python",
            "properties": {"sql": "SELECT * FROM raw"},
        }
    ],
    "consumes": [{"id": "raw_feed", "path": "/data/feed.csv", "format": "csv"}],
    "exposes": [
        {
            "exposeId": "btc_table",
            "kind": "table",
            "title": "Bitcoin Prices Table",
            "version": "1.0.0",
            "binding": {
                "platform": "gcp",
                "format": "bigquery_table",
                "location": {
                    "project": "my-proj",
                    "dataset": "crypto",
                    "table": "bitcoin_prices",
                    "region": "EU",
                },
            },
            "policy": {"classification": "Internal"},
            "tags": ["financial-data"],
            "labels": {"sensitivity": "internal"},
            "contract": {
                "schema": [
                    {
                        "name": "price_usd",
                        "type": "numeric",
                        "required": True,
                        "description": "BTC/USD",
                    },
                    {"name": "ts", "type": "timestamp", "required": True},
                ]
            },
        }
    ],
}

SAMPLE_057_OLD = {
    "fluidVersion": "0.5.7",
    "kind": "DataProduct",
    "id": "old.product",
    "name": "Old Product",
    "build": {
        "id": "transform",
        "engine": "sql",
        "transformation": {"properties": {"model": "models/main.sql"}},
    },
    "consumes": [{"name": "input_table", "location": {"path": "/data/input.parquet"}}],
    "exposes": [
        {
            "id": "output",
            "type": "table",
            "location": {
                "format": "bigquery_table",
                "properties": {
                    "project": "legacy-proj",
                    "dataset": "ds",
                    "table": "tbl",
                    "location": "US",
                },
            },
            "schema": [
                {"name": "col_a", "type": "string"},
            ],
        }
    ],
}

SAMPLE_SNOWFLAKE = {
    "fluidVersion": "0.7.1",
    "kind": "DataProduct",
    "id": "sf.product",
    "binding": {
        "location": {"database": "PROD_DB", "schema": "PUBLIC"},
    },
    "security": {
        "access_control": {"grants": [{"role": "ANALYST", "privilege": "SELECT"}]},
        "row_level_security": [{"table": "orders", "role": "ANALYST", "condition": "region='US'"}],
    },
    "build": {
        "procedures": [{"name": "refresh_data", "language": "SQL"}],
        "udfs": [{"name": "mask_email", "return_type": "VARCHAR"}],
        "sql": ["CREATE OR REPLACE TABLE ...", {"sql": "INSERT INTO ..."}],
    },
    "views": [{"name": "v_orders", "query": "SELECT * FROM orders"}],
    "streams": [{"name": "s_orders", "source_table": "orders"}],
    "exposes": [
        {
            "exposeId": "orders",
            "binding": {
                "format": "snowflake_table",
                "location": {"database": "PROD_DB", "schema": "PUBLIC", "table": "ORDERS"},
            },
            "contract": {
                "schema": [
                    {"name": "order_id", "type": "integer", "required": True},
                    {"name": "email", "type": "string", "labels": {"pii": "true"}},
                ]
            },
        }
    ],
}


# ═══════════════════════════════════════════════════════════════════
# ContractHelper
# ═══════════════════════════════════════════════════════════════════


class TestContractHelperIdentity:
    def test_basic_properties(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_071)
        assert ch.id == "crypto.bitcoin_prices"
        assert ch.name == "Bitcoin Prices"
        assert ch.kind == "DataProduct"
        assert ch.fluid_version == "0.7.1"
        assert ch.domain == "finance"
        assert ch.description == "Real-time bitcoin data"

    def test_metadata(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_071)
        assert ch.layer == "Gold"
        assert ch.owner["team"] == "data-eng"

    def test_tags_labels(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_071)
        assert "crypto" in ch.tags
        assert ch.labels["cost_center"] == "CC-123"

    def test_repr(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_071)
        r = repr(ch)
        assert "crypto.bitcoin_prices" in r
        assert "0.7.1" in r


class TestContractHelperExposes:
    def test_exposes_071(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_071)
        exps = ch.exposes()
        assert len(exps) == 1
        e = exps[0]
        assert e.id == "btc_table"
        assert e.kind == "table"
        assert e.platform == "gcp"
        assert e.format == "bigquery_table"
        assert e.dataset == "crypto"
        assert e.table == "bitcoin_prices"
        assert e.project == "my-proj"
        assert e.region == "EU"
        assert e.title == "Bitcoin Prices Table"

    def test_exposes_057_old_format(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_057_OLD)
        exps = ch.exposes()
        assert len(exps) == 1
        e = exps[0]
        assert e.id == "output"
        assert e.kind == "table"
        # Old format: location.format
        assert e.format == "bigquery_table"
        # Old format: location.properties.*
        assert e.project == "legacy-proj"
        assert e.dataset == "ds"
        assert e.table == "tbl"
        # region comes from location.properties.location (legacy GCP)
        assert e.region == "US"

    def test_expose_columns(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_071)
        e = ch.exposes()[0]
        assert len(e.columns) == 2
        assert e.columns[0].name == "price_usd"
        assert e.columns[0].type == "numeric"
        assert e.columns[0].required is True
        assert e.columns[0].description == "BTC/USD"

    def test_expose_columns_057_top_level_schema(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_057_OLD)
        e = ch.exposes()[0]
        assert len(e.columns) == 1
        assert e.columns[0].name == "col_a"

    def test_expose_policy_tags_labels(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_071)
        e = ch.exposes()[0]
        assert e.policy["classification"] == "Internal"
        assert "financial-data" in e.tags
        assert e.labels["sensitivity"] == "internal"

    def test_expose_snowflake_location(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_SNOWFLAKE)
        e = ch.exposes()[0]
        assert e.id == "orders"
        assert e.format == "snowflake_table"
        assert e.database == "PROD_DB"
        assert e.schema_name == "PUBLIC"
        assert e.table == "ORDERS"

    def test_expose_column_labels(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_SNOWFLAKE)
        cols = ch.exposes()[0].columns
        assert cols[1].name == "email"
        assert cols[1].labels.get("pii") == "true"


class TestContractHelperConsumes:
    def test_consumes_071(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_071)
        cons = ch.consumes()
        assert len(cons) == 1
        c = cons[0]
        assert c.id == "raw_feed"
        assert c.path == "/data/feed.csv"
        assert c.format == "csv"

    def test_consumes_057_location_path(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_057_OLD)
        cons = ch.consumes()
        assert len(cons) == 1
        c = cons[0]
        assert c.id == "input_table"
        assert c.path == "/data/input.parquet"


class TestContractHelperBuilds:
    def test_builds_array(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_071)
        bl = ch.builds()
        assert len(bl) == 1
        b = bl[0]
        assert b.id == "ingest"
        assert b.pattern == "hybrid-reference"
        assert b.engine == "python"
        assert b.sql == "SELECT * FROM raw"

    def test_builds_single_object(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_057_OLD)
        bl = ch.builds()
        assert len(bl) == 1
        b = bl[0]
        assert b.id == "transform"
        assert b.engine == "sql"
        # 0.4.0: transformation.properties.model -> sql
        assert b.sql == "models/main.sql"

    def test_primary_build(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_071)
        pb = ch.primary_build()
        assert pb is not None
        assert pb.id == "ingest"

    def test_primary_build_empty(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper({"id": "empty"})
        assert ch.primary_build() is None

    def test_snowflake_build_object(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_SNOWFLAKE)
        bl = ch.builds()
        assert len(bl) == 1
        # Snowflake uses singular "build" with procedures/udfs
        b = bl[0]
        assert b.raw.get("procedures") is not None


class TestContractHelperSecurity:
    def test_access_policy(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_071)
        ap = ch.access_policy
        assert len(ap["grants"]) == 1
        assert ap["grants"][0]["principal"] == "group:analysts"

    def test_snowflake_security(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_SNOWFLAKE)
        sec = ch.security
        grants = sec["access_control"]["grants"]
        assert len(grants) == 1
        assert grants[0]["role"] == "ANALYST"
        rls = sec["row_level_security"]
        assert len(rls) == 1

    def test_sovereignty(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_071)
        sov = ch.sovereignty
        assert sov["jurisdiction"] == "EU"

    def test_snowflake_top_level_binding(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_SNOWFLAKE)
        assert ch.binding_location["database"] == "PROD_DB"
        assert ch.binding_location["schema"] == "PUBLIC"

    def test_snowflake_views_streams(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_SNOWFLAKE)
        assert len(ch.views) == 1
        assert ch.views[0]["name"] == "v_orders"
        assert len(ch.streams) == 1
        assert ch.streams[0]["source_table"] == "orders"


class TestContractHelperDictCompat:
    def test_get(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_071)
        assert ch.get("id") == "crypto.bitcoin_prices"
        assert ch.get("nonexistent", 42) == 42

    def test_contains(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_071)
        assert "id" in ch
        assert "nonexistent" not in ch

    def test_raw(self):
        from fluid_provider_sdk import ContractHelper

        ch = ContractHelper(SAMPLE_071)
        assert ch.raw["fluidVersion"] == "0.7.1"


# ═══════════════════════════════════════════════════════════════════
# ProviderAction + validate_actions
# ═══════════════════════════════════════════════════════════════════


class TestProviderAction:
    def test_creation(self):
        from fluid_provider_sdk import ProviderAction

        a = ProviderAction(
            op="create_dataset",
            resource_type="dataset",
            resource_id="crypto_data",
            params={"project": "my-proj", "location": "US"},
            phase="infrastructure",
        )
        assert a.op == "create_dataset"
        assert a.resource_type == "dataset"
        assert a.phase == "infrastructure"

    def test_to_dict(self):
        from fluid_provider_sdk import ProviderAction

        a = ProviderAction(op="create_table", resource_id="tbl_1", resource_type="table")
        d = a.to_dict()
        assert d["op"] == "create_table"
        assert d["resource_id"] == "tbl_1"
        assert "phase" not in d  # "default" is omitted

    def test_from_dict(self):
        from fluid_provider_sdk import ProviderAction

        d = {
            "op": "grant_access",
            "resource_type": "role",
            "resource_id": "analyst_role",
            "depends_on": ["crypto_data"],
            "phase": "iam",
        }
        a = ProviderAction.from_dict(d)
        assert a.op == "grant_access"
        assert a.depends_on == ["crypto_data"]
        assert a.phase == "iam"

    def test_from_dict_legacy_extra_keys(self):
        """Legacy action dicts with unknown keys -> params."""
        from fluid_provider_sdk import ProviderAction

        d = {"op": "bq.ensure_table", "dataset": "crypto", "table": "btc", "schema": []}
        a = ProviderAction.from_dict(d)
        assert a.op == "bq.ensure_table"
        assert a.params["dataset"] == "crypto"
        assert a.params["table"] == "btc"

    def test_roundtrip(self):
        from fluid_provider_sdk import ProviderAction

        original = ProviderAction(
            op="execute_sql",
            resource_type="transformation",
            resource_id="step_1",
            params={"sql": "SELECT 1"},
            depends_on=["load_data"],
            phase="build",
            idempotent=False,
            description="Run query",
            tags={"env": "prod"},
        )
        d = original.to_dict()
        restored = ProviderAction.from_dict(d)
        assert restored.op == original.op
        assert restored.resource_id == original.resource_id
        assert restored.depends_on == original.depends_on
        assert restored.phase == original.phase
        assert restored.idempotent == original.idempotent
        assert restored.description == original.description

    def test_dict_compat_getitem(self):
        from fluid_provider_sdk import ProviderAction

        a = ProviderAction(op="load", resource_id="x")
        assert a["op"] == "load"
        assert "op" in a
        assert a.get("missing", 99) == 99


class TestValidateActions:
    def test_valid(self):
        from fluid_provider_sdk import ProviderAction, validate_actions

        actions = [
            ProviderAction(op="create_db", resource_id="db"),
            ProviderAction(op="create_table", resource_id="tbl", depends_on=["db"]),
        ]
        assert validate_actions(actions) == []

    def test_missing_op(self):
        from fluid_provider_sdk import ProviderAction, validate_actions

        actions = [ProviderAction(op="", resource_id="x")]
        errs = validate_actions(actions)
        assert any("op" in e for e in errs)

    def test_missing_resource_id(self):
        from fluid_provider_sdk import ProviderAction, validate_actions

        actions = [ProviderAction(op="create")]
        errs = validate_actions(actions)
        assert any("resource_id" in e for e in errs)

    def test_duplicate_resource_id(self):
        from fluid_provider_sdk import ProviderAction, validate_actions

        actions = [
            ProviderAction(op="a", resource_id="dup"),
            ProviderAction(op="b", resource_id="dup"),
        ]
        errs = validate_actions(actions)
        assert any("Duplicate" in e for e in errs)

    def test_unknown_dependency(self):
        from fluid_provider_sdk import ProviderAction, validate_actions

        actions = [
            ProviderAction(op="create_table", resource_id="tbl", depends_on=["nonexistent"]),
        ]
        errs = validate_actions(actions)
        assert any("nonexistent" in e for e in errs)


# ═══════════════════════════════════════════════════════════════════
# ColumnSpec
# ═══════════════════════════════════════════════════════════════════


class TestColumnSpec:
    def test_from_dict(self):
        from fluid_provider_sdk.contract import ColumnSpec

        d = {
            "name": "price_usd",
            "type": "numeric",
            "required": True,
            "description": "BTC price",
            "sensitivity": "cleartext",
            "semanticType": "currency",
            "labels": {"policyTag": "financial"},
            "tags": ["price-data"],
        }
        c = ColumnSpec.from_dict(d)
        assert c.name == "price_usd"
        assert c.type == "numeric"
        assert c.required is True
        assert c.semantic_type == "currency"
        assert c.labels["policyTag"] == "financial"
        assert "price-data" in c.tags
        assert c.raw == d

    def test_defaults(self):
        from fluid_provider_sdk.contract import ColumnSpec

        c = ColumnSpec.from_dict({"name": "col"})
        assert c.type == "string"
        assert c.required is False
        assert c.nullable is True


# ═══════════════════════════════════════════════════════════════════
# Local planner integration (uses ContractHelper when SDK present)
# ═══════════════════════════════════════════════════════════════════


class TestLocalPlannerWithContractHelper:
    def test_plan_uses_contract_helper(self):
        from fluid_build.providers.local.planner import _HAS_SDK_CONTRACT

        assert _HAS_SDK_CONTRACT is True

    def test_plan_produces_correct_actions(self):
        import logging

        from fluid_build.providers.local.planner import plan_actions

        contract = {
            "id": "test.dp",
            "consumes": [{"id": "src", "path": "/data/src.csv"}],
            "builds": [
                {
                    "id": "step1",
                    "properties": {
                        "sql": "SELECT * FROM src",
                        "parameters": {"inputs": [{"name": "src"}]},
                    },
                }
            ],
            "exposes": [
                {
                    "exposeId": "out",
                    "kind": "table",
                    "binding": {"platform": "local", "location": {"path": "out.csv"}},
                }
            ],
        }
        actions = plan_actions(contract, logger=logging.getLogger("test"))
        ops = [a["op"] for a in actions]
        assert "load_data" in ops
        assert "execute_sql" in ops
        assert "materialize" in ops

    def test_plan_empty_contract(self):
        import logging

        from fluid_build.providers.local.planner import plan_actions

        actions = plan_actions({"id": "empty"}, logger=logging.getLogger("test"))
        assert actions == []
