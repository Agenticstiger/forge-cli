# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Column-rule behaviour of the native quality engine: dialect quoting, multi-column
rules, value/pattern/reference sources and the backslash-safe string literal."""

from __future__ import annotations

import pytest

from fluid_build.providers import quality_engine as qe
from fluid_build.providers._sql_safety import quote_string_literal_for

T = "`c`.`s`.`t`"


class Recorder:
    """execute_fn double: records SQL, answers from a queue (or a fixed row)."""

    def __init__(self, *answers):
        self.sql = []
        self._answers = list(answers)

    def __call__(self, sql):
        self.sql.append(sql)
        if not self._answers:
            return [(0,)]
        return self._answers.pop(0)


def run(rule, execute_fn, dialect="databricks", **kw):
    (res,) = qe.execute_quality_checks([rule], T, execute_fn, dialect, **kw)
    return res


# ---------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    "dialect,expected",
    [
        ("databricks", "`x`"),
        ("bigquery", "`x`"),
        ("BigQuery", "`x`"),
        ("snowflake", '"x"'),
        ("ansi", '"x"'),
        (None, '"x"'),
    ],
)
def test_qi_quotes_per_dialect(dialect, expected):
    assert qe._qi("x", dialect) == expected


def test_qi_rejects_unsafe_identifier():
    with pytest.raises(ValueError):
        qe._qi("a`b", "databricks")


@pytest.mark.parametrize(
    "dialect,expected",
    [
        ("databricks", "DOUBLE"),
        ("bigquery", "FLOAT64"),
        ("snowflake", "DOUBLE PRECISION"),
        (None, "DOUBLE PRECISION"),
    ],
)
def test_double_type_per_dialect(dialect, expected):
    assert qe._double(dialect) == expected


def test_rule_columns_splits_and_strips():
    assert qe.rule_columns("a, b ,,c") == ["a", "b", "c"]
    assert qe.rule_columns("*") == ["*"]
    assert qe.rule_columns(None) == []


def test_expose_columns_prefers_contract_schema():
    expose = {"contract": {"schema": [{"name": "a"}, "junk"]}, "schema": [{"name": "z"}]}
    assert qe.expose_columns(expose) == [{"name": "a"}]
    assert qe.expose_columns({"schema": [{"name": "z"}]}) == [{"name": "z"}]
    assert qe.expose_columns("nope") == []


# ----------------------------------------------------- valid value sources

COLS = [
    {
        "name": "status",
        "validationRules": [{"type": "enum", "constraint": "GOOD | ESTIMATED, SUSPECT"}],
    },
    {"name": "cc", "validationRules": [{"type": "regex", "constraint": "^[A-Z]{2}$"}]},
    {"name": "site_id", "labels": {"references": "acme.sites.site_id"}},
]


def test_valid_values_explicit_wins():
    rule = {"selector": "status", "validValues": ["X"], "description": "status valid values: A."}
    assert qe.extract_valid_values(rule, COLS) == ["X"]


def test_valid_values_from_description_beats_enum():
    rule = {"selector": "status", "description": "status valid values: A, B."}
    assert qe.extract_valid_values(rule, COLS) == ["A", "B"]


def test_valid_values_from_enum_splits_on_pipe_and_comma():
    assert qe.extract_valid_values({"selector": "status"}, COLS) == [
        "GOOD",
        "ESTIMATED",
        "SUSPECT",
    ]


def test_valid_values_empty_without_any_source():
    assert qe.extract_valid_values({"selector": "cc"}, COLS) == []


def test_value_pattern_reads_regex_rule():
    assert qe.extract_value_pattern({"selector": "cc"}, COLS) == "^[A-Z]{2}$"
    assert qe.extract_value_pattern({"selector": "status"}, COLS) is None


def test_reference_rpartitions_on_last_dot():
    assert qe.extract_reference({"selector": "site_id"}, COLS) == ("acme.sites", "site_id")
    assert qe.extract_reference({"selector": "cc"}, COLS) is None


# ------------------------------------------------ multi-column completeness


def test_completeness_multi_column_is_one_statement_with_lowest_ratio():
    rec = Recorder([(1.0, 0.5, 0.9)])
    res = run(
        {
            "id": "r",
            "type": "completeness",
            "selector": "a,b,c",
            "threshold": 0.95,
            "operator": ">=",
        },
        rec,
    )
    assert len(rec.sql) == 1
    assert rec.sql[0].count("CAST(") == 3 and "AS DOUBLE" in rec.sql[0]
    assert res.passed is False
    assert res.actual == "0.5000"
    assert "'b'" in res.message and "'c'" in res.message and "'a'" not in res.message


def test_completeness_multi_column_passes_when_all_meet_bound():
    res = run({"id": "r", "type": "completeness", "selector": "a,b"}, Recorder([(1.0, 1.0)]))
    assert res.passed and res.expected == "1.0"


def test_completeness_null_ratio_counts_as_zero():
    res = run({"id": "r", "type": "completeness", "selector": "a,b"}, Recorder([(1.0, None)]))
    assert not res.passed and res.actual == "0.0000"


# ------------------------------------------------ multi-column accuracy


@pytest.mark.parametrize(
    "operator,func", [(">=", "MIN"), (">", "MIN"), ("<=", "MAX"), ("<", "MAX")]
)
def test_accuracy_multi_column_aggregate_follows_operator(operator, func):
    rec = Recorder([(1, 2)])
    run(
        {"id": "r", "type": "accuracy", "selector": "a,b", "threshold": 0, "operator": operator},
        rec,
    )
    assert rec.sql[0].count(f"{func}(`") == 2 and len(rec.sql) == 1


def test_accuracy_multi_column_lower_bound_fails_on_low_column():
    res = run(
        {"id": "r", "type": "accuracy", "selector": "a,b", "threshold": 5, "operator": ">="},
        Recorder([(10, 1)]),
    )
    assert not res.passed and "'b'" in res.message and res.actual == "1"


def test_accuracy_multi_column_equality_uses_violation_counts():
    rec = Recorder([(0, 3)])
    res = run(
        {"id": "r", "type": "accuracy", "selector": "a,b", "threshold": 1, "operator": "=="}, rec
    )
    assert "SUM(CASE WHEN" in rec.sql[0] and "<>" in rec.sql[0]
    assert not res.passed and res.actual == "3 violating row(s)"


def test_accuracy_multi_column_no_data_fails():
    res = run(
        {"id": "r", "type": "accuracy", "selector": "a,b", "threshold": 0, "operator": ">="},
        Recorder([(1, None)]),
    )
    assert not res.passed and "no data in 'b'" in res.message


# ------------------------------------- single-column rules over >1 columns


@pytest.mark.parametrize("rtype", ["uniqueness", "valid_values", "freshness"])
def test_single_column_rule_over_many_columns_not_executed(rtype):
    rec = Recorder()
    res = run({"id": "r", "type": rtype, "selector": "a,b"}, rec)
    assert res.passed is False and res.actual == "not executed" and rec.sql == []


# ------------------------------------------------------- pattern SQL


@pytest.mark.parametrize(
    "dialect,fragment",
    [
        ("databricks", "NOT (`cc` RLIKE '^[A-Z]{2}$')"),
        ("bigquery", "NOT REGEXP_CONTAINS(`cc`, '^[A-Z]{2}$')"),
        ("snowflake", "REGEXP_INSTR(\"cc\", '^[A-Z]{2}$') = 0"),
        ("ansi", "NOT regexp_matches(\"cc\", '^[A-Z]{2}$')"),
        ("duckdb", "NOT regexp_matches(\"cc\", '^[A-Z]{2}$')"),
    ],
)
def test_validity_pattern_sql_per_dialect(dialect, fragment):
    sql = qe._validity_pattern_sql(T, "cc", "^[A-Z]{2}$", dialect)
    assert fragment in sql and "IS NOT NULL" in sql


def test_validity_pattern_unknown_dialect_is_error_result():
    res = run(
        {"id": "r", "type": "valid_values", "selector": "cc"}, Recorder(), "oracle", columns=COLS
    )
    assert not res.passed and res.message.startswith("Error executing rule 'r'")
    assert "no regular-expression test" in res.message


def test_valid_values_via_enum_runs_not_in_and_keeps_type():
    rec = Recorder([(2,)])
    res = run({"id": "r", "type": "valid_values", "selector": "status"}, rec, columns=COLS)
    assert "NOT IN ('GOOD', 'ESTIMATED', 'SUSPECT')" in rec.sql[0]
    assert res.rule_type == "valid_values" and not res.passed


def test_legacy_validity_type_is_preserved():
    res = run(
        {"id": "r", "type": "validity", "selector": "s", "validValues": ["A"]}, Recorder([(0,)])
    )
    assert res.rule_type == "validity" and res.passed


def test_pattern_rule_via_column_regex():
    rec = Recorder([(0,)])
    res = run({"id": "r", "type": "valid_values", "selector": "cc"}, rec, columns=COLS)
    assert "RLIKE" in rec.sql[0] and res.passed and res.rule_type == "valid_values"


# ------------------------------------------------------- references


def test_reference_without_resolver_not_executed():
    res = run({"id": "r", "type": "valid_values", "selector": "site_id"}, Recorder(), columns=COLS)
    assert not res.passed and res.actual == "not executed"


def test_reference_resolver_lookup_error_message_is_surfaced():
    def resolver(product, column):
        raise LookupError("nothing tagged")

    res = run(
        {"id": "r", "type": "valid_values", "selector": "site_id"},
        Recorder(),
        columns=COLS,
        resolve_reference=resolver,
    )
    assert res.actual == "not executed" and "nothing tagged" in res.message


def test_reference_runs_anti_join():
    rec = Recorder([(3,)])
    seen = []

    def resolver(product, column):
        seen.append((product, column))
        return "`p`.`s`.`sites`"

    res = run(
        {"id": "r", "type": "valid_values", "selector": "site_id"},
        rec,
        columns=COLS,
        resolve_reference=resolver,
    )
    assert seen == [("acme.sites", "site_id")]
    assert (
        "NOT EXISTS (SELECT 1 FROM `p`.`s`.`sites` AS p WHERE p.`site_id` = c.`site_id`)"
        in rec.sql[0]
    )
    assert not res.passed and res.actual == "3 value(s) missing"


# ------------------------------------------------------- freshness


def test_freshness_databricks_sql():
    sql = qe._freshness_sql(T, "ts", "databricks")
    assert "timestampdiff(SECOND, MAX(`ts`), current_timestamp())" in sql


# ------------------------------------------- backslash-safe string literals


def scan_literal(text):
    """Reference scanner for the backslash-escaping grammar: decode one literal and
    return (value, index just past the closing quote)."""
    assert text[0] == "'"
    out, i = [], 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            out.append(text[i + 1])
            i += 2
        elif ch == "'":
            return "".join(out), i + 1
        else:
            out.append(ch)
            i += 1
    raise AssertionError("unterminated literal")


@pytest.mark.parametrize("dialect", ["databricks", "bigquery", "spark", "Databricks"])
@pytest.mark.parametrize(
    "value",
    ["plain", "trailing\\", "it's", "\\' OR 1=1 --", "a\\\\b", "'", "\\", "x'); DROP TABLE t; --"],
)
def test_backslash_dialect_literal_roundtrips_and_cannot_break_out(dialect, value):
    literal = quote_string_literal_for(dialect, value)
    decoded, end = scan_literal(literal + " OR 1=1")
    assert decoded == value
    assert literal[end - 1] == "'" and literal[:end] == literal


def test_backslash_dialect_escape_shape():
    assert quote_string_literal_for("databricks", "a'b") == "'a\\'b'"
    assert quote_string_literal_for("databricks", "a\\") == "'a\\\\'"


def test_other_dialect_delegates_to_existing_quoting():
    assert quote_string_literal_for("duckdb", "it's") == "'it''s'"


def test_non_string_value_rejected():
    with pytest.raises(ValueError):
        quote_string_literal_for("databricks", 5)  # type: ignore[arg-type]


# ── range validationRules and valid_values over several columns ─────────────

from fluid_build.providers.quality_engine import (  # noqa: E402
    ValueDomain,
    execute_quality_checks,
    parse_range,
    value_domain,
)


@pytest.mark.parametrize(
    "constraint, bounds",
    [
        (">= 0", [(">=", 0.0)]),
        (">= 0 and <= 100", [(">=", 0.0), ("<=", 100.0)]),
        ("between -1.5 and 2", [(">=", -1.5), ("<=", 2.0)]),
        ("> 0 AND < 1", [(">", 0.0), ("<", 1.0)]),
        ("0..100", []),
        (">= zero", []),
        (None, []),
    ],
)
def test_parse_range(constraint, bounds):
    assert parse_range(constraint) == bounds


ENERGY = [
    {"name": "grid_kwh", "validationRules": [{"type": "range", "constraint": ">= 0"}]},
    {"name": "solar_kwh", "validationRules": [{"type": "range", "constraint": ">= 0"}]},
    {"name": "quality", "validationRules": [{"type": "enum", "constraint": "GOOD,BAD"}]},
    {"name": "free_text"},
]


def _recording(answers):
    seen = []

    def execute(sql):
        seen.append(sql)
        return [(answers.pop(0),)]

    return seen, execute


def test_range_rule_counts_values_outside_the_bounds_on_databricks():
    seen, execute = _recording([0])
    rule = {"id": "r", "type": "valid_values", "selector": "grid_kwh", "severity": "error"}
    (res,) = execute_quality_checks([rule], "`c`.`s`.`t`", execute, "databricks", columns=ENERGY)
    assert res.passed and res.expected == "0 values outside >= 0"
    assert seen[0].endswith("WHERE `grid_kwh` IS NOT NULL AND NOT (`grid_kwh` >= 0.0)")


def test_valid_values_over_columns_checks_each_against_its_own_domain():
    seen, execute = _recording([0, 3, 1])
    rule = {
        "id": "nonneg",
        "type": "valid_values",
        "selector": "grid_kwh, solar_kwh, quality",
        "severity": "error",
    }
    (res,) = execute_quality_checks([rule], "t", execute, "databricks", columns=ENERGY)
    assert not res.passed and res.rule_type == "valid_values"
    assert len(seen) == 3
    assert "NOT (`solar_kwh` >= 0.0)" in seen[1] and "NOT IN ('GOOD', 'BAD')" in seen[2]
    assert "'solar_kwh' (3 invalid value(s))" in res.message
    assert "'quality' (1 invalid value(s))" in res.message
    assert res.actual == "4 invalid value(s)"


def test_valid_values_over_columns_all_clean_passes():
    _, execute = _recording([0, 0])
    rule = {"id": "nonneg", "type": "valid_values", "selector": "grid_kwh,solar_kwh"}
    (res,) = execute_quality_checks([rule], "t", execute, "databricks", columns=ENERGY)
    assert res.passed and res.message == "Validity OK for all 2 columns"
    assert res.actual == "0 invalid value(s)"


def test_valid_values_over_columns_with_an_undeclared_column_is_not_executed():
    _, execute = _recording([0])
    rule = {"id": "v", "type": "valid_values", "selector": "grid_kwh,free_text"}
    (res,) = execute_quality_checks([rule], "t", execute, "databricks", columns=ENERGY)
    assert not res.passed and res.actual == "not executed"
    assert "nothing checked for 'free_text'" in res.message


def test_value_domain_is_the_rule_list_else_every_column_declaration():
    columns = [
        {
            "name": "a",
            "validationRules": [
                {"type": "range", "constraint": ">= 0"},
                {"type": "regex", "constraint": "^x$"},
                {"type": "enum", "constraint": "X | Y"},
            ],
            "labels": {"references": "p.q.col"},
        },
        {
            "name": "b",
            "validationRules": [
                {"type": "range", "constraint": ">= 0"},
                {"type": "regex", "constraint": "^x$"},
            ],
        },
        {"name": "c", "validationRules": [{"type": "range", "constraint": ">= 0"}]},
        {"name": "d", "labels": {"references": "p.q.col"}},
    ]
    listed = {"id": "r", "selector": "a", "description": "a valid values: K, L."}
    assert value_domain(listed, columns) == ValueDomain(values=("K", "L"))
    assert value_domain({"selector": "a"}, columns) == ValueDomain(
        values=("X", "Y"), pattern="^x$", bounds=((">=", 0.0),), reference=("p.q", "col")
    )
    assert value_domain({"selector": "b"}, columns) == ValueDomain(
        pattern="^x$", bounds=((">=", 0.0),)
    )
    assert value_domain({"selector": "c"}, columns) == ValueDomain(bounds=((">=", 0.0),))
    assert value_domain({"selector": "d"}, columns) == ValueDomain(reference=("p.q", "col"))
    assert value_domain({"selector": "zz"}, columns) == ValueDomain()


def test_every_column_declaration_is_checked_in_one_statement():
    # A column with an enum, a regex and a range: a value that breaks any of them is
    # invalid (the range used to be dropped when a regex came first).
    columns = [
        {
            "name": "pct",
            "validationRules": [
                {"type": "regex", "constraint": "^[0-9]+$"},
                {"type": "range", "constraint": ">= 0 and <= 100"},
                {"type": "enum", "constraint": "0,50,100,150"},
            ],
        }
    ]
    seen, execute = _recording([1])
    rule = {"id": "p", "type": "valid_values", "selector": "pct", "severity": "error"}
    (res,) = execute_quality_checks([rule], "t", execute, "databricks", columns=columns)
    assert len(seen) == 1
    where = seen[0].split("WHERE", 1)[1]
    assert "NOT IN ('0', '50', '100', '150')" in where
    assert "NOT (`pct` RLIKE '^[0-9]+$')" in where
    assert "NOT (`pct` >= 0.0 AND `pct` <= 100.0)" in where
    assert where.count(" OR ") == 2
    assert not res.passed and res.actual == "1 invalid value(s)"


def test_a_column_with_values_and_a_reference_checks_both():
    columns = [
        {
            "name": "site_id",
            "validationRules": [{"type": "regex", "constraint": "^S[0-9]+$"}],
            "labels": {"references": "acme.sites.site_id"},
        }
    ]
    seen, execute = _recording([0, 2])
    rule = {"id": "ref", "type": "valid_values", "selector": "site_id", "severity": "warn"}
    (res,) = execute_quality_checks(
        [rule],
        "t",
        execute,
        "databricks",
        columns=columns,
        resolve_reference=lambda product, column: "`m`.`s`.`sites`",
    )
    assert len(seen) == 2 and "RLIKE" in seen[0] and "NOT EXISTS" in seen[1]
    assert not res.passed and "2 value(s) of 'site_id' are not in acme.sites.site_id" in res.message
    assert res.actual == "0 invalid value(s); 2 value(s) missing"


def test_accuracy_without_a_threshold_is_not_run():
    seen, execute = _recording([])
    for selector in ("a", "a,b"):
        rule = {"id": "acc", "type": "accuracy", "selector": selector, "severity": "error"}
        (res,) = execute_quality_checks([rule], "t", execute, "databricks")
        assert not res.passed and res.actual == "not executed" and "no threshold" in res.message
    assert seen == []
