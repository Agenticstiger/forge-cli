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

"""Tests for the dbt transformation engine."""

import json

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from fluid_build.engines import get_engine
from fluid_build.engines.base import Severity, TransformationIntent
from fluid_build.engines.dbt import _test_mapping as tm
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
        assert "intermediate" not in data["models"]["test_product_v1"]

    def test_dv2_projects_include_intermediate_layer(self, minimal_contract):
        minimal_contract["labels"] = {"dataModelingTechnique": "data_vault_2"}
        build = minimal_contract["builds"][0]
        content = generate_project_yml(minimal_contract, build)
        data = yaml.safe_load(content)
        assert data["models"]["test_product_v1"]["intermediate"]["+materialized"] == "view"

    def test_explicit_intermediate_materialization_is_preserved(self, minimal_contract):
        build = minimal_contract["builds"][0]
        build["properties"]["materializations"]["intermediate"] = "table"
        content = generate_project_yml(minimal_contract, build)
        data = yaml.safe_load(content)
        assert data["models"]["test_product_v1"]["intermediate"]["+materialized"] == "table"

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

    def _one_column(self, col: dict) -> dict:
        """Run generate_schema_yml over a single-column expose, return the column."""
        contract = {
            "id": "x",
            "exposes": [{"exposeId": "t", "contract": {"schema": [col]}}],
        }
        data = yaml.safe_load(generate_schema_yml(contract)["models/marts/schema.yml"])
        return data["models"][0]["columns"][0]

    def test_inline_range_uses_expectations_dialect(self):
        """Consolidation: the engine now emits the unified dbt_expectations
        range dialect for inline minimum/maximum (previously it emitted
        nothing for inline bounds; the old accuracy→dbt_utils.accepted_range
        path is retired)."""
        col = self._one_column({"name": "amount", "type": "NUMBER", "minimum": 0, "maximum": 100})
        assert {
            "dbt_expectations.expect_column_values_to_be_between": {
                "min_value": 0,
                "max_value": 100,
            }
        } in col["tests"]

    def test_surfaces_relationships_from_column_fk(self):
        """Consolidation: relationships now derive from the engine path too
        (from a column-level foreign-key reference)."""
        col = self._one_column(
            {"name": "customer_id", "foreign_key": {"to": "customers", "field": "id"}}
        )
        assert {"relationships": {"to": "ref('customers')", "field": "id"}} in col["tests"]

    def test_column_scoped_freshness_emits_recency(self):
        """Consolidation: the engine now surfaces freshness/recency (was
        exporter-only before)."""
        contract = {
            "id": "x",
            "exposes": [
                {
                    "exposeId": "t",
                    "contract": {
                        "schema": [{"name": "updated_at", "type": "TIMESTAMP"}],
                        "dq": {
                            "rules": [
                                {"type": "freshness", "selector": "updated_at", "window": "P1D"}
                            ]
                        },
                    },
                }
            ],
        }
        data = yaml.safe_load(generate_schema_yml(contract)["models/marts/schema.yml"])
        model = data["models"][0]
        # INTENTIONAL pin update: recency attaches at MODEL level even for a
        # column-scoped rule. dbt injects ``column_name`` into every generic
        # test reached through ``columns[].tests`` and the ``dbt_utils``
        # recency macro accepts no such kwarg, so the column-attached form
        # made the whole generated project fail ``dbt parse`` with
        # "macro 'dbt_macro__test_recency' takes no keyword argument
        # 'column_name'". The selected column survives as ``field``.
        assert "tests" not in model["columns"][0]
        rec = next(t for t in model["tests"] if isinstance(t, dict) and "dbt_utils.recency" in t)
        assert rec["dbt_utils.recency"]["field"] == "updated_at"
        # The ISO window drives datepart/interval (P1D → day/1) and the
        # non-dbt `_fluid_window` kwarg is gone — it broke dbt compile.
        assert rec["dbt_utils.recency"]["datepart"] == "day"
        assert rec["dbt_utils.recency"]["interval"] == 1
        assert "_fluid_window" not in rec["dbt_utils.recency"]


# ---------------------------------------------------------------------------
# Shared contract → dbt-test mapping (fluid_build/engines/dbt/_test_mapping.py)
#
# One module all three generators consume so they cannot drift. Forward
# (dqRule.type → dbt test) and reverse (dbt test → dqRule.type) tables are
# pinned symmetric; the reverse hook is what the planned dbt-manifest importer
# consumes.
# ---------------------------------------------------------------------------


class TestSharedTestMapping:
    def test_forward_reverse_tables_are_exact_inverses(self):
        # Reverse must be the exact inverse of forward over the mappable subset.
        assert set(tm.FORWARD_RULE_TO_TEST.values()) == set(tm.REVERSE_TEST_TO_RULE)
        for rule_type, test_name in tm.FORWARD_RULE_TO_TEST.items():
            assert tm.REVERSE_TEST_TO_RULE[test_name] == rule_type

    def test_roundtrip_rule_type_through_dbt_test(self):
        for rule_type in tm.FORWARD_RULE_TO_TEST:
            name = tm.rule_type_to_test_name(rule_type)
            assert name is not None
            assert tm.test_to_rule_type(name) == rule_type

    def test_roundtrip_dbt_test_through_rule_type(self):
        for test_name, rule_type in tm.REVERSE_TEST_TO_RULE.items():
            assert tm.rule_type_to_test_name(rule_type) == test_name

    @given(st.sampled_from(sorted(tm.FORWARD_RULE_TO_TEST)))
    def test_property_reverse_of_forward_roundtrips(self, rule_type):
        # reverse(forward(rule)) == rule for the mappable subset.
        assert tm.test_to_rule_type(tm.rule_type_to_test_name(rule_type)) == rule_type

    def test_unmappable_tests_have_no_rule_type(self):
        # relationships (referential integrity), the numeric range test, and the
        # fluid_* sentinels are intentionally outside the reversible subset.
        assert tm.test_to_rule_type(tm.relationships_test("customers", "id")) is None
        assert tm.test_to_rule_type(tm.numeric_range_test(min_value=0)) is None
        assert tm.test_to_rule_type(tm.sentinel_test("schema", "col")) is None

    def test_numeric_range_test_none_when_no_bounds(self):
        assert tm.numeric_range_test() is None


def _test_set(tests) -> set:
    """Normalise a dbt tests list to an order-independent comparable set."""
    return {t if isinstance(t, str) else json.dumps(t, sort_keys=True) for t in tests}


class TestCrossGeneratorConsistency:
    """Acceptance: all three generators emit identical dbt tests for the same
    contract intent (a key + FK + enum + numeric range column)."""

    def _engine_column_tests(self) -> set:
        col = {
            "name": "customer_id",
            "type": "NUMBER",
            "required": True,
            "unique": True,
            "foreign_key": {"to": "customers", "field": "id"},
            "enum": ["a", "b"],
            "minimum": 0,
            "maximum": 100,
        }
        contract = {"id": "x", "exposes": [{"exposeId": "t", "contract": {"schema": [col]}}]}
        data = yaml.safe_load(generate_schema_yml(contract)["models/marts/schema.yml"])
        return _test_set(data["models"][0]["columns"][0]["tests"])

    def _exporter_column_tests(self) -> set:
        from fluid_build.exporters.dbt_tests import render_dbt_tests

        col = {
            "name": "customer_id",
            "type": "NUMBER",
            "required": True,
            "unique": True,
            "foreign_key": {"to": "customers", "field": "id"},
            "enum": ["a", "b"],
            "minimum": 0,
            "maximum": 100,
        }
        contract = {
            "exposes": [
                {
                    "exposeId": "t",
                    "binding": {"location": {"table": "T"}},
                    "contract": {"schema": [col]},
                }
            ]
        }
        out = render_dbt_tests(contract)
        body = "\n".join(line for line in out.splitlines() if not line.startswith("#"))
        data = yaml.safe_load(body)
        return _test_set(data["models"][0]["columns"][0]["tests"])

    def _copilot_column_tests(self) -> set:
        from fluid_build.copilot.tools.dbt_test_generator import generate_dbt_tests

        # Same intent expressed in the copilot agent-schema shape.
        schema = {
            "model_name": "t",
            "columns": [
                {
                    "name": "customer_id",
                    "type": "NUMBER",
                    "primary_key": True,  # → unique + not_null (== required + key)
                    "foreign_key": {"to": "customers", "field": "id"},
                    "enum": ["a", "b"],
                    "min": 0,
                    "max": 100,
                }
            ],
        }
        out = generate_dbt_tests(schema)
        return _test_set(out["models"][0]["columns"][0]["tests"])

    def test_three_generators_agree(self):
        engine = self._engine_column_tests()
        exporter = self._exporter_column_tests()
        copilot = self._copilot_column_tests()
        assert engine == exporter == copilot, (
            "contract→dbt-test generators drifted:\n"
            f"  engine   = {sorted(engine)}\n"
            f"  exporter = {sorted(exporter)}\n"
            f"  copilot  = {sorted(copilot)}"
        )
        # And the shape is the unified dialect (not the retired dbt_utils range).
        assert any("expect_column_values_to_be_between" in t for t in engine)
        assert any("relationships" in t for t in engine)


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

    def test_local_platform_uses_project_local_duckdb_path(self, minimal_contract, tmp_path):
        build = minimal_contract["builds"][0]
        content = generate_profiles(minimal_contract, build, output_dir=tmp_path)
        data = yaml.safe_load(content)
        profile = list(data.values())[0]
        assert profile["outputs"]["dev"]["path"] == str((tmp_path / "dev.duckdb").resolve())

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


# ---------------------------------------------------------------------------
# profiles.yml resolves the schema the CONTRACT declares (regression)
#
# The Snowflake profile hardcoded
# ``env_var('SNOWFLAKE_DBT_SCHEMA', env_var('SNOWFLAKE_FLUID_SCHEMA','PUBLIC'))``
# and never read the contract, so a contract declaring
# ``builds[].execution.runtime.resources.schema`` built every model into
# PUBLIC while ``dbt debug`` reported "Connection test: OK". ``database`` WAS
# resolved correctly, which made the failure asymmetric and easy to miss.
# ---------------------------------------------------------------------------


class TestProfilesSchemaFromContract:
    @staticmethod
    def _snowflake(contract, *, resources=None, binding_schema=None):
        build = contract["builds"][0]
        build["execution"]["runtime"]["platform"] = "snowflake"
        if resources is not None:
            build["execution"]["runtime"]["resources"] = resources
        if binding_schema is not None:
            contract["exposes"] = [
                {
                    "exposeId": "t",
                    "binding": {"location": {"schema": binding_schema}},
                    "contract": {"schema": [{"name": "c", "type": "string"}]},
                }
            ]
        data = yaml.safe_load(generate_profiles(contract, build))
        return list(data.values())[0]["outputs"]["dev"]

    def test_runtime_resources_schema_wins(self, minimal_contract):
        dev = self._snowflake(
            minimal_contract, resources={"schema": "{{ env.SNOWFLAKE_SCHEMA }}"}
        )
        assert dev["schema"] == "{{ env_var('SNOWFLAKE_SCHEMA') }}"

    def test_literal_schema_is_passed_through(self, minimal_contract):
        dev = self._snowflake(minimal_contract, resources={"schema": "ANALYTICS_GOLD"})
        assert dev["schema"] == "ANALYTICS_GOLD"

    def test_expose_binding_schema_is_the_fallback(self, minimal_contract):
        dev = self._snowflake(minimal_contract, binding_schema="${MART_SCHEMA}")
        assert dev["schema"] == "{{ env_var('MART_SCHEMA') }}"

    def test_env_var_chain_only_when_contract_declares_nothing(self, minimal_contract):
        dev = self._snowflake(minimal_contract)
        assert "env_var('SNOWFLAKE_DBT_SCHEMA'" in dev["schema"]
        assert "PUBLIC" in dev["schema"]

    def test_bigquery_dataset_resolves_too(self, minimal_contract):
        build = minimal_contract["builds"][0]
        build["execution"]["runtime"]["platform"] = "gcp"
        build["execution"]["runtime"]["resources"] = {"dataset": "{{ env.BQ_DATASET }}"}
        data = yaml.safe_load(generate_profiles(minimal_contract, build))
        outputs = list(data.values())[0]["outputs"]
        assert outputs["dev"]["dataset"] == "{{ env_var('BQ_DATASET') }}"
        assert outputs["prod"]["dataset"] == "{{ env_var('BQ_DATASET') }}"


# ---------------------------------------------------------------------------
# dq rule routing: column vs model level (regression)
#
# ``selector: "*"`` used to become a dbt column literally named ``*`` on the
# engine path, and a column-scoped freshness rule emitted a column-attached
# ``dbt_utils.recency`` that made the project unparseable. Both surfaces now
# route through the one ``_test_mapping.partition_rules``.
# ---------------------------------------------------------------------------


class TestRuleRouting:
    @staticmethod
    def _schema_yml(rules):
        contract = {
            "id": "x",
            "exposes": [
                {
                    "exposeId": "t",
                    "contract": {
                        "schema": [
                            {"name": "id", "type": "integer"},
                            {"name": "amount", "type": "number(12,2)"},
                            {"name": "loaded_at", "type": "timestamp"},
                        ],
                        "dq": {"rules": rules},
                    },
                }
            ],
        }
        return yaml.safe_load(generate_schema_yml(contract)["models/marts/schema.yml"])["models"][0]

    def test_table_selector_never_becomes_a_column(self):
        model = self._schema_yml(
            [{"type": "anomaly_detection", "selector": "*", "threshold": 100}]
        )
        assert "*" not in {c["name"] for c in model["columns"]}
        assert {"dbt_expectations.expect_table_row_count_to_be_between": {"min_value": 100}} in (
            model["tests"]
        )

    def test_table_freshness_measures_a_declared_temporal_column(self):
        model = self._schema_yml([{"type": "freshness", "selector": "*", "window": "P1D"}])
        rec = next(t for t in model["tests"] if isinstance(t, dict) and "dbt_utils.recency" in t)
        assert rec["dbt_utils.recency"]["field"] == "loaded_at"

    def test_accuracy_emits_an_executable_predicate_not_a_comment(self):
        """``dbt_utils.expression_is_true`` on a column compiles to
        ``where not(<column> <expression>)``. A ``--`` comment there swallowed
        the closing paren and every ``dbt test`` died on a Snowflake syntax
        error."""
        model = self._schema_yml(
            [{"type": "accuracy", "selector": "amount", "operator": ">=", "threshold": 0}]
        )
        col = next(c for c in model["columns"] if c["name"] == "amount")
        expr = next(t for t in col["tests"] if "dbt_utils.expression_is_true" in t)
        assert expr["dbt_utils.expression_is_true"]["expression"] == ">= 0"
        assert "--" not in yaml.safe_dump(model)

    def test_accuracy_equality_operator_becomes_sql(self):
        model = self._schema_yml(
            [{"type": "accuracy", "selector": "amount", "operator": "==", "threshold": 5}]
        )
        col = next(c for c in model["columns"] if c["name"] == "amount")
        expr = next(t for t in col["tests"] if "dbt_utils.expression_is_true" in t)
        assert expr["dbt_utils.expression_is_true"]["expression"] == "= 5"

    def test_both_surfaces_route_a_rule_to_the_same_place(self):
        """#421's premise: one contract intent, one answer on every surface."""
        from fluid_build.exporters.dbt_tests import render_dbt_tests

        rules = [
            {"type": "freshness", "selector": "loaded_at", "window": "PT6H"},
            {"type": "anomaly_detection", "selector": "*", "threshold": 100},
            {"type": "accuracy", "selector": "amount", "operator": ">=", "threshold": 0},
            {"type": "completeness", "selector": "id"},
        ]
        engine_model = self._schema_yml(rules)
        exporter_contract = {
            "exposes": [
                {
                    "exposeId": "t",
                    "binding": {"location": {"table": "T_PHYSICAL"}},
                    "contract": {
                        "schema": [
                            {"name": "id", "type": "integer"},
                            {"name": "amount", "type": "number(12,2)"},
                            {"name": "loaded_at", "type": "timestamp"},
                        ],
                        "dq": {"rules": rules},
                    },
                }
            ]
        }
        exporter_model = yaml.safe_load(render_dbt_tests(exporter_contract))["models"][0]

        # Same dbt node name — the exporter output is meant to be dropped into
        # the generated project, and a mismatched name silently runs 0 tests.
        assert engine_model["name"] == exporter_model["name"] == "t"
        assert engine_model["tests"] == exporter_model["tests"]
        assert [
            (c["name"], c.get("tests")) for c in engine_model["columns"]
        ] == [(c["name"], c.get("tests")) for c in exporter_model["columns"]]
