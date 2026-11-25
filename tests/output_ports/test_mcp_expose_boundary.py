# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The expose is the boundary of every MCP read, for every caller.

``sample`` and ``query_sql`` read only the columns ``contract.schema`` declares, and
``query_sql`` reads only the exposed table (with ``VALUES``, ``UNNEST`` and, on DuckDB,
a FROM-first CTE of its own) and calls only listed functions. That holds on every
expose: one with no row filter or column restriction, and for a reader no row filter
narrows. ``query`` reads what the
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
    from ._fixtures import duckdb_csv_session

    csv = tmp_path / "customers.csv"
    csv.write_text(
        ",".join(_HEADER) + "\n" + "".join(",".join(map(str, row)) + "\n" for row in _ROWS),
        encoding="utf-8",
    )
    return lambda expose: duckdb_csv_session(csv, expose, table="customers")


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


def test_sample_keeps_a_declared_column_as_the_engine_names_it(state_for):
    """``sample`` reads ``SELECT *`` and keeps the declared columns by name without
    regard to case, so a column an engine returns in another case (Snowflake's upper
    case, a quoted mixed-case Postgres column) is still returned, under the engine's
    name, and a column no one declared is dropped before the rows leave the driver."""
    state = state_for(_expose(schema=[{"name": "ID"}, {"name": "Current_User"}]))
    driver = state.get_driver()
    execute = driver.execute

    def upper_case(**kwargs: Any) -> Any:
        result = execute(**kwargs)
        columns = tuple(str(column).upper() for column in result.columns)
        rows = tuple({key.upper(): value for key, value in row.items()} for row in result.rows)
        return SimpleNamespace(columns=columns, rows=rows)

    driver.execute = upper_case
    payload = tool_sample(state, {"limit": 10})
    assert payload["columns"] == ["ID", "CURRENT_USER"]
    assert sorted(row["CURRENT_USER"] for row in payload["rows"]) == ["alice", "bob", "carol"]


def test_sample_keeps_a_declared_column_named_like_a_keyword(tmp_path):
    """``sample`` names no column in its statement, so a declared column named like a
    keyword the drivers' statement guard refuses in a body reads as any other."""
    from ._fixtures import duckdb_csv_session

    csv = tmp_path / "events.csv"
    csv.write_text("id,update,union\n1,a,b\n", encoding="utf-8")
    expose = _expose(schema=[{"name": "id"}, {"name": "update"}, {"name": "union"}])
    payload = tool_sample(duckdb_csv_session(csv, expose, table="events"), {"limit": 10})
    assert payload["columns"] == ["id", "update", "union"]
    assert payload["rows"] == [{"id": 1, "update": "a", "union": "b"}]


@pytest.mark.parametrize(
    ("dialect", "columns", "declared", "kept"),
    [
        # Postgres and Snowflake can hold two columns whose names differ only in case;
        # the declared name keeps the column the engine reads for it.
        ("postgres", ["id", "notes", "NOTES"], ["id", "notes"], ["id", "notes"]),
        ("snowflake", ["ID", "NOTES", "notes"], ["id", "notes"], ["ID", "NOTES"]),
        # A quoted mixed-case column, the only one of its name, is still read.
        ("postgres", ["id", "UserId"], ["id", "UserId"], ["id", "UserId"]),
        ("duckdb", ["ID", "Region"], ["id", "region"], ["ID", "Region"]),
    ],
)
def test_sample_keeps_the_column_the_engine_reads_for_each_declared_name(
    dialect, columns, declared, kept
):
    expose = _expose(schema=[{"name": name} for name in declared])
    assert query_compiler.declared_result_columns(expose, columns, dialect) == kept


def test_sample_refuses_when_no_column_is_the_declared_names_own():
    expose = _expose(schema=[{"name": "id"}, {"name": "notes"}])
    with pytest.raises(query_compiler.QueryValidationError, match="several columns named"):
        query_compiler.declared_result_columns(expose, ["id", "Notes", "NOTES"], "postgres")


def test_sample_does_not_return_an_undeclared_column_named_like_a_declared_one(state_for):
    """The declared ``order`` keeps its own column; an undeclared ``ORDER`` the engine
    also returns is dropped, and does not take the declared column's place."""
    state = state_for(_expose(schema=[{"name": "id"}, {"name": "order"}]))
    driver = state.get_driver()
    execute = driver.execute

    def with_a_case_twin(**kwargs: Any) -> Any:
        result = execute(**kwargs)
        rows = tuple({**row, "ORDER": "UNDECLARED"} for row in result.rows)
        return SimpleNamespace(columns=(*result.columns, "ORDER"), rows=rows)

    driver.execute = with_a_case_twin
    payload = tool_sample(state, {"limit": 10})
    assert payload["columns"] == ["id", "order"]
    assert "UNDECLARED" not in repr(payload)
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


def test_a_reader_keeps_the_conditions_but_not_the_unlisted_functions():
    """The condition fence protects withheld rows, and a reader has none, so a reader's
    conditions are not limited. The function list holds for every caller: an engine
    function can read past the projection by naming another relation or the server's
    state in its argument."""
    compiled = query_compiler.compile_free_form_sql(
        sql="SELECT id FROM customers WHERE CAST(id AS VARCHAR) <> 'x'",
        table_reference="customers",
        limit=10,
        expose=_expose(row_filters=_EU_ONLY),
        caller_attributes={"principal": READER},
        dialect="duckdb",
    )
    assert compiled.row_filter is None and "CAST(id AS VARCHAR)" in compiled.sql
    with pytest.raises(query_compiler.QueryValidationError, match="calls hash, which"):
        query_compiler.compile_free_form_sql(
            sql="SELECT hash(region) AS h FROM customers",
            table_reference="customers",
            limit=10,
            expose=_expose(row_filters=_EU_ONLY),
            caller_attributes={"principal": READER},
            dialect="duckdb",
        )


@pytest.mark.parametrize(
    ("dialect", "call"),
    [
        ("postgres", "pg_show_all_settings()"),
        ("postgres", "pg_get_viewdef('customers')"),
        ("snowflake", "GET_DDL('table', 'customers')"),
        ("duckdb", "my_macro(region)"),
    ],
)
@pytest.mark.parametrize("filtered", [False, True], ids=["plain-expose", "reader"])
def test_every_caller_calls_only_listed_functions(dialect, call, filtered):
    """Each of these reads something the projection does not hold: the server's
    settings, a view's definition, an object's DDL, or whatever a user-defined function
    reads."""
    name = call.split("(")[0]
    with pytest.raises(query_compiler.QueryValidationError, match=f"calls {name}, which"):
        query_compiler.compile_free_form_sql(
            sql=f"SELECT {call} AS v FROM customers",
            table_reference="customers",
            limit=10,
            expose=_expose(row_filters=_EU_ONLY if filtered else None),
            caller_attributes={"principal": READER},
            dialect=dialect,
        )


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
    # A restricted column is never read, so its name is not checked.
    assert query_compiler.readable_columns(expose, restricted=["order date"]) == ["id"]
    # sample names no column, so it reads one with such a name.
    assert query_compiler.declared_columns(expose) == ["id", "Order Date"]


@pytest.mark.parametrize(
    ("dialect", "reference", "sql", "alias"),
    [
        ("postgres", "public.order", "SELECT id FROM public.order", '"order"'),
        ("postgres", 'public."Order"', 'SELECT id FROM public."Order"', '"Order"'),
        ("postgres", "public.Customers", "SELECT id FROM public.Customers", '"customers"'),
        ("snowflake", "PUBLIC.CUSTOMERS", "SELECT id FROM PUBLIC.customers", '"CUSTOMERS"'),
        ("bigquery", "ds.order", "SELECT id FROM ds.`order`", "`order`"),
    ],
)
def test_the_projection_takes_the_callers_name_for_the_table_quoted(dialect, reference, sql, alias):
    """A read without an alias gets the caller's own name for the table, folded the way
    the engine folds an unquoted name and quoted, so a table named like a reserved
    word still reads and the statement's references to it still resolve."""
    compiled = query_compiler.compile_free_form_sql(
        sql=sql,
        table_reference=reference,
        limit=10,
        expose=_expose(schema=[{"name": "id"}]),
        dialect=dialect,
    )
    assert f") AS {alias}\nLIMIT 10" in compiled.sql, compiled.sql


@pytest.mark.parametrize(
    ("dialect", "reference", "sql", "refused"),
    [
        ("postgres", "public.customers", 'SELECT id FROM "Customers"', True),
        ("postgres", "public.customers", 'SELECT id FROM "customers"', False),
        ("postgres", "public.customers", "SELECT id FROM Customers", False),
        ("snowflake", "PUBLIC.CUSTOMERS", 'SELECT id FROM "customers"', True),
        ("snowflake", "PUBLIC.CUSTOMERS", 'SELECT id FROM "CUSTOMERS"', False),
        ("snowflake", "PUBLIC.CUSTOMERS", "SELECT id FROM customers", False),
    ],
)
def test_a_quoted_name_the_engine_reads_as_another_table_is_refused(
    dialect, reference, sql, refused
):
    """Postgres and Snowflake keep the case of a quoted name, so ``"Customers"`` is
    another table there than ``customers``: serving the exposed table's rows under it
    would label them as that table's."""

    def compile_it() -> Any:
        return query_compiler.compile_free_form_sql(
            sql=sql,
            table_reference=reference,
            limit=10,
            expose=_expose(schema=[{"name": "id"}]),
            dialect=dialect,
        )

    if refused:
        with pytest.raises(query_compiler.QueryValidationError, match="another table"):
            compile_it()
    else:
        compile_it()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'pg_authid'::regclass AS r FROM customers",
        "SELECT CAST('lower' AS regproc) AS r FROM customers",
        "SELECT id::regrole AS r FROM customers",
    ],
)
def test_a_postgres_cast_that_looks_a_name_up_is_refused(sql):
    with pytest.raises(query_compiler.QueryValidationError, match="looks a name up"):
        query_compiler.compile_free_form_sql(
            sql=sql,
            table_reference="customers",
            limit=10,
            expose=_expose(),
            dialect="postgres",
        )


@pytest.mark.parametrize(
    ("dialect", "call"),
    [("duckdb", "string_split(region, ',')"), ("postgres", "string_to_array(region, ',')")],
)
def test_splitting_a_string_is_a_listed_function(dialect, call):
    query_compiler.compile_free_form_sql(
        sql=f"SELECT {call} AS parts FROM customers",
        table_reference="customers",
        limit=10,
        expose=_expose(),
        dialect=dialect,
    )


def test_a_table_named_like_a_reserved_word_reads_end_to_end():
    con = duckdb.connect()
    con.execute('CREATE TABLE "order" AS SELECT * FROM (VALUES (1, 7), (2, 8)) AS v(id, n)')
    compiled = query_compiler.compile_free_form_sql(
        sql='SELECT sum(n) AS total FROM "order"',
        table_reference='"order"',
        limit=10,
        expose=_expose(schema=[{"name": "id"}, {"name": "n"}]),
        dialect="duckdb",
    )
    assert con.execute(compiled.sql).fetchall() == [(15,)]


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
    read = query_compiler._SQLGLOT_DIALECTS.get(dialect or "")
    names = ", ".join(query_compiler._projected_name(name, read) for name in ["Region", "user"])
    assert names == rendered


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
