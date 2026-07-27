# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Regression tests: a bound rule must be decided by the right aggregate.

``_check_accuracy`` always issued ``SELECT MIN(col)``, so every
upper-bound rule (``<=`` / ``<``) was trivially satisfied by the
column's smallest value. Proven live: ``ACCOUNT_BALANCE <= 5000``
reported green against a column whose MIN is -998.97 and whose MAX is
9999.99, with 2314 of 5000 rows violating it.

The repo's pre-existing quality-engine tests only ever used ``>=``,
which is why the defect survived.
"""

from __future__ import annotations

import pytest

from fluid_build.providers.quality_engine import execute_quality_checks


class _Recorder:
    """Capture issued SQL and replay canned results."""

    def __init__(self, results):
        self.sql = []
        self._results = list(results)

    def __call__(self, sql):
        self.sql.append(sql)
        return self._results.pop(0)


def _run(rule, results):
    exec_fn = _Recorder(results)
    out = execute_quality_checks([rule], '"DB"."S"."T"', exec_fn)
    return out[0], exec_fn.sql


# ---------------------------------------------------------------------------
# Bound direction
# ---------------------------------------------------------------------------


class TestAccuracyBounds:
    def test_lower_bound_uses_min(self):
        rule = {
            "id": "balance_floor",
            "type": "accuracy",
            "selector": "ACCOUNT_BALANCE",
            "threshold": 0,
            "operator": ">=",
            "severity": "error",
        }
        result, sql = _run(rule, [[(-998.97,)]])
        assert "MIN" in sql[0]
        assert result.passed is False

    def test_upper_bound_uses_max_and_fails(self):
        rule = {
            "id": "balance_cap_5000",
            "type": "accuracy",
            "selector": "ACCOUNT_BALANCE",
            "threshold": 5000,
            "operator": "<=",
            "severity": "error",
        }
        result, sql = _run(rule, [[(9999.99,)]])
        assert "MAX" in sql[0]
        assert "MIN" not in sql[0]
        assert result.passed is False
        assert result.actual == "9999.99"
        assert result.expected == "<= 5000"

    def test_upper_bound_passes_when_max_is_within(self):
        rule = {
            "id": "balance_cap",
            "type": "accuracy",
            "selector": "ACCOUNT_BALANCE",
            "threshold": 5000,
            "operator": "<",
            "severity": "error",
        }
        result, sql = _run(rule, [[(4999.0,)]])
        assert "MAX" in sql[0]
        assert result.passed is True

    @pytest.mark.parametrize("operator,negated", [("==", "<>"), ("=", "<>"), ("!=", "=")])
    def test_equality_bounds_count_violating_rows(self, operator, negated):
        """No single aggregate decides an equality bound.

        ``MIN(col) == t`` says nothing about the other rows, so these
        are evaluated by counting rows that break the rule.
        """
        rule = {
            "id": "status_fixed",
            "type": "accuracy",
            "selector": "CODE",
            "threshold": 1,
            "operator": operator,
            "severity": "error",
        }
        result, sql = _run(rule, [[(7,)]])
        assert "COUNT(*)" in sql[0]
        assert f'"CODE" {negated} 1.0' in sql[0]
        assert result.passed is False
        assert result.actual == "7 violating row(s)"

    def test_equality_bound_passes_with_no_violations(self):
        rule = {
            "id": "status_fixed",
            "type": "accuracy",
            "selector": "CODE",
            "threshold": 1,
            "operator": "==",
            "severity": "error",
        }
        result, _ = _run(rule, [[(0,)]])
        assert result.passed is True

    def test_threshold_is_coerced_to_a_numeric_literal(self):
        """Nothing but a number can reach the generated SQL text."""
        rule = {
            "id": "r",
            "type": "accuracy",
            "selector": "CODE",
            "threshold": "1; DROP TABLE T --",
            "operator": "==",
            "severity": "error",
        }
        result, sql = _run(rule, [[(0,)]])
        assert sql == []  # never executed
        assert result.passed is False
        assert "Error executing rule" in result.message


# ---------------------------------------------------------------------------
# Misconfigured / unimplemented gates must not silently pass
# ---------------------------------------------------------------------------


class TestUnenforcedGates:
    def test_valid_values_without_a_list_keeps_declared_severity(self):
        """A gate that checked nothing was force-downgraded to 'warning'.

        The author declared ``severity: error`` and the run exited 0 —
        a governance gate enforcing nothing while reading as green.
        """
        rule = {
            "id": "segment_allowed",
            "type": "valid_values",
            "selector": "MARKET_SEGMENT",
            "severity": "error",
        }
        result, sql = _run(rule, [])
        assert result.passed is False
        assert result.severity == "error"
        assert sql == []

    @pytest.mark.parametrize("rule_type", ["schema", "drift_detection"])
    def test_unimplemented_schema_legal_types_fail_at_declared_severity(self, rule_type):
        """``$defs.dqRule.type`` accepts eight types; the engine runs six."""
        rule = {
            "id": "gate",
            "type": rule_type,
            "selector": "CUSTOMER_ID",
            "severity": "error",
        }
        result, sql = _run(rule, [])
        assert result.passed is False
        assert result.severity == "error"
        assert "does not implement" in result.message
        assert sql == []

    def test_unknown_type_keeps_declared_severity(self):
        rule = {
            "id": "gate",
            "type": "not_a_real_type",
            "selector": "CUSTOMER_ID",
            "severity": "critical",
        }
        result, _ = _run(rule, [])
        assert result.passed is False
        assert result.severity == "critical"
