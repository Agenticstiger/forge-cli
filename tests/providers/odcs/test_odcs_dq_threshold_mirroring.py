# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""FLUID's completeness/uniqueness/valid_values threshold is the share of rows that PASS.

ODCS ``nullValues`` / ``duplicateValues`` / ``invalidValues`` count the BAD rows, so the
comparison is mirrored onto the bad share (``unit: percent``), as the SodaCL exporter does.
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


def test_uniqueness_99_percent_is_at_most_1_percent_duplicates() -> None:
    check = _export("uniqueness", threshold=0.99, operator=">=")
    assert check["metric"] == "duplicateValues"
    assert check["unit"] == "percent"
    assert _cmp(check) == {"mustBeLessOrEqualTo": 1}


def test_valid_values_threshold_mirrors_onto_invalid_share() -> None:
    check = _export("valid_values", threshold=0.9, operator=">")
    assert check["metric"] == "invalidValues"
    assert check["unit"] == "percent"
    assert _cmp(check) == {"mustBeLessThan": 10}


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
