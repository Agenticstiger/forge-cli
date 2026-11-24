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
        expose["policy"] = {"rowFilters": row_filters}
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
    expose = _expose(row_filters=[{"column": "tenant_id", "equals": "${caller.tenant}"}])
    kind, rows = _run(
        con,
        expose,
        restricted=("region", "tenant_id"),
        dimensions=["segment"],
        caller_attributes={"tenant": "t1"},
    )
    assert kind == "rows", rows
    assert {segment: float(total) for segment, total in rows} == {"retail": 30.0}


def test_row_filter_is_not_bypassed_by_a_subquery_over_the_base_table(con):
    expose = _expose(
        dimension_expr="EXISTS(FROM customers WHERE tenant_id = 't2')",
        row_filters=[{"column": "tenant_id", "equals": "${caller.tenant}"}],
    )
    kind, detail = _run(con, expose, dimensions=["d"], caller_attributes={"tenant": "t1"})
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


def test_row_filter_sits_inside_the_projection_with_bound_parameters():
    expose = _expose(row_filters=[{"column": "tenant_id", "equals": "${caller.tenant}"}])
    compiled = compile_semantic_query(
        expose=expose,
        measure="m",
        dimensions=["segment"],
        filters={"segment": "retail"},
        limit=5,
        table_reference="customers",
        dialect="duckdb",
        restricted_columns=("region", "tenant_id"),
        caller_attributes={"tenant": "t1"},
    )
    assert (
        'FROM (SELECT id, segment, amount FROM customers WHERE ("tenant_id" = :p_1)) '
        "AS _fluid_allowed"
    ) in compiled.sql
    assert "WHERE ((segment) = :p_0)\n" in compiled.sql
    assert compiled.params == ["retail", "t1"]


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
        row_filters=[{"column": "tenant_id", "in": "${caller.tenants}"}],
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
        caller_attributes={"tenants": ["t1", "t2"]},
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
            dialect="duckdb",
        )
    # BigQuery reads "region" as a string literal, so it is not a reference there.
    compile_free_form_sql(
        sql='SELECT "region" AS x FROM customers',
        table_reference="customers",
        limit=5,
        restricted_columns=("region",),
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
        expose=_expose(row_filters=[{"column": "tenant_id", "equals": "t1"}]),
        measure="m",
        dimensions=["segment"],
        limit=5,
        table_reference="customers",
        restricted_columns=("region",),
    )
    guard_against_injection_markers(compiled.sql)
    for hostile in (
        compiled.sql.replace("FROM customers", "FROM customers UNION SELECT 1 FROM customers"),
        compiled.sql.replace("(SELECT id", "(SELECT (SELECT region FROM customers) AS id"),
        "SELECT x FROM (SELECT region FROM customers) AS t",
        compiled.sql.replace(
            "AS _fluid_allowed", "AS _fluid_allowed JOIN (SELECT region FROM t) j"
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
    expose = _expose(
        dimension_expr='"region"',
        row_filters=[{"column": "tenant_id", "equals": "${caller.tenant}"}],
    )
    expose["exposeId"] = "customers"
    expose["binding"] = {
        "platform": "local",
        "format": "csv",
        "location": {"path": str(csv), "table": "customers"},
    }
    expose["policy"]["authz"] = {
        "columnRestrictions": [
            {"principal": "*", "columns": ["region", "tenant_id"], "access": "deny"}
        ]
    }
    driver = build_driver(expose=expose, contract={"exposes": [expose]})
    state = SimpleNamespace(
        expose=expose,
        caller_attributes={"tenant": "t1"},
        policy=SimpleNamespace(max_sample_rows=100),
        query_timeout_seconds=None,
        get_driver=lambda: driver,
    )
    payload = tool_query(state, {"metric": "mt", "dimensions": ["segment"]})
    assert {row["segment"]: float(row["mt"]) for row in payload["rows"]} == {"retail": 30.0}
    with pytest.raises(QueryValidationError):
        tool_query(state, {"metric": "mt", "dimensions": ["d"]})


# ---------------------------------------------------------------------
# ``query``: every WHERE term is parenthesised, so no expression can end the row filter
# ---------------------------------------------------------------------

_TENANT_FILTER = [{"column": "tenant_id", "equals": "${caller.tenant}"}]
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
        caller_attributes={"tenant": "t1"},
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
        caller_attributes={"tenant": "t1"},
    )
    assert kind == "rows", rows
    assert [float(total) for (total,) in rows] == [_TENANT_ONLY_SUM]


def test_every_where_term_is_parenthesised():
    expose = _expose(
        dimension_expr=_ESCAPING_DIMENSION,
        metric_filter="amount > 15 OR id > 0",
        row_filters=[
            {"column": "tenant_id", "equals": "${caller.tenant}"},
            {"column": "region", "in": "${caller.regions}"},
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
            caller_attributes={"tenant": "t1", "regions": ["a", "b"]},
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
        assert '("tenant_id" = :p_2)' in where
        assert '("region" IN (:p_3, :p_4))' in where
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
    policy: Dict[str, Any] = {}
    if row_filters:
        policy["rowFilters"] = row_filters
    if restricted_columns:
        policy["authz"] = {
            "columnRestrictions": [
                {"principal": "*", "columns": list(restricted_columns), "access": "deny"}
            ]
        }
    if policy:
        expose["policy"] = policy
    driver = build_driver(expose=expose, contract={"exposes": [expose]})
    return SimpleNamespace(
        expose=expose,
        caller_attributes={"tenant": "t1"},
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
