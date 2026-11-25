# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A filtered caller's conditions are typed, and refused where the gateway cannot type them.

An engine may evaluate a condition on a row the row filter removes. A value it casts or
matches row by row can raise an error on such a row, and the error tells the caller
that the row exists. So for a caller a row filter narrows, each WHERE, ON, USING,
HAVING and QUALIFY condition is checked:

* every condition, and each operand of AND, OR, NOT and IS TRUE / IS FALSE, is a
  predicate, a column ``contract.schema`` declares as a boolean, or TRUE or FALSE;
* every comparison relates values of one known scalar kind, the kinds coming from the
  declared types, which must be the table's own;
* LIKE and ILIKE take a string and a string literal pattern with no escape;
* ``lower``, ``upper`` and ``coalesce`` take typed columns and literals.

Each refused statement below is one the engine could fail on for a withheld row's value.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.output_ports.mcp import query_compiler  # noqa: E402
from fluid_build.output_ports.mcp._handlers import tool_query_sql  # noqa: E402

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
        column_kinds=query_compiler._declared_column_kinds(_expose()),
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
        "SELECT id FROM customers WHERE amount > 5",
        "SELECT id FROM customers WHERE created > DATE '2024-01-01'",
        "SELECT id FROM customers WHERE created > CURRENT_DATE - INTERVAL '7' DAY",
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
    with pytest.raises(query_compiler.QueryValidationError, match=_REFUSAL):
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
# Declared types against the table's own
# ---------------------------------------------------------------------


@pytest.fixture()
def state_for(tmp_path):
    """A ``SessionState`` stand-in over a DuckDB driver reading a typed CSV."""
    from fluid_build.output_ports.mcp.drivers import build_driver

    csv = tmp_path / "customers.csv"
    csv.write_text(
        "id,region,note,amount,active,created\n"
        "1,EU,first,10,true,2024-01-02\n"
        "2,US,second,20,false,2024-02-03\n"
        "3,EU,third,30,true,2024-03-04\n",
        encoding="utf-8",
    )

    def _make(schema: List[Tuple[str, Optional[str]]]) -> SimpleNamespace:
        expose = _expose(schema)
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


def test_the_table_types_are_read_once_per_driver(state_for):
    state = state_for(_CSV_SCHEMA)
    driver = state.get_driver()
    seen: List[str] = []
    execute = driver.execute

    def counting(**kwargs: Any):
        seen.append(kwargs["sql"])
        return execute(**kwargs)

    driver.execute = counting
    for _ in range(3):
        _ids(state, "SELECT id FROM customers WHERE amount > 5")
    assert sum("information_schema.columns" in sql for sql in seen) == 1


def test_an_unreadable_catalog_refuses_a_filtered_caller(state_for):
    state = state_for(_CSV_SCHEMA)
    driver = state.get_driver()
    execute = driver.execute

    def failing(**kwargs: Any):
        if "information_schema" in kwargs["sql"]:
            raise RuntimeError("catalog unavailable")
        return execute(**kwargs)

    driver.execute = failing
    with pytest.raises(query_compiler.RowFilterUnavailable, match="column types"):
        _ids(state, "SELECT id FROM customers")


@pytest.mark.parametrize(
    ("dialect", "reference", "statement"),
    [
        (
            "duckdb",
            "customers",
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE lower(table_name) = 'customers'",
        ),
        (
            "postgres",
            "appdb.public.users",
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE lower(table_name) = 'users' AND lower(table_schema) = 'public'",
        ),
        (
            "snowflake",
            "ANALYTICS.PUBLIC.ORDERS",
            "SELECT column_name, data_type FROM ANALYTICS.information_schema.columns "
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


@pytest.mark.parametrize(
    ("written", "kind"),
    [
        ("varchar(20)", "string"),
        ("character varying", "string"),
        ("NUMBER(38,0)", "number"),
        ("double precision", "number"),
        ("timestamp with time zone", "temporal"),
        ("TIMESTAMP_NTZ", "temporal"),
        ("BOOL", "boolean"),
        ("ARRAY<STRING>", "complex"),
        ("int[]", "complex"),
        ("VARIANT", "complex"),
        ("jsonb", "complex"),
        ("row(a integer)", "complex"),
        ("USER-DEFINED", None),
        ("", None),
    ],
)
def test_a_type_is_read_as_its_kind(written, kind):
    assert query_compiler.kind_of_type(written) == kind
