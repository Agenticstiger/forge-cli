# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""FLUID's completeness/uniqueness/valid_values threshold is the share of rows that PASS.

ODCS ``nullValues`` / ``duplicateValues`` / ``invalidValues`` count the BAD rows. A full share
is ``mustBe: 0``; a partial completeness share is mirrored onto the null share
(``unit: percent``); valid_values is every value, as ``fluid test`` checks it; a partial
uniqueness share has no ODCS equivalent and is not exported. The SodaCL exporter does the same.
Copying ``threshold: 1.0, operator: '>='`` straight across used to export "at least one
null", the opposite of the rule.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.providers.odcs.mappers.quality import _dq_rule_to_quality

pytestmark = pytest.mark.unit


def _export(rule_type: str, **kw: Any) -> Dict[str, Any]:
    check, reason = _dq_rule_to_quality({"type": rule_type, "selector": "c", **kw})
    assert check is not None, reason
    return check


def _cmp(check: Dict[str, Any]) -> Dict[str, Any]:
    drop = {"type", "metric", "dimension", "description", "severity", "unit"}
    return {k: v for k, v in check.items() if k not in drop}


@pytest.mark.parametrize(
    "rule_type,metric",
    [
        ("completeness", "nullValues"),
        ("uniqueness", "duplicateValues"),
        ("valid_values", "invalidValues"),
    ],
)
def test_full_share_means_zero_bad_rows(rule_type: str, metric: str) -> None:
    for kw in ({}, {"threshold": 1.0, "operator": ">="}, {"threshold": 1, "operator": "=="}):
        check = _export(rule_type, **kw)
        assert check["metric"] == metric
        assert _cmp(check) == {"mustBe": 0}
        assert "unit" not in check


def test_completeness_95_percent_is_at_most_5_percent_nulls() -> None:
    check = _export("completeness", threshold=0.95, operator=">=")
    assert check["metric"] == "nullValues"
    assert check["unit"] == "percent"
    assert _cmp(check) == {"mustBeLessOrEqualTo": 5}


def test_a_partial_uniqueness_share_is_not_exported() -> None:
    # FLUID's uniqueness share is distinct over non-null values: for [a, a, a, a, b] that is
    # 2/5, a bad share of 60%, while "rows involved in duplicates" is 80%. No ODCS
    # duplicateValues percent says what the rule says (the SodaCL exporter refuses it too).
    check, reason = _dq_rule_to_quality(
        {"type": "uniqueness", "selector": "c", "threshold": 0.99, "operator": ">="}
    )
    assert check is None
    assert "partial uniqueness" in reason


def test_valid_values_is_every_value_whatever_the_threshold() -> None:
    # ``fluid test`` checks every value of a valid_values rule, and its share is over non-null
    # values while ODCS's percent is over all rows: the export says what fluid test checks.
    for kw in ({"threshold": 0.9, "operator": ">"}, {"threshold": 0.5}):
        check = _export("valid_values", **kw)
        assert check["metric"] == "invalidValues"
        assert _cmp(check) == {"mustBe": 0}
        assert "unit" not in check


@pytest.mark.parametrize(
    "operator,expected",
    [
        (">=", "mustBeLessOrEqualTo"),
        (">", "mustBeLessThan"),
        ("<=", "mustBeGreaterOrEqualTo"),
        ("<", "mustBeGreaterThan"),
        ("==", "mustBe"),
        ("!=", "mustNotBe"),
    ],
)
def test_operator_is_mirrored(operator: str, expected: str) -> None:
    check = _export("completeness", threshold=0.7, operator=operator)
    assert check["unit"] == "percent"
    # (1 - 0.7) * 100 must not leak float noise
    assert _cmp(check) == {expected: 30}


def test_less_than_operator_on_a_full_share_is_not_the_zero_shortcut() -> None:
    check = _export("completeness", threshold=1.0, operator="<")
    assert check["unit"] == "percent"
    assert _cmp(check) == {"mustBeGreaterThan": 0}


def test_operator_without_threshold_falls_back_to_zero() -> None:
    assert _cmp(_export("uniqueness", operator="<=")) == {"mustBe": 0}


def test_a_threshold_without_an_operator_reads_as_at_least() -> None:
    # ``util/dq_checks.py`` (what ``fluid test`` runs) defaults the operator to ``>=``, so a bare
    # 0.95 is "at least 95% pass", not "every row".
    check = _export("completeness", threshold=0.95)
    assert check["unit"] == "percent"
    assert _cmp(check) == {"mustBeLessOrEqualTo": 5}
    assert _cmp(_export("completeness", threshold=1.0)) == {"mustBe": 0}


@pytest.mark.parametrize(
    "threshold", [99, -0.1, 1.5, "high", "0.9", float("nan"), float("inf"), True, 10**400]
)
def test_a_threshold_that_is_not_a_share_is_not_exported(threshold: Any) -> None:
    # ``fluid test`` refuses these; exporting 1 - 99 = -9800 percent would be nonsense.
    check, reason = _dq_rule_to_quality(
        {"type": "completeness", "selector": "c", "threshold": threshold, "operator": ">="}
    )
    assert check is None
    assert "share of rows" in reason


def test_an_unknown_operator_is_not_exported() -> None:
    check, reason = _dq_rule_to_quality(
        {"type": "completeness", "selector": "c", "threshold": 0.9, "operator": "~"}
    )
    assert check is None
    assert "'~'" in reason
