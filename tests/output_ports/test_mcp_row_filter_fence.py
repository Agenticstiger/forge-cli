# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A filtered caller's conditions are typed, and refused where the gateway cannot type them.

An engine may evaluate a condition on a row the row filter removes. A value it casts or
matches row by row can raise an error on such a row, and the error tells the caller
that the row exists. So for a caller a row filter narrows, each WHERE, ON, USING,
HAVING and QUALIFY condition is checked:

* every condition, and each operand of AND, OR, NOT and IS TRUE / IS FALSE, is a
  predicate, a boolean column, or TRUE or FALSE;
* every comparison relates values of one type, the columns' types read from the table's
  catalog (a declared type must agree), or declared when no driver reads them;
* exact numbers meet only when the larger integer part and the larger scale fit in 38
  digits, and a float meets any number;
* a date meets a date, a timestamp a timestamp, a timestamp with a time zone its own kind;
* LIKE and ILIKE take a string and a string literal pattern with no escape;
* ``lower``, ``upper`` and ``coalesce`` take typed columns and constants.

Each refused statement below is one the engine could fail on for a withheld row's value.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.output_ports.mcp import query_compiler  # noqa: E402
from fluid_build.output_ports.mcp._handlers import tool_query_sql  # noqa: E402
from fluid_build.output_ports.mcp.query_compiler import ColumnType  # noqa: E402
from fluid_build.providers._sql_safety import quote_string_literal  # noqa: E402

EU = "group:eu-analysts@northwind.example"
READER = "group:all-staff@northwind.example"
_PREDICATE = "((_fluid_rows.region = 'EU'))"

_SCHEMA: List[Tuple[str, Optional[str]]] = [
    ("id", "integer"),
    ("region", "string"),
    ("note", "varchar(40)"),
    ("amount", "integer"),
    ("active", "boolean"),
    ("created", "date"),
    ("doc", "json"),
    ("loose", None),  # declared without a type
]


def _expose(schema: List[Tuple[str, Optional[str]]] = _SCHEMA) -> Dict[str, Any]:
    return {
        "exposeId": "customers",
        "contract": {
            "schema": [{"name": name, **({"type": kind} if kind else {})} for name, kind in schema]
        },
        "policy": {
            "authz": {
                "rowFilters": [{"principal": EU, "where": "region = 'EU'"}],
                "readers": [READER],
            }
        },
    }


def _compile(sql: str, dialect: str = "duckdb", caller: str = EU):
    return query_compiler.compile_free_form_sql(
        sql=sql,
        table_reference="customers",
        limit=10,
        expose=_expose(),
        caller_attributes={"principal": caller},
        dialect=dialect,
    )


def _fence(sql: str, dialect: str = "duckdb") -> str:
    """The projection a filtered caller's statement compiles to, without the keyword
    checks in front of it (they refuse a nested SELECT before the fence sees it)."""
    return query_compiler._project_exposed_table(
        sql,
        table_reference="customers",
        allowed=[name for name, _ in _SCHEMA],
        dialect=dialect,
        predicate=_PREDICATE,
        column_types=query_compiler._column_types(_expose(), dialect, None),
    )


_REFUSAL = "on an expose with row filters"
_ENGINES = ["duckdb", "postgres"]


# ---------------------------------------------------------------------
# Boolean position
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "where",
    [
        pytest.param("note", id="string-column"),
        pytest.param("note IS TRUE", id="is-true"),
        pytest.param("note IS NOT FALSE", id="is-not-false"),
        pytest.param("NOT note", id="not"),
        pytest.param("amount AND id = 1", id="and-operand"),
        pytest.param("id = 1 OR note", id="or-operand"),
        pytest.param("(note)", id="parenthesised"),
    ],
)
@pytest.mark.parametrize("dialect", _ENGINES)
def test_a_value_that_is_not_a_predicate_is_refused_as_a_condition(where, dialect):
    with pytest.raises(query_compiler.QueryValidationError, match="as a condition"):
        _compile(f"SELECT id FROM customers WHERE {where}", dialect)


@pytest.mark.parametrize("where", ["note IS UNKNOWN", "note IS NOT UNKNOWN"])
@pytest.mark.parametrize("dialect", _ENGINES)
def test_is_unknown_is_refused(where, dialect):
    """sqlglot reads it as IS NULL; the engine casts the value to a boolean."""
    with pytest.raises(query_compiler.QueryValidationError, match="IS UNKNOWN"):
        _compile(f"SELECT id FROM customers WHERE {where}", dialect)


@pytest.mark.parametrize(
    "where",
    [
        "active",
        "active IS TRUE",
        "NOT active",
        "active AND id = 1",
        "note IS NULL",
        "note IS NOT NULL",
        "TRUE",
        "note = 'is unknown'",
    ],
)
@pytest.mark.parametrize("dialect", _ENGINES)
def test_predicates_and_boolean_columns_are_conditions(where, dialect):
    _compile(f"SELECT id FROM customers WHERE {where}", dialect)


# ---------------------------------------------------------------------
# Comparisons the gateway cannot type
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "SELECT a.id FROM customers AS a JOIN (VALUES (5)) AS v(note) USING (note)",
            id="using-values",
        ),
        pytest.param(
            "SELECT a.id FROM customers AS a JOIN (VALUES (5)) AS v(x) ON a.note = v.x",
            id="on-values",
        ),
        pytest.param("SELECT id FROM customers WHERE loose = 5", id="untyped-column"),
        pytest.param("SELECT id FROM customers WHERE loose = amount", id="untyped-and-typed"),
        pytest.param(
            "SELECT loose FROM customers GROUP BY loose HAVING loose = 5", id="untyped-having"
        ),
        pytest.param("SELECT id FROM customers WHERE doc = 5", id="json-number"),
        pytest.param("SELECT id FROM customers WHERE doc = '5'", id="json-string"),
        pytest.param("SELECT id FROM customers WHERE note = 'a' || 'b'", id="concatenation"),
        pytest.param("SELECT id FROM customers WHERE note = 5", id="string-number"),
        pytest.param("SELECT id FROM customers WHERE amount = '5'", id="number-string"),
        pytest.param("SELECT id FROM customers WHERE created > '2024-01-01'", id="date-string"),
        pytest.param("SELECT id FROM customers WHERE amount = id + 0", id="arithmetic"),
        pytest.param("SELECT id FROM customers WHERE -amount > 0", id="negated-column"),
        pytest.param("SELECT id FROM customers WHERE loose = loose", id="two-untyped-columns"),
        pytest.param(
            "SELECT a.id FROM customers AS a JOIN (VALUES (5, 6)) AS v(x, y) ON v.x = v.y",
            id="two-values-columns",
        ),
        pytest.param(
            "SELECT a.id FROM customers AS a JOIN (VALUES (5, 6)) AS v(x, y) ON v.x IN (v.y)",
            id="values-columns-in",
        ),
        pytest.param(
            "SELECT a.id FROM customers AS a JOIN (VALUES (5, 6)) AS v(x, y) "
            "ON v.x BETWEEN v.y AND v.y",
            id="values-columns-between",
        ),
    ],
)
@pytest.mark.parametrize("dialect", _ENGINES)
def test_a_comparison_the_gateway_cannot_type_is_refused(sql, dialect):
    with pytest.raises(query_compiler.QueryValidationError, match=_REFUSAL):
        _compile(sql, dialect)


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "SELECT id FROM customers WHERE note IN (FROM (VALUES (5)) v)", id="in-values"
        ),
        pytest.param(
            "SELECT id FROM customers WHERE EXISTS (FROM (VALUES (5)) v(x) WHERE x = note)",
            id="exists-values",
        ),
        pytest.param(
            "SELECT * FROM (FROM customers SELECT note, amount) AS d(amount, note) "
            "WHERE amount = 5",
            id="derived-alias-renames",
        ),
        pytest.param(
            "SELECT * FROM (WITH c AS (FROM customers SELECT note AS amount) FROM c "
            "WHERE amount = 5) AS z",
            id="cte-renames",
        ),
        pytest.param("SELECT * FROM customers AS a NATURAL JOIN customers AS b", id="natural"),
        pytest.param("SELECT id FROM customers WHERE note IN (FROM customers)", id="in-star"),
    ],
)
def test_what_a_subquery_or_a_renaming_returns_is_typed_or_refused(sql):
    with pytest.raises(query_compiler.QueryValidationError, match=_REFUSAL):
        _fence(sql)


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "SELECT count(*) FROM customers AS c(a1, a2, created) "
            "WHERE c.created = DATE '2024-01-01'",
            id="exposed-table",
        ),
        pytest.param(
            "SELECT * FROM (WITH x AS (FROM customers) FROM x AS y(p, q) WHERE q = 'EU') AS z",
            id="cte-reference",
        ),
    ],
)
@pytest.mark.parametrize("dialect", ["duckdb", "snowflake"])
def test_an_alias_column_list_on_a_table_is_refused(sql, dialect):
    """It renames the columns by position, so a name no longer has its declared type."""
    if "WITH" in sql and dialect != "duckdb":
        pytest.skip("a FROM-first CTE is DuckDB syntax")
    with pytest.raises(query_compiler.QueryValidationError, match="alias column list"):
        _fence(sql, dialect)


def test_a_window_alias_is_a_kept_value_in_qualify():
    _compile(
        "SELECT id, row_number() OVER (PARTITION BY region ORDER BY id) AS r "
        "FROM customers QUALIFY r = 1"
    )


def test_an_alias_that_shadows_a_column_is_still_typed():
    with pytest.raises(query_compiler.QueryValidationError, match=_REFUSAL):
        _compile(
            "SELECT amount, count(*) AS amount FROM customers GROUP BY amount "
            "HAVING amount = '5'"
        )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM customers WHERE amount > 5",
        "SELECT id FROM customers WHERE created > DATE '2024-01-01'",
        "SELECT id FROM customers WHERE created > CURRENT_DATE - 7",
        "SELECT id FROM customers WHERE id = 5 AND note <> 'x'",
        "SELECT id FROM customers WHERE (id, note) IN ((1, 'x'))",
        "SELECT a.id FROM customers AS a JOIN customers AS b USING (id)",
        "SELECT a.id FROM customers AS a JOIN customers AS b ON a.amount = b.amount",
        "SELECT region, count(*) FROM customers GROUP BY region HAVING count(*) > 1",
        "SELECT * FROM (FROM customers) AS d WHERE d.amount > 5",
        "SELECT * FROM (FROM customers SELECT id, note) AS d(i, n) WHERE n = 'x'",
        "SELECT id FROM customers WHERE id IN (FROM customers SELECT amount)",
    ],
)
def test_typed_comparisons_are_allowed(sql):
    _fence(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id FROM customers WHERE id = ANY(ARRAY[1, 2])",
        "SELECT id FROM customers WHERE note <> ALL(ARRAY['x'])",
        "SELECT id FROM customers WHERE amount >= 5::bigint",
    ],
)
def test_typed_postgres_comparisons_are_allowed(sql):
    _compile(sql, "postgres")


# ---------------------------------------------------------------------
# LIKE and ILIKE
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dialect", "where"),
    [
        pytest.param("duckdb", "note LIKE 'HIDDEN-A!' ESCAPE '!'", id="escape-duckdb"),
        pytest.param("postgres", "note LIKE 'HIDDEN-A!' ESCAPE '!'", id="escape-postgres"),
        pytest.param("postgres", "note ILIKE 'x!' ESCAPE '!'", id="ilike-escape"),
        pytest.param("duckdb", "note LIKE region", id="column-pattern"),
        pytest.param("duckdb", "note LIKE 'a' || '%'", id="expression-pattern"),
        pytest.param("postgres", "note LIKE ANY (ARRAY['a%'])", id="any-pattern"),
        pytest.param("duckdb", "amount LIKE '1%'", id="number-subject"),
    ],
)
def test_a_like_an_engine_could_fail_on_is_refused(dialect, where):
    with pytest.raises(query_compiler.QueryValidationError, match="LIKE"):
        _compile(f"SELECT id FROM customers WHERE {where}", dialect)


@pytest.mark.parametrize("dialect", _ENGINES)
def test_a_backslash_in_a_like_pattern_is_refused(dialect):
    """Postgres's default escape is the backslash; the statement checks refuse the
    character before the fence, which refuses it in a pattern on its own too."""
    sql = "SELECT id FROM customers WHERE note LIKE 'a\\%'"
    with pytest.raises(ValueError):
        _compile(sql, dialect)
    with pytest.raises(query_compiler.QueryValidationError, match="LIKE"):
        _fence(sql, dialect)


@pytest.mark.parametrize(
    "where", ["note LIKE 'S%'", "note NOT LIKE '%x%'", "note ILIKE 's%'", "lower(note) LIKE 'a%'"]
)
@pytest.mark.parametrize("dialect", _ENGINES)
def test_a_like_with_a_plain_pattern_is_allowed(where, dialect):
    _compile(f"SELECT id FROM customers WHERE {where}", dialect)


# ---------------------------------------------------------------------
# lower, upper and coalesce; shapes that stay refused
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "where",
    [
        "lower(note) = 'x'",
        "upper(region) IN ('EU', 'US')",
        "coalesce(amount, 0) > 5",
        "coalesce(note, region) = 'x'",
        "lower('X') = note",
    ],
)
@pytest.mark.parametrize("dialect", _ENGINES)
def test_lower_upper_and_coalesce_over_typed_values_are_allowed(where, dialect):
    _compile(f"SELECT id FROM customers WHERE {where}", dialect)


@pytest.mark.parametrize(
    ("dialect", "where"),
    [
        pytest.param("duckdb", "lower(amount) = '1'", id="lower-of-a-number"),
        pytest.param("duckdb", "coalesce(amount, 'x') = 'x'", id="coalesce-mixed"),
        pytest.param("duckdb", "coalesce(amount + 1, 0) > 5", id="coalesce-of-arithmetic"),
        pytest.param("duckdb", "coalesce(loose, 0) > 5", id="coalesce-of-untyped"),
        pytest.param("duckdb", "amount + 1 > 5", id="arithmetic"),
        pytest.param("postgres", "note COLLATE \"C\" = 'x'", id="collate"),
        pytest.param("postgres", "note SIMILAR TO 'a%'", id="similar-to"),
        pytest.param("postgres", "ROW(id, amount) = ROW(1, 2)", id="row-constructor"),
    ],
)
def test_other_shapes_stay_refused(dialect, where):
    """Refused by the condition fence, or before it by the function list."""
    with pytest.raises(query_compiler.QueryValidationError, match=f"{_REFUSAL}|which query_sql"):
        _compile(f"SELECT id FROM customers WHERE {where}", dialect)


# ---------------------------------------------------------------------
# Function names, generators, qualifiers
# ---------------------------------------------------------------------


def test_a_quoted_function_name_is_compared_as_written():
    """Postgres resolves ``"LOWER"`` to another function than ``lower``."""
    with pytest.raises(query_compiler.QueryValidationError, match='calls "LOWER",'):
        _compile('SELECT "LOWER"(note) FROM customers', "postgres")
    _compile('SELECT "lower"(note) FROM customers', "postgres")


@pytest.mark.parametrize(
    "call", ["version()", "current_database()", "gen_random_uuid()", "current_schema()", "uuid()"]
)
def test_a_typed_function_is_refused_by_the_name_the_caller_wrote(call):
    name = call.split("(")[0]
    with pytest.raises(query_compiler.QueryValidationError, match=f"calls {name},"):
        _compile(f"SELECT {call} FROM customers", "postgres")


@pytest.mark.parametrize(
    ("dialect", "call"),
    [
        ("postgres", "generate_series(1, 3)"),
        ("duckdb", "range(1, 3)"),
        ("duckdb", "repeat(note, 3)"),
        ("postgres", "lpad(note, 5)"),
        ("duckdb", "rpad(note, 5)"),
    ],
)
def test_row_and_string_generators_are_not_listed(dialect, call):
    with pytest.raises(query_compiler.QueryValidationError, match="calls"):
        _compile(f"SELECT {call} FROM customers", dialect)


def test_the_qualifier_refusal_names_the_spellings_it_accepts():
    """In-memory DuckDB: ``memory.customers`` names a schema called memory."""
    with pytest.raises(
        query_compiler.QueryValidationError,
        match="customers or main.customers or memory.main.customers",
    ):
        _compile("SELECT id FROM memory.customers")


# ---------------------------------------------------------------------
# Exact numbers, floats, dates and timestamps
# ---------------------------------------------------------------------

# The table's own types, as DuckDB's catalog writes them.
_HELD = {
    "id": "INTEGER",
    "region": "VARCHAR",
    "note": "VARCHAR",
    "big": "DECIMAL(38,0)",
    "price": "DECIMAL(10,2)",
    "fine": "DECIMAL(20,18)",
    "huge": "HUGEINT",
    "uhuge": "UHUGEINT",
    "ratio": "DOUBLE",
    "created": "DATE",
    "seen": "TIMESTAMP",
    "stamped": "TIMESTAMP WITH TIME ZONE",
    # Types the gateway does not type: their columns have no type, declared or not.
    "flags": "BIT",
    "nanos": "TIMESTAMP_NS",
}
# The same, as Postgres's pg_catalog.format_type writes them.
_PG_HELD = {
    "id": "integer",
    "region": "text",
    "amount": "numeric(10,2)",
    "total": "numeric",
    "ratio": "double precision",
    "created": "date",
    "seen": "timestamp without time zone",
    "stamped": "timestamp with time zone",
}


def _compile_held(
    where: str,
    held: Dict[str, str] = _HELD,
    dialect: str = "duckdb",
    declared: Optional[Dict[str, str]] = None,
):
    """Compile ``SELECT id FROM customers WHERE <where>`` for a filtered caller, the
    table holding the columns ``held`` (name: the engine's type) and ``contract.schema``
    declaring them with the types in ``declared``, untyped when it names none."""
    schema = [(name, (declared or {}).get(name)) for name in held]
    types = {name: query_compiler.column_type(text, dialect) for name, text in held.items()}
    return query_compiler.compile_free_form_sql(
        sql=f"SELECT id FROM customers WHERE {where}",
        table_reference="customers",
        limit=10,
        expose=_expose(schema),
        caller_attributes={"principal": EU},
        dialect=dialect,
        physical_types=lambda: types,
    )


_PG = (_PG_HELD, "postgres")
_DUCK = (_HELD, "duckdb")


@pytest.mark.parametrize(
    ("where", "held", "dialect"),
    [
        # Exact numbers within 38 digits, and a float against any number.
        pytest.param("big = 5", *_DUCK, id="38-digits-and-an-integer"),
        pytest.param("big = id", *_DUCK, id="38-digits-and-an-int-column"),
        pytest.param("price > 0.5", *_DUCK, id="decimal-and-a-decimal-literal"),
        pytest.param("price BETWEEN 1 AND 9.99", *_DUCK, id="between"),
        pytest.param("coalesce(price, 0) > 1.25", *_DUCK, id="coalesce"),
        pytest.param("id IN (1, 2, 3)", *_DUCK, id="in-list"),
        pytest.param("(id, price) IN ((1, 2.5))", *_DUCK, id="tuple"),
        pytest.param("price = ANY(ARRAY[1, 2.5])", *_DUCK, id="any-array"),
        pytest.param("ratio > 0.5", *_DUCK, id="float-column"),
        pytest.param("big > 1.5e0", *_DUCK, id="float-literal"),
        pytest.param("huge > CAST(5 AS DOUBLE)", *_DUCK, id="float-cast"),
        # Integers meet integers by widening, never through a DECIMAL.
        pytest.param("huge = 5", *_DUCK, id="hugeint-and-an-integer-literal"),
        pytest.param("huge = id", *_DUCK, id="hugeint-and-an-int-column"),
        pytest.param(f"huge = {'9' * 38}", *_DUCK, id="hugeint-and-a-38-digit-literal"),
        pytest.param("id > 1e-30", *_DUCK, id="exponent-literal-is-a-float-on-duckdb"),
        pytest.param("amount > 5.25", *_PG, id="postgres-numeric-10-2"),
        pytest.param("id = amount", *_PG, id="postgres-int-and-numeric"),
        pytest.param("ratio > 1.5e0", *_PG, id="postgres-float-column"),
        pytest.param("total > CAST(5 AS DOUBLE PRECISION)", *_PG, id="postgres-float-cast"),
        # A date, a timestamp and a timestamp with a time zone each meet their own kind.
        pytest.param("created = DATE '2024-01-01'", *_DUCK, id="date-literal"),
        pytest.param(
            "coalesce(created, DATE '2024-01-01') = DATE '2024-01-02'", *_DUCK, id="date-coalesce"
        ),
        pytest.param("created > CURRENT_DATE - 7", *_DUCK, id="current-date-minus-days"),
        pytest.param(
            "created > CAST(CURRENT_DATE - INTERVAL '7' DAY AS DATE)", *_DUCK, id="relative-date"
        ),
        pytest.param(
            "created > CAST(CURRENT_DATE - INTERVAL '7' DAY AS DATE)",
            *_PG,
            id="postgres-relative-date",
        ),
        pytest.param(
            "created BETWEEN DATE '2024-01-01' AND CURRENT_DATE", *_DUCK, id="date-between"
        ),
        pytest.param("seen > TIMESTAMP '2024-01-01 00:00:00'", *_DUCK, id="timestamp-literal"),
        pytest.param(
            "seen > TIMESTAMP '2024-01-01 00:00:00' - INTERVAL '1' DAY",
            *_DUCK,
            id="timestamp-minus-interval",
        ),
        pytest.param("stamped > now() - INTERVAL '1' DAY", *_DUCK, id="now-minus-interval"),
        pytest.param("stamped < CURRENT_TIMESTAMP", *_DUCK, id="current-timestamp"),
        pytest.param(
            "stamped > TIMESTAMPTZ '2024-01-01 00:00:00+00'", *_DUCK, id="timestamptz-literal"
        ),
        pytest.param("created = CURRENT_DATE", *_PG, id="postgres-current-date"),
        pytest.param("stamped > now()", *_PG, id="postgres-now"),
    ],
)
def test_comparisons_the_type_rules_allow(where, held, dialect):
    _compile_held(where, held, dialect)


@pytest.mark.parametrize(
    ("where", "held", "dialect", "match"),
    [
        # Exact numbers whose common type needs more than 38 digits.
        pytest.param("big = 0.5", *_DUCK, "38 digits", id="38-digits-and-a-decimal"),
        pytest.param("big = price", *_DUCK, "38 digits", id="38-digits-and-a-scale-of-2"),
        pytest.param("big = fine", *_DUCK, "38 digits", id="38-digits-and-a-scale-of-18"),
        pytest.param("huge = big", *_DUCK, "38 digits", id="hugeint-and-decimal-38-0"),
        pytest.param("huge = 0.5", *_DUCK, "38 digits", id="hugeint-and-a-decimal"),
        pytest.param("uhuge = id", *_DUCK, "38 digits", id="uhugeint-and-an-int-column"),
        pytest.param("uhuge = -5", *_DUCK, "38 digits", id="uhugeint-and-a-negative-integer"),
        pytest.param(
            "id = 1e-30",
            {"id": "NUMBER(38,0)", "region": "TEXT"},
            "snowflake",
            "38 digits",
            id="snowflake-exponent-literal-is-exact",
        ),
        pytest.param("coalesce(big, 0.5) > 1", *_DUCK, "38 digits", id="coalesce"),
        pytest.param("big IN (1, 0.5)", *_DUCK, "38 digits", id="in-list"),
        pytest.param("big BETWEEN 0.5 AND 1", *_DUCK, "38 digits", id="between"),
        pytest.param("(big, id) IN ((0.5, 1))", *_DUCK, "38 digits", id="tuple"),
        pytest.param("big = ANY(ARRAY[1, 0.5])", *_DUCK, "38 digits", id="any-array"),
        # Two values neither of which has a type the gateway knows.
        pytest.param("flags = nanos", *_DUCK, "cannot type", id="two-untyped-columns"),
        pytest.param("flags = flags", *_DUCK, "cannot type", id="an-untyped-column-twice"),
        pytest.param("flags IN (nanos)", *_DUCK, "cannot type", id="untyped-in"),
        pytest.param("flags BETWEEN nanos AND nanos", *_DUCK, "cannot type", id="untyped-between"),
        pytest.param(
            "coalesce(flags, nanos) IS NULL", *_DUCK, "cannot type", id="untyped-coalesce"
        ),
        pytest.param(f"id = {'1' * 38}.5", *_DUCK, "38 digits", id="a-39-digit-literal"),
        # An exact number of unknown precision: Postgres's unconstrained numeric.
        pytest.param("total > 5", *_PG, "does not know", id="unconstrained-numeric"),
        pytest.param("total = amount", *_PG, "does not know", id="unconstrained-and-typed"),
        pytest.param("total > 1.5e0", *_PG, "does not know", id="postgres-scientific-is-exact"),
        # Dates and timestamps of different kinds.
        pytest.param("created < now()", *_DUCK, "different types", id="date-and-now"),
        pytest.param(
            "created < CURRENT_TIMESTAMP", *_DUCK, "different types", id="date-and-timestamptz"
        ),
        pytest.param(
            "created = TIMESTAMP '2024-01-01 00:00:00'",
            *_DUCK,
            "different types",
            id="date-and-timestamp",
        ),
        pytest.param("seen < now()", *_DUCK, "different types", id="timestamp-and-timestamptz"),
        pytest.param(
            "stamped > TIMESTAMP '2024-01-01 00:00:00'",
            *_DUCK,
            "different types",
            id="timestamptz-and-timestamp",
        ),
        pytest.param("created > '2024-01-01'", *_DUCK, "different types", id="date-and-string"),
        pytest.param(
            "coalesce(created, now()) IS NULL", *_DUCK, "different types", id="date-coalesce"
        ),
        pytest.param(
            "created IN (DATE '2024-01-01', TIMESTAMP '2024-01-01 00:00:00')",
            *_DUCK,
            "different types",
            id="in-list",
        ),
        pytest.param(
            "created > CURRENT_DATE - INTERVAL '7' DAY",
            *_DUCK,
            "cannot type",
            id="date-minus-interval",
        ),
        pytest.param("seen < now()", *_PG, "different types", id="postgres-timestamp-and-now"),
        pytest.param("created < now()", *_PG, "different types", id="postgres-date-and-now"),
    ],
)
def test_comparisons_the_type_rules_refuse(where, held, dialect, match):
    with pytest.raises(query_compiler.QueryValidationError, match=match):
        _compile_held(where, held, dialect)


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "athena", "bigquery", "snowflake"])
def test_a_relative_date_written_as_a_cast_is_a_date_on_every_engine(dialect):
    """Engines differ on the type of a date minus an interval; its cast to DATE has one."""
    held = {"id": "INTEGER", "region": "VARCHAR", "created": "DATE"}
    _compile_held("created > CAST(CURRENT_DATE - INTERVAL '7' DAY AS DATE)", held, dialect)


@pytest.mark.parametrize(
    ("text", "dialect", "expected"),
    [
        ("0.5", "duckdb", ColumnType("number", 2, 1)),
        ("123.450", "duckdb", ColumnType("number", 6, 3)),
        ("5", "duckdb", ColumnType("number", 1, 0, True)),
        ("5.", "duckdb", ColumnType("number", 1, 0)),
        (f"{'9' * 38}", "duckdb", ColumnType("number", 38, 0, True)),
        (f"{'9' * 39}", "duckdb", ColumnType("number", 39, 0)),
        ("1.5e0", "duckdb", ColumnType("float")),
        ("1.5e0", "athena", ColumnType("float")),
        ("1.5e0", "bigquery", ColumnType("float")),
        ("1e3", "postgres", ColumnType("number", 4, 0)),
        # Not documented as a float there: an exact number of its digits.
        ("1e-30", "snowflake", ColumnType("number", 31, 30)),
    ],
)
def test_a_numeric_literal_is_typed_by_its_digits(text, dialect, expected):
    import sqlglot

    read = query_compiler._SQLGLOT_DIALECTS[dialect]
    literal = sqlglot.parse_one(text, read=read)
    assert query_compiler._literal_type(literal, read) == expected


# ---------------------------------------------------------------------
# HAVING and QUALIFY
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "match"),
    [
        pytest.param(
            "SELECT note FROM customers GROUP BY note HAVING note IN (5, max(id))",
            "different types",
            id="in-list-with-an-aggregate",
        ),
        pytest.param(
            "SELECT note FROM customers GROUP BY note HAVING count(*) BETWEEN 1 AND 'x'",
            "different types",
            id="between-with-an-aggregate",
        ),
        pytest.param(
            "SELECT note AS amount FROM customers GROUP BY note HAVING amount = 5",
            "cannot type",
            id="alias-of-another-type",
        ),
        pytest.param(
            "SELECT upper(note) AS amount FROM customers GROUP BY 1 HAVING amount = 5",
            "cannot type",
            id="alias-of-a-computed-value",
        ),
    ],
)
def test_the_values_beside_an_aggregate_are_typed(sql, match):
    """An aggregate's value is one of the rows the filter keeps; the values beside it
    are typed. DuckDB reads ``amount`` in HAVING as the alias when the column
    ``amount`` is not grouped, so such a name is typed only when both readings agree."""
    with pytest.raises(query_compiler.QueryValidationError, match=match):
        _compile(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT note FROM customers GROUP BY note HAVING note IN ('a', max(note))",
        "SELECT note FROM customers GROUP BY note HAVING count(*) > 1",
        "SELECT note FROM customers GROUP BY note HAVING max(amount) > 1 AND note <> 'x'",
    ],
)
def test_a_comparison_with_an_aggregate_of_one_type_is_allowed(sql):
    _compile(sql)


# ---------------------------------------------------------------------
# The table's own types
# ---------------------------------------------------------------------


def test_the_tables_types_type_an_untyped_contract_schema():
    """Without a type in contract.schema, the table's catalog types the column."""
    _compile_held("price > 5 AND created = DATE '2024-01-01' AND note LIKE 'a%'")
    untyped = [(name, None) for name, _ in _SCHEMA]
    with pytest.raises(query_compiler.QueryValidationError, match="cannot type"):
        query_compiler.compile_free_form_sql(
            sql="SELECT id FROM customers WHERE amount > 5",
            table_reference="customers",
            limit=10,
            expose=_expose(untyped),
            caller_attributes={"principal": EU},
            dialect="duckdb",
        )


def test_the_tables_type_is_the_one_compared():
    """A declared ``date`` agrees with the table's TIMESTAMP (both are dates and times),
    and the column is compared as the timestamp the table holds."""
    held = {**_HELD, "created": "TIMESTAMP"}
    _compile_held("created > TIMESTAMP '2024-01-01 00:00:00'", held, declared={"created": "date"})
    with pytest.raises(query_compiler.QueryValidationError, match="different types"):
        _compile_held("created = DATE '2024-01-01'", held, declared={"created": "date"})


def test_a_column_the_gateway_cannot_type_in_the_table_stays_untyped():
    """The declaration could not be checked against a type the gateway does not type,
    so it does not type the column."""
    held = {**_HELD, "price": "BIT"}
    with pytest.raises(query_compiler.QueryValidationError, match="cannot type"):
        _compile_held("price > 5", held, declared={"price": "integer"})
    _compile_held("id > 5", held, declared={"price": "integer"})


@pytest.fixture()
def state_for(tmp_path):
    """A ``SessionState`` stand-in over a DuckDB driver reading a typed CSV."""
    from ._fixtures import duckdb_csv_session

    csv = tmp_path / "customers.csv"
    csv.write_text(
        "id,region,note,amount,active,created\n"
        "1,EU,first,10,true,2024-01-02\n"
        "2,US,second,20,false,2024-02-03\n"
        "3,EU,third,30,true,2024-03-04\n",
        encoding="utf-8",
    )

    return lambda schema: duckdb_csv_session(csv, _expose(schema), table="customers")


_CSV_SCHEMA: List[Tuple[str, Optional[str]]] = [
    ("id", "integer"),
    ("region", "string"),
    ("note", "string"),
    ("amount", "bigint"),
    ("active", "boolean"),
    ("created", "date"),
]


def _ids(state, sql: str, caller: str = EU) -> List[int]:
    payload = tool_query_sql(state, {"sql": sql}, caller_attributes={"principal": caller})
    return sorted(int(row["id"]) for row in payload["rows"])


def test_declared_types_that_match_the_table_serve_a_filtered_caller(state_for):
    state = state_for(_CSV_SCHEMA)
    assert _ids(state, "SELECT id FROM customers WHERE amount > 5 AND active") == [1, 3]
    assert _ids(state, "SELECT id FROM customers WHERE created > DATE '2024-01-31'") == [3]
    assert _ids(state, "SELECT id FROM customers WHERE created > CURRENT_DATE - 100000") == [1, 3]
    relative = "CAST(CURRENT_DATE - INTERVAL '100000' DAY AS DATE)"
    assert _ids(state, f"SELECT id FROM customers WHERE created > {relative}") == [1, 3]


def test_an_untyped_schema_serves_a_filtered_caller_by_the_tables_types(state_for):
    state = state_for([(name, None) for name, _ in _CSV_SCHEMA])
    assert _ids(state, "SELECT id FROM customers WHERE amount > 15 AND active") == [3]


def test_a_declared_type_the_table_does_not_hold_refuses_a_filtered_caller(state_for):
    schema = [(name, "string" if name == "amount" else kind) for name, kind in _CSV_SCHEMA]
    state = state_for(schema)
    with pytest.raises(query_compiler.RowFilterUnavailable, match="'amount' as a string"):
        _ids(state, "SELECT id FROM customers WHERE note = 'first'")
    # A reader's statement is not typed, so the mismatch does not stop it.
    assert _ids(state, "SELECT id FROM customers", caller=READER) == [1, 2, 3]


def test_a_declared_column_the_table_lacks_refuses_a_filtered_caller(state_for):
    state = state_for([*_CSV_SCHEMA, ("missing", "integer")])
    with pytest.raises(query_compiler.RowFilterUnavailable, match="no such column"):
        _ids(state, "SELECT id FROM customers")


def _count_catalog_reads(driver: Any, fail: Optional[List[bool]] = None) -> List[str]:
    """Wrap ``driver.execute`` to record each catalog read, failing them while
    ``fail[0]`` is true."""
    seen: List[str] = []
    execute = driver.execute

    def counting(**kwargs: Any):
        if "information_schema.columns" in kwargs["sql"]:
            seen.append(kwargs["sql"])
            if fail and fail[0]:
                raise RuntimeError("catalog unavailable")
        return execute(**kwargs)

    driver.execute = counting
    return seen


def test_the_table_types_are_read_again_after_a_few_minutes(state_for, monkeypatch):
    from fluid_build.output_ports.mcp.drivers import base

    assert base.PHYSICAL_TYPES_TTL_SECONDS == 300.0
    clock = [1000.0]
    monkeypatch.setattr(base, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    state = state_for(_CSV_SCHEMA)
    fail = [False]
    seen = _count_catalog_reads(state.get_driver(), fail)
    for _ in range(3):
        _ids(state, "SELECT id FROM customers WHERE amount > 5")
    assert len(seen) == 1
    clock[0] += base.PHYSICAL_TYPES_TTL_SECONDS - 1
    _ids(state, "SELECT id FROM customers WHERE amount > 5")
    assert len(seen) == 1
    clock[0] += 2
    _ids(state, "SELECT id FROM customers WHERE amount > 5")
    assert len(seen) == 2
    # A read that fails once the types are stale refuses: the old types are not used.
    clock[0] += base.PHYSICAL_TYPES_TTL_SECONDS
    fail[0] = True
    with pytest.raises(query_compiler.RowFilterUnavailable, match="column types"):
        _ids(state, "SELECT id FROM customers WHERE amount > 5")
    with pytest.raises(query_compiler.RowFilterUnavailable, match="column types"):
        _ids(state, "SELECT id FROM customers WHERE amount > 5")


def test_conflicting_catalog_rows_leave_a_column_untyped(state_for):
    """Two rows for one name with different types (a same-named table elsewhere) make
    the column's type unknown: comparisons on it are refused, the rest served."""
    state = state_for(_CSV_SCHEMA)
    driver = state.get_driver()
    execute = driver.execute

    def doubled(**kwargs: Any):
        result = execute(**kwargs)
        if "information_schema" not in kwargs["sql"]:
            return result
        extra = {"column_name": "amount", "data_type": "VARCHAR"}
        return SimpleNamespace(columns=result.columns, rows=(*result.rows, extra))

    driver.execute = doubled
    assert _ids(state, "SELECT id FROM customers WHERE note = 'first'") == [1]
    with pytest.raises(query_compiler.QueryValidationError, match="cannot type"):
        _ids(state, "SELECT id FROM customers WHERE amount > 5")


def test_an_unreadable_catalog_refuses_a_filtered_caller(state_for):
    state = state_for(_CSV_SCHEMA)
    _count_catalog_reads(state.get_driver(), [True])
    with pytest.raises(query_compiler.RowFilterUnavailable, match="column types"):
        _ids(state, "SELECT id FROM customers")


def test_a_comparison_a_withheld_row_would_fail_is_refused_end_to_end(tmp_path):
    """DuckDB casts DECIMAL(38,0) and 0.5 to DECIMAL(38,1), and a 38-digit value does
    not fit: the comparison fails on a withheld row, and not without it. The fence
    refuses the statement; a comparison within 38 digits is served."""
    data = tmp_path / "customers.parquet"
    path = quote_string_literal(data.as_posix())
    values = f"(1, 'EU', 1::DECIMAL(38,0)), (2, 'US', {'9' * 38}::DECIMAL(38,0))"
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT * FROM (VALUES {values}) AS v(id, region, big)) TO {path} "
        "(FORMAT parquet)"
    )
    con.execute(f"CREATE TABLE eu AS SELECT * FROM read_parquet({path}) WHERE region = 'EU'")
    with pytest.raises(duckdb.Error):
        con.execute(f"SELECT id FROM read_parquet({path}) WHERE big = 0.5").fetchall()
    assert con.execute("SELECT id FROM eu WHERE big = 0.5").fetchall() == []

    from ._fixtures import duckdb_csv_session

    schema = [("id", "integer"), ("region", "string"), ("big", None)]
    state = duckdb_csv_session(data, _expose(schema), table="customers", file_format="parquet")
    with pytest.raises(query_compiler.QueryValidationError, match="38 digits"):
        _ids(state, "SELECT id FROM customers WHERE big = 0.5")
    assert _ids(state, "SELECT id FROM customers WHERE big = 1") == [1]
    assert _ids(state, "SELECT id FROM customers WHERE big > 0.5e0") == [1]


@pytest.mark.parametrize(
    ("dialect", "reference", "statement"),
    [
        (
            "duckdb",
            "customers",
            "SELECT column_name, data_type, numeric_precision, numeric_scale "
            "FROM information_schema.columns "
            "WHERE lower(table_name) = 'customers' AND table_schema = current_schema()",
        ),
        (
            "postgres",
            "appdb.public.users",
            "SELECT a.attname AS column_name, CASE "
            "WHEN t.typtype = 'e' OR t.typname = 'citext' THEN 'text' "
            "WHEN t.typtype = 'd' THEN pg_catalog.format_type(t.typbasetype, t.typtypmod) "
            "ELSE pg_catalog.format_type(a.atttypid, a.atttypmod) END AS data_type "
            "FROM pg_catalog.pg_attribute AS a "
            "JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid "
            "WHERE a.attrelid = pg_catalog.to_regclass('appdb.public.users') "
            "AND a.attnum > 0 AND NOT a.attisdropped",
        ),
        (
            "snowflake",
            "ANALYTICS.PUBLIC.ORDERS",
            "SELECT column_name, data_type, numeric_precision, numeric_scale "
            "FROM ANALYTICS.information_schema.columns "
            "WHERE lower(table_name) = 'orders' AND lower(table_schema) = 'public'",
        ),
        (
            "bigquery",
            "`proj-1.ds.orders`",
            "SELECT column_name, data_type FROM `proj-1`.`ds`.INFORMATION_SCHEMA.COLUMNS "
            "WHERE table_name = 'orders'",
        ),
        (
            "athena",
            '"sales"."orders"',
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE lower(table_name) = 'orders' AND lower(table_schema) = 'sales'",
        ),
    ],
)
def test_the_catalog_statement_each_engine_runs(dialect, reference, statement):
    assert query_compiler.catalog_columns_sql(reference, dialect) == statement


_NO_SIZE = (None, None)


@pytest.mark.parametrize(
    ("written", "dialect", "kind", "size"),
    [
        ("varchar(20)", "duckdb", "string", _NO_SIZE),
        ("character varying", "postgres", "string", _NO_SIZE),
        ("NUMBER(38,0)", "snowflake", "number", (38, 0)),
        ("DECIMAL(18,3)", "duckdb", "number", (18, 3)),
        ("numeric(10, 2)", "postgres", "number", (10, 2)),
        ("numeric", "postgres", "number", _NO_SIZE),
        ("NUMERIC", "bigquery", "number", (38, 9)),
        ("BIGNUMERIC", "bigquery", "number", (76, 38)),
        ("decimal", "athena", "number", (38, 0)),
        ("tinyint", "athena", "integer", (3, 0)),
        ("smallint", "postgres", "integer", (5, 0)),
        ("INTEGER", "duckdb", "integer", (10, 0)),
        ("INTEGER", "snowflake", "integer", (38, 0)),
        ("bigint", "postgres", "integer", (19, 0)),
        ("HUGEINT", "duckdb", "integer", (39, 0)),
        ("UBIGINT", "duckdb", "integer", (20, 0)),
        # DuckDB casts a UHUGEINT to the signed type it meets: compared as a DECIMAL.
        ("UHUGEINT", "duckdb", "number", (39, 0)),
        ("INT64", "bigquery", "integer", (19, 0)),
        ("LONG", "databricks", "integer", (19, 0)),
        ("SHORT", "databricks", "integer", (5, 0)),
        ("double precision", "postgres", "float", _NO_SIZE),
        ("FLOAT64", "bigquery", "float", _NO_SIZE),
        ("DATE", "duckdb", "date", _NO_SIZE),
        ("TIMESTAMP", "duckdb", "timestamp", _NO_SIZE),
        ("TIMESTAMP WITH TIME ZONE", "duckdb", "timestamptz", _NO_SIZE),
        ("timestamp(3) without time zone", "postgres", "timestamp", _NO_SIZE),
        ("timestamp(3) with time zone", "athena", "timestamptz", _NO_SIZE),
        ("TIMESTAMP", "bigquery", "timestamptz", _NO_SIZE),
        ("DATETIME", "bigquery", "timestamp", _NO_SIZE),
        ("TIMESTAMP_NTZ", "snowflake", "timestamp", _NO_SIZE),
        ("TIMESTAMP_LTZ", "snowflake", "timestamptz", _NO_SIZE),
        ("time with time zone", "postgres", "timetz", _NO_SIZE),
        ("BOOL", "bigquery", "boolean", _NO_SIZE),
        ("ARRAY<STRING>", "bigquery", "complex", _NO_SIZE),
        ("int[]", "postgres", "complex", _NO_SIZE),
        ("INTEGER[3]", "duckdb", "complex", _NO_SIZE),
        ("VARIANT", "snowflake", "complex", _NO_SIZE),
        ("jsonb", "postgres", "complex", _NO_SIZE),
        ("row(a integer)", "athena", "complex", _NO_SIZE),
        ("TIMESTAMP_NS", "duckdb", None, _NO_SIZE),
        ("USER-DEFINED", "postgres", None, _NO_SIZE),
        ("money", "postgres", None, _NO_SIZE),
        ("", "duckdb", None, _NO_SIZE),
    ],
)
def test_a_catalog_type_is_read_with_its_precision(written, dialect, kind, size):
    """``integer`` marks an integer type: DECIMAL(d, 0) that widens to a larger integer."""
    if kind is None:
        expected = None
    elif kind == "integer":
        expected = ColumnType("number", *size, True)
    else:
        expected = ColumnType(kind, *size)
    assert query_compiler.column_type(written, dialect) == expected


def test_a_catalog_gives_a_decimal_its_precision_apart():
    """Snowflake's catalog writes NUMBER and gives the precision and scale in columns."""
    assert query_compiler.column_type("NUMBER", "snowflake", 12, 4) == ColumnType("number", 12, 4)
    assert query_compiler.column_type("NUMBER", "snowflake", None, None) == ColumnType(
        "number", 38, 0
    )
