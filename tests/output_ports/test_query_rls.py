# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Regression tests: ``policy.authz.rowFilters`` (row-level security) MUST be
enforced on the ``query`` and ``query_sql`` tools — not just ``sample``.

These pin, at the compiler and the handler:

1. the semantic ``query`` reads the table through
   ``(SELECT <columns> FROM <table> AS _fluid_rows WHERE <predicate>)``, and a
   caller's own filter still binds its parameter beside it;
2. free-form ``query_sql`` reads every reference to the exposed table through the
   same filtered projection, so a caller-shaped projection (a literal aliased as
   the filter column) cannot widen it;
3. fail-closed (:class:`RowFilterIdentityMissing`) when the request carries no
   principal a filter or reader names, and (:class:`RowFilterUnavailable`) when a
   filter cannot be applied, the old ``policy.rowFilters`` key included;
4. the handler wiring passes ``caller_attributes`` / ``expose`` through, so the
   SQL the driver executes is the filtered one, and a refusal never reaches it.

``tests/output_ports/test_mcp_authz_row_filters.py`` runs the same policy end to
end on DuckDB.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fluid_build.output_ports.mcp._handlers import tool_query, tool_query_sql
from fluid_build.output_ports.mcp.query_compiler import (
    RowFilterIdentityMissing,
    RowFilterUnavailable,
    compile_free_form_sql,
    compile_semantic_query,
    row_filter_predicate,
)

_MEASURES = [{"name": "row_count", "agg": "count", "expr": "id"}]
_SCHEMA = [{"name": "id"}, {"name": "tenant_id"}, {"name": "region"}, {"name": "secret"}]
ACME = "group:acme@tenants.example"

TENANT_EXPOSE = {
    "exposeId": "demo",
    "contract": {"schema": _SCHEMA},
    "semantics": {"measures": _MEASURES},
    "policy": {"authz": {"rowFilters": [{"principal": ACME, "where": "tenant_id = 'acme'"}]}},
}
_PROJECTION = (
    "(SELECT id, tenant_id, region, secret FROM db.t AS _fluid_rows "
    "WHERE ((_fluid_rows.tenant_id = 'acme')))"
)


# ---------------------------------------------------------------------------
# Compiler level — query() executes compiled.sql verbatim, so a row filter in
# the compiled SQL IS the executed SQL.
# ---------------------------------------------------------------------------


class TestSemanticQueryRowFilter:
    def test_row_filter_sits_inside_the_projection(self):
        compiled = compile_semantic_query(
            expose=TENANT_EXPOSE,
            measure="row_count",
            limit=100,
            caller_attributes={"principal": ACME},
            table_reference="db.t",
        )
        assert f"FROM {_PROJECTION} AS _fluid_allowed" in compiled.sql
        assert compiled.params == []

    def test_a_callers_filter_still_binds_beside_the_row_filter(self):
        compiled = compile_semantic_query(
            expose=TENANT_EXPOSE,
            measure="row_count",
            filters={"region": "us"},
            limit=100,
            caller_attributes={"principal": ACME},
            table_reference="db.t",
        )
        assert "WHERE ((region) = :p_0)" in compiled.sql
        assert _PROJECTION in compiled.sql
        assert compiled.params == ["us"]

    def test_fail_closed_when_the_principal_is_missing(self):
        with pytest.raises(RowFilterIdentityMissing, match="principal"):
            compile_semantic_query(
                expose=TENANT_EXPOSE,
                measure="row_count",
                limit=100,
                caller_attributes={"model_id": "x", "tenant_id": "acme"},
                table_reference="db.t",
            )

    def test_no_rowfilter_no_projection(self):
        compiled = compile_semantic_query(
            expose={"exposeId": "d", "semantics": {"measures": _MEASURES}},
            measure="row_count",
            limit=10,
            caller_attributes={"principal": ACME},
            table_reference="db.t",
        )
        assert "_fluid_rows" not in compiled.sql and "FROM db.t" in compiled.sql


class TestFreeFormQueryRowFilter:
    def test_every_read_of_the_table_is_filtered(self):
        compiled = compile_free_form_sql(
            sql="SELECT a.id FROM db.t AS a JOIN db.t AS b ON a.id = b.id",
            table_reference="db.t",
            limit=50,
            expose=TENANT_EXPOSE,
            caller_attributes={"principal": ACME},
        )
        assert compiled.sql.count(_PROJECTION) == 2
        assert compiled.params == []

    def test_a_projection_spoof_cannot_widen_the_filter(self):
        # A caller-controlled projection that fabricates the filter column. The
        # filter is applied where the table is read, inside the projection, so the
        # caller's alias never meets it.
        compiled = compile_free_form_sql(
            sql="SELECT 'acme' AS tenant_id, secret FROM db.t",
            table_reference="db.t",
            limit=50,
            expose=TENANT_EXPOSE,
            caller_attributes={"principal": ACME},
        )
        assert compiled.sql.startswith(
            f"SELECT 'acme' AS tenant_id, secret FROM {_PROJECTION} AS t"
        )

    def test_no_rewrite_when_no_rowfilters(self):
        compiled = compile_free_form_sql(
            sql="SELECT id FROM db.t",
            table_reference="db.t",
            limit=50,
        )
        assert "SELECT * FROM (" not in compiled.sql and "_fluid_rows" not in compiled.sql
        assert compiled.sql.rstrip().endswith("LIMIT 50")
        assert compiled.params == []

    def test_no_double_limit_when_caller_already_limited(self):
        # The server-side LIMIT must not be appended a second time when the caller's
        # SQL already ends in ``LIMIT <n>`` (two trailing LIMITs is a syntax error on
        # most engines), row filter or not.
        for expose, attrs in ((None, None), (TENANT_EXPOSE, {"principal": ACME})):
            compiled = compile_free_form_sql(
                sql="SELECT id FROM db.t LIMIT 5",
                table_reference="db.t",
                limit=50,
                expose=expose,
                caller_attributes=attrs,
            )
            assert compiled.sql.count("LIMIT") == 1
            assert compiled.sql.rstrip().endswith("LIMIT 5")

    def test_fail_closed_when_the_principal_is_missing(self):
        with pytest.raises(RowFilterIdentityMissing, match="principal"):
            compile_free_form_sql(
                sql="SELECT id FROM db.t",
                table_reference="db.t",
                limit=50,
                expose=TENANT_EXPOSE,
                caller_attributes={"model_id": "x"},
            )


# ---------------------------------------------------------------------------
# Handler level — proves tool_query / tool_query_sql actually thread
# caller_attributes + expose into the compiler (a regression that drops them
# would re-open the bypass even with the compiler fixed).
# ---------------------------------------------------------------------------


class _CapturingDriver:
    def __init__(self):
        self.captured = None
        self._restricted_columns: set = set()
        self._pii_columns: set = set()

    def descriptor(self):
        return SimpleNamespace(
            table_reference="db.t",
            dialect="duckdb",
            platform="local",
            format="csv",
            capabilities={},
        )

    def query(self, *, compiled, timeout_seconds=None):
        self.captured = compiled
        return SimpleNamespace(columns=[], rows=[])


def _fake_state(expose, caller_attributes, driver):
    return SimpleNamespace(
        expose=expose,
        caller_attributes=caller_attributes,
        policy=SimpleNamespace(max_sample_rows=100),
        query_timeout_seconds=None,
        get_driver=lambda: driver,
    )


class TestHandlerWiring:
    def test_tool_query_executes_rowfiltered_sql(self):
        driver = _CapturingDriver()
        state = _fake_state(TENANT_EXPOSE, {"principal": ACME}, driver)
        tool_query(state, {"measure": "row_count", "limit": 10})
        assert driver.captured is not None
        assert _PROJECTION in driver.captured.sql

    def test_tool_query_sql_executes_rowfiltered_sql(self):
        driver = _CapturingDriver()
        state = _fake_state(TENANT_EXPOSE, {"principal": ACME}, driver)
        tool_query_sql(state, {"sql": "SELECT id, tenant_id FROM db.t", "limit": 10})
        assert driver.captured is not None
        assert _PROJECTION in driver.captured.sql

    def test_tool_query_fail_closed_never_executes(self):
        driver = _CapturingDriver()
        state = _fake_state(TENANT_EXPOSE, {"model_id": "x"}, driver)
        with pytest.raises(RowFilterIdentityMissing):
            tool_query(state, {"measure": "row_count", "limit": 10})
        assert driver.captured is None  # fail-closed: no SQL executed

    def test_tool_query_sql_fail_closed_never_executes(self):
        driver = _CapturingDriver()
        state = _fake_state(TENANT_EXPOSE, {"model_id": "x"}, driver)
        with pytest.raises(RowFilterIdentityMissing):
            tool_query_sql(state, {"sql": "SELECT id FROM db.t", "limit": 10})
        assert driver.captured is None


# ---------------------------------------------------------------------------
# A row filter that cannot be applied is refused, never skipped
# ---------------------------------------------------------------------------

_MALFORMED_ROW_FILTERS = [
    pytest.param({"principal": ACME, "where": "plan = 'gold'"}, id="undeclared-column"),
    pytest.param({"principal": ACME}, id="no-predicate"),
    pytest.param({"where": "tenant_id = 'acme'"}, id="no-principal"),
    pytest.param({"principal": 7, "where": "tenant_id = 'acme'"}, id="non-string-principal"),
    pytest.param({"principal": ACME, "where": ["tenant_id = 'acme'"]}, id="list-predicate"),
    pytest.param({"principal": ACME, "where": "tenant_id = 'acme' OR"}, id="not-an-expression"),
    pytest.param({"principal": ACME, "where": "tenant_id", "name": "Bad Name"}, id="bad-name"),
    pytest.param("tenant_id = 'acme'", id="not-a-mapping"),
]


def _expose_with_row_filter(entry):
    return {**TENANT_EXPOSE, "policy": {"authz": {"rowFilters": [entry]}}}


class TestMalformedRowFilterIsRefused:
    @pytest.mark.parametrize("entry", _MALFORMED_ROW_FILTERS)
    def test_semantic_query(self, entry):
        with pytest.raises(RowFilterUnavailable, match="rowFilters"):
            compile_semantic_query(
                expose=_expose_with_row_filter(entry),
                measure="row_count",
                limit=10,
                caller_attributes={"principal": ACME},
                table_reference="db.t",
            )

    @pytest.mark.parametrize("entry", _MALFORMED_ROW_FILTERS)
    def test_free_form_query(self, entry):
        with pytest.raises(RowFilterUnavailable, match="rowFilters"):
            compile_free_form_sql(
                sql="SELECT id FROM db.t",
                table_reference="db.t",
                limit=10,
                expose=_expose_with_row_filter(entry),
                caller_attributes={"principal": ACME},
            )

    @pytest.mark.parametrize("entry", _MALFORMED_ROW_FILTERS)
    def test_sample_predicate(self, entry):
        # ``sample`` resolves its predicate through the same function.
        with pytest.raises(RowFilterUnavailable, match="rowFilters"):
            row_filter_predicate(_expose_with_row_filter(entry), {"principal": ACME})

    def test_a_valid_entry_beside_a_malformed_one_does_not_hide_it(self):
        expose = {
            **TENANT_EXPOSE,
            "policy": {
                "authz": {
                    "rowFilters": [
                        {"principal": ACME, "where": "tenant_id = 'acme'"},
                        {"principal": "group:other@tenants.example", "where": "region LIKE"},
                    ]
                }
            },
        }
        with pytest.raises(RowFilterUnavailable, match=r"rowFilters\[1\]"):
            compile_semantic_query(
                expose=expose,
                measure="row_count",
                limit=10,
                caller_attributes={"principal": ACME},
                table_reference="db.t",
            )

    def test_the_old_key_is_refused_with_the_field_to_use(self):
        expose = {
            **TENANT_EXPOSE,
            "policy": {"rowFilters": [{"column": "tenant_id", "equals": "acme"}]},
        }
        with pytest.raises(RowFilterUnavailable, match=r"policy\.authz\.rowFilters"):
            compile_semantic_query(
                expose=expose,
                measure="row_count",
                limit=10,
                caller_attributes={"principal": ACME},
                table_reference="db.t",
            )

    def test_no_row_filter_and_an_empty_list_are_not_malformed(self):
        for policy in ({}, {"authz": {}}, {"authz": {"rowFilters": []}}, {"authz": None}):
            compiled = compile_semantic_query(
                expose={**TENANT_EXPOSE, "policy": policy},
                measure="row_count",
                limit=10,
                table_reference="db.t",
            )
            assert "WHERE" not in compiled.sql
