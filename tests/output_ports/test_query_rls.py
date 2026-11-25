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
from typing import Any, Dict, Optional

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
_SCHEMA = [
    {"name": "id", "type": "integer"},
    {"name": "tenant_id", "type": "string"},
    {"name": "region", "type": "string"},
    {"name": "secret", "type": "string"},
]
ACME = "group:acme@tenants.example"
AS_ACME = {"principal": ACME}
NO_PRINCIPAL = {"model_id": "x", "tenant_id": "acme"}

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
# query_sql on a known engine selects each declared column folded and quoted.
_QUOTED_PROJECTION = (
    '(SELECT "id", "tenant_id", "region", "secret" FROM db.t AS _fluid_rows '
    "WHERE ((_fluid_rows.tenant_id = 'acme')))"
)


def _semantic(expose: Dict[str, Any] = TENANT_EXPOSE, attrs: Any = AS_ACME, **kwargs: Any):
    return compile_semantic_query(
        expose=expose,
        measure="row_count",
        limit=kwargs.pop("limit", 100),
        caller_attributes=attrs,
        table_reference="db.t",
        **kwargs,
    )


def _free_form(
    sql: str = "SELECT id FROM db.t",
    expose: Optional[Dict[str, Any]] = TENANT_EXPOSE,
    attrs: Any = AS_ACME,
):
    return compile_free_form_sql(
        sql=sql, table_reference="db.t", limit=50, expose=expose, caller_attributes=attrs
    )


# ---------------------------------------------------------------------------
# Compiler level — query() executes compiled.sql verbatim, so a row filter in
# the compiled SQL IS the executed SQL.
# ---------------------------------------------------------------------------


class TestSemanticQueryRowFilter:
    def test_row_filter_sits_inside_the_projection(self):
        compiled = _semantic()
        assert f"FROM {_PROJECTION} AS _fluid_allowed" in compiled.sql
        assert compiled.params == []

    def test_a_callers_filter_still_binds_beside_the_row_filter(self):
        compiled = _semantic(filters={"region": "us"})
        assert "WHERE ((region) = :p_0)" in compiled.sql
        assert _PROJECTION in compiled.sql
        assert compiled.params == ["us"]

    def test_no_rowfilter_no_projection(self):
        compiled = _semantic({"exposeId": "d", "semantics": {"measures": _MEASURES}}, limit=10)
        assert "_fluid_rows" not in compiled.sql and "FROM db.t" in compiled.sql


class TestFreeFormQueryRowFilter:
    def test_every_read_of_the_table_is_filtered(self):
        compiled = _free_form("SELECT a.id FROM db.t AS a JOIN db.t AS b ON a.id = b.id")
        assert compiled.sql.count(_PROJECTION) == 2
        assert compiled.params == []

    def test_a_projection_spoof_cannot_widen_the_filter(self):
        # A caller-controlled projection that fabricates the filter column. The
        # filter is applied where the table is read, inside the projection, so the
        # caller's alias never meets it.
        compiled = _free_form("SELECT 'acme' AS tenant_id, secret FROM db.t")
        assert compiled.sql.startswith(
            f"SELECT 'acme' AS tenant_id, secret FROM {_PROJECTION} AS t"
        )

    def test_no_rewrite_when_no_rowfilters(self):
        compiled = _free_form(expose=None, attrs=None)
        assert "SELECT * FROM (" not in compiled.sql and "_fluid_rows" not in compiled.sql
        assert compiled.sql.rstrip().endswith("LIMIT 50")
        assert compiled.params == []

    def test_no_double_limit_when_caller_already_limited(self):
        # The server-side LIMIT must not be appended a second time when the caller's
        # SQL already ends in ``LIMIT <n>`` (two trailing LIMITs is a syntax error on
        # most engines), row filter or not.
        for expose, attrs in ((None, None), (TENANT_EXPOSE, AS_ACME)):
            compiled = _free_form("SELECT id FROM db.t LIMIT 5", expose, attrs)
            assert compiled.sql.count("LIMIT") == 1
            assert compiled.sql.rstrip().endswith("LIMIT 5")


@pytest.mark.parametrize("compile_for", [_semantic, _free_form], ids=["query", "query_sql"])
def test_fail_closed_when_the_principal_is_missing(compile_for):
    with pytest.raises(RowFilterIdentityMissing, match="principal"):
        compile_for(attrs=NO_PRINCIPAL)


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


_TOOL_CALLS = [
    pytest.param(tool_query, {"measure": "row_count", "limit": 10}, id="query"),
    pytest.param(tool_query_sql, {"sql": "SELECT id, tenant_id FROM db.t", "limit": 10}, id="sql"),
]


class TestHandlerWiring:
    @pytest.mark.parametrize("tool, arguments", _TOOL_CALLS)
    def test_the_tool_executes_rowfiltered_sql(self, tool, arguments):
        driver = _CapturingDriver()
        payload = tool(_fake_state(TENANT_EXPOSE, AS_ACME, driver), arguments)
        assert driver.captured is not None
        projection = _QUOTED_PROJECTION if tool is tool_query_sql else _PROJECTION
        assert projection in driver.captured.sql
        # The caller is shown the statement with its predicate, policy, left out.
        assert "'acme'" not in payload["compiled"]["sql"]
        assert "(<row filter>)" in payload["compiled"]["sql"]

    @pytest.mark.parametrize("tool, arguments", _TOOL_CALLS)
    def test_the_tool_fails_closed_and_never_executes(self, tool, arguments):
        driver = _CapturingDriver()
        with pytest.raises(RowFilterIdentityMissing):
            tool(_fake_state(TENANT_EXPOSE, NO_PRINCIPAL, driver), arguments)
        assert driver.captured is None  # fail-closed: no SQL executed


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


def _expose_with(policy: Dict[str, Any]) -> Dict[str, Any]:
    return {**TENANT_EXPOSE, "policy": policy}


def _expose_with_row_filter(entry):
    return _expose_with({"authz": {"rowFilters": [entry]}})


class TestMalformedRowFilterIsRefused:
    @pytest.mark.parametrize("entry", _MALFORMED_ROW_FILTERS)
    @pytest.mark.parametrize("compile_for", [_semantic, _free_form], ids=["query", "query_sql"])
    def test_every_compiler_refuses_it(self, entry, compile_for):
        with pytest.raises(RowFilterUnavailable, match="rowFilters"):
            compile_for(expose=_expose_with_row_filter(entry))

    @pytest.mark.parametrize("entry", _MALFORMED_ROW_FILTERS)
    def test_sample_predicate(self, entry):
        # ``sample`` resolves its predicate through the same function.
        with pytest.raises(RowFilterUnavailable, match="rowFilters"):
            row_filter_predicate(_expose_with_row_filter(entry), AS_ACME)

    @pytest.mark.parametrize(
        "policy, match",
        [
            pytest.param(
                {
                    "authz": {
                        "rowFilters": [
                            {"principal": ACME, "where": "tenant_id = 'acme'"},
                            {"principal": "group:other@tenants.example", "where": "region LIKE"},
                        ]
                    }
                },
                r"rowFilters\[1\]",
                id="a-valid-entry-beside-a-malformed-one",
            ),
            pytest.param(
                {"rowFilters": [{"column": "tenant_id", "equals": "acme"}]},
                r"policy\.authz\.rowFilters",
                id="the-old-key",
            ),
        ],
    )
    def test_the_refusal_names_what_to_fix(self, policy, match):
        with pytest.raises(RowFilterUnavailable, match=match):
            _semantic(_expose_with(policy), limit=10)

    def test_no_row_filter_and_an_empty_list_are_not_malformed(self):
        for policy in ({}, {"authz": {}}, {"authz": {"rowFilters": []}}, {"authz": None}):
            compiled = _semantic(_expose_with(policy), attrs=None, limit=10)
            assert "WHERE" not in compiled.sql
