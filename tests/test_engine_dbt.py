# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License")

"""Tests for the dbt transformation engine."""

import pytest
import yaml

from fluid_build.engines import get_engine
from fluid_build.engines.base import Severity, TransformationIntent
from fluid_build.engines.dbt.models import generate_models
from fluid_build.engines.dbt.profiles import generate_profiles
from fluid_build.engines.dbt.project_yml import _sanitize_project_name, generate_project_yml
from fluid_build.engines.dbt.schema_yml import generate_schema_yml
from fluid_build.engines.dbt.sources import generate_sources

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def customer_360_contract():
    """Load the real customer-360 example contract."""
    from pathlib import Path

    contract_path = (
        Path(__file__).parent.parent
        / "fluid_build"
        / "templates"
        / "customer-360"
        / "contract.fluid.yaml"
    )
    if not contract_path.exists():
        pytest.skip("customer-360 example contract not found")
    with contract_path.open() as f:
        contract = yaml.safe_load(f)
    # Skip if contract doesn't use dbt engine
    builds = contract.get("builds", [])
    if not builds or builds[0].get("engine") != "dbt":
        pytest.skip("customer-360 template contract does not use dbt engine")
    return contract


@pytest.fixture
def minimal_contract():
    return {
        "fluidVersion": "0.7.2",
        "kind": "DataProduct",
        "id": "gold.analytics.test_product_v1",
        "name": "Test Product",
        "consumes": [
            {"exposeId": "orders", "productId": "silver.sales.orders_v1", "purpose": "Order data"},
            {
                "exposeId": "customers",
                "productId": "silver.crm.customers_v1",
                "purpose": "Customer data",
            },
        ],
        "builds": [
            {
                "id": "main_transform",
                "engine": "dbt",
                "pattern": "hybrid-reference",
                "properties": {
                    "model": "main",
                    "materializations": {"staging": "view", "marts": "table"},
                },
                "execution": {"runtime": {"platform": "local"}},
            }
        ],
        "exposes": [
            {
                "exposeId": "customer_orders",
                "kind": "table",
                "contract": {
                    "schema": [
                        {"name": "customer_id", "type": "STRING", "required": True},
                        {"name": "total_orders", "type": "INTEGER"},
                        {"name": "total_amount", "type": "NUMBER"},
                    ],
                    "dq": {
                        "rules": [
                            {
                                "id": "cid_complete",
                                "type": "completeness",
                                "selector": "customer_id",
                                "threshold": 1.0,
                                "operator": ">=",
                                "severity": "error",
                            },
                            {"id": "cid_unique", "type": "uniqueness", "selector": "customer_id"},
                        ]
                    },
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# Project name sanitization
# ---------------------------------------------------------------------------


class TestSanitizeProjectName:
    def test_dotted_id(self):
        assert _sanitize_project_name("gold.analytics.customer_360_v1") == "customer_360_v1"

    def test_simple_id(self):
        assert _sanitize_project_name("my_project") == "my_project"

    def test_special_chars(self):
        assert _sanitize_project_name("my-project-v2") == "my_project_v2"

    def test_leading_digit(self):
        assert _sanitize_project_name("123_project") == "project"

    def test_empty_fallback(self):
        assert _sanitize_project_name("") == "fluid_project"


# ---------------------------------------------------------------------------
# dbt_project.yml generation
# ---------------------------------------------------------------------------


class TestProjectYml:
    def test_basic(self, minimal_contract):
        build = minimal_contract["builds"][0]
        content = generate_project_yml(minimal_contract, build)
        data = yaml.safe_load(content)
        assert data["name"] == "test_product_v1"
        assert data["config-version"] == 2
        assert data["models"]["test_product_v1"]["staging"]["+materialized"] == "view"
        assert data["models"]["test_product_v1"]["marts"]["+materialized"] == "table"

    def test_with_vars(self, minimal_contract):
        build = minimal_contract["builds"][0]
        build["properties"]["vars"] = {"threshold": 100, "date_start": "2024-01-01"}
        content = generate_project_yml(minimal_contract, build)
        data = yaml.safe_load(content)
        assert data["vars"]["threshold"] == 100


# ---------------------------------------------------------------------------
# sources.yml generation
# ---------------------------------------------------------------------------


class TestSources:
    def test_basic(self, minimal_contract):
        content = generate_sources(minimal_contract)
        assert content is not None
        data = yaml.safe_load(content)
        assert data["version"] == 2
        tables = data["sources"][0]["tables"]
        table_names = [t["name"] for t in tables]
        assert "orders" in table_names
        assert "customers" in table_names

    def test_no_consumes(self):
        contract = {"consumes": []}
        assert generate_sources(contract) is None

    def test_with_schema_context(self, minimal_contract):
        schema_context = {
            "schemas": {
                "orders": {
                    "columns": {"order_id": "integer", "customer_id": "string", "amount": "number"}
                },
            }
        }
        content = generate_sources(minimal_contract, schema_context=schema_context)
        data = yaml.safe_load(content)
        orders_table = [t for t in data["sources"][0]["tables"] if t["name"] == "orders"][0]
        assert "columns" in orders_table
        col_names = [c["name"] for c in orders_table["columns"]]
        assert "order_id" in col_names

    def test_fallback_when_upstream_missing(self, minimal_contract, tmp_path, monkeypatch):
        """With no upstream contract in the workspace we emit env_var() placeholders."""
        monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
        content = generate_sources(minimal_contract, workspace_root=tmp_path)
        data = yaml.safe_load(content)
        src = data["sources"][0]
        assert "env_var('SNOWFLAKE_DATABASE')" in src["database"]
        assert "env_var('SNOWFLAKE_STAGE_SCHEMA'" in src["schema"]
        # identifier gets the uppercase fallback.
        identifiers = {t.get("identifier") for t in src["tables"]}
        assert "ORDERS" in identifiers or "CUSTOMERS" in identifiers

    def test_resolves_upstream_snowflake_binding(self, minimal_contract, tmp_path, monkeypatch):
        """When an upstream contract is found, sources.yml uses its real binding."""
        monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
        upstream_dir = tmp_path / "bronze_orders"
        upstream_dir.mkdir()
        upstream = {
            "fluidVersion": "0.7.2",
            "kind": "DataProduct",
            "id": "silver.sales.orders_v1",
            "exposes": [
                {
                    "exposeId": "orders",
                    "kind": "table",
                    "binding": {
                        "platform": "snowflake",
                        "format": "snowflake_table",
                        "location": {
                            "database": "{{ env.SNOWFLAKE_DATABASE }}",
                            "schema": "{{ env.SNOWFLAKE_STAGE_SCHEMA }}",
                            "table": "ORDERS_RAW",
                        },
                    },
                }
            ],
        }
        (upstream_dir / "contract.fluid.yaml").write_text(yaml.safe_dump(upstream))

        content = generate_sources(minimal_contract, workspace_root=tmp_path)
        data = yaml.safe_load(content)

        orders_block = None
        for src in data["sources"]:
            for table in src["tables"]:
                if table["name"] == "orders":
                    orders_block = (src, table)
                    break
            if orders_block:
                break
        assert orders_block is not None
        src, table = orders_block
        # FLUID {{ env.X }} gets rewritten to dbt env_var('X').
        assert "env_var('SNOWFLAKE_DATABASE')" in src["database"]
        assert "env_var('SNOWFLAKE_STAGE_SCHEMA')" in src["schema"]
        # Real physical table name from the upstream binding.
        assert table["identifier"] == "ORDERS_RAW"

    def test_groups_tables_by_distinct_schema(self, tmp_path, monkeypatch):
        """Consumes from two different schemas produce two dbt source blocks."""
        monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
        # Two upstream contracts on different Snowflake schemas.
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "contract.fluid.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "bronze.a_v1",
                    "exposes": [
                        {
                            "exposeId": "party_source",
                            "binding": {
                                "location": {
                                    "database": "${SNOWFLAKE_DATABASE}",
                                    "schema": "${SNOWFLAKE_STAGE_SCHEMA}",
                                    "table": "PARTY",
                                }
                            },
                        }
                    ],
                }
            )
        )
        (tmp_path / "b").mkdir()
        (tmp_path / "b" / "contract.fluid.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "bronze.b_v1",
                    "exposes": [
                        {
                            "exposeId": "policy_source",
                            "binding": {
                                "location": {
                                    "database": "${SNOWFLAKE_DATABASE}",
                                    "schema": "${SNOWFLAKE_GOVERNANCE_SCHEMA}",
                                    "table": "POLICY",
                                }
                            },
                        }
                    ],
                }
            )
        )

        downstream = {
            "id": "silver.mix_v1",
            "consumes": [
                {"productId": "bronze.a_v1", "exposeId": "party_source"},
                {"productId": "bronze.b_v1", "exposeId": "policy_source"},
            ],
        }
        content = generate_sources(downstream, workspace_root=tmp_path)
        data = yaml.safe_load(content)
        assert len(data["sources"]) == 2
        schemas = {s["schema"] for s in data["sources"]}
        assert any("env_var('SNOWFLAKE_STAGE_SCHEMA')" in s for s in schemas)
        assert any("env_var('SNOWFLAKE_GOVERNANCE_SCHEMA')" in s for s in schemas)

    def test_env_var_extra_search_path(self, minimal_contract, tmp_path, monkeypatch):
        """FLUID_UPSTREAM_CONTRACTS lets operators point at sibling repos."""
        external = tmp_path / "external"
        external.mkdir()
        (external / "up").mkdir()
        (external / "up" / "contract.fluid.yaml").write_text(
            yaml.safe_dump(
                {
                    "id": "silver.sales.orders_v1",
                    "exposes": [
                        {
                            "exposeId": "orders",
                            "binding": {
                                "location": {
                                    "database": "ANALYTICS",
                                    "schema": "RAW",
                                    "table": "ORDERS_CURRENT",
                                }
                            },
                        }
                    ],
                }
            )
        )
        monkeypatch.setenv("FLUID_UPSTREAM_CONTRACTS", str(external))

        # workspace_root intentionally somewhere ELSE (empty dir) so the
        # only way to find the upstream is via the env var.
        other = tmp_path / "other"
        other.mkdir()
        content = generate_sources(minimal_contract, workspace_root=other)
        data = yaml.safe_load(content)
        orders = next(t for src in data["sources"] for t in src["tables"] if t["name"] == "orders")
        assert orders["identifier"] == "ORDERS_CURRENT"


# ---------------------------------------------------------------------------
# Model generation
# ---------------------------------------------------------------------------


class TestModels:
    def test_skeleton_generation(self, minimal_contract):
        build = minimal_contract["builds"][0]
        files = generate_models(minimal_contract, build)
        # Should have staging models for each consume + mart for each expose
        assert "models/staging/stg_orders.sql" in files
        assert "models/staging/stg_customers.sql" in files
        assert "models/marts/customer_orders.sql" in files

    def test_staging_model_content(self, minimal_contract):
        build = minimal_contract["builds"][0]
        files = generate_models(minimal_contract, build)
        stg = files["models/staging/stg_orders.sql"]
        assert "config(materialized='view')" in stg
        assert "source('raw', 'orders')" in stg

    def test_mart_model_has_columns(self, minimal_contract):
        build = minimal_contract["builds"][0]
        files = generate_models(minimal_contract, build)
        mart = files["models/marts/customer_orders.sql"]
        assert "customer_id" in mart
        assert "total_orders" in mart
        assert "config(materialized='table')" in mart

    def test_embedded_logic(self):
        contract = {
            "id": "test",
            "builds": [
                {
                    "id": "my_query",
                    "pattern": "embedded-logic",
                    "properties": {"sql": "SELECT 1 AS id"},
                }
            ],
            "exposes": [],
        }
        build = contract["builds"][0]
        files = generate_models(contract, build)
        assert "models/marts/my_query.sql" in files
        assert "SELECT 1 AS id" in files["models/marts/my_query.sql"]

    def test_multi_stage(self):
        contract = {
            "id": "test",
            "builds": [
                {
                    "id": "pipeline",
                    "pattern": "multi-stage",
                    "properties": {
                        "stages": [
                            {
                                "name": "stg_raw",
                                "properties": {"sql": "SELECT * FROM raw_data"},
                                "dependsOn": [],
                            },
                            {
                                "name": "mart_output",
                                "properties": {"sql": "SELECT * FROM stg_raw"},
                                "dependsOn": ["stg_raw"],
                            },
                        ]
                    },
                }
            ],
            "exposes": [],
        }
        build = contract["builds"][0]
        files = generate_models(contract, build)
        assert "models/staging/stg_raw.sql" in files
        assert "models/marts/mart_output.sql" in files

    def test_with_transformation_intent(self, minimal_contract):
        intent = TransformationIntent(
            stages=[
                {
                    "name": "stg_orders",
                    "sql": "SELECT order_id, amount FROM {{ source('raw', 'orders') }}",
                    "layer": "staging",
                },
                {
                    "name": "customer_orders",
                    "sql": "SELECT customer_id, count(*) as total FROM {{ ref('stg_orders') }} GROUP BY 1",
                    "layer": "marts",
                },
            ]
        )
        build = minimal_contract["builds"][0]
        files = generate_models(minimal_contract, build, transformation_intent=intent)
        assert "models/staging/stg_orders.sql" in files
        assert "models/marts/customer_orders.sql" in files
        assert "order_id" in files["models/staging/stg_orders.sql"]


# ---------------------------------------------------------------------------
# schema.yml generation
# ---------------------------------------------------------------------------


class TestSchemaYml:
    def test_dq_rules(self, minimal_contract):
        files = generate_schema_yml(minimal_contract)
        assert "models/marts/schema.yml" in files
        data = yaml.safe_load(files["models/marts/schema.yml"])
        model = data["models"][0]
        assert model["name"] == "customer_orders"
        col_names = [c["name"] for c in model["columns"]]
        assert "customer_id" in col_names
        # customer_id should have not_null (completeness) + unique (uniqueness)
        cid_col = [c for c in model["columns"] if c["name"] == "customer_id"][0]
        assert "not_null" in cid_col["tests"]
        assert "unique" in cid_col["tests"]

    def test_no_exposes(self):
        contract = {"exposes": []}
        assert generate_schema_yml(contract) == {}


# ---------------------------------------------------------------------------
# profiles.yml generation
# ---------------------------------------------------------------------------


class TestProfiles:
    def test_local_platform(self, minimal_contract):
        build = minimal_contract["builds"][0]
        content = generate_profiles(minimal_contract, build)
        assert content is not None
        data = yaml.safe_load(content)
        profile = list(data.values())[0]
        assert profile["outputs"]["dev"]["type"] == "duckdb"

    def test_gcp_platform(self, minimal_contract):
        build = minimal_contract["builds"][0]
        build["execution"]["runtime"]["platform"] = "gcp"
        content = generate_profiles(minimal_contract, build)
        data = yaml.safe_load(content)
        profile = list(data.values())[0]
        assert profile["outputs"]["dev"]["type"] == "bigquery"

    def test_snowflake_platform(self, minimal_contract):
        build = minimal_contract["builds"][0]
        build["execution"]["runtime"]["platform"] = "snowflake"
        content = generate_profiles(minimal_contract, build)
        data = yaml.safe_load(content)
        profile = list(data.values())[0]
        assert profile["outputs"]["dev"]["type"] == "snowflake"

    def test_snowflake_profile_uses_env_vars(self, minimal_contract):
        """Connection params must be env_var() references, not hardcoded strings.

        Regression guard: an earlier generator hardcoded role=TRANSFORMER,
        database=ANALYTICS, warehouse=TRANSFORM_WH, schema=DEV — none of
        which exist outside the original author's Snowflake account.
        """
        build = minimal_contract["builds"][0]
        build["execution"]["runtime"]["platform"] = "snowflake"
        content = generate_profiles(minimal_contract, build)
        data = yaml.safe_load(content)
        dev = list(data.values())[0]["outputs"]["dev"]
        assert "env_var('SNOWFLAKE_ROLE')" in dev["role"]
        assert "env_var('SNOWFLAKE_DATABASE')" in dev["database"]
        assert "env_var('SNOWFLAKE_WAREHOUSE')" in dev["warehouse"]
        assert "env_var('SNOWFLAKE_DBT_SCHEMA'" in dev["schema"]
        # No leftover hardcoded values from the old generator.
        assert "TRANSFORMER" not in content
        assert "ANALYTICS" not in content
        assert "TRANSFORM_WH" not in content


# ---------------------------------------------------------------------------
# DbtEngine validation
# ---------------------------------------------------------------------------


class TestDbtEngineValidation:
    def test_valid_contract(self, minimal_contract):
        engine = get_engine("dbt")
        issues = engine.validate(minimal_contract, minimal_contract["builds"][0])
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert len(errors) == 0

    def test_wrong_engine(self, minimal_contract):
        build = minimal_contract["builds"][0].copy()
        build["engine"] = "python"
        engine = get_engine("dbt")
        issues = engine.validate(minimal_contract, build)
        assert any(i.severity == Severity.ERROR for i in issues)

    def test_no_exposes_warning(self):
        contract = {
            "exposes": [],
            "builds": [{"id": "x", "engine": "dbt", "pattern": "hybrid-reference"}],
        }
        engine = get_engine("dbt")
        issues = engine.validate(contract, contract["builds"][0])
        assert any(i.severity == Severity.WARNING for i in issues)


# ---------------------------------------------------------------------------
# End-to-end: customer-360 contract
# ---------------------------------------------------------------------------


class TestCustomer360:
    def test_generates_expected_files(self, customer_360_contract):
        engine = get_engine("dbt")
        build = customer_360_contract["builds"][0]
        files = engine.generate(customer_360_contract, build)

        assert "dbt_project.yml" in files
        assert "profiles.yml" in files
        assert "models/sources.yml" in files
        # Should have at least one staging model and one mart model
        staging = [f for f in files if f.startswith("models/staging/")]
        marts = [f for f in files if f.startswith("models/marts/")]
        assert len(staging) >= 1
        assert len(marts) >= 1
