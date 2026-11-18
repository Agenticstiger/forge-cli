# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The six SQL renderings of FLUID SQL/1 (``RFC-data-rules.md`` §2.8).

``test_duckdb_oracle.py`` runs the DuckDB rendering against the vectors. This file
covers what that cannot reach, because the other five engines are not installed:

- every accepted vector renders in every dialect, as a value and as a predicate, and
  the SQL parses with sqlglot in that dialect (a syntax check, not a semantics one);
- the generic forms that DuckDB does not itself use (the ``EXTRACT`` month and year
  difference, whole months divided by 12) are forced on in DuckDB and compared with
  the exact evaluators over a grid of dates; so is the ``CASE`` form of half-to-even;
- a deep text prints bounded SQL (a form that repeats its operand never blows up);
- the per-dialect spellings: quoting, collation, exact types and day differences.
"""

from __future__ import annotations

import datetime
import sys
import time
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

import pytest

from fluid_build import fluid_sql as fs
from fluid_build.fluid_sql import _nodes as n
from fluid_build.fluid_sql._render import _Renderer
from fluid_build.fluid_sql._types import Column, Model, column_type
from fluid_build.fluid_sql._vectors import build_models, load_vectors

pytestmark = pytest.mark.unit

sqlglot = pytest.importorskip("sqlglot")

DOCUMENT = load_vectors()
MODELS = build_models(DOCUMENT)
ACCEPTED = [v for v in DOCUMENT["vectors"] if v["expect"]["verdict"] == "accepted"]

#: FLUID dialect name -> sqlglot dialect name. Athena's queries run on Trino, and sqlglot's own
#: "athena" dialect tokenizes a SELECT with Hive's backslash escapes (it fails on ``'C:\\'``, valid
#: in Trino), so Athena is read as "trino": a sqlglot gap, not ours.
SQLGLOT_DIALECT = {
    "duckdb": "duckdb",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "databricks": "databricks",
    "postgres": "postgres",
    "athena": "trino",
}

#: Vector ids sqlglot cannot parse for one dialect, with why. Each one was checked by
#: hand: the SQL is valid in the engine, the gap is in sqlglot's grammar.
SQLGLOT_GAPS: Dict[Tuple[str, str], str] = {}


def model_of(columns: Dict[str, str]) -> Model:
    return Model("m", {name: Column(name, t, column_type(t)) for name, t in columns.items()})


def compile_text(text: str, columns: Dict[str, str], site: str = fs.VALUE) -> Tuple[Any, Any]:
    tree = fs.parse(text)
    return tree, fs.check(tree, model_of(columns), site)


def render(text: str, columns: Dict[str, str], dialect: str, site: str = fs.VALUE) -> str:
    tree, checked = compile_text(text, columns, site)
    return fs.render_sql(
        tree, checked, fs.column_reader(dialect, "t"), dialect, predicate=site == fs.PREDICATE
    )


def parses(sql: str, dialect: str) -> None:
    # sqlglot's parser recurses once per nesting level (several frames each), and the vectors
    # nest 64 deep: give it room instead of reading its RecursionError as our bug.
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 20_000))
    try:
        sqlglot.parse_one(f"SELECT {sql} AS r FROM tbl AS t", read=SQLGLOT_DIALECT[dialect])
    finally:
        sys.setrecursionlimit(limit)


# -- 1. every accepted vector, every dialect -----------------------------------------------


def _vector_sql(vector: Dict[str, Any], dialect: str, predicate: bool) -> str:
    model = MODELS[vector["model"]]
    derive = model.columns[vector["derive"]] if vector.get("derive") else None
    tree = fs.parse(vector["text"])
    checked = fs.check(tree, model, vector["site"], derive)
    return fs.render_sql(tree, checked, fs.column_reader(dialect, "t"), dialect, predicate)


@pytest.mark.parametrize("dialect", fs.DIALECTS)
@pytest.mark.parametrize("vector", ACCEPTED, ids=[v["id"] for v in ACCEPTED])
def test_every_accepted_vector_renders_and_parses(vector: Dict[str, Any], dialect: str) -> None:
    gap = SQLGLOT_GAPS.get((vector["id"], dialect))
    if gap:
        pytest.xfail(f"sqlglot gap: {gap}")
    modes = [vector["site"] == fs.PREDICATE]
    if vector["site"] == fs.PREDICATE:
        modes.append(False)  # a predicate is also a value (a bool)
    for predicate in modes:
        sql = _vector_sql(vector, dialect, predicate)
        assert sql and "\n" not in sql
        parses(sql, dialect)


def test_there_are_enough_vectors_to_mean_something() -> None:
    assert len(ACCEPTED) >= 100


# -- 2. the generic month / year difference, run in DuckDB ----------------------------------


class _GenericDuckDb(_Renderer):
    """DuckDB with the EXTRACT form of a month and year difference forced on, and whole
    months divided by 12 as in the other engines' ``div12`` (never negative here)."""

    def __init__(self, checked: Any, column_sql: Any) -> None:
        super().__init__("duckdb", checked, column_sql)
        self.whole_units = False

    def div12(self, months: str) -> str:
        return f"({months} // 12)"


def _generic_sql(text: str, columns: Dict[str, str], site: str = fs.VALUE) -> str:
    tree, checked = compile_text(text, columns, site)
    renderer = _GenericDuckDb(checked, fs.column_reader("duckdb", "t"))
    sql = renderer.value(tree)
    assert "date_sub" not in sql
    return sql


def _dates() -> List[datetime.date]:
    days = {
        (1900, 2, 28),
        (1900, 3, 1),
        (1999, 12, 31),
        (2000, 2, 29),
        (2000, 3, 31),
        (2023, 1, 31),
        (2023, 2, 28),
        (2023, 3, 31),
        (2023, 4, 30),
        (2023, 5, 31),
        (2023, 6, 15),
        (2023, 12, 31),
        (2024, 1, 1),
        (2024, 1, 30),
        (2024, 1, 31),
        (2024, 2, 28),
        (2024, 2, 29),
        (2024, 3, 1),
        (2024, 3, 29),
        (2024, 3, 31),
        (2024, 4, 30),
        (2024, 5, 31),
        (2024, 6, 30),
        (2024, 12, 31),
        (2025, 1, 31),
        (2025, 2, 28),
        (2025, 3, 1),
        (2025, 3, 31),
        (2025, 4, 30),
        (2025, 12, 31),
        (2026, 1, 1),
        (2026, 1, 30),
        (2026, 2, 28),
        (2026, 3, 1),
        (2026, 3, 31),
        (2028, 2, 28),
        (2028, 2, 29),
        (2028, 3, 29),
        (2028, 3, 31),
        (2100, 2, 28),
        (2100, 3, 31),
    }
    return sorted(datetime.date(*d) for d in days)


@pytest.fixture(scope="module")
def duck() -> Any:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    yield con
    con.close()


@pytest.fixture(scope="module")
def date_grid(duck: Any) -> int:
    dates = _dates()
    assert len(dates) >= 40
    duck.execute("CREATE TABLE grid (a DATE, b DATE)")
    pairs = [(a, b) for a in dates for b in dates]  # both orders, and every date with itself
    duck.executemany("INSERT INTO grid VALUES (?, ?)", pairs)
    return len(pairs)


@pytest.mark.parametrize(
    "function, exact",
    [("MONTHS_BETWEEN", fs.months_between), ("YEARS_BETWEEN", fs.years_between)],
)
def test_generic_unit_difference_equals_the_exact_evaluator(
    function: str, exact: Any, duck: Any, date_grid: int
) -> None:
    sql = _generic_sql(f"{function}(a, b)", {"a": "date", "b": "date"})
    rows = duck.execute(f'SELECT a, b, {sql} FROM grid AS "t"').fetchall()
    assert len(rows) == date_grid
    wrong = [(a, b, got, exact(a, b)) for a, b, got in rows if got != exact(a, b)]
    assert not wrong, (sql, wrong[:5])


@pytest.mark.parametrize("function", ["MONTHS_BETWEEN", "YEARS_BETWEEN"])
def test_duckdb_native_unit_difference_equals_the_exact_evaluator(
    function: str, duck: Any, date_grid: int
) -> None:
    exact = {"MONTHS_BETWEEN": fs.months_between, "YEARS_BETWEEN": fs.years_between}[function]
    sql = render(f"{function}(a, b)", {"a": "date", "b": "date"}, "duckdb")
    assert "date_sub(" in sql
    rows = duck.execute(f'SELECT a, b, {sql} FROM grid AS "t"').fetchall()
    wrong = [(a, b, got, exact(a, b)) for a, b, got in rows if got != exact(a, b)]
    assert not wrong, (sql, wrong[:5])


@pytest.mark.parametrize("function", ["MONTHS_BETWEEN", "YEARS_BETWEEN"])
def test_generic_unit_difference_of_null_is_null(function: str, duck: Any) -> None:
    sql = _generic_sql(f"{function}(a, b)", {"a": "date", "b": "date"})
    for a, b in [(None, "2024-03-31"), ("2024-03-31", None), (None, None)]:
        got = duck.execute(
            f'SELECT {sql} FROM (SELECT CAST(? AS DATE) AS a, CAST(? AS DATE) AS b) AS "t"',
            [a, b],
        ).fetchone()[0]
        assert got is None


# -- 3. half to even ---------------------------------------------------------------------


TIES = [
    "0.125",
    "-0.125",
    "2.5",
    "-2.5",
    "0.135",
    "-0.135",
    "1.0005",
    "-1.0005",
    "0.5",
    "-0.5",
    "1.5",
    "-1.5",
    "0.0000",
    "0.0049",
    "0.0051",
    "1.2345",
    "-1.2355",
    "123456.7895",
    "-123456.7885",
    "0.3750",
    "0.6250",
    "9999999.9995",
]


@pytest.mark.parametrize("places", [0, 1, 2, 3])
def test_case_form_of_half_to_even_equals_round_fraction(places: int, duck: Any) -> None:
    text = f"ROUND(x, {places}, 'HALF_TO_EVEN')"
    sql = render(text, {"x": "decimal(12,4)"}, "duckdb")
    assert "CASE WHEN ABS(" in sql  # the generic form, not an engine mode
    duck.execute(f"CREATE OR REPLACE TABLE ties_{places} (x DECIMAL(12, 4))")
    duck.executemany(f"INSERT INTO ties_{places} VALUES (?)", [(t,) for t in TIES])
    rows = duck.execute(f'SELECT x, {sql} FROM ties_{places} AS "t"').fetchall()
    assert len(rows) == len(TIES)
    for x, got in rows:
        want = fs.round_fraction(Fraction(x), places, "HALF_TO_EVEN")
        assert Fraction(got) == want, (x, places, got, want, sql)
        tree = fs.parse(text)
        assert fs.evaluate(tree, {"x": Fraction(x)}) == want


def test_half_to_even_differs_from_the_default_on_ties(duck: Any) -> None:
    even = render("ROUND(x, 2, 'HALF_TO_EVEN')", {"x": "decimal(12,4)"}, "duckdb")
    away = render("ROUND(x, 2)", {"x": "decimal(12,4)"}, "duckdb")
    assert away.startswith("ROUND(") and "CASE" not in away
    sql = f"SELECT {even}, {away} FROM (SELECT CAST('0.1250' AS DECIMAL(12, 4)) AS x) AS \"t\""
    got_even, got_away = duck.execute(sql).fetchone()
    assert (Fraction(got_even), Fraction(got_away)) == (Fraction("0.12"), Fraction("0.13"))


def test_half_to_even_spelling_per_dialect() -> None:
    cols = {"x": "decimal(12,4)"}
    text = "ROUND(x, 2, 'HALF_TO_EVEN')"
    assert render(text, cols, "snowflake") == 'ROUND("t"."x", 2, \'HALF_TO_EVEN\')'
    assert render(text, cols, "databricks") == "bround(`t`.`x`, 2)"
    athena = render(text, cols, "athena")
    assert "truncate(" in athena and "MOD(" in athena and "TRUNC(" not in athena
    for dialect in ("postgres", "bigquery"):
        sql = render(text, cols, dialect)
        assert "TRUNC(" in sql and "MOD(" in sql and "truncate(" not in sql
    duck = render(text, cols, "duckdb")
    assert "TRUNC(" in duck and " % 2" in duck and "MOD(" not in duck


def test_round_that_cuts_no_digit_is_the_plain_round() -> None:
    cols = {"x": "decimal(12,2)"}
    for dialect in fs.DIALECTS:
        sql = render("ROUND(x, 2, 'HALF_TO_EVEN')", cols, dialect)
        assert sql.startswith("ROUND(") and "CASE" not in sql and "bround" not in sql


# -- 4. bounded output -------------------------------------------------------------------


def _nested_round(depth: int) -> str:
    text = "x"
    for _ in range(depth):
        text = f"ROUND({text} * 1.5, 6, 'HALF_TO_EVEN')"
    return text


def test_nested_half_even_rounds_are_bounded() -> None:
    cols = {"x": "decimal(20,8)"}
    shallow = render(_nested_round(2), cols, "postgres")
    assert shallow.count("CASE WHEN ABS(") >= 2 and len(shallow) < fs.MAX_RENDERED_CHARS
    text = _nested_round(25)
    assert len(text) <= fs.MAX_CODE_POINTS
    start = time.perf_counter()
    try:
        sql = render(text, cols, "postgres")
    except fs.RenderTooLarge:
        pass  # each level repeats its operand ~6 times: the cap is what stops it
    else:
        assert len(sql) <= fs.MAX_RENDERED_CHARS
    assert time.perf_counter() - start < 5.0  # linear work; slack for a loaded runner


def test_the_render_cap_is_a_value_error() -> None:
    assert issubclass(fs.RenderTooLarge, ValueError)
    assert fs.MAX_RENDERED_CHARS == 1_000_000


def _coalesced_date(depth: int) -> str:
    text = "a"
    for _ in range(depth):
        text = f"COALESCE({text}, b)"
    return f"MONTHS_BETWEEN(c, {text})"


def test_month_difference_output_grows_polynomially() -> None:
    cols = {"a": "date", "b": "date", "c": "date"}
    sizes = {}
    for depth in (8, 16, 32):
        text = _coalesced_date(depth)
        assert len(text) <= fs.MAX_CODE_POINTS
        sizes[depth] = len(render(text, cols, "postgres"))
    assert sizes[16] < 3 * sizes[8]
    assert sizes[32] < 3 * sizes[16]


def _case_date(depth: int) -> str:
    text = "a"
    for _ in range(depth):
        text = f"CASE WHEN MONTHS_BETWEEN(c, {text}) > 0 THEN a ELSE b END"
    return text


def test_a_date_that_repeats_itself_each_level_hits_the_cap_not_a_hang() -> None:
    cols = {"a": "date", "b": "date", "c": "date"}
    text = _case_date(12)  # every level prints the one below it eight times
    assert len(text) <= fs.MAX_CODE_POINTS
    start = time.perf_counter()
    with pytest.raises(fs.RenderTooLarge):
        render(text, cols, "postgres")
    assert time.perf_counter() - start < 5.0  # linear work; slack for a loaded runner


# -- 5. dialect specifics ----------------------------------------------------------------


def test_postgres_collates_string_ordering_only() -> None:
    cols = {"s": "string", "t": "string", "n": "bigint", "d": "date"}
    sql = render("s < 'b' AND s >= t AND s BETWEEN 'a' AND 'z'", cols, "postgres", fs.PREDICATE)
    assert sql.count('COLLATE "C"') == 3
    assert '("t"."s" COLLATE "C") < ' in sql
    plain = render("n < 1 AND n BETWEEN 1 AND 5 AND d > DATE '2024-01-01'", cols, "postgres")
    assert "COLLATE" not in plain
    equality = render("s = 'b' AND s IN ('a', 'b')", cols, "postgres", fs.PREDICATE)
    assert "COLLATE" not in equality
    for dialect in fs.DIALECTS:
        if dialect != "postgres":
            assert "COLLATE" not in render("s < 'b'", cols, dialect, fs.PREDICATE)


def test_identifier_quoting_per_dialect() -> None:
    cols = {"a`b": "bigint"}
    expect = {
        "bigquery": r"`t`.`a\`b`",
        "databricks": "`t`.`a``b`",
        "duckdb": '"t"."a`b"',
        "snowflake": '"t"."a`b"',
        "postgres": '"t"."a`b"',
        "athena": '"t"."a`b"',
    }
    assert set(expect) == set(fs.DIALECTS)
    for dialect, want in expect.items():
        assert render('"a`b"', cols, dialect) == want
    double = {'a"b': "bigint"}
    for dialect in ("duckdb", "snowflake", "postgres", "athena"):
        assert render('"a""b"', double, dialect) == '"t"."a""b"'
    assert fs.sql_name("databricks", "x") == "`x`"
    assert fs.sql_name("bigquery", "x") == "`x`"
    assert fs.sql_name("postgres", "x") == '"x"'


def test_string_literal_with_a_quote_and_a_backslash_per_dialect() -> None:
    text = "'it''s a\\b'"
    cols: Dict[str, str] = {}
    assert render(text, cols, "bigquery") == r"'it\'s a\\b'"
    assert render(text, cols, "databricks") == r"'it\'s a\\b'"
    assert render(text, cols, "snowflake") == r"'it''s a\\b'"
    for dialect in ("duckdb", "postgres", "athena"):
        assert render(text, cols, dialect) == "'it''s a\\b'"
    for dialect in fs.DIALECTS:
        assert fs.sql_literal(dialect, "x") == "'x'"


def test_exact_number_type_per_dialect() -> None:
    assert fs.exact_number_type("snowflake", 4) == "NUMBER(38, 4)"
    assert fs.exact_number_type("bigquery", 4) == "BIGNUMERIC(38, 4)"
    assert fs.exact_number_type("postgres", 4) == "NUMERIC(38, 4)"
    for dialect in ("duckdb", "databricks", "athena"):
        assert fs.exact_number_type(dialect, 4) == "DECIMAL(38, 4)"
    assert fs.exact_number_type("bigquery", None) == "FLOAT64"
    assert fs.exact_number_type("postgres", None) == "NUMERIC"  # exact; round() takes it
    for dialect in ("duckdb", "snowflake", "databricks", "athena"):
        assert fs.exact_number_type(dialect, None) == "DOUBLE"
    assert fs.exact_number_type("duckdb", 99) == "DECIMAL(38, 38)"
    assert fs.exact_number_type("duckdb", -1) == "DECIMAL(38, 0)"


def test_division_and_comparison_use_the_exact_type_per_dialect() -> None:
    cols = {"x": "decimal(12,4)", "y": "decimal(12,2)", "f": "double"}
    names = {
        "snowflake": "NUMBER(38, 4)",
        "bigquery": "BIGNUMERIC(38, 4)",
        "postgres": "NUMERIC(38, 4)",
        "duckdb": "DECIMAL(38, 4)",
        "databricks": "DECIMAL(38, 4)",
        "athena": "DECIMAL(38, 18)",  # Trino divides at the larger operand scale
    }
    for dialect, name in names.items():
        assert "CAST(" in render("x / y", cols, dialect)
        assert f" AS {name}) / NULLIF(" in render("x / y", cols, dialect)
        compared = name if dialect != "athena" else "DECIMAL(38, 4)"
        assert f"AS {compared})" in render("x = y", cols, dialect, fs.PREDICATE)
    doubles = {"bigquery": "FLOAT64", "postgres": "NUMERIC", "duckdb": "DOUBLE"}
    for dialect, name in doubles.items():
        assert f"AS {name})" in render("f / y", cols, dialect)


def test_days_between_per_dialect() -> None:
    cols = {"a": "date", "b": "date"}
    text = "DAYS_BETWEEN(a, b)"
    q = {d: fs.column_reader(d, "t") for d in fs.DIALECTS}
    a = {d: q[d](None, "a") for d in fs.DIALECTS}
    b = {d: q[d](None, "b") for d in fs.DIALECTS}
    expect = {
        "duckdb": f"date_sub('day', {a['duckdb']}, {b['duckdb']})",
        "snowflake": f"DATEDIFF(day, {a['snowflake']}, {b['snowflake']})",
        "bigquery": f"DATE_DIFF({b['bigquery']}, {a['bigquery']}, DAY)",
        "databricks": f"datediff({b['databricks']}, {a['databricks']})",
        "postgres": f"({b['postgres']} - {a['postgres']})",
        "athena": f"date_diff('day', {a['athena']}, {b['athena']})",
    }
    for dialect, want in expect.items():
        assert render(text, cols, dialect) == want


def test_month_and_year_difference_spelling_per_dialect() -> None:
    cols = {"a": "date", "b": "date"}
    assert render("MONTHS_BETWEEN(a, b)", cols, "duckdb") == 'date_sub(\'month\', "t"."a", "t"."b")'
    assert render("YEARS_BETWEEN(a, b)", cols, "duckdb") == 'date_sub(\'year\', "t"."a", "t"."b")'
    last_day = {
        "snowflake": "LAST_DAY(",
        "bigquery": "LAST_DAY(",
        "databricks": "LAST_DAY(",
        "postgres": "DATE_TRUNC('month'",
        "athena": "last_day_of_month(",
    }
    div = {
        "snowflake": "TRUNC(",
        "bigquery": "DIV(",
        "databricks": " div 12",
        "postgres": "DIV(",
        "athena": " / 12)",
    }
    for dialect, marker in last_day.items():
        months = render("MONTHS_BETWEEN(a, b)", cols, dialect)
        years = render("YEARS_BETWEEN(a, b)", cols, dialect)
        assert marker in months and "EXTRACT(YEAR FROM" in months
        assert "date_sub" not in months + years and "DATEDIFF" not in months + years
        assert div[dialect] in years
        assert months.count("CASE WHEN") >= 3


def test_an_unknown_dialect_is_refused() -> None:
    tree, checked = compile_text("1 + 1", {})
    with pytest.raises(ValueError):
        fs.render_sql(tree, checked, fs.column_reader("duckdb", "t"), "oracle")
    with pytest.raises(ValueError):
        fs.render_sql(tree, checked, fs.column_reader("duckdb", "t"), "oracle", predicate=True)
    assert set(fs.DIALECTS) == {
        "duckdb",
        "snowflake",
        "bigquery",
        "databricks",
        "postgres",
        "athena",
    }


def test_links_read_from_an_alias_named_like_the_relationship() -> None:
    tree = fs.parse("o.amount > 1")
    model = model_of({})
    target = model_of({"amount": "bigint"})
    model.links["o"] = fs.Link("o", target, target_name="m")
    checked = fs.check(tree, model, fs.PREDICATE)
    for dialect in fs.DIALECTS:
        sql = fs.render_sql(tree, checked, fs.column_reader(dialect, "t"), dialect, True)
        assert fs.sql_name(dialect, "o") + "." + fs.sql_name(dialect, "amount") in sql
        parses(sql, dialect)


def test_node_module_is_the_one_the_renderer_uses() -> None:
    assert n.DEFAULT_ROUND_MODE
    assert _Renderer("duckdb", compile_text("1", {})[1], fs.column_reader("duckdb")).whole_units
    assert not _Renderer("athena", compile_text("1", {})[1], fs.column_reader("athena")).whole_units
    _unused: Optional[int] = None
    assert _unused is None


def test_bigquery_reads_a_decimal_literal_as_bignumeric() -> None:
    cols = {"net": "decimal(12,2)"}
    sql = render("ROUND(net * 1.15, 2)", cols, "bigquery")
    assert "BIGNUMERIC '1.15'" in sql and " 1.15" not in sql
    assert "* 2)" in render("net * 2", cols, "bigquery")  # an integer stays INT64
    parses(sql, "bigquery")


def test_postgres_rounds_a_quotient_or_a_float_in_numeric() -> None:
    cols = {"net": "decimal(12,2)", "qty": "integer", "f": "double"}
    for text in ("ROUND(net / qty / 12, 2)", "ROUND(f * 2, 2)", "ROUND(f, 2, 'HALF_TO_EVEN')"):
        sql = render(text, cols, "postgres")
        assert "DOUBLE PRECISION" not in sql
        parses(sql, "postgres")


def test_athena_divides_with_eighteen_places_at_least() -> None:
    assert "AS DECIMAL(38, 18)) / NULLIF(" in render("net / 3", {"net": "decimal(12,2)"}, "athena")
