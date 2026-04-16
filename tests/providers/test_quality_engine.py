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

"""Tests for the quality engine — SQL generation, check execution, result conversion."""

from __future__ import annotations

import pytest

from fluid_build.providers.quality_engine import (
    QualityCheckResult,
    _accuracy_min_sql,
    _completeness_sql,
    _freshness_sql,
    _parse_duration_seconds,
    _uniqueness_sql,
    _validate_ident,
    execute_quality_checks,
    quality_results_to_issues,
)

# ---------------------------------------------------------------------------
# SQL generation helpers
# ---------------------------------------------------------------------------


class TestCompletenessSql:
    def test_basic(self):
        sql = _completeness_sql("my_col", "my_table")
        assert "my_col" in sql
        assert "my_table" in sql
        assert "COUNT" in sql.upper()

    def test_returns_non_null_fraction(self):
        sql = _completeness_sql("email", "users")
        assert "email" in sql
        assert "users" in sql


class TestUniquenessSql:
    def test_basic(self):
        sql = _uniqueness_sql("order_id", "orders")
        assert "order_id" in sql
        assert "orders" in sql
        assert "DISTINCT" in sql.upper() or "COUNT" in sql.upper()


class TestAccuracySql:
    def test_min_value(self):
        sql = _accuracy_min_sql("price", "products")
        assert "price" in sql
        assert "products" in sql
        assert "MIN" in sql.upper()


class TestFreshnessSql:
    def test_snowflake_dialect(self):
        sql = _freshness_sql("updated_at", "events", dialect="snowflake")
        assert "updated_at" in sql
        assert "DATEDIFF" in sql.upper() or "TIMESTAMPDIFF" in sql.upper()

    def test_bigquery_dialect(self):
        sql = _freshness_sql("updated_at", "events", dialect="bigquery")
        assert "TIMESTAMP_DIFF" in sql.upper()

    def test_ansi_dialect(self):
        sql = _freshness_sql("updated_at", "events", dialect="ansi")
        assert "updated_at" in sql


# ---------------------------------------------------------------------------
# Duration parser
# ---------------------------------------------------------------------------


class TestParseDuration:
    def test_hours(self):
        assert _parse_duration_seconds("1h") == 3600

    def test_minutes(self):
        assert _parse_duration_seconds("30m") == 1800

    def test_days(self):
        assert _parse_duration_seconds("7d") == 604800

    def test_seconds(self):
        assert _parse_duration_seconds("120s") == 120

    def test_bare_number_returns_none(self):
        # bare numbers without units are not supported
        result = _parse_duration_seconds("60")
        assert result is None

    def test_invalid_returns_none(self):
        result = _parse_duration_seconds("abc")
        assert result is None or result == 0


# ---------------------------------------------------------------------------
# Identifier validation
# ---------------------------------------------------------------------------


class TestValidateIdent:
    def test_valid_simple(self):
        assert _validate_ident("my_column") == "my_column"

    def test_valid_with_numbers(self):
        assert _validate_ident("col_123") == "col_123"

    def test_rejects_sql_injection(self):
        with pytest.raises(ValueError):
            _validate_ident("col; DROP TABLE--")

    def test_rejects_spaces(self):
        with pytest.raises(ValueError):
            _validate_ident("col name")


# ---------------------------------------------------------------------------
# execute_quality_checks (integration-style with mock execute_fn)
# ---------------------------------------------------------------------------


class TestExecuteQualityChecks:
    def _make_executor(self, return_values):
        """Return a mock execute_fn that returns canned results."""
        calls = []
        idx = [0]

        def _exec(sql):
            calls.append(sql)
            val = return_values[idx[0]] if idx[0] < len(return_values) else [(0,)]
            idx[0] += 1
            return val

        _exec.calls = calls
        return _exec

    def test_completeness_pass(self):
        rules = [
            {
                "id": "no_nulls",
                "type": "completeness",
                "selector": "customer_id",
                "threshold": 0.95,
                "operator": ">=",
                "severity": "error",
            }
        ]
        executor = self._make_executor([[(1.0,)]])
        results = execute_quality_checks(rules, "my_table", executor)
        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].rule_id == "no_nulls"

    def test_completeness_fail(self):
        rules = [
            {
                "id": "no_nulls",
                "type": "completeness",
                "selector": "customer_id",
                "threshold": 1.0,
                "operator": ">=",
                "severity": "error",
            }
        ]
        executor = self._make_executor([[(0.85,)]])
        results = execute_quality_checks(rules, "my_table", executor)
        assert len(results) == 1
        assert results[0].passed is False

    def test_uniqueness_pass(self):
        rules = [
            {
                "id": "unique_ids",
                "type": "uniqueness",
                "selector": "order_id",
                "threshold": 1.0,
                "operator": ">=",
                "severity": "error",
            }
        ]
        executor = self._make_executor([[(1.0,)]])
        results = execute_quality_checks(rules, "orders", executor)
        assert len(results) == 1
        assert results[0].passed is True

    def test_validity_pass(self):
        rules = [
            {
                "id": "valid_status",
                "type": "validity",
                "selector": "status",
                "validValues": ["active", "inactive"],
                "severity": "warning",
            }
        ]
        # validity check returns (invalid_count,) — 0 means pass
        executor = self._make_executor([[(0,)]])
        results = execute_quality_checks(rules, "users", executor)
        assert len(results) == 1
        assert results[0].passed is True

    def test_accuracy_pass(self):
        rules = [
            {
                "id": "positive_price",
                "type": "accuracy",
                "selector": "price",
                "threshold": 0,
                "operator": ">=",
                "severity": "warning",
            }
        ]
        executor = self._make_executor([[(5.0,)]])
        results = execute_quality_checks(rules, "products", executor)
        assert len(results) == 1
        assert results[0].passed is True

    def test_accuracy_fail(self):
        rules = [
            {
                "id": "positive_price",
                "type": "accuracy",
                "selector": "price",
                "threshold": 0,
                "operator": ">=",
                "severity": "warning",
            }
        ]
        executor = self._make_executor([[(-1.0,)]])
        results = execute_quality_checks(rules, "products", executor)
        assert len(results) == 1
        assert results[0].passed is False

    def test_unknown_type_skipped(self):
        rules = [
            {
                "id": "unkn",
                "type": "nonexistent_check",
                "selector": "x",
                "severity": "error",
            }
        ]
        executor = self._make_executor([])
        results = execute_quality_checks(rules, "t", executor)
        assert len(results) == 1
        assert results[0].passed is False
        assert (
            "unsupported" in results[0].message.lower() or "unknown" in results[0].message.lower()
        )

    def test_multiple_rules(self):
        rules = [
            {
                "id": "r1",
                "type": "completeness",
                "selector": "a",
                "threshold": 0.9,
                "operator": ">=",
                "severity": "error",
            },
            {
                "id": "r2",
                "type": "uniqueness",
                "selector": "b",
                "threshold": 0.9,
                "operator": ">=",
                "severity": "warning",
            },
        ]
        executor = self._make_executor([[(0.95,)], [(0.99,)]])
        results = execute_quality_checks(rules, "t", executor)
        assert len(results) == 2
        assert all(r.passed for r in results)


# ---------------------------------------------------------------------------
# quality_results_to_issues
# ---------------------------------------------------------------------------


class TestQualityResultsToIssues:
    def test_passed_result_produces_no_issue(self):
        results = [
            QualityCheckResult(
                rule_id="r1",
                rule_type="completeness",
                selector="col",
                passed=True,
                severity="error",
                message="OK",
                expected=0.95,
                actual=1.0,
            )
        ]
        issues = quality_results_to_issues(results)
        # Passed results don't generate issues
        assert len(issues) == 0

    def test_failed_error_result(self):
        results = [
            QualityCheckResult(
                rule_id="r1",
                rule_type="completeness",
                selector="col",
                passed=False,
                severity="error",
                message="Completeness 0.5 < 0.95",
                expected=0.95,
                actual=0.5,
            )
        ]
        issues = quality_results_to_issues(results)
        assert len(issues) == 1
        assert issues[0].severity == "error"

    def test_failed_warning_result(self):
        results = [
            QualityCheckResult(
                rule_id="r1",
                rule_type="uniqueness",
                selector="col",
                passed=False,
                severity="warning",
                message="Uniqueness 0.8 < 1.0",
                expected=1.0,
                actual=0.8,
            )
        ]
        issues = quality_results_to_issues(results)
        assert len(issues) == 1
        assert issues[0].severity == "warning"
