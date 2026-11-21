# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A requested fact: which rules can be measured, and the counts on rows as sources give them."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, Dict, List

import pytest

from fluid_build.profiler import facts
from fluid_build.profiler.facts import FactCount, FactRefused, count_fact, plan_fact

pytestmark = pytest.mark.unit

SECRET = "SECRET-VALUE-91"  # pragma: allowlist secret


def _contract() -> Dict[str, Any]:
    columns = [
        ("order_id", "string"),
        ("amount", "decimal(10,2)"),
        ("status", "string"),
        ("created_at", "timestamp"),
        ("paid_at", "timestamp"),
        ("seen_start", "timestamptz"),
        ("seen_end", "timestamptz"),
        ("price", "decimal(10,2)"),
        ("qty", "integer"),
        ("total", "decimal(12,2)"),
        ("customer_id", "string"),
        ("shipped", "date"),
        ("flag", "boolean"),
        ("ratio", "double"),
    ]
    invariants: List[Dict[str, Any]] = [
        {"id": "non_negative", "check": "amount >= 0"},
        {"id": "not_negative", "check": "NOT (amount < 0)"},
        {"id": "paid_has_date", "when": "status = 'paid'", "check": "paid_at IS NOT NULL"},
        {"id": "total_is_price_qty", "derive": "total", "expr": "price * qty"},
        {
            "id": "n_lines",
            "derive": "qty",
            "aggregate": {"function": "count", "from": "order_lines", "via": "order"},
        },
        {"id": "same_status", "dependency": {"from": ["customer_id"], "to": ["status"]}},
        {"id": "german", "check": "customer.country = 'DE'"},
        {"id": "german_when", "when": "customer.country = 'DE'", "check": "amount >= 0"},
        {"id": "broken", "check": "amount >="},
        {"id": "unknown_column", "check": "nope > 1"},
        {"id": "shipped_2024", "check": "shipped >= DATE '2024-01-01'"},
        {"id": "flagged", "check": "flag = TRUE"},
        {"id": "created_before_paid", "check": "created_at <= paid_at"},
        {"id": "window", "check": "seen_start <= seen_end"},
        {"id": "ratio_pos", "check": "ratio > 0"},
        {"id": "two_forms", "check": "amount >= 0", "derive": "total", "expr": "price"},
        {"id": "dup", "check": "amount >= 0"},
        {"id": "dup", "check": "amount >= 1"},
        {"id": "no_column", "check": "1 = 1"},
    ]
    return {
        "exposes": [
            {
                "exposeId": "orders",
                "contract": {
                    "schema": [{"name": n, "type": t} for n, t in columns],
                    "invariants": invariants,
                },
                "semantics": {
                    "relationships": [
                        {
                            "name": "customer",
                            "to": "customers",
                            "fromColumns": ["customer_id"],
                            "toColumns": ["customer_id"],
                        }
                    ]
                },
            },
            {
                "exposeId": "customers",
                "contract": {
                    "schema": [
                        {"name": "customer_id", "type": "string"},
                        {"name": "country", "type": "string"},
                    ]
                },
            },
        ]
    }


def _plan(rule: str) -> facts.FactPlan:
    return plan_fact(_contract(), f"orders.{rule}", "orders", rule)


def _count(rule: str, **values: List[Any]) -> FactCount:
    return count_fact(_contract(), _plan(rule), values)


# ---------------------------------------------------------------------------
# plan_fact
# ---------------------------------------------------------------------------


def test_a_check_reads_its_columns() -> None:
    plan = _plan("non_negative")
    assert plan == facts.FactPlan("orders.non_negative", "orders", "non_negative", ("amount",))


def test_a_check_with_a_when_reads_both_in_the_expose_order() -> None:
    assert _plan("paid_has_date").columns == ("status", "paid_at")


def test_a_derive_reads_its_target_and_its_expression() -> None:
    assert _plan("total_is_price_qty").columns == ("price", "qty", "total")


@pytest.mark.parametrize(
    "rule, words",
    [
        ("n_lines", "aggregate"),
        ("same_status", "row order"),
        ("german", "related"),
        ("german_when", "related"),
        ("broken", "not FLUID SQL/1"),
        ("unknown_column", "not FLUID SQL/1"),
        ("two_forms", "no form, or two"),
        ("dup", "more than once"),
        ("no_column", "reads no column"),
    ],
)
def test_a_rule_that_cannot_be_measured_says_why(rule: str, words: str) -> None:
    plan = _plan(rule)
    assert plan.columns == ()
    assert plan.reason is not None and words in plan.reason


@pytest.mark.parametrize(
    "name, table, rule, event",
    [
        ("orders.non_negative", "ghost", "non_negative", "fact-name-mismatch"),
        ("ghost.non_negative", "ghost", "non_negative", "fact-table-unknown"),
        ("orders.ghost", "orders", "ghost", "fact-rule-unknown"),
        ("non_negative", "orders", "non_negative", "fact-name-mismatch"),
        ("orders.other", "orders", "non_negative", "fact-name-mismatch"),
    ],
)
def test_plan_refusals(name: str, table: str, rule: str, event: str) -> None:
    with pytest.raises(FactRefused) as raised:
        plan_fact(_contract(), name, table, rule)
    assert raised.value.event == event


def test_a_contract_with_no_exposes_refuses_the_table() -> None:
    with pytest.raises(FactRefused) as raised:
        plan_fact({}, "orders.r", "orders", "r")
    assert raised.value.event == "fact-table-unknown"


# ---------------------------------------------------------------------------
# count_fact: values as each source type yields them
# ---------------------------------------------------------------------------


def test_a_check_on_csv_text_and_nulls() -> None:
    # A NULL makes the comparison FALSE: covered, not satisfied.
    got = _count("non_negative", amount=["1.50", "-0.01", "0.00", None, "10"])
    assert got == FactCount(covered=5, satisfied=3)


def test_a_check_on_parquet_decimals() -> None:
    got = _count("non_negative", amount=[Decimal("1.50"), Decimal("-0.01"), None])
    assert got == FactCount(3, 1)


def test_a_check_on_json_numbers_and_strings() -> None:
    assert _count("non_negative", amount=[3, -3, "2.50", "-2.50"]) == FactCount(4, 2)


def test_a_rule_over_a_binary_float_column_is_not_measured() -> None:
    # A binary float has no exact reading; the profiler refuses one everywhere else.
    plan = _plan("ratio_pos")
    assert plan.columns == () and plan.reason is not None and "binary floating" in plan.reason


def test_a_float_value_in_a_decimal_column_is_refused_without_its_value() -> None:
    with pytest.raises(FactRefused) as refused:
        _count("non_negative", amount=[0.25])
    assert refused.value.event == "fact-value-unreadable"
    assert "0.25" not in str(refused.value)


def test_two_valued_not_counts_a_null_comparison_as_satisfied() -> None:
    # The engine reads NOT (NULL < 0) as NOT FALSE: TRUE.
    assert _count("not_negative", amount=[None, "-1", "1"]) == FactCount(3, 2)


def test_a_when_that_is_null_does_not_cover() -> None:
    got = _count(
        "paid_has_date",
        status=["paid", "paid", None, "open", "paid"],
        paid_at=[
            "2024-01-01T00:00:00.000Z",
            None,
            None,
            None,
            datetime.datetime(2024, 1, 2, 10, 0, 0),
        ],
    )
    assert got == FactCount(covered=3, satisfied=2)


def test_a_derive_agrees_on_null_with_null_only() -> None:
    got = _count(
        "total_is_price_qty",
        price=["2.50", "2.50", None, None, "1.00"],
        qty=["4", "4", "4", "4", "2"],
        total=["10.00", "10.01", None, "1.00", None],
    )
    # satisfied: row 1 (equal) and row 3 (NULL and NULL); rows 2, 4, 5 differ.
    assert got == FactCount(5, 2)


def test_a_derive_on_database_values() -> None:
    got = _count(
        "total_is_price_qty",
        price=[Decimal("2.50"), Decimal("0.10")],
        qty=[4, 3],
        total=[Decimal("10.00"), Decimal("0.30")],
    )
    assert got == FactCount(2, 2)


def test_dates_as_text_as_dates_and_as_the_wire_midnight() -> None:
    got = _count(
        "shipped_2024",
        shipped=[
            "2024-01-01",
            datetime.date(2023, 12, 31),
            "2023-12-31T00:00:00.000Z",
            "2025-06-01T00:00:00Z",
            None,
        ],
    )
    assert got == FactCount(5, 2)


def test_booleans_as_text_and_as_values() -> None:
    got = _count("flagged", flag=["true", "false", True, False, None])
    assert got == FactCount(5, 2)


def test_a_boolean_text_other_than_true_or_false_is_refused() -> None:
    with pytest.raises(FactRefused) as refused:
        _count("flagged", flag=["TRUE"])
    assert refused.value.event == "fact-value-unreadable"


def test_timestamps_zone_less_and_labelled_utc_compare() -> None:
    got = _count(
        "created_before_paid",
        created_at=[
            "2024-01-01T10:00:00.000Z",
            "2024-01-01 11:00:00",
            datetime.datetime(2024, 1, 1, 12, 0, 1),
            None,
        ],
        paid_at=[
            datetime.datetime(2024, 1, 1, 10, 0, 0),
            "2024-01-01T10:59:59.000Z",
            "2024-01-01T12:00:00.000Z",
            "2024-01-01T12:00:00.000Z",
        ],
    )
    assert got == FactCount(4, 1)


def test_timestamps_with_zone_compare_as_instants() -> None:
    utc = datetime.timezone.utc
    got = _count(
        "window",
        seen_start=[
            "2024-01-01T00:00:00.000Z",
            datetime.datetime(2024, 1, 1, 5, tzinfo=utc),
            "2024-01-01T00:00:00",
        ],
        seen_end=[
            "2024-01-01T01:00:00+01:00",
            datetime.datetime(2024, 1, 1, 4, tzinfo=utc),
            "2024-01-01T00:00:00.000Z",
        ],
    )
    assert got == FactCount(3, 2)


def test_a_table_with_no_rows_covers_none() -> None:
    assert _count("non_negative", amount=[]) == FactCount(0, 0)


# ---------------------------------------------------------------------------
# Agreement with the engine's reading, worked by hand
# ---------------------------------------------------------------------------


def test_the_engine_counts_the_same_rows() -> None:
    # The engine covers the rows where a `when` is true, treats an unknown text as not
    # satisfied, and holds a derive when the column is not distinct from its value.
    rows = {
        "status": ["paid", "paid", "open", None],
        "paid_at": ["2024-01-01T00:00:00.000Z", None, "2024-01-01T00:00:00.000Z", None],
    }
    assert _count("paid_has_date", **rows) == FactCount(covered=2, satisfied=1)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule, column, bad",
    [
        ("non_negative", "amount", SECRET),
        ("non_negative", "amount", "1_0"),
        ("non_negative", "amount", "1e5000"),
        ("non_negative", "amount", "NaN"),
        ("non_negative", "amount", float("nan")),
        ("non_negative", "amount", True),
        ("flagged", "flag", SECRET),
        ("flagged", "flag", 1),
        ("shipped_2024", "shipped", "2024-13-45"),
        ("shipped_2024", "shipped", SECRET),
        ("shipped_2024", "shipped", datetime.datetime(2024, 1, 1, 10)),
        ("shipped_2024", "shipped", "2024-01-01T10:00:00.000Z"),
        ("created_before_paid", "created_at", SECRET),
        ("created_before_paid", "created_at", "2024-01-01"),
        ("window", "seen_start", 5),
        ("paid_has_date", "status", 7),
    ],
)
def test_a_value_that_cannot_be_read_names_its_column_and_row_only(
    rule: str, column: str, bad: Any
) -> None:
    plan = _plan(rule)
    values: Dict[str, List[Any]] = {name: [None, None] for name in plan.columns}
    values[column] = [None, bad]
    with pytest.raises(FactRefused) as raised:
        count_fact(_contract(), plan, values)
    refusal = raised.value
    assert refusal.event == "fact-value-unreadable"
    assert refusal.context["column"] == column and refusal.context["row"] == "2"
    text = str(refusal) + repr(refusal.context)
    assert column in text
    for shown in (SECRET, "1_0", "5000", "2024-13-45"):
        assert shown not in text


def test_a_rule_that_is_not_measured_is_not_counted() -> None:
    with pytest.raises(FactRefused) as raised:
        count_fact(_contract(), _plan("n_lines"), {})
    assert raised.value.event == "fact-not-measured"


def test_a_missing_column_refuses() -> None:
    with pytest.raises(FactRefused) as raised:
        _count("paid_has_date", status=["paid"])
    assert raised.value.event == "fact-column-missing"
    assert raised.value.context["column"] == "paid_at"


def test_columns_of_different_lengths_refuse() -> None:
    with pytest.raises(FactRefused) as raised:
        _count("paid_has_date", status=["paid", "open"], paid_at=[None])
    assert raised.value.event == "fact-column-length"


def test_a_plan_the_contract_does_not_give_refuses() -> None:
    stale = facts.FactPlan("orders.non_negative", "orders", "non_negative", ("price",))
    with pytest.raises(FactRefused) as raised:
        count_fact(_contract(), stale, {"price": ["1"]})
    assert raised.value.event == "fact-plan-stale"


def test_a_plan_for_another_table_refuses_by_its_name() -> None:
    bad = facts.FactPlan("orders.non_negative", "ghost", "non_negative", ())
    with pytest.raises(FactRefused) as raised:
        count_fact(_contract(), bad, {})
    assert raised.value.event == "fact-name-mismatch"
