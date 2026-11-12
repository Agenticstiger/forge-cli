# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Databricks Lakeflow pipelines, Stage 1 unit: ``engine: lakeflow`` -> ``databricks_pipeline``.

Pure functions only: no ``tofu``, no network. The module docstring of
``iac/providers/databricks_lakeflow.py`` is the spec these tests pin.
"""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import jsonschema
import pytest
import yaml

from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers import databricks as dbx
from fluid_build.iac.providers.databricks_lakeflow import compile_lakeflow_pipelines
from fluid_build.iac.source import contract_source
from fluid_build.util.contract import is_externally_run_build, is_lakeflow_build

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

WAREHOUSE = "wh-0123456789abcdef"
SCHEMAS = Path(dbx.__file__).resolve().parents[2] / "schemas"
SITES = "fluid_iactest_ldp.bronze_sites.sites"
QUERY = f"SELECT s.site_id, CAST(1 AS DECIMAL(10,2)) AS load_kg FROM {SITES} s"
ENV = {dbx.WAREHOUSE_ENV: WAREHOUSE}


def _rules() -> List[Dict[str, Any]]:
    return [
        {
            "id": "capacity_key_unique",
            "type": "uniqueness",
            "selector": "site_id",
            "threshold": 1.0,
            "operator": ">=",
            "severity": "critical",
        },
        {
            "id": "capacity_required_complete",
            "type": "completeness",
            "selector": "load_kg",
            "severity": "error",
        },
        {
            "id": "capacity_method_valid",
            "type": "valid_values",
            "selector": "method",
            "severity": "warn",
        },
        {
            "id": "capacity_freshness",
            "type": "freshness",
            "selector": "_ingested_at",
            "window": "PT26H",
            "operator": "<=",
            "severity": "warn",
        },
        {"id": "capacity_schema", "type": "schema", "selector": "*", "severity": "error"},
        {
            "id": "capacity_site_ref",
            "type": "valid_values",
            "selector": "site_id",
            "severity": "warn",
        },
    ]


def _contract() -> Dict[str, Any]:
    """One Databricks table, built by one lakeflow build, with six data-quality rules."""
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "silver.test.capacity_v1",
        "name": "Capacity",
        "metadata": {"owner": {"team": "data-platform"}, "layer": "Silver"},
        "exposes": [
            {
                "exposeId": "capacity",
                "kind": "table",
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": {
                        "database": "fluid_iactest_ldp",
                        "schema": "silver_capacity",
                        "table": "capacity",
                    },
                },
                "contract": {
                    "schema": [
                        {
                            "name": "site_id",
                            "type": "STRING",
                            "required": True,
                            "description": "The site's id",
                            "sensitivity": "internal",
                            "labels": {"references": "bronze.test.sites_v1.site_id"},
                        },
                        {"name": "load_kg", "type": "DECIMAL(10,2)", "required": True},
                        {
                            "name": "method",
                            "type": "STRING",
                            "validationRules": [{"type": "enum", "constraint": "A,B,C"}],
                        },
                        {"name": "_ingested_at", "type": "TIMESTAMP"},
                    ],
                    "dq": {"rules": _rules()},
                },
            }
        ],
        "consumes": [{"productId": "bronze.test.sites_v1", "exposeId": "sites"}],
        "accessPolicy": {
            "grants": [
                {
                    "principal": "group:analysts",
                    "permissions": ["read"],
                    "resources": ["$.exposes[?(@.exposeId=='capacity')]"],
                }
            ]
        },
        "builds": [
            {
                "id": "build-capacity",
                "pattern": "embedded-logic",
                "engine": "lakeflow",
                "outputs": ["capacity"],
                "execution": {"trigger": {"type": "manual"}, "runtime": {"platform": "databricks"}},
                "properties": {"sql": QUERY},
            }
        ],
    }


def _upstream() -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "bronze.test.sites_v1",
        "name": "Sites",
        "exposes": [
            {
                "exposeId": "sites",
                "kind": "table",
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": {
                        "database": "fluid_iactest_ldp",
                        "schema": "bronze_sites",
                        "table": "sites",
                    },
                },
                "contract": {"schema": [{"name": "site_id", "type": "STRING"}]},
            }
        ],
    }


def _edit(**changes: Any) -> Dict[str, Any]:
    """The base contract with ``mutate`` callbacks applied: ``_edit(x=lambda c: ...)``."""
    contract = _contract()
    for mutate in changes.values():
        mutate(contract)
    return contract


def _resolver(product_id: str, expose_id: Optional[str], column: str):
    assert (product_id, expose_id, column) == ("bronze.test.sites_v1", "sites", "site_id")
    return SITES, None


def _no_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)


def _workspace(tmp_path: Path, contract: Dict[str, Any]) -> Path:
    (tmp_path / "fluid.workspace.yaml").write_text("version: 1\n", encoding="utf-8")
    up = tmp_path / "sites"
    up.mkdir()
    (up / "contract.fluid.yaml").write_text(yaml.safe_dump(_upstream()), encoding="utf-8")
    down = tmp_path / "capacity"
    down.mkdir()
    path = down / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return path


def _compile(
    contract: Dict[str, Any], tmp_path: Optional[Path] = None, *, validating: bool = False
) -> dbx._Compiled:
    """Compile with the reference rule resolved from a workspace beside the contract."""
    path = _workspace(tmp_path, contract) if tmp_path is not None else None
    with contract_source(path):
        return dbx._compile(contract, ENV, validating=validating)


def _kinds(compiled: dbx._Compiled) -> List[str]:
    return [refusal.kind for refusal in compiled.refusals]


def _only(compiled: dbx._Compiled, rtype: str) -> Dict[str, Any]:
    assert not compiled.refusals, _kinds(compiled)
    (body,) = compiled.resources[rtype].values()
    return body


def _sql(compiled: dbx._Compiled) -> str:
    body = _only(compiled, "databricks_workspace_file")
    return base64.b64decode(body["content_base64"]).decode("utf-8")


def _refused(contract: Dict[str, Any], kind: str, tmp_path: Path) -> dbx._Compiled:
    compiled = _compile(contract, tmp_path)
    assert f"databricks-lakeflow-{kind}" in _kinds(compiled), _kinds(compiled)
    with contract_source(tmp_path / "capacity" / "contract.fluid.yaml"):
        # The apply never gets half a pipeline: the plugin raises the first refusal ...
        with pytest.raises(UnsupportedBindingError):
            dbx.DatabricksIacPlugin().emit(contract)
        # ... and ``fluid validate`` reports it at stage 2.
        errors, _warnings = dbx.validate_databricks_binding(contract, ENV)
    assert errors
    return compiled


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", WAREHOUSE)
    monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)


# ── 1. schema ───────────────────────────────────────────────────────────────
def _schema_errors(version: str, contract: Dict[str, Any]) -> List[Any]:
    schema = json.loads((SCHEMAS / f"fluid-schema-{version}.json").read_text("utf-8"))
    validator = jsonschema.validators.validator_for(schema)(schema)
    return list(validator.iter_errors(contract))


def test_the_base_contract_validates_against_schema_0_7_7():
    assert [e.message for e in _schema_errors("0.7.7", _contract())] == []


def test_schema_0_7_6_rejects_the_lakeflow_engine():
    contract = _contract()
    contract["fluidVersion"] = "0.7.6"
    errors = _schema_errors("0.7.6", contract)
    assert errors
    assert any("lakeflow" in e.message or list(e.absolute_path)[-1:] == ["engine"] for e in errors)


def test_an_older_schema_says_the_lakeflow_engine_needs_fluid_version_0_7_7():
    from fluid_build.schema_manager import format_schema_error

    contract = _contract()
    contract["fluidVersion"] = "0.7.6"
    [engine] = [e for e in _schema_errors("0.7.6", contract) if e.absolute_path[-1] == "engine"]
    message = format_schema_error(engine)
    assert "0.7.7" in message and "fluidVersion" in message, message


# ── 2. engine predicates ────────────────────────────────────────────────────
@pytest.mark.parametrize("engine", ["lakeflow", " Lakeflow "])
def test_lakeflow_engine_is_recognised_ignoring_case_and_space(engine):
    assert is_lakeflow_build({"engine": engine})


def test_dbt_is_not_a_lakeflow_build():
    assert not is_lakeflow_build({"engine": "dbt"})


def test_a_lakeflow_build_runs_outside_forge():
    assert is_externally_run_build({"engine": "lakeflow"})
    assert not is_externally_run_build({"engine": "dbt"})


# ── 3. the emitted module ───────────────────────────────────────────────────
class TestModule:
    def test_no_sql_table_but_a_schema(self, tmp_path):
        compiled = _compile(_contract(), tmp_path)
        assert not compiled.refusals, _kinds(compiled)
        assert "databricks_sql_table" not in compiled.resources
        assert len(compiled.resources["databricks_schema"]) == 1

    def test_readers_get_select_and_use_schema_on_the_schema_and_nothing_on_a_table(self, tmp_path):
        compiled = _compile(_contract(), tmp_path)
        assert not compiled.refusals, _kinds(compiled)
        bodies = list(compiled.resources["databricks_grant"].values())
        on_schema = [b for b in bodies if "schema" in b]
        assert [(b["principal"], b["privileges"]) for b in on_schema] == [
            ("analysts", ["SELECT", "USE_SCHEMA"])
        ]
        assert not [b for b in bodies if "table" in b]
        assert ("schema", "fluid_iactest_ldp.silver_capacity") in {
            (kind, name) for kind, name, _p, _priv in compiled.grants
        }

    def test_the_pipeline(self, tmp_path):
        compiled = _compile(_contract(), tmp_path)
        pipeline = _only(compiled, "databricks_pipeline")
        (schema_key,) = compiled.resources["databricks_schema"]
        (file_key,) = compiled.resources["databricks_workspace_file"]
        assert pipeline["serverless"] is True
        assert pipeline["continuous"] is False
        assert pipeline["development"] is False
        assert pipeline["channel"] == "CURRENT"
        # A failed triggered update is not retried by Lakeflow (it retries five times).
        assert pipeline["configuration"] == {"pipelines.numUpdateRetryAttempts": "0"}
        assert pipeline["catalog"] == "fluid_iactest_ldp"
        assert str(pipeline["schema"]) == f"${{databricks_schema.{schema_key}.name}}"
        assert pipeline["tags"] == {
            "fluid_product": "silver.test.capacity_v1",
            "fluid_build": "build-capacity",
        }
        (library,) = pipeline["library"]
        assert str(library["file"][0]["path"]) == (
            f"${{databricks_workspace_file.{file_key}.path}}"
        )

    def test_the_source_file_waits_for_its_folder_and_the_home_is_looked_up(self, tmp_path):
        compiled = _compile(_contract(), tmp_path)
        assert not compiled.refusals, _kinds(compiled)
        (dir_key,) = compiled.resources["databricks_directory"]
        body = _only(compiled, "databricks_workspace_file")
        assert body["depends_on"] == [f"databricks_directory.{dir_key}"]
        assert body["path"].endswith("/pipeline/capacity.sql")
        assert "me" in compiled.data["databricks_current_user"]

    def test_the_plugin_emits_the_same_module(self, tmp_path, monkeypatch):
        path = _workspace(tmp_path, _contract())
        plugin = dbx.DatabricksIacPlugin()
        with contract_source(path):
            module = plugin.emit(_contract())
            data = plugin.emit_data(_contract())
        assert "databricks_pipeline" in module and "databricks_sql_table" not in module
        assert "me" in data["databricks_current_user"]

    def test_a_continuous_trigger_is_accepted(self, tmp_path):
        contract = _edit(
            t=lambda c: c["builds"][0]["execution"].update(trigger={"type": "continuous"})
        )
        pipeline = _only(_compile(contract, tmp_path), "databricks_pipeline")
        assert pipeline["continuous"] is True
        # A continuous pipeline keeps Lakeflow's own restarts.
        assert "configuration" not in pipeline

    def test_without_a_trigger_the_pipeline_is_triggered(self, tmp_path):
        contract = _edit(t=lambda c: c["builds"][0]["execution"].pop("trigger"))
        assert _only(_compile(contract, tmp_path), "databricks_pipeline")["continuous"] is False


# ── 4. the generated SQL ────────────────────────────────────────────────────
class TestSql:
    @pytest.fixture()
    def sql(self, tmp_path) -> str:
        return _sql(_compile(_contract(), tmp_path))

    def test_the_table_is_a_materialized_view_with_the_expose_columns(self, sql):
        assert "CREATE OR REFRESH MATERIALIZED VIEW `capacity` (" in sql
        assert "`site_id` string NOT NULL COMMENT 'The site\\'s id'" in sql
        assert "`load_kg` decimal(10,2) NOT NULL" in sql.replace("DECIMAL", "decimal")

    def test_error_drops_and_warn_only_counts(self, sql):
        assert (
            "CONSTRAINT `capacity_required_complete` EXPECT (`load_kg` IS NOT NULL) "
            "ON VIOLATION DROP ROW"
        ) in sql
        (line,) = [ln for ln in sql.splitlines() if "`capacity_method_valid`" in ln]
        assert line.strip().startswith("CONSTRAINT `capacity_method_valid` EXPECT (")
        assert "ON VIOLATION" not in line
        assert "`method` IN ('A', 'B', 'C')" in line

    def test_info_is_a_plain_expect_and_critical_fails_the_update(self, tmp_path):
        def add(contract: Dict[str, Any]) -> None:
            rules = contract["exposes"][0]["contract"]["dq"]["rules"]
            rules.append(
                {
                    "id": "capacity_info",
                    "type": "completeness",
                    "selector": "site_id",
                    "severity": "info",
                }
            )
            rules.append(
                {
                    "id": "capacity_critical",
                    "type": "completeness",
                    "selector": "method",
                    "severity": "critical",
                }
            )

        sql = _sql(_compile(_edit(a=add), tmp_path))
        (info,) = [ln for ln in sql.splitlines() if "`capacity_info`" in ln]
        assert info.strip() == "CONSTRAINT `capacity_info` EXPECT (`site_id` IS NOT NULL),"
        assert (
            "CONSTRAINT `capacity_critical` EXPECT (`method` IS NOT NULL) "
            "ON VIOLATION FAIL UPDATE"
        ) in sql

    def test_uniqueness_is_a_private_view_that_fails_the_update(self, sql):
        assert "CREATE OR REFRESH PRIVATE MATERIALIZED VIEW `capacity__capacity_key_unique`" in sql
        view = sql.split("`capacity__capacity_key_unique`", 1)[1].split(";", 1)[0]
        assert (
            "CONSTRAINT `capacity_key_unique` EXPECT (_fluid_rows = 1) ON VIOLATION FAIL UPDATE"
            in view
        )
        assert "GROUP BY `site_id`" in view

    def test_freshness_is_the_window_in_seconds(self, sql):
        view = sql.split("`capacity__capacity_freshness`", 1)[1].split(";", 1)[0]
        assert "INTERVAL 93600 SECONDS" in view
        assert "ON VIOLATION" not in view

    def test_the_reference_view_left_joins_the_referenced_table(self, sql):
        view = sql.split("`capacity__capacity_site_ref`", 1)[1].split(";", 1)[0]
        assert "LEFT JOIN" in view
        assert "`fluid_iactest_ldp`.`bronze_sites`.`sites`" in view

    def test_a_schema_rule_adds_no_constraint_and_no_view(self, sql):
        assert "capacity_schema" not in sql

    def test_the_properties_carry_the_product_and_the_tag_list(self, sql):
        assert "'fluid.product_id' = 'silver.test.capacity_v1'" in sql
        assert "'fluid.build_id' = 'build-capacity'" in sql
        (line,) = [ln for ln in sql.splitlines() if "'fluid.tags'" in ln]
        assert "fluid_product_id" in line

    def test_the_users_query_follows_as(self, sql):
        assert f"\nAS\n{QUERY}\n;\n" in sql

    def test_a_trailing_semicolon_is_stripped_once(self, tmp_path):
        contract = _edit(q=lambda c: c["builds"][0]["properties"].update(sql=QUERY + " ;\n"))
        sql = _sql(_compile(contract, tmp_path))
        assert f"\nAS\n{QUERY}\n;\n" in sql
        assert ";\n;" not in sql

    def test_the_reference_resolves_through_an_injected_resolver(self):
        contract = _contract()
        compiled = dbx._compile(contract, ENV)  # the workspace route fails here ...
        assert "databricks-lakeflow-reference-unresolved" in _kinds(compiled)
        tables = compiled.tables
        schema = dbx.tofu_ref("databricks_schema.s.name")
        result = compile_lakeflow_pipelines(
            contract, tables, schemas={tables[0].full_name: schema}, tags={}, resolve=_resolver
        )
        assert not result.refusals, [r.kind for r in result.refusals]
        (pipeline,) = result.pipelines
        assert pipeline.table == "fluid_iactest_ldp.silver_capacity.capacity"
        source = base64.b64decode(
            next(b for t, _k, _o, b in result.resources if t == "databricks_workspace_file")[
                "content_base64"
            ]
        ).decode()
        assert "LEFT JOIN" in source and "`bronze_sites`.`sites`" in source


# ── 5. refusals ─────────────────────────────────────────────────────────────
class TestSecurityReview:
    """The 2026-11-12 security review's two candidates, both fixed."""

    @pytest.mark.parametrize(
        "sql,left_open",
        [
            ("SELECT id FROM src /*", "a /* comment"),
            ("SELECT id FROM src /* a /* b */", "a /* comment"),
            ("SELECT 'x", "a ' string"),
            ('SELECT "x', 'a " string'),
            ("SELECT `x FROM t", "a ` identifier"),
        ],
    )
    def test_a_query_left_open_is_refused(self, tmp_path, sql, left_open):
        contract = _edit(q=lambda c: c["builds"][0]["properties"].update(sql=sql))
        compiled = _refused(contract, "query", tmp_path)
        assert any(left_open in str(r) for r in compiled.refusals), compiled.refusals

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1 AS id /* a /* nested */ comment */",
            "SELECT 'it\\'s', '/*', `a``b` FROM t -- trailing /*",
        ],
    )
    def test_a_closed_query_is_kept(self, tmp_path, sql):
        contract = _edit(q=lambda c: c["builds"][0]["properties"].update(sql=sql))
        assert "databricks-lakeflow-query" not in _kinds(_compile(contract, tmp_path))

    def test_the_query_is_the_last_text_in_the_file(self, tmp_path):
        """A rule description written after the query could be swallowed by a comment the
        query opens; the check views now come first and the query last."""
        contract = _contract()
        sql = _sql(_compile(contract, tmp_path))
        table_at = sql.index("CREATE OR REFRESH MATERIALIZED VIEW `capacity`")
        assert sql.rindex("CREATE OR REFRESH PRIVATE MATERIALIZED VIEW") < table_at
        query = contract["builds"][0]["properties"]["sql"].strip().rstrip(";").rstrip()
        assert sql.rstrip().endswith(query + "\n;")

    def test_the_schema_select_requires_a_schema_holding_only_the_table(self, tmp_path):
        compiled = _compile(_contract(), tmp_path)
        (grant,) = compiled.resources["databricks_grant"].values()
        assert grant["privileges"] == ["SELECT", "USE_SCHEMA"]
        (precondition,) = grant["lifecycle"]["precondition"]
        (key,) = compiled.data["databricks_tables"]
        condition = str(precondition["condition"])
        assert f"data.databricks_tables.{key}.ids" in condition
        assert '"fluid_iactest_ldp.silver_capacity.capacity"' in condition
        assert "length(setsubtract(" in condition and "== 0" in condition
        tables = compiled.data["databricks_tables"][key]
        assert str(tables["schema_name"]).startswith("${databricks_schema.")
        assert "holds tables the contract does not declare" in str(precondition["error_message"])


class TestRefusals:
    def test_another_pattern(self, tmp_path):
        contract = _edit(p=lambda c: c["builds"][0].update(pattern="hybrid-reference"))
        _refused(contract, "pattern", tmp_path)

    def test_two_statements(self, tmp_path):
        contract = _edit(q=lambda c: c["builds"][0]["properties"].update(sql="SELECT 1; SELECT 2"))
        _refused(contract, "query", tmp_path)

    @pytest.mark.parametrize(
        "trigger", [{"type": "cron", "cron": "0 * * * *"}, {"type": "schedule"}]
    )
    def test_a_schedule_is_refused(self, tmp_path, trigger):
        contract = _edit(t=lambda c: c["builds"][0]["execution"].update(trigger=trigger))
        _refused(contract, "trigger-unsupported", tmp_path)

    def test_retries(self, tmp_path):
        contract = _edit(r=lambda c: c["builds"][0]["execution"].update(retries=2))
        _refused(contract, "setting-unsupported", tmp_path)

    def test_two_outputs(self, tmp_path):
        def two(contract: Dict[str, Any]) -> None:
            second = copy.deepcopy(contract["exposes"][0])
            second["exposeId"] = "capacity2"
            second["binding"]["location"]["schema"] = "silver_capacity2"
            contract["exposes"].append(second)
            contract["builds"][0]["outputs"] = ["capacity", "capacity2"]

        _refused(_edit(t=two), "outputs", tmp_path)

    def test_a_write_grant(self, tmp_path):
        contract = _edit(
            g=lambda c: c["accessPolicy"]["grants"].append(
                {
                    "principal": "group:writers",
                    "permissions": ["write"],
                    "resources": ["$.exposes[?(@.exposeId=='capacity')]"],
                }
            )
        )
        _refused(contract, "write-grant", tmp_path)

    def test_a_column_restriction(self, tmp_path):
        def restrict(contract: Dict[str, Any]) -> None:
            contract["exposes"][0]["policy"] = {
                "authz": {
                    "readers": ["group:analysts"],
                    "columnRestrictions": [
                        {"principal": "group:analysts", "columns": ["site_id"], "access": "deny"}
                    ],
                }
            }

        compiled = _compile(_edit(r=restrict), tmp_path)
        assert "databricks-lakeflow-enforcement" in _kinds(compiled), _kinds(compiled)

    def test_a_second_expose_in_the_same_schema(self, tmp_path):
        def share(contract: Dict[str, Any]) -> None:
            other = copy.deepcopy(contract["exposes"][0])
            other["exposeId"] = "other"
            other["binding"]["location"]["table"] = "other"
            other["contract"].pop("dq")
            contract["exposes"].append(other)

        _refused(_edit(s=share), "schema-shared", tmp_path)

    def test_a_dbt_build_writing_the_same_expose(self, tmp_path):
        dbt = {
            "id": "dbt-capacity",
            "pattern": "hybrid-reference",
            "engine": "dbt-databricks",
            "outputs": ["capacity"],
            "execution": {"runtime": {"platform": "databricks"}},
            "properties": {"model": "x"},
        }
        _refused(_edit(b=lambda c: c["builds"].append(dbt)), "writers", tmp_path)

    def test_a_valid_values_rule_with_nothing_to_check_against(self, tmp_path):
        def bare(contract: Dict[str, Any]) -> None:
            contract["exposes"][0]["contract"]["schema"][2].pop("validationRules")

        _refused(_edit(b=bare), "rule-unexpressible", tmp_path)

    def test_a_share_threshold_on_an_error_rule(self, tmp_path):
        def share(contract: Dict[str, Any]) -> None:
            rules = contract["exposes"][0]["contract"]["dq"]["rules"]
            rules[1].update(threshold=0.9, operator=">=")

        _refused(_edit(s=share), "rule-threshold", tmp_path)

    def test_the_same_share_threshold_on_a_warn_rule_is_accepted(self, tmp_path):
        def share(contract: Dict[str, Any]) -> None:
            rules = contract["exposes"][0]["contract"]["dq"]["rules"]
            rules[1].update(threshold=0.9, operator=">=", severity="warn")

        compiled = _compile(_edit(s=share), tmp_path)
        assert not compiled.refusals, _kinds(compiled)
        (line,) = [ln for ln in _sql(compiled).splitlines() if "`capacity_required_complete`" in ln]
        assert line.strip().startswith("CONSTRAINT `capacity_required_complete` EXPECT (")
        assert "ON VIOLATION" not in line

    def test_a_substitution_in_an_enum_value(self, tmp_path):
        def literal(contract: Dict[str, Any]) -> None:
            contract["exposes"][0]["contract"]["schema"][2]["validationRules"] = [
                {"type": "enum", "constraint": "A,${x}"}
            ]

        _refused(_edit(l=literal), "literal-unsafe", tmp_path)

    def test_an_unresolvable_reference(self):
        compiled = dbx._compile(_contract(), ENV)
        assert "databricks-lakeflow-reference-unresolved" in _kinds(compiled)

    def test_validate_reports_the_refusal_with_its_fix(self):
        errors, _warnings = dbx.validate_databricks_binding(_contract(), ENV)
        assert any("sites" in e and "fluid.workspace.yaml" in e for e in errors), errors


# ── 6. the plan ─────────────────────────────────────────────────────────────
def test_the_planner_lists_the_pipeline_with_its_expectations(tmp_path):
    with contract_source(_workspace(tmp_path, _contract())):
        actions = dbx.planned_objects(_contract())
    assert not [a for a in actions if a["resource_type"] == "databricks_sql_table"]
    (pipeline,) = [a for a in actions if a["resource_type"] == "databricks_pipeline"]
    table = "fluid_iactest_ldp.silver_capacity.capacity"
    view = lambda name: f"fluid_iactest_ldp.silver_capacity.capacity__{name}"  # noqa: E731
    expected = {
        (table, "capacity_required_complete", "error"),
        (table, "capacity_method_valid", "warn"),
        (view("capacity_key_unique"), "capacity_key_unique", "critical"),
        (view("capacity_freshness"), "capacity_freshness", "warn"),
        (view("capacity_site_ref"), "capacity_site_ref", "warn"),
    }
    listed = {
        (e["dataset"], e["rule"], e["severity"]) if isinstance(e, dict) else tuple(e)
        for e in pipeline["expectations"]
    }
    assert listed == expected


# ── 7. verify ───────────────────────────────────────────────────────────────
def test_verify_sees_readers_tags_and_column_tags(tmp_path):
    with contract_source(_workspace(tmp_path, _contract())):
        tables, _jobs = dbx.managed_objects(_contract(), ENV)
    table = tables[0]
    assert "analysts" in table.readers
    assert ("fluid_product_id", "silver.test.capacity_v1") in table.tags
    assert ("site_id", "fluid_sensitivity", "internal") in table.column_tags


# ── delivery review, 2026-11-12 ────────────────────────────────────────────
def test_a_lakeflow_build_whose_expose_another_build_names_is_refused_not_a_crash(tmp_path):
    """The lakeflow build declares no outputs and a dbt build names the expose: the
    expose is the dbt build's (builds_writing_expose), so the pipeline would write a
    table another build owns."""

    def dbt_writes_it(contract):
        del contract["builds"][0]["outputs"]
        contract["builds"].append(
            {
                "id": "dbt-capacity",
                "engine": "dbt-databricks",
                "pattern": "hybrid-reference",
                "outputs": ["capacity"],
                "properties": {"model": "capacity"},
                "execution": {"runtime": {"platform": "databricks"}},
            }
        )

    compiled = _compile(_edit(d=dbt_writes_it), tmp_path)
    assert "databricks-lakeflow-writers" in _kinds(compiled)


def test_removing_a_pipeline_is_data_loss_its_file_and_folder_are_not():
    """Deleting a pipeline can delete its tables, so the data-loss gate counts it; its
    generated source file and folder are run definitions, removed ungated."""
    from fluid_build.cli._apply_opentofu_engine import _data_bearing_changes

    removals = [
        ("databricks_pipeline.p", "databricks_pipeline"),
        ("databricks_workspace_file.f", "databricks_workspace_file"),
        ("databricks_directory.d", "databricks_directory"),
    ]
    counted, revoked = _data_bearing_changes({"add": 0, "change": 0, "remove": 3}, removals)
    assert counted["remove"] == 1 and revoked == []


def test_a_schema_holding_other_tables_fails_the_plan_with_one_clean_line():
    output = (
        "Error: Resource precondition failed\n  on main.tf.json line 42: Schema\n"
        "fluid_iactest_ldp.silver_capacity holds tables the contract does not declare\n"
        "(fluid_iactest_ldp.silver_capacity.someone_elses). A Lakeflow pipeline's readers ..."
    )
    refusal = dbx.DatabricksIacPlugin().explain_plan_failure(output, _contract(), [])
    assert refusal is not None and refusal.kind == "databricks-lakeflow-schema-not-owned"
    assert "(someone_elses)" in str(refusal)


def test_verify_knows_a_pipeline_table_gets_its_tags_from_an_update(tmp_path):
    with contract_source(_workspace(tmp_path, _contract())):
        tables, _jobs = dbx.managed_objects(_contract(), ENV)
    (table,) = tables
    assert table.pipeline_written is True and table.dbt_written is False


def test_a_pipeline_table_with_keys_is_warned_and_left_out_of_key_reconciling(
    tmp_path, monkeypatch
):
    """Unity Catalog keys go on a table with ALTER TABLE, which a pipeline's materialized view
    refuses; until the definition carries them, they are said and not attempted."""
    contract = _contract()
    site = contract["exposes"][0]["contract"]["schema"][0]
    site["tags"] = ["primary-key"]
    compiled = _compile(contract, tmp_path)
    assert any("primary and foreign keys are not declared" in w for w in compiled.warnings)
    seen: List[Any] = []
    monkeypatch.setattr(dbx, "reconcile", lambda targets, *a, **k: seen.append(targets))
    with contract_source(tmp_path / "capacity" / "contract.fluid.yaml"):
        dbx._declare_keys(contract, lambda line: None)
    assert seen == [[]]
