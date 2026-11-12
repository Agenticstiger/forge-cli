# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""DatabricksValidationProvider against a fake workspace client (no SDK, no network)."""

from __future__ import annotations

import pytest

from fluid_build.providers.databricks.statements import StatementError
from fluid_build.providers.databricks_validation import (
    DatabricksValidationProvider,
    sql_table_ref,
    table_name,
)
from fluid_build.providers.validation_provider import FieldSchema, ResourceSchema, ResourceType


class NotFound(Exception):
    status_code = 404


def table_json(name="smart_meters", columns=None):
    cols = columns or [
        ("site_id", "string", "STRING", False),
        ("qty", "int", "INT", True),
        ("price", "decimal(12,3)", "DECIMAL", True),
    ]
    return {
        "name": name,
        "table_type": "MANAGED",
        "columns": [
            {"name": n, "type_text": tt, "type_name": tn, "nullable": nb} for n, tt, tn, nb in cols
        ],
    }


class FakeApi:
    """Routes api_client.do by method+path; records every call."""

    def __init__(self):
        self.calls = []
        self.tables = {}  # full name -> json or exception
        self.warehouses = {"w": {"id": "w"}}
        self.statements = []  # (predicate, rows)
        self.sql = []

    def do(self, method, path, query=None, body=None, **kw):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/api/2.0/sql/warehouses/"):
            item = self.warehouses.get(path.rsplit("/", 1)[1], NotFound("no wh"))
        elif method == "GET" and path.startswith("/api/2.1/unity-catalog/tables/"):
            from urllib.parse import unquote

            item = self.tables.get(unquote(path.rsplit("/", 1)[1]), NotFound("no table"))
        elif method == "POST" and path.rstrip("/") == "/api/2.0/sql/statements":
            self.sql.append(body)
            item = self._answer(body)
        else:
            raise AssertionError(f"unexpected call {method} {path}")
        if isinstance(item, Exception):
            raise item
        return item

    def _answer(self, body):
        text = body["statement"]
        for pred, rows, cols in self.statements:
            if pred in text:
                return {
                    "statement_id": "s",
                    "status": {"state": "SUCCEEDED"},
                    "manifest": {
                        "schema": {"columns": [{"name": c, "type_name": t} for c, t in cols]}
                    },
                    "result": {"data_array": rows},
                }
        return {
            "statement_id": "s",
            "status": {"state": "SUCCEEDED"},
            "manifest": {"schema": {"columns": [{"name": "n", "type_name": "LONG"}]}},
            "result": {"data_array": [["0"]]},
        }

    def stmt_calls(self):
        return [c for c in self.calls if c[0] == "POST"]


class FakeClient:
    def __init__(self, api):
        self.api_client = api


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def provider(api):
    return DatabricksValidationProvider(
        {"client_factory": lambda: FakeClient(api), "warehouse_id": "w"}
    )


EXPOSE = {
    "id": "meters",
    "binding": {
        "platform": "databricks",
        "location": {"database": "main", "schema": "energy", "table": "smart_meters"},
    },
    "contract": {
        "schema": [
            {"name": "site_id", "type": "VARCHAR", "required": True},
            {"name": "qty", "type": "INTEGER"},
            {"name": "price", "type": "DECIMAL(12,3)"},
        ]
    },
}
FULL = "main.energy.smart_meters"


# ---------------------------------------------------------------- names


def test_table_name_validates_parts():
    assert table_name(EXPOSE) == ("main", "energy", "smart_meters")
    assert table_name({"binding": {}}) is None
    bad = {"binding": {"location": {"database": "m", "schema": "s", "table": "bad-name"}}}
    with pytest.raises(ValueError):
        table_name(bad)


def test_table_name_rejects_backtick():
    bad = {"binding": {"location": {"database": "m", "schema": "s", "table": "t`x"}}}
    with pytest.raises(ValueError):
        table_name(bad)


def test_sql_table_ref_backticks_each_part():
    assert sql_table_ref(("a", "b", "c")) == "`a`.`b`.`c`"


# ----------------------------------------------------------- connection


def test_validate_connection_gets_warehouse(provider, api):
    assert provider.validate_connection() is True
    assert api.calls == [("GET", "/api/2.0/sql/warehouses/w", None)]


def test_validate_connection_404_names_warehouse(api):
    p = DatabricksValidationProvider(
        {"client_factory": lambda: FakeClient(api), "warehouse_id": "gone"}
    )
    with pytest.raises(StatementError, match="gone"):
        p.validate_connection()


def test_validate_connection_other_error_is_wrapped(api):
    api.warehouses["w"] = RuntimeError("boom")
    p = DatabricksValidationProvider(
        {"client_factory": lambda: FakeClient(api), "warehouse_id": "w"}
    )
    with pytest.raises(StatementError, match="RuntimeError: boom"):
        p.validate_connection()


def test_validate_connection_needs_warehouse_env(api, monkeypatch):
    monkeypatch.delenv("DATABRICKS_WAREHOUSE_ID", raising=False)
    p = DatabricksValidationProvider({"client_factory": lambda: FakeClient(api)})
    with pytest.raises(StatementError, match="DATABRICKS_WAREHOUSE_ID"):
        p.validate_connection()


# ---------------------------------------------------------------- schema


def test_get_resource_schema_missing_table_is_none(provider):
    assert provider.get_resource_schema(EXPOSE) is None


def test_get_resource_schema_maps_columns_and_counts_rows(provider, api):
    api.tables[FULL] = table_json()
    api.statements.append(("COUNT(*)", [["42"]], [("c", "LONG")]))
    schema = provider.get_resource_schema(EXPOSE)
    assert [(f.name, f.type, f.mode) for f in schema.fields] == [
        ("site_id", "string", "REQUIRED"),
        ("qty", "int", "NULLABLE"),
        ("price", "decimal(12,3)", "NULLABLE"),
    ]
    assert schema.row_count == 42 and schema.fully_qualified_name == FULL
    assert schema.metadata["table_type"] == "MANAGED"
    assert api.sql[0]["statement"] == "SELECT COUNT(*) FROM `main`.`energy`.`smart_meters`"


def test_get_resource_schema_requires_location(provider):
    with pytest.raises(ValueError, match="binding.location"):
        provider.get_resource_schema({"binding": {}})


def test_get_resource_schema_other_error_wrapped(provider, api):
    api.tables[FULL] = RuntimeError("kaboom")
    with pytest.raises(StatementError, match="kaboom"):
        provider.get_resource_schema(EXPOSE)


def live_schema(fields, rows=5):
    return ResourceSchema(ResourceType.TABLE, FULL, fields, row_count=rows)


def live_fields():
    return [
        FieldSchema("site_id", "string", "REQUIRED"),
        FieldSchema("qty", "int", "NULLABLE"),
        FieldSchema("price", "decimal(12,3)", "NULLABLE"),
    ]


def test_validate_resource_contract_types_match_live_types(provider):
    res = provider.validate_resource(EXPOSE, live_schema(live_fields()))
    assert res.success and res.issues == []


def test_validate_resource_missing_table(provider):
    res = provider.validate_resource(EXPOSE, None)
    assert not res.success
    (issue,) = res.issues
    assert issue.category == "missing_resource" and FULL in issue.message


def test_validate_resource_real_type_change_is_type_mismatch(provider):
    fields = live_fields()
    fields[1] = FieldSchema("qty", "bigint", "NULLABLE")
    res = provider.validate_resource(EXPOSE, live_schema(fields))
    assert not res.success
    assert [(i.category, i.severity) for i in res.issues] == [("type_mismatch", "error")]


def test_validate_resource_missing_column(provider):
    res = provider.validate_resource(EXPOSE, live_schema(live_fields()[:2]))
    assert [i.category for i in res.issues] == ["missing_field"]


def test_validate_resource_empty_table_is_warning_only(provider):
    res = provider.validate_resource(EXPOSE, live_schema(live_fields(), rows=0))
    assert res.success
    assert [(i.category, i.severity) for i in res.issues] == [("empty_table", "warning")]


def test_validate_resource_sla_row_count_min(provider):
    spec = {**EXPOSE, "sla": {"row_count_min": 10}}
    res = provider.validate_resource(spec, live_schema(live_fields(), rows=3))
    assert not res.success
    assert [i.category for i in res.issues] == ["row_count_below_threshold"]


# ----------------------------------------------------------- quality rules


@pytest.fixture
def live(api):
    """The tested table exists (the rule tests read data from it)."""
    api.tables[FULL] = table_json()
    return api


def test_rules_on_a_missing_table_run_no_statement(provider, api):
    rules = [
        {"id": "c1", "type": "completeness", "selector": "site_id", "severity": "error"},
        {"id": "u1", "type": "uniqueness", "selector": "site_id"},
    ]
    results = provider.run_quality_results(EXPOSE, rules)
    assert [r.rule_id for r in results] == ["c1", "u1"]
    assert all(not r.passed and r.actual == "not executed" for r in results)
    assert results[0].severity == "error"
    assert FULL in results[0].message and "does not exist" in results[0].message
    assert api.sql == []


def test_table_metadata_is_read_once_for_schema_and_rules(provider, live):
    live.statements.append(("COUNT(*)", [["4"]], [("n", "LONG")]))
    provider.get_resource_schema(EXPOSE)
    live.statements.insert(0, ("NULLIF(COUNT(*), 0)", [["1.0"]], [("r", "DOUBLE")]))
    provider.run_quality_results(
        EXPOSE, [{"id": "c1", "type": "completeness", "selector": "site_id"}]
    )
    gets = [
        c for c in live.calls if c[0] == "GET" and FULL.replace(".", "") in c[1].replace(".", "")
    ]
    assert len(gets) == 1


def test_run_quality_results_uses_databricks_dialect_and_backticks(provider, live):
    rules = [{"id": "c1", "type": "completeness", "selector": "site_id"}]
    live.statements.insert(0, ("NULLIF(COUNT(*), 0)", [["1.0"]], [("r", "DOUBLE")]))
    (res,) = provider.run_quality_results(EXPOSE, rules)
    stmt = live.sql[0]["statement"]
    assert res.passed and res.rule_id == "c1"
    assert "FROM `main`.`energy`.`smart_meters`" in stmt
    assert "`site_id`" in stmt and "AS DOUBLE" in stmt


def test_run_quality_results_passes_columns_for_enum(provider, live):
    spec = {
        **EXPOSE,
        "contract": {
            "schema": [
                {
                    "name": "status",
                    "validationRules": [{"type": "enum", "constraint": "A|B"}],
                }
            ]
        },
    }
    (res,) = provider.run_quality_results(
        spec, [{"id": "v", "type": "valid_values", "selector": "status"}]
    )
    assert res.passed and "NOT IN ('A', 'B')" in live.sql[0]["statement"]


def test_run_quality_results_requires_location(provider):
    with pytest.raises(ValueError):
        provider.run_quality_results({"binding": {}}, [])


# ---------------------------------------------------------- references

REF_COLUMNS = [{"name": "site_id", "labels": {"references": "acme.sites.site_id"}}]
REF_EXPOSE = {**EXPOSE, "contract": {"schema": REF_COLUMNS}}
REF_RULE = {"id": "fk", "type": "valid_values", "selector": "site_id"}


def tag_rows(live, rows):
    live.statements.insert(
        0,
        (
            "table_tags",
            rows,
            [("catalog_name", "STRING"), ("schema_name", "STRING"), ("table_name", "STRING")],
        ),
    )


def test_reference_lookup_uses_tag_query_with_params_then_anti_join(provider, live):
    tag_rows(live, [["main", "ref", "sites"], ["main", "ref", "other"]])
    live.tables["main.ref.sites"] = table_json("sites", [("site_id", "string", "STRING", True)])
    live.tables["main.ref.other"] = table_json("other", [("x", "string", "STRING", True)])
    live.statements.insert(1, ("NOT EXISTS", [["0"]], [("m", "LONG")]))
    (res,) = provider.run_quality_results(REF_EXPOSE, [REF_RULE])
    tag_call = live.sql[0]
    assert "system.information_schema.table_tags" in tag_call["statement"]
    assert tag_call["parameters"] == [
        {"name": "tag", "type": "STRING", "value": "fluid_product_id"},
        {"name": "product", "type": "STRING", "value": "acme.sites"},
    ]
    assert "FROM `main`.`ref`.`sites` AS p" in live.sql[1]["statement"]
    assert res.passed


def test_reference_no_candidate_is_not_executed_with_lookup_text(provider, live):
    tag_rows(live, [])
    (res,) = provider.run_quality_results(REF_EXPOSE, [REF_RULE])
    assert not res.passed and res.actual == "not executed"
    assert "fluid_product_id=acme.sites" in res.message


def test_reference_ambiguous_is_not_executed(provider, live):
    tag_rows(live, [["main", "a", "sites"], ["main", "b", "sites"]])
    for s in ("a", "b"):
        live.tables[f"main.{s}.sites"] = table_json(
            "sites", [("site_id", "string", "STRING", True)]
        )
    (res,) = provider.run_quality_results(REF_EXPOSE, [REF_RULE])
    assert res.actual == "not executed" and "ambiguous" in res.message


def test_reference_lookup_cached_per_product_and_column(provider, live):
    tag_rows(live, [["main", "ref", "sites"]])
    live.tables["main.ref.sites"] = table_json("sites", [("site_id", "string", "STRING", True)])
    rules = [REF_RULE, {**REF_RULE, "id": "fk2"}]
    provider.run_quality_results(REF_EXPOSE, rules)
    tag_queries = [b for b in live.sql if "table_tags" in b["statement"]]
    assert len(tag_queries) == 1
    anti_joins = [b for b in live.sql if "NOT EXISTS" in b["statement"]]
    assert len(anti_joins) == 2


def test_reference_unreadable_candidate_is_skipped(provider, live):
    tag_rows(live, [["main", "ref", "sites"]])  # table GET raises NotFound
    (res,) = provider.run_quality_results(REF_EXPOSE, [REF_RULE])
    assert res.actual == "not executed"


def test_run_quality_checks_returns_issues_for_failures(provider, live):
    live.statements.insert(0, ("NULLIF(COUNT(*), 0)", [["0.5"]], [("r", "DOUBLE")]))
    issues = provider.run_quality_checks(
        EXPOSE, [{"id": "c1", "type": "completeness", "selector": "site_id", "severity": "error"}]
    )
    assert [(i.category, i.severity) for i in issues] == [("quality", "error")]


def test_normalize_type_and_provider_name(provider):
    assert provider.provider_name == "databricks"
    assert provider.normalize_type("STRING") == "string"


def test_reference_prefers_the_tested_tables_catalog(provider, live):
    # A look-alike table tagged with the same product id in another catalog does
    # not make the lookup ambiguous, and is not the one read.
    tag_rows(live, [["main", "ref", "sites"], ["shadow", "ref", "sites"]])
    for catalog in ("main", "shadow"):
        live.tables[f"{catalog}.ref.sites"] = table_json(
            "sites", [("site_id", "string", "STRING", True)]
        )
    live.statements.insert(0, ("NOT EXISTS", [["0"]], [("n", "LONG")]))
    (res,) = provider.run_quality_results(REF_EXPOSE, [REF_RULE])
    assert res.passed, res.message
    anti_join = next(b for b in live.sql if "NOT EXISTS" in b["statement"])["statement"]
    assert "`main`.`ref`.`sites`" in anti_join and "shadow" not in anti_join


def test_reference_two_candidates_elsewhere_stay_ambiguous(provider, live):
    tag_rows(live, [["other1", "ref", "sites"], ["other2", "ref", "sites"]])
    for catalog in ("other1", "other2"):
        live.tables[f"{catalog}.ref.sites"] = table_json(
            "sites", [("site_id", "string", "STRING", True)]
        )
    (res,) = provider.run_quality_results(REF_EXPOSE, [REF_RULE])
    assert res.actual == "not executed" and "ambiguous" in res.message
