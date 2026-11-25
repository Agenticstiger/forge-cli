# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Column restrictions on the governed ``query`` path hold for every spelling.

A contract's dimension / measure / metric-filter expressions are free SQL. The
compiler used to enforce ``columnRestrictions`` by scanning those expressions for
the restricted NAME, which a spelling that names no column (a quoted identifier
read as a string literal, a column selector, the table's own name used as a row
value) walked straight past. The compiler now evaluates every expression over a
projection that holds only the allowed columns, so a restricted column does not
exist for them.

These tests run the compiled statement on an in-memory DuckDB and assert on what
the ENGINE returns, so a spelling the scan does not know about still has to fail.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.output_ports.mcp.query_compiler import (  # noqa: E402
    QueryValidationError,
    compile_semantic_query,
)

LEAK_MARKER = "DENIED-"

#: A principal ``policy.authz.rowFilters`` limits to tenant t1's rows, and the caller
#: attributes of a request whose verified identity is that principal.
T1 = "group:tenant-t1@northwind.example"
AS_T1 = {"principal": T1}
_T1_ROWS = [{"principal": T1, "where": "tenant_id = 't1'"}]


@pytest.fixture()
def con():
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE customers AS SELECT * FROM (VALUES "
        "(1, 'DENIED-EU', 'retail', 10, 't1'), "
        "(2, 'DENIED-US', 'retail', 20, 't1'), "
        "(3, 'DENIED-EU', 'wholesale', 30, 't2')) "
        "AS t(id, region, segment, amount, tenant_id)"
    )
    yield connection
    connection.close()


_SCHEMA = [
    {"name": "id"},
    {"name": "region"},
    {"name": "segment"},
    {"name": "amount"},
    {"name": "tenant_id"},
]


def _expose(
    *,
    dimension_expr: Optional[str] = None,
    measure_expr: str = "amount",
    agg: str = "sum",
    metric_filter: Optional[str] = None,
    row_filters: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    semantics: Dict[str, Any] = {
        "measures": [{"name": "m", "agg": agg, "expr": measure_expr}],
        "dimensions": [{"name": "segment", "type": "categorical", "expr": "segment"}],
        "metrics": [
            {
                "name": "mt",
                "type": "simple",
                "measure": "m",
                **({"filter": metric_filter} if metric_filter else {}),
            }
        ],
    }
    if dimension_expr is not None:
        semantics["dimensions"].append({"name": "d", "type": "categorical", "expr": dimension_expr})
    expose: Dict[str, Any] = {
        "exposeId": "customers",
        "contract": {"schema": list(_SCHEMA)},
        "semantics": semantics,
    }
    if row_filters:
        expose["policy"] = {"authz": {"rowFilters": row_filters}}
    return expose


def _run(
    connection,
    expose: Dict[str, Any],
    *,
    restricted: Sequence[str] = ("region",),
    use_metric: bool = False,
    dimensions: Optional[List[str]] = None,
    filters: Optional[Dict[str, Any]] = None,
    caller_attributes: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Any]:
    """Compile and run; return ``(kind, detail)`` with kind one of
    ``refused`` / ``engine_error`` / ``rows``."""
    kwargs: Dict[str, Any] = {"metric": "mt"} if use_metric else {"measure": "m"}
    try:
        compiled = compile_semantic_query(
            expose=expose,
            dimensions=dimensions,
            filters=filters,
            limit=50,
            table_reference="customers",
            dialect="duckdb",
            restricted_columns=restricted,
            caller_attributes=caller_attributes or {},
            **kwargs,
        )
    except QueryValidationError as exc:
        return "refused", str(exc)
    sql = compiled.render_sql_for_dialect("duckdb")
    bound = {f"p_{i}": v for i, v in enumerate(compiled.params)}
    try:
        rows = (
            connection.execute(sql, bound).fetchall()
            if bound
            else connection.execute(sql).fetchall()
        )
    except duckdb.Error as exc:
        return "engine_error", str(exc)
    return "rows", rows


def _leaked(rows: Any) -> bool:
    return LEAK_MARKER in repr(rows)


# Every one of these reads ``region`` while naming it in a way the old name
# scan did not recognise (or not at all). None may produce a result.
_HOSTILE_DIMENSIONS = [
    pytest.param('"region"', id="quoted-name"),
    pytest.param('"REGION"', id="quoted-name-upper"),
    pytest.param('"Region"', id="quoted-name-mixed-case"),
    pytest.param('upper("region")', id="quoted-name-in-function"),
    pytest.param('CASE WHEN id > 0 THEN "region" ELSE NULL END', id="quoted-name-in-case"),
    pytest.param("coalesce(\"region\", 'x')", id="quoted-name-in-coalesce"),
    pytest.param("to_json(customers)", id="table-name-as-row"),
    pytest.param("customers", id="bare-table-name-as-row"),
    pytest.param("EXISTS(FROM customers WHERE \"region\" = 'DENIED-EU')", id="exists-subquery"),
    pytest.param("(FROM customers WHERE id = 1)", id="from-first-subquery"),
    pytest.param("id IN (FROM customers)", id="in-subquery"),
]


@pytest.mark.parametrize("dimension_expr", _HOSTILE_DIMENSIONS)
def test_dimension_cannot_read_a_restricted_column(con, dimension_expr):
    kind, detail = _run(
        con, _expose(dimension_expr=dimension_expr), dimensions=["d"], use_metric=False
    )
    assert kind in ("refused", "engine_error"), f"evaluated: {detail!r}"


_HOSTILE_MEASURES = [
    pytest.param("COLUMNS('reg.*')", id="columns-regex"),
    pytest.param("COLUMNS('REG.*')", id="columns-regex-upper"),
    pytest.param("to_json(customers)", id="table-name-as-row"),
    pytest.param("to_json(COLUMNS(*))", id="columns-star-in-function"),
    pytest.param("concat_ws(',', *COLUMNS(*))", id="unpacked-columns-star"),
    pytest.param("COLUMNS(*)", id="columns-star"),
    pytest.param("to_json(COLUMNS(customers.*))", id="columns-qualified-star"),
    pytest.param("concat_ws(',', *COLUMNS(customers.*))", id="unpacked-qualified-star"),
    pytest.param("COLUMNS(lambda c: c = 'region')", id="columns-lambda"),
    pytest.param('"region"', id="quoted-name"),
    pytest.param('CASE WHEN id > 0 THEN "region" END', id="quoted-name-in-case"),
]


@pytest.mark.parametrize("measure_expr", _HOSTILE_MEASURES)
def test_measure_cannot_read_a_restricted_column(con, measure_expr):
    kind, detail = _run(con, _expose(measure_expr=measure_expr, agg="max"))
    # A selector that matches nothing readable may legitimately return the
    # allowed columns; what it may never do is return the restricted one.
    assert kind in ("refused", "engine_error") or not _leaked(detail), f"leaked: {detail!r}"


@pytest.mark.parametrize(
    "measure_expr",
    [
        pytest.param("COLUMNS('reg.*')", id="columns-regex"),
        pytest.param("to_json(customers)", id="table-name-as-row"),
        pytest.param('"region"', id="quoted-name"),
    ],
)
def test_measure_that_only_names_the_restricted_column_is_not_evaluated(con, measure_expr):
    kind, detail = _run(con, _expose(measure_expr=measure_expr, agg="max"))
    assert kind in ("refused", "engine_error"), f"evaluated: {detail!r}"


def test_metric_filter_cannot_read_a_restricted_column_through_a_quoted_name(con):
    kind, detail = _run(con, _expose(metric_filter="\"region\" = 'DENIED-EU'"), use_metric=True)
    assert kind in ("refused", "engine_error"), f"evaluated: {detail!r}"


def test_metric_filter_cannot_use_a_column_selector(con):
    kind, detail = _run(
        con, _expose(metric_filter="COLUMNS('reg.*') = 'DENIED-EU'"), use_metric=True
    )
    assert kind in ("refused", "engine_error"), f"evaluated: {detail!r}"


def test_a_filter_key_whose_dimension_reads_a_restricted_column_is_not_evaluated(con):
    expose = _expose(dimension_expr='"region"')
    kind, detail = _run(con, expose, filters={"d": "DENIED-EU"})
    assert kind in ("refused", "engine_error"), f"evaluated: {detail!r}"


# ---------------------------------------------------------------------
# What must keep working
# ---------------------------------------------------------------------


def test_allowed_columns_still_aggregate_and_group(con):
    kind, rows = _run(con, _expose(), dimensions=["segment"], use_metric=True)
    assert kind == "rows"
    assert {segment: float(total) for segment, total in rows} == {"retail": 30.0, "wholesale": 30.0}


def test_count_star_still_works_with_a_restricted_column(con):
    """A bare ``*`` over the projection counts rows; it cannot name ``region``."""
    kind, rows = _run(con, _expose(measure_expr="*", agg="count"), dimensions=["segment"])
    assert kind == "rows", rows
    assert {segment: n for segment, n in rows} == {"retail": 2, "wholesale": 1}


def test_a_standard_from_form_inside_a_function_is_not_mistaken_for_a_subquery(con):
    expose = _expose(dimension_expr="EXTRACT(year FROM DATE '2024-05-06')")
    kind, rows = _run(con, expose, dimensions=["d"])
    assert kind == "rows", rows


def test_row_filter_on_a_restricted_column_still_isolates_rows(con):
    """The filter column is policy, not caller input: it may be a column the
    agent is denied, and it must still narrow the rows."""
    expose = _expose(row_filters=_T1_ROWS)
    kind, rows = _run(
        con,
        expose,
        restricted=("region", "tenant_id"),
        dimensions=["segment"],
        caller_attributes=AS_T1,
    )
    assert kind == "rows", rows
    assert {segment: float(total) for segment, total in rows} == {"retail": 30.0}


def test_row_filter_is_not_bypassed_by_a_subquery_over_the_base_table(con):
    expose = _expose(
        dimension_expr="EXISTS(FROM customers WHERE tenant_id = 't2')",
        row_filters=_T1_ROWS,
    )
    kind, detail = _run(con, expose, dimensions=["d"], caller_attributes=AS_T1)
    assert kind in ("refused", "engine_error"), f"evaluated: {detail!r}"


# ---------------------------------------------------------------------
# The projection is the boundary, not the name scan
# ---------------------------------------------------------------------


def test_the_projection_holds_even_when_the_name_scan_misses(con, monkeypatch):
    """Switch the friendly scan off: a restricted column spelled any way at all must
    still fail, because it is not in the relation the expression reads."""
    from fluid_build.output_ports.mcp import query_compiler

    monkeypatch.setattr(query_compiler, "_reject_restricted_reference", lambda *a, **k: None)
    for expression in ('"region"', 'upper("REGION")', "region", "to_json(customers)"):
        kind, detail = _run(con, _expose(dimension_expr=expression), dimensions=["d"])
        assert kind == "engine_error", f"{expression!r} evaluated: {detail!r}"
    kind, detail = _run(con, _expose(measure_expr="COLUMNS('reg.*')", agg="max"))
    assert kind == "engine_error", f"evaluated: {detail!r}"


def test_an_unrestricted_expose_keeps_the_plain_statement_shape(con):
    compiled = compile_semantic_query(
        expose=_expose(),
        measure="m",
        dimensions=["segment"],
        limit=5,
        table_reference="customers",
        dialect="duckdb",
        restricted_columns=(),
    )
    assert "\nFROM customers\n" in compiled.sql
    assert "SELECT" not in compiled.sql.split("FROM", 1)[1]


def test_restricted_expose_reads_a_projection_of_the_allowed_columns_only():
    compiled = compile_semantic_query(
        expose=_expose(),
        measure="m",
        dimensions=["segment"],
        limit=5,
        table_reference="customers",
        dialect="duckdb",
        restricted_columns=("Region",),
    )
    assert "FROM (SELECT id, segment, amount, tenant_id FROM customers) AS _fluid_allowed" in (
        compiled.sql
    )


def test_a_restricted_expose_with_nothing_readable_declared_fails_closed():
    expose = _expose()
    expose["contract"]["schema"] = [{"name": "region"}]
    with pytest.raises(QueryValidationError, match="no readable column"):
        compile_semantic_query(
            expose=expose,
            measure="m",
            limit=5,
            table_reference="customers",
            restricted_columns=("region",),
        )
    expose["contract"]["schema"] = []
    with pytest.raises(QueryValidationError, match="no readable column"):
        compile_semantic_query(
            expose=expose,
            measure="m",
            limit=5,
            table_reference="customers",
            restricted_columns=("region",),
        )


def test_row_filter_sits_inside_the_projection_beside_bound_parameters():
    expose = _expose(row_filters=_T1_ROWS)
    compiled = compile_semantic_query(
        expose=expose,
        measure="m",
        dimensions=["segment"],
        filters={"segment": "retail"},
        limit=5,
        table_reference="customers",
        dialect="duckdb",
        restricted_columns=("region", "tenant_id"),
        caller_attributes=AS_T1,
    )
    assert (
        "FROM (SELECT id, segment, amount FROM customers AS _fluid_rows "
        "WHERE ((_fluid_rows.tenant_id = 't1'))) AS _fluid_allowed"
    ) in compiled.sql
    assert "WHERE ((segment) = :p_0)\n" in compiled.sql
    assert compiled.params == ["retail"]


# ---------------------------------------------------------------------
# Every supported dialect renders and parses
# ---------------------------------------------------------------------

sqlglot = pytest.importorskip("sqlglot")

_DIALECTS = [
    ("duckdb", "duckdb", "customers"),
    ("postgres", "postgres", "db.public.customers"),
    ("snowflake", "snowflake", "DB.PUBLIC.CUSTOMERS"),
    ("bigquery", "bigquery", "`proj-1.ds.customers`"),
    ("athena", "trino", '"db"."customers"'),
]


@pytest.mark.parametrize("dialect,read,table", _DIALECTS, ids=[d[0] for d in _DIALECTS])
def test_projection_parses_in_every_dialect(dialect, read, table):
    from sqlglot import exp

    expose = _expose(
        measure_expr="amount * 2",
        row_filters=[{"principal": T1, "where": "tenant_id IN ('t1', 't2')"}],
    )
    compiled = compile_semantic_query(
        expose=expose,
        measure="m",
        dimensions=["segment"],
        filters={"segment": "retail"},
        limit=5,
        table_reference=table,
        dialect=dialect,
        restricted_columns=("region", "tenant_id"),
        caller_attributes=AS_T1,
    )
    for rendered in (compiled.sql, compiled.render_sql_for_dialect(dialect)):
        # sqlglot reads ``%`` as modulo, so Snowflake's DB-API ``%(p_N)s`` is swapped
        # for a neutral placeholder; the statement shape is what is under test.
        rendered = re.sub(r"%\(p_\d+\)s", "?", rendered)
        tree = sqlglot.parse_one(rendered, read=read)
        outer_from = tree.args["from_"] if "from_" in tree.args else tree.args["from"]
        subquery = outer_from.this
        assert isinstance(subquery, exp.Subquery)
        assert subquery.alias == "_fluid_allowed"
        inner = subquery.this
        names = [column.name for column in inner.expressions]
        assert names == ["id", "segment", "amount"], names
        assert "region" not in {c.name.lower() for c in tree.find_all(exp.Column)}
        # The only table anywhere in the statement is the base table, once.
        assert len(list(tree.find_all(exp.Table))) == 1
        assert inner.find(exp.Where) is not None


# ---------------------------------------------------------------------
# The early scan reads quotes the way the engine does
# ---------------------------------------------------------------------


@pytest.mark.parametrize("dialect", [None, "duckdb", "postgres", "snowflake", "athena"])
def test_double_quotes_are_an_identifier_outside_bigquery(dialect):
    from fluid_build.output_ports.mcp.query_compiler import _strip_string_literals

    assert '"region"' in _strip_string_literals("""upper("region") = 'region'""", dialect)
    assert "'region'" not in _strip_string_literals("""upper("region") = 'region'""", dialect)


@pytest.mark.parametrize("dialect", ["bigquery", "spark", "databricks"])
def test_double_quotes_are_a_string_where_the_dialect_says_so(dialect):
    from fluid_build.output_ports.mcp.query_compiler import _strip_string_literals

    assert "region" not in _strip_string_literals('label = "region"', dialect)


def test_an_apostrophe_inside_a_quoted_identifier_does_not_open_a_literal():
    from fluid_build.output_ports.mcp.query_compiler import _strip_string_literals

    scanned = _strip_string_literals("""coalesce("a'b", region, 'c')""", "duckdb")
    assert "region" in scanned


def test_free_form_sql_rejects_a_quoted_restricted_column():
    from fluid_build.output_ports.mcp.query_compiler import compile_free_form_sql

    with pytest.raises(QueryValidationError, match="region"):
        compile_free_form_sql(
            sql='SELECT "region" AS x FROM customers',
            table_reference="customers",
            limit=5,
            restricted_columns=("region",),
            expose=_expose(),
            dialect="duckdb",
        )
    # BigQuery reads "region" as a string literal, so it is not a reference there.
    compile_free_form_sql(
        sql='SELECT "region" AS x FROM customers',
        table_reference="customers",
        limit=5,
        restricted_columns=("region",),
        expose=_expose(),
        dialect="bigquery",
    )


def test_free_form_default_dialect_is_the_strict_reading():
    from fluid_build.output_ports.mcp.query_compiler import compile_free_form_sql

    with pytest.raises(QueryValidationError, match="region"):
        compile_free_form_sql(
            sql='SELECT "region" AS x FROM customers',
            table_reference="customers",
            limit=5,
            restricted_columns=("region",),
        )


# ---------------------------------------------------------------------
# The driver guard admits the compiler's projection and nothing else
# ---------------------------------------------------------------------


def test_driver_guard_admits_the_compiled_projection_and_still_refuses_other_selects():
    from fluid_build.output_ports.mcp.drivers.base import guard_against_injection_markers

    compiled = compile_semantic_query(
        expose=_expose(row_filters=_T1_ROWS),
        measure="m",
        dimensions=["segment"],
        limit=5,
        table_reference="customers",
        restricted_columns=("region",),
        caller_attributes=AS_T1,
    )
    guard_against_injection_markers(compiled.sql)
    for hostile in (
        compiled.sql.replace("FROM customers", "FROM customers UNION SELECT 1 FROM customers"),
        compiled.sql.replace("(SELECT id", "(SELECT (SELECT region FROM customers) AS id"),
        "SELECT x FROM (SELECT * FROM customers) AS t",
        "SELECT x FROM (SELECT region + 1 FROM customers) AS t",
        # The guard knows the compiler's own shape (bare columns), not which table it
        # reads: a caller cannot write a SELECT at all, because the allowlist refuses it
        # in caller and contract text, so any other SELECT is a regression and is refused.
        "SELECT x FROM customers WHERE id IN (SELECT id + 1 FROM other)",
        compiled.sql.replace(
            "AS _fluid_allowed", "AS _fluid_allowed JOIN (SELECT region + 1 FROM t) j"
        ),
    ):
        with pytest.raises(ValueError):
            guard_against_injection_markers(hostile)


def test_end_to_end_through_the_driver_on_a_restricted_expose(tmp_path):
    from types import SimpleNamespace

    from fluid_build.output_ports.mcp._handlers import tool_query
    from fluid_build.output_ports.mcp.drivers import build_driver

    csv = tmp_path / "customers.csv"
    csv.write_text(
        "id,region,segment,amount,tenant_id\n"
        "1,DENIED-EU,retail,10,t1\n"
        "2,DENIED-US,retail,20,t1\n"
        "3,DENIED-EU,wholesale,30,t2\n"
    )
    expose = _expose(dimension_expr='"region"', row_filters=_T1_ROWS)
    expose["exposeId"] = "customers"
    expose["binding"] = {
        "platform": "local",
        "format": "csv",
        "location": {"path": str(csv), "table": "customers"},
    }
    expose["policy"]["authz"]["columnRestrictions"] = [
        {"principal": "*", "columns": ["region", "tenant_id"], "access": "deny"}
    ]
    driver = build_driver(expose=expose, contract={"exposes": [expose]})
    state = SimpleNamespace(
        expose=expose,
        caller_attributes=AS_T1,
        policy=SimpleNamespace(max_sample_rows=100),
        query_timeout_seconds=None,
        get_driver=lambda: driver,
    )
    payload = tool_query(state, {"metric": "mt", "dimensions": ["segment"]})
    assert {row["segment"]: float(row["mt"]) for row in payload["rows"]} == {"retail": 30.0}
    with pytest.raises(QueryValidationError):
        tool_query(state, {"metric": "mt", "dimensions": ["d"]})


# ---------------------------------------------------------------------
# Free-form ``query_sql``: the same boundary, on the caller's own SQL
# ---------------------------------------------------------------------

from fluid_build.output_ports.mcp.query_compiler import compile_free_form_sql  # noqa: E402


def _free_form(connection, sql: str, *, restricted: Sequence[str] = ("region",)):
    """Compile caller SQL and run it. Returns ``(kind, detail)`` where ``detail`` is
    ``(column_names, rows)`` for ``rows``."""
    try:
        compiled = compile_free_form_sql(
            sql=sql,
            table_reference="customers",
            limit=50,
            restricted_columns=restricted,
            expose=_expose(),
            dialect="duckdb",
        )
    except QueryValidationError as exc:
        return "refused", str(exc)
    try:
        cursor = connection.execute(compiled.sql)
        rows = cursor.fetchall()
        names = [col[0] for col in cursor.description]
    except duckdb.Error as exc:
        return "engine_error", str(exc)
    return "rows", (names, rows)


_HOSTILE_FREE_FORM = [
    pytest.param('SELECT "region" AS x FROM customers', id="quoted-name"),
    pytest.param('SELECT "REGION" FROM customers', id="quoted-name-upper"),
    pytest.param("SELECT max(COLUMNS('reg.*')) AS x FROM customers", id="columns-regex"),
    pytest.param("SELECT approx_top_k(COLUMNS('reg'), 5) AS x FROM customers", id="columns-top-k"),
    pytest.param(
        "SELECT id FROM customers WHERE EXISTS(FROM customers c2 WHERE c2.\"region\" = 'DENIED-EU')",
        id="exists-over-quoted-name",
    ),
]


_WHOLE_ROW_FREE_FORM = [
    pytest.param("SELECT to_json(c) AS x FROM customers c", id="alias-as-row"),
    pytest.param("SELECT to_json(customers) AS x FROM customers", id="table-as-row"),
    pytest.param(
        "SELECT a.id, to_json(b) AS x FROM customers a JOIN customers b ON a.id = b.id",
        id="self-join",
    ),
    pytest.param("SELECT to_json(t) AS x FROM (FROM customers) t", id="from-first-subquery"),
]


@pytest.mark.parametrize("sql", _WHOLE_ROW_FREE_FORM)
def test_free_form_whole_row_value_holds_only_the_allowed_columns(con, sql):
    kind, detail = _free_form(con, sql)
    assert kind != "rows" or (
        "region" not in repr(detail).lower() and not _leaked(detail[1])
    ), detail


@pytest.mark.parametrize("sql", _HOSTILE_FREE_FORM)
def test_free_form_sql_cannot_read_a_restricted_column(con, sql):
    kind, detail = _free_form(con, sql)
    assert kind in ("refused", "engine_error"), f"evaluated: {detail!r}"


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT * FROM customers", id="star"),
        pytest.param("SELECT c.* FROM customers c", id="alias-star"),
        pytest.param("SELECT customers.* FROM customers", id="table-star"),
        pytest.param("SELECT max(COLUMNS(*)) FROM customers", id="columns-star"),
        pytest.param("SELECT * FROM main.customers", id="schema-qualified-star"),
    ],
)
def test_free_form_star_selects_only_the_allowed_columns(con, sql):
    kind, detail = _free_form(con, sql)
    assert kind == "rows", detail
    names, rows = detail
    assert "region" not in [n.lower() for n in names], names
    assert not _leaked(rows)


def test_free_form_queries_over_allowed_columns_still_work(con):
    kind, detail = _free_form(
        con,
        "SELECT c.segment, sum(c.amount) AS total FROM customers c "
        "JOIN customers d ON c.id = d.id WHERE d.id > 0 GROUP BY c.segment ORDER BY c.segment",
    )
    assert kind == "rows", detail
    assert detail[1] == [("retail", 30), ("wholesale", 30)]
    kind, detail = _free_form(con, "SELECT count(*) AS n FROM customers")
    assert (kind, detail[1]) == ("rows", [(3,)])
    kind, detail = _free_form(con, "SELECT segment FROM customers WHERE segment = 'region' LIMIT 1")
    assert kind == "rows", detail


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('customers.csv')",
        "SELECT * FROM read_parquet('customers.parquet')",
        "SELECT * FROM 'customers.csv'",
        "SELECT * FROM range(3)",
    ],
)
def test_free_form_sql_refuses_a_table_function_or_a_file_on_a_restricted_expose(con, sql):
    kind, detail = _free_form(con, sql)
    assert kind == "refused", detail


def test_a_cte_body_and_a_shadowing_cte_name_see_the_projection(con):
    """A CTE is not reachable through ``compile_free_form_sql`` (a statement must start
    with SELECT), but the rewrite handles it so that stays true if that ever changes."""
    from fluid_build.output_ports.mcp.query_compiler import _project_exposed_table

    allowed = ["id", "segment", "amount", "tenant_id"]
    # Any relation besides the exposed table and the statement's own CTEs is refused.
    with pytest.raises(QueryValidationError, match="sql reads 'x'"):
        _project_exposed_table(
            "SELECT * FROM x",
            table_reference="customers",
            allowed=allowed,
            dialect="duckdb",
        )
    with_cte = _project_exposed_table(
        "WITH x AS (SELECT * FROM customers) SELECT to_json(x) FROM x",
        table_reference="customers",
        allowed=allowed,
        dialect="duckdb",
    )
    assert "region" not in con.execute(with_cte).fetchall()[0][0]
    with pytest.raises(QueryValidationError, match="CTE named"):
        _project_exposed_table(
            "WITH customers AS (SELECT 1 AS id) SELECT * FROM customers",
            table_reference="customers",
            allowed=allowed,
            dialect="duckdb",
        )


def test_free_form_projection_applies_to_pii_columns_too(con):
    """The handler passes restricted + PII columns; none of them is projected."""
    kind, detail = _free_form(con, "SELECT * FROM customers", restricted=("region", "tenant_id"))
    assert kind == "rows"
    assert [n.lower() for n in detail[0]] == ["id", "segment", "amount"]


def test_free_form_with_no_declared_readable_column_fails_closed():
    expose = _expose()
    expose["contract"]["schema"] = [{"name": "region"}]
    with pytest.raises(QueryValidationError, match="no readable column"):
        compile_free_form_sql(
            sql="SELECT 1 FROM customers",
            table_reference="customers",
            limit=5,
            restricted_columns=("region",),
            expose=expose,
        )


def test_free_form_without_restrictions_keeps_the_callers_statement():
    compiled = compile_free_form_sql(
        sql="SELECT  region FROM customers",
        table_reference="customers",
        limit=5,
        restricted_columns=(),
        expose=_expose(),
    )
    assert compiled.sql == "SELECT  region FROM customers\nLIMIT 5"


@pytest.mark.parametrize("dialect,read,table", _DIALECTS, ids=[d[0] for d in _DIALECTS])
def test_free_form_rewrite_parses_in_every_dialect(dialect, read, table):
    from sqlglot import exp

    compiled = compile_free_form_sql(
        sql=f"SELECT c.segment, SUM(c.amount) AS total FROM {table} c "
        f"JOIN {table} d ON c.id = d.id GROUP BY c.segment",
        table_reference=table,
        limit=5,
        restricted_columns=("region", "tenant_id"),
        expose=_expose(),
        dialect=dialect,
    )
    tree = sqlglot.parse_one(compiled.sql, read=read)
    subqueries = [s for s in tree.find_all(exp.Subquery)]
    assert len(subqueries) == 2
    for sub in subqueries:
        assert [c.name for c in sub.this.expressions] == ["id", "segment", "amount"]
    assert not {c.name.lower() for c in tree.find_all(exp.Column)} & {"region", "tenant_id"}
    assert compiled.sql.rstrip().endswith("LIMIT 5")


def test_driver_guard_admits_the_free_form_projection_in_joins_and_lists():
    from fluid_build.output_ports.mcp.drivers.base import guard_against_injection_markers

    compiled = compile_free_form_sql(
        sql="SELECT a.id FROM customers a JOIN customers b ON a.id = b.id, customers c",
        table_reference="customers",
        limit=5,
        restricted_columns=("region",),
        expose=_expose(),
        dialect="duckdb",
    )
    guard_against_injection_markers(compiled.sql)
    with pytest.raises(ValueError):
        guard_against_injection_markers(compiled.sql + " UNION SELECT 1")


def test_free_form_end_to_end_through_the_driver(tmp_path):
    from types import SimpleNamespace

    from fluid_build.output_ports.mcp._handlers import tool_query_sql
    from fluid_build.output_ports.mcp.drivers import build_driver

    csv = tmp_path / "customers.csv"
    csv.write_text(
        "id,region,segment,amount,tenant_id\n"
        "1,DENIED-EU,retail,10,t1\n"
        "2,DENIED-US,retail,20,t1\n"
    )
    expose = _expose()
    expose["binding"] = {
        "platform": "local",
        "format": "csv",
        "location": {"path": str(csv), "table": "customers"},
    }
    expose["policy"] = {
        "authz": {
            "columnRestrictions": [{"principal": "*", "columns": ["region"], "access": "deny"}]
        }
    }
    driver = build_driver(expose=expose, contract={"exposes": [expose]})
    state = SimpleNamespace(
        expose=expose,
        caller_attributes={},
        policy=SimpleNamespace(max_sample_rows=100),
        query_timeout_seconds=None,
        get_driver=lambda: driver,
    )
    ok = tool_query_sql(
        state, {"sql": "SELECT segment, sum(amount) AS total FROM customers GROUP BY segment"}
    )
    assert ok["rows"] == [{"segment": "retail", "total": 30}]
    star = tool_query_sql(state, {"sql": "SELECT * FROM customers"})
    assert "region" not in star["columns"] and LEAK_MARKER not in repr(star)
    whole_row = tool_query_sql(state, {"sql": "SELECT to_json(c) AS x FROM customers c"})
    assert "region" not in repr(whole_row) and LEAK_MARKER not in repr(whole_row)
    for sql in (
        "SELECT max(COLUMNS('reg.*')) AS x FROM customers",
        'SELECT "region" AS x FROM customers',
        f"SELECT * FROM read_csv('{csv.as_posix()}')",
    ):
        with pytest.raises(Exception) as caught:
            result = tool_query_sql(state, {"sql": sql})
            pytest.fail(f"evaluated: {result!r}")
        assert not isinstance(caught.value, pytest.fail.Exception)


def test_free_form_keeps_the_callers_text_outside_the_table_reference(con):
    """Only the table reference is replaced, so engine functions sqlglot cannot print
    back (approx_top_k on DuckDB) and the caller's own layout still reach the engine."""
    from fluid_build.output_ports.mcp.drivers.base import guard_against_injection_markers

    compiled = compile_free_form_sql(
        sql="select approx_top_k(segment, 2) AS x\nfrom\n  main.customers",
        table_reference="customers",
        limit=5,
        restricted_columns=("region",),
        expose=_expose(),
        dialect="duckdb",
    )
    assert compiled.sql.startswith("select approx_top_k(segment, 2) AS x\nfrom\n  (SELECT id,")
    guard_against_injection_markers(compiled.sql)
    assert con.execute(compiled.sql).fetchall() == [(["retail", "wholesale"],)]


# ---------------------------------------------------------------------
# ``query_sql``: a table reference the rewrite cannot see is refused, not skipped
# ---------------------------------------------------------------------


@pytest.fixture()
def con_s2(con):
    con.execute("CREATE SCHEMA s2")
    con.execute("CREATE TABLE s2.other AS SELECT 1 AS k")
    return con


def _free_form_guarded(connection, sql: str, *, restricted: Sequence[str] = ("region",)):
    """Like ``_free_form``, and runs the driver's statement guard on the compiled SQL
    first, the way every driver does before it executes."""
    from fluid_build.output_ports.mcp.drivers.base import guard_against_injection_markers

    try:
        compiled = compile_free_form_sql(
            sql=sql,
            table_reference="customers",
            limit=50,
            restricted_columns=restricted,
            expose=_expose(),
            dialect="duckdb",
        )
    except QueryValidationError as exc:
        return "refused", str(exc)
    try:
        guard_against_injection_markers(compiled.sql)
    except ValueError as exc:
        return "guard", str(exc)
    try:
        cursor = connection.execute(compiled.sql)
        rows = cursor.fetchall()
        names = [col[0] for col in cursor.description]
    except duckdb.Error as exc:
        return "engine_error", str(exc)
    return "rows", (names, rows)


_TABLE_KEYWORD_FORMS = [
    pytest.param("SELECT to_json(x) AS j FROM (TABLE customers) x", id="alias-as-row"),
    pytest.param("SELECT * FROM s2.other, (TABLE customers)", id="comma-list"),
    pytest.param("SELECT * FROM s2.other JOIN (TABLE customers) ON true", id="join"),
    pytest.param('SELECT * FROM (TABLE "customers") x', id="quoted-name"),
    pytest.param("SELECT * FROM (TABLE main.customers) x", id="schema-qualified"),
    pytest.param("SELECT * FROM (table customers) x", id="lower-case"),
]


@pytest.mark.parametrize("sql", _TABLE_KEYWORD_FORMS)
def test_the_table_keyword_is_refused_on_a_restricted_expose(con_s2, sql):
    kind, detail = _free_form(con_s2, sql)
    assert kind == "refused", detail
    assert "TABLE" in detail
    assert not _leaked(detail)


def test_the_table_keyword_refusal_does_not_depend_on_the_rewrite(monkeypatch):
    """The token check runs before the caller's statement is parsed, so a parser that
    reads the form some other way cannot skip it."""
    import sqlglot as _sqlglot

    statement = "SELECT * FROM s2.other, (TABLE customers)"
    parse, parse_one = _sqlglot.parse, _sqlglot.parse_one

    def _guarded(real):
        def _call(sql, *args, **kwargs):
            # sqlglot parses small snippets of its own while it loads a dialect; only
            # the caller's statement must not reach a parser.
            assert statement not in str(sql), "the statement was parsed before the token check"
            return real(sql, *args, **kwargs)

        return _call

    monkeypatch.setattr(_sqlglot, "parse", _guarded(parse))
    monkeypatch.setattr(_sqlglot, "parse_one", _guarded(parse_one))
    with pytest.raises(QueryValidationError, match="TABLE"):
        compile_free_form_sql(
            sql=statement,
            table_reference="customers",
            limit=5,
            restricted_columns=("region",),
            expose=_expose(),
            dialect="duckdb",
        )


def test_a_quoted_table_identifier_is_not_the_table_keyword():
    compiled = compile_free_form_sql(
        sql='SELECT "TABLE" FROM customers',
        table_reference="customers",
        limit=5,
        restricted_columns=("region",),
        expose=_expose(),
        dialect="duckdb",
    )
    assert compiled.sql.startswith('SELECT "TABLE" FROM (SELECT id, segment')


def test_a_table_name_that_merely_contains_the_word_is_not_the_keyword(con):
    kind, detail = _free_form(con, "SELECT segment AS table_name, tables.id FROM customers tables")
    assert kind == "rows", detail


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT * FROM TABLE('CUSTOMERS')", id="table-function"),
        pytest.param("SELECT * FROM TABLE(IDENTIFIER('CUSTOMERS'))", id="table-identifier"),
        pytest.param("SELECT * FROM TABLE ( 'CUSTOMERS' ) t", id="table-spaced-alias"),
    ],
)
def test_snowflake_table_forms_are_refused(sql):
    with pytest.raises(QueryValidationError):
        compile_free_form_sql(
            sql=sql,
            table_reference="CUSTOMERS",
            limit=5,
            restricted_columns=("REGION",),
            expose=_expose(),
            dialect="snowflake",
        )


def test_snowflake_identifier_function_as_a_table_is_refused():
    with pytest.raises(QueryValidationError):
        compile_free_form_sql(
            sql="SELECT * FROM IDENTIFIER('CUSTOMERS')",
            table_reference="CUSTOMERS",
            limit=5,
            restricted_columns=("REGION",),
            expose=_expose(),
            dialect="snowflake",
        )


# A source the compiler does not know how to project is refused, whatever the node is
# called, so a form added to the parser later does not become a way around the rewrite.
@pytest.mark.parametrize(
    "dialect,sql",
    [
        pytest.param("snowflake", "SELECT * FROM TABLE('X')", id="table-from-rows"),
        pytest.param("snowflake", "SELECT * FROM a, TABLE(IDENTIFIER('X'))", id="join-list"),
        pytest.param(
            "postgres", "SELECT * FROM ROWS FROM (generate_series(1, 2)) r", id="rows-from"
        ),
        pytest.param("postgres", "SELECT * FROM generate_series(1, 3) g", id="function-table"),
    ],
)
def test_a_source_that_is_not_a_plain_table_is_refused(dialect, sql):
    import sqlglot as _sqlglot

    from fluid_build.output_ports.mcp.query_compiler import _require_plain_sources

    with pytest.raises(QueryValidationError):
        _require_plain_sources(_sqlglot.parse_one(sql, read=dialect))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM customers",
        "SELECT * FROM main.customers c JOIN s2.other o ON true",
        "SELECT * FROM unnest([1, 2]) AS u(x)",
        "SELECT * FROM (VALUES (1)) AS v(a)",
        "SELECT * FROM customers, LATERAL (SELECT 1 AS one) l",
        "SELECT * FROM (SELECT 1 AS one) s",
    ],
)
def test_the_plain_sources_stay_accepted(sql):
    import sqlglot as _sqlglot

    from fluid_build.output_ports.mcp.query_compiler import _require_plain_sources

    _require_plain_sources(_sqlglot.parse_one(sql, read="duckdb"))


def test_a_values_list_and_an_unnest_still_work_beside_the_exposed_table(con):
    kind, detail = _free_form(
        con, "SELECT c.id, v.a FROM customers c, (VALUES (1), (2)) AS v(a) WHERE c.id = v.a"
    )
    assert kind == "rows", detail
    assert sorted(detail[1]) == [(1, 1), (2, 2)]
    kind, detail = _free_form(con, "SELECT c.id, u.x FROM customers c, unnest([7]) AS u(x)")
    assert kind == "rows", detail
    assert len(detail[1]) == 3


# DuckDB's own parser reads the rewritten statement too.
def test_the_engine_parser_cross_check_refuses_a_bare_read_of_the_exposed_table():
    from fluid_build.output_ports.mcp.query_compiler import _crosscheck_duckdb_projection

    allowed = {"id", "segment", "amount", "tenant_id"}
    for bare in (
        "SELECT to_json(x) FROM (TABLE customers) x",
        "SELECT * FROM customers",
        "SELECT * FROM main.customers c",
        "SELECT * FROM (FROM customers) t",
        "SELECT * FROM s2.other, (TABLE customers)",
        # the projection itself is wrong: a restricted column, a star, an expression
        "SELECT * FROM (SELECT id, region FROM customers) AS customers",
        "SELECT * FROM (SELECT * FROM customers) AS customers",
        "SELECT * FROM (SELECT id + 1 FROM customers) AS customers",
        "SELECT * FROM (SELECT id FROM customers WHERE id > 0) AS customers",
        "SELECT * FROM (SELECT id FROM customers, s2.other) AS customers",
    ):
        with pytest.raises(QueryValidationError):
            _crosscheck_duckdb_projection(bare, "customers", allowed)
    for fine in (
        "SELECT * FROM (SELECT id, segment FROM customers) AS customers",
        "SELECT * FROM c: (SELECT id, amount FROM customers)",
        "SELECT a.id FROM (SELECT id FROM customers) AS a JOIN (SELECT id FROM customers) AS b ON true",
        "SELECT * FROM s2.other",
    ):
        _crosscheck_duckdb_projection(fine, "customers", allowed)


def test_the_engine_parser_cross_check_runs_on_the_rewritten_statement(monkeypatch):
    from fluid_build.output_ports.mcp import query_compiler

    seen: List[str] = []
    real = query_compiler._crosscheck_duckdb_projection

    def spy(sql, name, allowed, predicate=None):
        seen.append(sql)
        return real(sql, name, allowed, predicate)

    monkeypatch.setattr(query_compiler, "_crosscheck_duckdb_projection", spy)
    compile_free_form_sql(
        sql="SELECT id FROM customers",
        table_reference="customers",
        limit=5,
        restricted_columns=("region",),
        expose=_expose(),
        dialect="duckdb",
    )
    assert len(seen) == 1 and "(SELECT id, segment, amount, tenant_id FROM customers)" in seen[0]
    seen.clear()
    compile_free_form_sql(
        sql="SELECT id FROM customers",
        table_reference="customers",
        limit=5,
        restricted_columns=("REGION",),
        expose=_expose(),
        dialect="snowflake",
    )
    assert seen == []


# ---------------------------------------------------------------------
# ``query_sql`` and the driver guard: the compiler's projection at any position
# ---------------------------------------------------------------------


def test_driver_guard_admits_the_projection_wherever_the_compiler_writes_it():
    from fluid_build.output_ports.mcp.drivers.base import guard_against_injection_markers

    for sql in (
        "SELECT * FROM ((SELECT id, segment FROM customers) AS customers)\nLIMIT 5",
        "SELECT * FROM c: (SELECT id, segment FROM customers)\nLIMIT 5",
        "SELECT a.id FROM (SELECT id FROM customers) AS a JOIN "
        "((SELECT id FROM customers) AS b) ON a.id = b.id\nLIMIT 5",
        "SELECT * FROM (SUMMARIZE (SELECT id, segment FROM customers))\nLIMIT 5",
    ):
        guard_against_injection_markers(sql)
    for hostile in (
        "SELECT * FROM ((SELECT id FROM customers) UNION SELECT 1)\nLIMIT 5",
        "SELECT * FROM c: (SELECT id + 1 FROM customers)\nLIMIT 5",
        "SELECT * FROM (SELECT * FROM customers) AS t\nLIMIT 5",
        "SELECT * FROM ((SELECT (SELECT region FROM customers) AS id FROM customers) AS t)",
        "SELECT x FROM customers WHERE id IN (SELECT id + 1 FROM other)",
    ):
        with pytest.raises(ValueError):
            guard_against_injection_markers(hostile)


def test_a_prefix_alias_reads_the_allowed_columns(con):
    kind, detail = _free_form_guarded(con, "SELECT * FROM c: customers")
    assert kind == "rows", detail
    names, rows = detail
    assert "region" not in [n.lower() for n in names]
    assert not _leaked(rows)
    assert len(rows) == 3


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param("SELECT * FROM (customers)", id="parenthesised-name"),
        pytest.param("SELECT * FROM (SUMMARIZE customers)", id="summarize"),
        pytest.param("SELECT * FROM (DESCRIBE customers)", id="describe"),
        pytest.param("SELECT * FROM (PIVOT customers ON segment USING sum(amount))", id="pivot"),
    ],
)
def test_forms_beyond_a_plain_select_run_over_the_allowed_columns_or_are_refused_clearly(con, sql):
    kind, detail = _free_form_guarded(con, sql)
    assert kind in ("rows", "refused"), f"{kind}: {detail!r}"
    if kind == "rows":
        names, rows = detail
        assert "region" not in repr(detail).lower() and not _leaked(rows)
    else:
        # A refusal says what to do; it is not the driver guard's "compiler safety
        # regression" text, and it is a typed validation error.
        assert "regression" not in detail
        assert "query" in detail.lower() or "select" in detail.lower()


# ---------------------------------------------------------------------
# ``query_sql`` with column restrictions needs the expose to project from
# ---------------------------------------------------------------------


def test_free_form_sql_with_restricted_columns_and_no_expose_fails_closed():
    with pytest.raises(QueryValidationError, match="expose"):
        compile_free_form_sql(
            sql="SELECT id FROM customers",
            table_reference="customers",
            limit=5,
            restricted_columns=("region",),
        )


def test_free_form_sql_without_restrictions_needs_no_expose():
    compiled = compile_free_form_sql(
        sql="SELECT id FROM customers",
        table_reference="customers",
        limit=5,
    )
    assert compiled.sql == "SELECT id FROM customers\nLIMIT 5"


# ---------------------------------------------------------------------
# ``query``: every WHERE term is parenthesised, so no expression can end the row filter
# ---------------------------------------------------------------------

_TENANT_FILTER = _T1_ROWS
# The tenant t1 holds amounts 10 and 20; t2 holds 30. The right answer for t1 is 30.
_TENANT_ONLY_SUM = 30.0
_ESCAPING_DIMENSION = "amount > 15 OR id"


@pytest.mark.parametrize("restricted", [(), ("region",), ("region", "tenant_id")])
def test_a_filtered_dimension_whose_expression_has_an_or_cannot_widen_the_row_filter(
    con, restricted
):
    expose = _expose(dimension_expr=_ESCAPING_DIMENSION, row_filters=_TENANT_FILTER)
    kind, rows = _run(
        con,
        expose,
        restricted=restricted,
        dimensions=[],
        filters={"d": True},
        caller_attributes=AS_T1,
    )
    assert kind == "rows", rows
    assert [float(total) for (total,) in rows] == [_TENANT_ONLY_SUM]


def test_a_metric_filter_with_an_or_cannot_widen_the_row_filter(con):
    expose = _expose(metric_filter="amount > 15 OR id > 0", row_filters=_TENANT_FILTER)
    kind, rows = _run(
        con,
        expose,
        restricted=(),
        use_metric=True,
        caller_attributes=AS_T1,
    )
    assert kind == "rows", rows
    assert [float(total) for (total,) in rows] == [_TENANT_ONLY_SUM]


def test_every_where_term_is_parenthesised():
    expose = _expose(
        dimension_expr=_ESCAPING_DIMENSION,
        metric_filter="amount > 15 OR id > 0",
        row_filters=[
            {"principal": T1, "where": "tenant_id = 't1'"},
            {"principal": "group:regions@northwind.example", "where": "region IN ('a', 'b')"},
        ],
    )
    for restricted in ((), ("region",)):
        compiled = compile_semantic_query(
            expose=expose,
            metric="mt",
            filters={"d": True, "segment": "retail"},
            limit=5,
            table_reference="customers",
            dialect="duckdb",
            restricted_columns=restricted,
            caller_attributes={"principal": [T1, "group:regions@northwind.example"]},
        )
        where_lines = [
            line
            for line in compiled.sql.splitlines()
            if " WHERE " in line or line.startswith("WHERE ")
        ]
        where = " ".join(where_lines)
        assert "((amount > 15 OR id) = :p_0)" in where
        assert "((segment) = :p_1)" in where
        assert "(amount > 15 OR id > 0)" in where
        # The row filter is the union of the caller's two principals' filters, each
        # parenthesised, inside the projection.
        assert (
            "WHERE ((_fluid_rows.tenant_id = 't1') OR (_fluid_rows.region IN ('a', 'b')))"
        ) in where
        # Between terms there is only an ``AND``, never a bare predicate.
        assert re.search(r"\) AND \(", where)


def test_the_no_filter_statement_has_no_where():
    compiled = compile_semantic_query(
        expose=_expose(),
        measure="m",
        limit=5,
        table_reference="customers",
        dialect="duckdb",
    )
    assert "WHERE" not in compiled.sql


# ---------------------------------------------------------------------
# ``query``: redacted columns cannot be read by an expression that selects them by shape
# ---------------------------------------------------------------------

_PII_SCHEMA = [
    {"name": "id"},
    {"name": "segment"},
    {"name": "amount"},
    {"name": "tenant_id"},
    {"name": "email", "sensitivity": "pii"},
    {"name": "salary", "sensitivity": "pii"},
]
_PII_ROWS = (
    "id,segment,amount,tenant_id,email,salary\n"
    "1,retail,10,t1,PII-a@x.example,100\n"
    "2,retail,20,t1,PII-b@x.example,200\n"
    "3,wholesale,30,t2,PII-c@x.example,300\n"
)
_PII_MARKER = "PII-"


def _pii_state(
    tmp_path,
    *,
    measure_expr="amount",
    agg="sum",
    dimension_expr=None,
    metric_filter=None,
    row_filters=None,
    restricted_columns=(),
):
    from types import SimpleNamespace

    from fluid_build.output_ports.mcp.drivers import build_driver

    csv = tmp_path / "customers.csv"
    csv.write_text(_PII_ROWS)
    semantics: Dict[str, Any] = {
        "measures": [{"name": "m", "agg": agg, "expr": measure_expr}],
        "dimensions": [{"name": "segment", "type": "categorical", "expr": "segment"}],
        "metrics": [
            {
                "name": "mt",
                "type": "simple",
                "measure": "m",
                **({"filter": metric_filter} if metric_filter else {}),
            }
        ],
    }
    if dimension_expr is not None:
        semantics["dimensions"].append({"name": "d", "type": "categorical", "expr": dimension_expr})
    expose: Dict[str, Any] = {
        "exposeId": "customers",
        "contract": {"schema": list(_PII_SCHEMA)},
        "semantics": semantics,
        "binding": {
            "platform": "local",
            "format": "csv",
            "location": {"path": str(csv), "table": "customers"},
        },
    }
    authz: Dict[str, Any] = {}
    if row_filters:
        authz["rowFilters"] = row_filters
    if restricted_columns:
        authz["columnRestrictions"] = [
            {"principal": "*", "columns": list(restricted_columns), "access": "deny"}
        ]
    if authz:
        expose["policy"] = {"authz": authz}
    driver = build_driver(expose=expose, contract={"exposes": [expose]})
    return SimpleNamespace(
        expose=expose,
        caller_attributes=AS_T1,
        policy=SimpleNamespace(max_sample_rows=100),
        query_timeout_seconds=None,
        get_driver=lambda: driver,
    )


def _query(state, **arguments):
    from fluid_build.output_ports.mcp._handlers import tool_query

    payload = tool_query(state, arguments)
    return payload["rows"]


_REDACTED_FORMS = [
    pytest.param("COLUMNS('ema.*')", "max", id="columns-regex"),
    pytest.param("COLUMNS('EMA.*')", "max", id="columns-regex-upper"),
    pytest.param("COLUMNS(lambda c: c = 'email')", "max", id="columns-lambda"),
    pytest.param("COLUMNS(*)", "max", id="columns-star"),
    pytest.param("\"COLUMNS\"('ema.*')", "max", id="columns-quoted-function"),
    pytest.param("columns ('ema.*')", "max", id="columns-spaced"),
    pytest.param("to_json(COLUMNS(*))", "max", id="columns-in-function"),
    pytest.param("customers", "max", id="whole-row-table-name"),
    pytest.param('"customers"', "max", id="whole-row-quoted"),
    pytest.param("to_json(customers)", "max", id="whole-row-in-function"),
    pytest.param("to_json(CUSTOMERS)", "max", id="whole-row-upper"),
    pytest.param("to_json(_fluid_allowed)", "max", id="whole-row-projection-alias"),
    pytest.param("to_json(_FLUID_ALLOWED)", "max", id="whole-row-projection-alias-upper"),
    pytest.param('to_json("_fluid_allowed")', "max", id="whole-row-projection-alias-quoted"),
    pytest.param("to_json(main.customers)", "max", id="whole-row-schema-qualified"),
    pytest.param("to_json(customers.*)", "max", id="qualified-star"),
    pytest.param("to_json(*)", "max", id="star-in-function"),
    pytest.param("to_json( * )", "max", id="star-spaced"),
    pytest.param("struct_pack(a := *)", "max", id="star-after-operator"),
    pytest.param("IDENTIFIER('email')", "max", id="identifier-function"),
    pytest.param("identifier ('em' || 'ail')", "max", id="identifier-function-concat"),
    pytest.param("*", "max", id="star-for-max"),
    pytest.param("*", "count_distinct", id="star-for-count-distinct"),
]


@pytest.mark.parametrize("restricted", [(), ("tenant_id",)], ids=["plain", "projected"])
@pytest.mark.parametrize("measure_expr,agg", _REDACTED_FORMS)
def test_a_measure_cannot_select_a_redacted_column_by_shape(
    tmp_path, measure_expr, agg, restricted
):
    state = _pii_state(tmp_path, measure_expr=measure_expr, agg=agg, restricted_columns=restricted)
    with pytest.raises(QueryValidationError):
        result = _query(state, measure="m")
        pytest.fail(f"evaluated: {result!r}")


@pytest.mark.parametrize("restricted", [(), ("tenant_id",)], ids=["plain", "projected"])
@pytest.mark.parametrize(
    "expression", [p.values[0] for p in _REDACTED_FORMS if p.values[1] == "max"]
)
def test_a_dimension_cannot_select_a_redacted_column_by_shape(tmp_path, expression, restricted):
    state = _pii_state(tmp_path, dimension_expr=expression, restricted_columns=restricted)
    with pytest.raises(QueryValidationError):
        result = _query(state, measure="m", dimensions=["d"])
        pytest.fail(f"evaluated: {result!r}")


@pytest.mark.parametrize(
    "expression",
    [
        "COLUMNS('ema.*') IS NOT NULL",
        "to_json(customers) <> ''",
        "to_json(_fluid_allowed) <> ''",
        "(*) IS NOT NULL",
    ],
)
def test_a_metric_filter_cannot_select_a_redacted_column_by_shape(tmp_path, expression):
    state = _pii_state(tmp_path, metric_filter=expression, restricted_columns=("tenant_id",))
    with pytest.raises(QueryValidationError):
        result = _query(state, metric="mt")
        pytest.fail(f"evaluated: {result!r}")


def test_a_filter_key_cannot_select_a_redacted_column_by_shape(tmp_path):
    state = _pii_state(tmp_path, dimension_expr="to_json(customers)")
    with pytest.raises(QueryValidationError):
        result = _query(state, measure="m", filters={"d": "x"})
        pytest.fail(f"evaluated: {result!r}")


def test_the_compiler_refuses_these_shapes_with_the_redacted_columns_it_was_given():
    compiled = None
    with pytest.raises(QueryValidationError, match="redact"):
        compiled = compile_semantic_query(
            expose=_expose(measure_expr="COLUMNS('ema.*')", agg="max"),
            measure="m",
            limit=5,
            table_reference="customers",
            dialect="duckdb",
            redacted_columns=("email",),
        )
    assert compiled is None
    # No redacted column on the expose: the same expression is a plain measure.
    compile_semantic_query(
        expose=_expose(measure_expr="COLUMNS('ema.*')", agg="max"),
        measure="m",
        limit=5,
        table_reference="customers",
        dialect="duckdb",
    )


def test_a_sensitive_numeric_still_sums_and_averages(tmp_path):
    state = _pii_state(tmp_path, measure_expr="salary", agg="sum")
    assert sorted(float(r["m"]) for r in _query(state, measure="m", dimensions=["segment"])) == [
        300.0,
        300.0,
    ]
    state = _pii_state(
        tmp_path, measure_expr="salary", agg="avg", restricted_columns=("tenant_id",)
    )
    rows = _query(state, measure="m", dimensions=["segment"])
    assert sorted(float(r["m"]) for r in rows) == [150.0, 300.0]


def test_count_distinct_over_a_redacted_column_still_works(tmp_path):
    state = _pii_state(tmp_path, measure_expr="email", agg="count_distinct")
    rows = _query(state, measure="m")
    assert [int(r["m"]) for r in rows] == [3]
    assert _PII_MARKER not in repr(rows)


def test_count_star_and_multiplication_still_work_on_an_expose_with_redacted_columns(tmp_path):
    state = _pii_state(tmp_path, measure_expr="*", agg="count")
    assert [int(r["m"]) for r in _query(state, measure="m")] == [3]
    state = _pii_state(tmp_path, measure_expr="amount * 2 + salary * 0", agg="sum")
    assert [float(r["m"]) for r in _query(state, measure="m")] == [120.0]
    state = _pii_state(
        tmp_path, measure_expr="amount ** 2", agg="sum", restricted_columns=("tenant_id",)
    )
    assert [float(r["m"]) for r in _query(state, measure="m")] == [1400.0]
    compile_semantic_query(
        expose=_expose(dimension_expr="count(*) OVER (PARTITION BY segment)"),
        measure="m",
        dimensions=["d"],
        limit=5,
        table_reference="customers",
        dialect="duckdb",
        redacted_columns=("email",),
    )


def test_a_dimension_that_reads_a_redacted_column_by_name_is_still_redacted(tmp_path):
    for expression in ("email", "lower(email)", '"email"', 'upper("EMAIL")'):
        state = _pii_state(tmp_path, dimension_expr=expression)
        rows = _query(state, measure="m", dimensions=["d"])
        assert rows and _PII_MARKER not in repr(rows), (expression, rows)
        assert {r["d"] for r in rows} == {"[REDACTED-PII]"}, (expression, rows)


def test_max_over_a_redacted_column_is_redacted(tmp_path):
    state = _pii_state(tmp_path, measure_expr="email", agg="max")
    rows = _query(state, measure="m")
    assert _PII_MARKER not in repr(rows)
    assert rows[0]["m"] == "[REDACTED-PII]"


def test_a_table_named_column_is_not_a_whole_row_when_it_is_qualified(tmp_path):
    state = _pii_state(tmp_path, measure_expr="customers.amount", agg="sum")
    assert [float(r["m"]) for r in _query(state, measure="m")] == [60.0]
    state = _pii_state(
        tmp_path, measure_expr="_fluid_allowed.amount", agg="sum", restricted_columns=("tenant_id",)
    )
    assert [float(r["m"]) for r in _query(state, measure="m")] == [60.0]
    state = _pii_state(tmp_path, measure_expr="customers . amount", agg="sum")
    assert [float(r["m"]) for r in _query(state, measure="m")] == [60.0]


def test_a_string_that_looks_like_a_shape_is_not_a_shape(tmp_path):
    state = _pii_state(
        tmp_path,
        measure_expr="amount",
        agg="sum",
        metric_filter="segment <> 'COLUMNS(*) customers * IDENTIFIER(x)'",
    )
    assert [float(r["mt"]) for r in _query(state, metric="mt")] == [60.0]


def test_an_expose_with_no_redacted_columns_is_not_subject_to_the_shape_rule():
    for measure_expr in ("COLUMNS('amo.*')", "customers", "*"):
        compile_semantic_query(
            expose=_expose(measure_expr=measure_expr, agg="max"),
            measure="m",
            limit=5,
            table_reference="customers",
            dialect="duckdb",
            restricted_columns=(),
            redacted_columns=(),
        )


# ---------------------------------------------------------------------------
# The table's implicit alias, as each engine's table reference spells it
# ---------------------------------------------------------------------------

_TABLE_REFERENCES = [
    pytest.param("bigquery", "`proj.ds.customers`", id="bigquery-one-pair-of-backticks"),
    pytest.param("bigquery", "`proj`.`ds`.`customers`", id="bigquery-backtick-per-part"),
    pytest.param("bigquery", "`my-proj.ds.customers`", id="bigquery-hyphenated-project"),
    pytest.param("bigquery", "proj.ds.customers", id="bigquery-bare"),
    pytest.param("postgres", "db.public.customers", id="postgres-bare"),
    pytest.param("postgres", 'db.public."customers"', id="postgres-quoted-last-part"),
    pytest.param("snowflake", "DB.SCHEMA.customers", id="snowflake-bare"),
    pytest.param("snowflake", '"DB"."SCHEMA"."customers"', id="snowflake-quoted"),
    pytest.param("athena", '"db"."customers"', id="athena-quoted"),
    pytest.param("duckdb", "customers", id="duckdb-single-part"),
    pytest.param(None, "main.customers", id="unknown-dialect"),
]


@pytest.mark.parametrize("dialect,table_reference", _TABLE_REFERENCES)
def test_the_table_alias_is_the_last_part_of_the_reference(dialect, table_reference):
    from fluid_build.output_ports.mcp.query_compiler import _table_last_identifier

    assert _table_last_identifier(table_reference, dialect) == "customers"
    # the default dialect reads the same last part
    assert _table_last_identifier(table_reference) == "customers"


@pytest.mark.parametrize("reference", ["read_csv('x.csv')", "main.customers()", "", "  "])
def test_a_reference_that_ends_in_no_name_has_no_alias(reference):
    from fluid_build.output_ports.mcp.query_compiler import _table_last_identifier

    assert _table_last_identifier(reference, "postgres") is None


@pytest.mark.parametrize("dialect,table_reference", _TABLE_REFERENCES)
@pytest.mark.parametrize("expression", ["TO_JSON_STRING(customers)", "to_json(CUSTOMERS)"])
def test_a_whole_row_value_is_refused_whatever_the_table_reference_looks_like(
    dialect, table_reference, expression
):
    with pytest.raises(QueryValidationError, match="redact"):
        compile_semantic_query(
            expose=_expose(dimension_expr=expression),
            measure="m",
            dimensions=["d"],
            limit=5,
            table_reference=table_reference,
            dialect=dialect,
            redacted_columns=("region",),
        )


def test_a_qualified_column_of_a_bigquery_table_is_still_a_column():
    compile_semantic_query(
        expose=_expose(dimension_expr="upper(customers.segment)"),
        measure="m",
        dimensions=["d"],
        limit=5,
        table_reference="`proj.ds.customers`",
        dialect="bigquery",
        redacted_columns=("region",),
    )


# ---------------------------------------------------------------------------
# Parentheses inside a quoted span do not count
# ---------------------------------------------------------------------------

# Read as plain text the two quoted spans cancel and the parentheses balance; the
# engine reads each as one name, so the statement closes a parenthesis it never opened.
_HIDDEN_CLOSE = '"((" IS NULL OR amount > 0) OR (amount > 0 AND "))" IS NULL'


def test_quoted_parentheses_do_not_hide_an_unbalanced_expression():
    from fluid_build.output_ports.mcp.query_compiler import _parens_balanced

    assert _parens_balanced("(a) AND (b)")
    assert not _parens_balanced("a) OR (b")
    assert not _parens_balanced(_HIDDEN_CLOSE)
    # a parenthesis in a name or a value is not a parenthesis
    assert _parens_balanced("length(\"we(ird\") + length('x))')")
    assert _parens_balanced("length(`we((ird`) + length('x))')")
    assert _parens_balanced("upper(\"a''(\") = 'it''s )'")
    assert not _parens_balanced('("unclosed')
    assert not _parens_balanced('"(" IS NULL OR (')


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "bigquery", None])
def test_a_filter_key_expression_with_hidden_parentheses_is_refused(dialect):
    with pytest.raises(QueryValidationError, match="unbalanced"):
        compile_semantic_query(
            expose=_expose(dimension_expr=_HIDDEN_CLOSE),
            measure="m",
            filters={"d": "x"},
            limit=5,
            table_reference="customers",
            dialect=dialect,
        )


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", None])
def test_a_metric_filter_with_hidden_parentheses_is_refused(dialect):
    with pytest.raises(QueryValidationError, match="unbalanced"):
        compile_semantic_query(
            expose=_expose(metric_filter=_HIDDEN_CLOSE),
            metric="mt",
            limit=5,
            table_reference="customers",
            dialect=dialect,
        )


def test_a_balanced_expression_with_parentheses_in_a_name_still_filters(con):
    kind, rows = _run(
        con,
        _expose(dimension_expr="length('x))') + length(segment)"),
        restricted=(),
        filters={"d": 14},
        dimensions=["d"],
    )
    assert kind == "rows"
