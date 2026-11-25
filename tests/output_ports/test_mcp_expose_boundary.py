# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The expose is the boundary of every MCP read, for every caller.

``sample`` and ``query_sql`` read only the columns ``contract.schema`` declares, and
``query_sql`` reads only the exposed table (with its own CTEs, ``VALUES`` and
``UNNEST``). That holds on every expose: one with no row filter or column
restriction, and for a reader no row filter narrows. ``query`` reads what the
contract's semantics name and is unchanged, so Command Center's governed lane
compiles as before.

The DuckDB table here holds a column the contract does not declare, and two names an
engine reads differently when they stand bare: ``order`` is reserved, and DuckDB
reads a bare ``current_user`` as the session's user, not as the column.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.output_ports.mcp import query_compiler  # noqa: E402
from fluid_build.output_ports.mcp._handlers import tool_query_sql, tool_sample  # noqa: E402

READER = "group:all-staff@northwind.example"
EU = "group:eu-analysts@northwind.example"

_HEADER = ("id", "region", "order", "current_user", "internal_note")
_ROWS = [
    (1, "EU", 7, "alice", "hidden-1"),
    (2, "US", 8, "bob", "hidden-2"),
    (3, "EU", 9, "carol", "hidden-3"),
]
_DECLARED = ["id", "region", "order", "current_user"]
_EU_ONLY = [{"principal": EU, "where": "region = 'EU'"}]


def _expose(
    *, row_filters: Optional[List[Dict[str, Any]]] = None, schema: Any = None
) -> Dict[str, Any]:
    expose: Dict[str, Any] = {
        "exposeId": "customers",
        "contract": {
            "schema": [{"name": name} for name in _DECLARED] if schema is None else schema
        },
    }
    if row_filters is not None:
        expose["policy"] = {"authz": {"rowFilters": row_filters, "readers": [READER]}}
    return expose


@pytest.fixture()
def state_for(tmp_path):
    """A ``SessionState`` stand-in over a DuckDB driver reading a CSV of ``_ROWS``."""
    from fluid_build.output_ports.mcp.drivers import build_driver

    csv = tmp_path / "customers.csv"
    csv.write_text(
        ",".join(_HEADER) + "\n" + "".join(",".join(map(str, row)) + "\n" for row in _ROWS),
        encoding="utf-8",
    )

    def _make(expose: Dict[str, Any]) -> SimpleNamespace:
        expose = dict(expose)
        expose["binding"] = {
            "platform": "local",
            "format": "csv",
            "location": {"path": str(csv), "table": "customers"},
        }
        driver = build_driver(expose=expose, contract={"exposes": [expose]})
        return SimpleNamespace(
            expose=expose,
            caller_attributes={},
            policy=SimpleNamespace(max_sample_rows=100),
            query_timeout_seconds=None,
            get_driver=lambda: driver,
        )

    return _make


def _users(payload: Dict[str, Any]) -> List[str]:
    return sorted(row["current_user"] for row in payload["rows"])


# ---------------------------------------------------------------------
# sample
# ---------------------------------------------------------------------


def test_sample_returns_only_the_declared_columns(state_for):
    payload = tool_sample(state_for(_expose()), {"limit": 10})
    assert payload["columns"] == _DECLARED
    assert all("internal_note" not in row for row in payload["rows"])
    # Each column is read as itself, not as what the bare name means to the engine.
    assert _users(payload) == ["alice", "bob", "carol"]
    assert sorted(row["order"] for row in payload["rows"]) == [7, 8, 9]


def test_a_filtered_callers_sample_returns_only_the_declared_columns(state_for):
    state = state_for(_expose(row_filters=_EU_ONLY))
    payload = tool_sample(state, {"limit": 10}, caller_attributes={"principal": EU})
    assert payload["columns"] == _DECLARED
    assert sorted(row["id"] for row in payload["rows"]) == [1, 3]


# ---------------------------------------------------------------------
# query_sql
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "expose",
    [_expose(), _expose(row_filters=_EU_ONLY)],
    ids=["plain-expose", "reader-of-a-filtered-expose"],
)
def test_query_sql_reads_every_row_but_only_the_declared_columns(state_for, expose):
    payload = tool_query_sql(
        state_for(expose),
        {"sql": "SELECT * FROM customers ORDER BY id"},
        caller_attributes={"principal": READER},
    )
    assert payload["columns"] == _DECLARED
    assert [row["id"] for row in payload["rows"]] == [1, 2, 3]
    assert _users(payload) == ["alice", "bob", "carol"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT internal_note FROM customers",
        "SELECT c.internal_note FROM customers AS c",
        "SELECT md5(internal_note) AS x FROM customers",
        "SELECT id FROM customers WHERE internal_note = 'hidden-1'",
    ],
)
def test_query_sql_cannot_read_an_undeclared_column(state_for, sql):
    """The column is in no relation the statement can see, however it is written."""
    with pytest.raises(duckdb.BinderException, match="internal_note"):
        tool_query_sql(state_for(_expose()), {"sql": sql}, caller_attributes={})


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("duckdb", "SELECT * FROM information_schema.tables"),
        ("duckdb", "SELECT column_name FROM information_schema.columns"),
        ("duckdb", "SELECT * FROM other_table"),
        ("duckdb", "SELECT c.id FROM customers AS c JOIN other_table AS o ON c.id = o.id"),
        ("duckdb", "SELECT * FROM duckdb_tables()"),
        ("duckdb", "SELECT * FROM read_csv('elsewhere.csv')"),
        ("postgres", "SELECT relname FROM pg_catalog.pg_class"),
        ("postgres", "SELECT * FROM payroll"),
        ("snowflake", "SELECT * FROM other_db.public.other_table"),
        ("bigquery", "SELECT * FROM `project.dataset.other`"),
    ],
)
@pytest.mark.parametrize("row_filters", [None, _EU_ONLY], ids=["plain-expose", "reader"])
def test_query_sql_reads_no_other_relation_for_any_caller(dialect, sql, row_filters):
    with pytest.raises(query_compiler.QueryValidationError):
        query_compiler.compile_free_form_sql(
            sql=sql,
            table_reference="customers",
            limit=10,
            expose=_expose(row_filters=row_filters),
            caller_attributes={"principal": READER},
            dialect=dialect,
        )


def test_a_reader_keeps_every_function():
    """The function list and the condition fence protect withheld rows, and a reader
    has none, so they do not apply to it."""
    compiled = query_compiler.compile_free_form_sql(
        sql="SELECT hash(region) AS h FROM customers WHERE CAST(id AS VARCHAR) <> 'x'",
        table_reference="customers",
        limit=10,
        expose=_expose(row_filters=_EU_ONLY),
        caller_attributes={"principal": READER},
        dialect="duckdb",
    )
    assert compiled.row_filter is None and "hash(region)" in compiled.sql


# ---------------------------------------------------------------------
# Refusals and rendering
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "expose",
    [_expose(schema=[]), {"exposeId": "customers"}],
    ids=["empty-schema", "no-contract"],
)
def test_an_expose_that_declares_no_column_is_refused(state_for, expose):
    state = state_for(expose)
    with pytest.raises(query_compiler.QueryValidationError, match="declares no column"):
        tool_sample(state, {"limit": 10})
    with pytest.raises(query_compiler.QueryValidationError, match="declares no column"):
        tool_query_sql(state, {"sql": "SELECT id FROM customers"}, caller_attributes={})


def test_a_declared_name_that_is_not_a_plain_identifier_is_refused_by_name():
    expose = _expose(schema=[{"name": "id"}, {"name": "Order Date"}])
    with pytest.raises(query_compiler.QueryValidationError, match="'Order Date'"):
        query_compiler.readable_columns(expose)


@pytest.mark.parametrize(
    ("dialect", "rendered"),
    [
        # Folded as the engine folds a bare name, then quoted.
        ("postgres", '"region", "user"'),
        ("snowflake", '"REGION", "USER"'),
        # Case-insensitive engines keep the declared case.
        ("duckdb", '"Region", "user"'),
        ("trino", '"Region", "user"'),
        ("bigquery", "`Region`, `user`"),
        ("databricks", "`Region`, `user`"),
        # An unknown engine gets the bare names.
        (None, "Region, user"),
    ],
)
def test_the_projection_names_each_column_as_its_bare_name_would(dialect, rendered):
    assert query_compiler.projection_columns(["Region", "user"], dialect) == rendered


def test_query_still_reads_through_the_contracts_semantics():
    """A ``query`` caller names only measures, metrics and dimensions the contract
    declares, so ``query`` is not projected: an expose with no ``contract.schema`` and a
    metric filter on another column compiles, as Command Center's governed lane needs."""
    expose = {
        "exposeId": "subs",
        "semantics": {
            "measures": [{"name": "mrr", "agg": "sum", "expr": "mrr_usd"}],
            "dimensions": [{"name": "plan_tier", "expr": "plan_tier"}],
            "metrics": [
                {
                    "name": "active_mrr",
                    "type": "simple",
                    "measure": "mrr",
                    "filter": "status = 'active'",
                }
            ],
        },
    }
    compiled = query_compiler.compile_semantic_query(
        expose=expose,
        metric="active_mrr",
        dimensions=["plan_tier"],
        limit=100,
        table_reference="`fluid-test.gold.subscription_summary`",
        dialect="bigquery",
    )
    sql = compiled.render_sql_for_dialect("bigquery")
    assert "FROM `fluid-test.gold.subscription_summary`" in sql
    assert "status = 'active'" in sql and "(SELECT" not in sql
