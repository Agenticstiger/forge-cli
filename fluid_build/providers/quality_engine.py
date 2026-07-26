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

"""
Data quality check engine for live data validation.

Executes DQ rules declared in ``contract.dq.rules`` against actual data
via provider-specific SQL engines.  Supports five rule types:

- **completeness** — fraction of non-null values for a column
- **uniqueness**   — fraction of distinct values for a column
- **accuracy**     — comparison of column values against a threshold
- **validity**     — column values must be within an allowed set
- **freshness**    — maximum age of the most recent timestamp in a column
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fluid_build.providers._sql_safety import quote_string_literal
from fluid_build.providers.validation_provider import ValidationIssue

LOG = logging.getLogger("fluid.providers.quality_engine")

# Safe SQL identifier regex (letters, digits, underscores only)
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_ident(name: str) -> str:
    if not _SAFE_IDENT.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


@dataclass
class QualityCheckResult:
    """Result of a single data quality check."""

    rule_id: str
    rule_type: str
    selector: str
    passed: bool
    severity: str  # error | warning | info
    message: str
    expected: Optional[Any] = None
    actual: Optional[Any] = None


# ------------------------------------------------------------------
# SQL generators per rule type
# ------------------------------------------------------------------


def _completeness_sql(table_ref: str, column: str) -> str:
    """Generate SQL to compute non-null fraction for a column."""
    col = _validate_ident(column)
    return (
        "SELECT "
        f'CAST(SUM(CASE WHEN "{col}" IS NOT NULL THEN 1 ELSE 0 END) AS DOUBLE PRECISION) '
        "/ NULLIF(COUNT(*), 0) AS completeness_ratio "
        f"FROM {table_ref}"
    )


def _uniqueness_sql(table_ref: str, column: str) -> str:
    """Generate SQL to compute distinct-value fraction for a column."""
    col = _validate_ident(column)
    return (
        "SELECT "
        f'CAST(COUNT(DISTINCT "{col}") AS DOUBLE PRECISION) '
        f'/ NULLIF(COUNT("{col}"), 0) AS uniqueness_ratio '
        f"FROM {table_ref}"
    )


def _accuracy_min_sql(table_ref: str, column: str) -> str:
    """Generate SQL to get the minimum value of a numeric column."""
    col = _validate_ident(column)
    return f'SELECT MIN("{col}") AS min_val FROM {table_ref}'


def _accuracy_max_sql(table_ref: str, column: str) -> str:
    """Generate SQL to get the maximum value of a numeric column."""
    col = _validate_ident(column)
    return f'SELECT MAX("{col}") AS max_val FROM {table_ref}'


def _accuracy_violations_sql(table_ref: str, column: str, operator: str, threshold) -> str:
    """Count rows whose value does NOT satisfy ``value <operator> threshold``.

    Used for equality/inequality bounds, where no single aggregate can
    decide the rule: ``MIN(col) == t`` says nothing about the rest of
    the column.
    """
    col = _validate_ident(column)
    negated = _NEGATED_OPERATORS[operator]
    # ``threshold`` is schema-typed ``number``; coerce so nothing but a
    # numeric literal can reach the SQL text.
    literal = float(threshold)
    return (
        f"SELECT COUNT(*) AS violation_count FROM {table_ref} "
        f'WHERE "{col}" IS NOT NULL AND "{col}" {negated} {literal}'
    )


def _freshness_sql(table_ref: str, column: str, dialect: str = "ansi") -> str:
    """Generate SQL to get age of most-recent timestamp value (in seconds)."""
    col = _validate_ident(column)
    if dialect == "snowflake":
        return (
            f"SELECT DATEDIFF('second', MAX(\"{col}\"), CURRENT_TIMESTAMP()) AS age_seconds "
            f"FROM {table_ref}"
        )
    elif dialect == "bigquery":
        return (
            f"SELECT TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(`{col}`), SECOND) AS age_seconds "
            f"FROM {table_ref}"
        )
    else:
        # ANSI / DuckDB / Athena (Presto)
        return (
            f'SELECT EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - MAX("{col}"))) AS age_seconds '
            f"FROM {table_ref}"
        )


# ------------------------------------------------------------------
# Comparator
# ------------------------------------------------------------------

_OPERATORS = {
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
    "==": lambda a, b: a == b,
    "=": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}

# Which aggregate decides a bound rule for the whole column.
#
# ``value >= t`` holds for every row iff ``MIN(value) >= t``; ``value <= t``
# holds for every row iff ``MAX(value) <= t``. Issuing MIN for *every*
# operator meant an upper bound was decided by the smallest value in the
# column, so ``ACCOUNT_BALANCE <= 5000`` passed against a column whose
# minimum is -998.97 and whose maximum is 9999.99.
_LOWER_BOUND_OPERATORS = frozenset({">=", ">"})
_UPPER_BOUND_OPERATORS = frozenset({"<=", "<"})

# Equality / inequality bounds are not decidable from a single aggregate —
# they are evaluated by counting violating rows instead.
_NEGATED_OPERATORS = {
    "==": "<>",
    "=": "<>",
    "!=": "=",
}


def _compare(actual, threshold, operator: str) -> bool:
    fn = _OPERATORS.get(operator)
    if fn is None:
        raise ValueError(f"Unsupported operator: {operator!r}")
    try:
        return fn(float(actual), float(threshold))
    except (TypeError, ValueError):
        return False


# ------------------------------------------------------------------
# Freshness duration parser  (e.g. "1h", "30m", "7d", "3600s", "PT6H")
# ------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])$", re.IGNORECASE)

_DURATION_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Pure-weeks ISO shape (P2W). Schema-valid per $defs/isoDuration, but the
# shared parser below rejects weeks for streaming-runner reasons that
# don't apply here — a week is always exactly 7 days.
_ISO_WEEKS_RE = re.compile(r"^P(\d+)W$")


def _parse_duration_seconds(value: str) -> Optional[int]:
    """Parse a duration string into seconds.

    Accepts both the legacy human shorthand ('1h', '30m', '7d',
    '3600s') and the ISO-8601 durations the contract schema requires
    for ``dqRule.window`` ('PT6H', 'PT90M', 'P2D', 'P1W'). Returns
    ``None`` for anything without a fixed length in seconds —
    calendar-dependent shapes (months/years) and degenerate or
    non-positive ISO values ('P', 'PT0S').
    """
    text = value.strip()
    m = _DURATION_RE.match(text)
    if m:
        return int(m.group(1)) * _DURATION_MULTIPLIERS[m.group(2).lower()]
    m = _ISO_WEEKS_RE.match(text)
    if m:
        return int(m.group(1)) * 7 * 86400
    # Deferred so providers don't hard-depend on build_runners at
    # import time (the edge stays confined to this call path).
    from fluid_build.build_runners._late_arrival import parse_iso_duration

    td = parse_iso_duration(text)
    if td is None:
        return None
    seconds = int(td.total_seconds())
    return seconds if seconds > 0 else None


#: Public alias. The SodaCL exporter normalises ``dqRule.window`` through the
#: *same* parser the native engine uses, so a window the native engine accepts
#: can never be silently reinterpreted (or dropped) by the Soda engine.
parse_duration_seconds = _parse_duration_seconds


def extract_valid_values(rule: Dict[str, Any]) -> List[str]:
    """Allowed values for a ``validity`` / ``valid_values`` rule.

    ``$defs.dqRule`` is ``additionalProperties: false`` and has no
    ``validValues`` key, so schema-valid contracts declare the list inside
    the description: ``"COLUMN valid values: A, B, C."``. An explicit
    ``validValues`` list still wins when present (hand-written files).

    Shared by the native engine and the SodaCL exporter so the two cannot
    disagree about which values a rule allows.
    """
    explicit = rule.get("validValues") or []
    if explicit:
        return [str(v) for v in explicit]
    description = rule.get("description") or ""
    if not isinstance(description, str) or " valid values:" not in description.lower():
        return []
    m = re.search(r"valid values:\s*([^.]+)", description, re.IGNORECASE)
    if not m:
        return []
    return [v.strip() for v in m.group(1).split(",") if v.strip()]


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def execute_quality_checks(
    rules: List[Dict[str, Any]],
    table_ref: str,
    execute_fn,
    dialect: str = "ansi",
) -> List[QualityCheckResult]:
    """
    Execute a list of DQ rules against a live table.

    Parameters
    ----------
    rules : list[dict]
        DQ rules from ``contract.dq.rules``.
    table_ref : str
        Fully-qualified (and quoted if needed) table reference for SQL.
    execute_fn : callable
        ``execute_fn(sql) -> list[tuple]`` — runs a SQL statement and
        returns the result rows.
    dialect : str
        SQL dialect hint: ``"ansi"``, ``"snowflake"``, ``"bigquery"``.

    Returns
    -------
    list[QualityCheckResult]
    """
    results: List[QualityCheckResult] = []

    for rule in rules:
        rule_id = rule.get("id", "unnamed")
        rule_type = rule.get("type", "").lower()
        selector = rule.get("selector", "")
        severity = rule.get("severity", "warning")
        description = rule.get("description", "")
        threshold = rule.get("threshold")
        operator = rule.get("operator", ">=")

        if not selector:
            results.append(
                QualityCheckResult(
                    rule_id=rule_id,
                    rule_type=rule_type,
                    selector="",
                    passed=False,
                    severity=severity,
                    message=f"Rule '{rule_id}' missing 'selector' (column name)",
                )
            )
            continue

        try:
            # Support validValues from explicit key OR parsed from description
            # Description format: "column valid values: val1, val2, val3."
            # Shared with the SodaCL exporter so both engines read the same list.
            explicit_vv = extract_valid_values(rule)

            result = _execute_single_rule(
                rule_id=rule_id,
                rule_type=rule_type,
                selector=selector,
                severity=severity,
                description=description,
                threshold=threshold,
                operator=operator,
                valid_values=explicit_vv,
                window=rule.get("window", rule.get("freshness")),
                table_ref=table_ref,
                execute_fn=execute_fn,
                dialect=dialect,
            )
            results.append(result)
        except Exception as e:
            results.append(
                QualityCheckResult(
                    rule_id=rule_id,
                    rule_type=rule_type,
                    selector=selector,
                    passed=False,
                    severity=severity,
                    message=f"Error executing rule '{rule_id}': {e}",
                )
            )

    return results


def quality_results_to_issues(
    results: List[QualityCheckResult],
    path_prefix: str = "contract.dq.rules",
) -> List[ValidationIssue]:
    """Convert QualityCheckResults into ValidationIssues for the report."""
    issues: List[ValidationIssue] = []
    for r in results:
        if not r.passed:
            issues.append(
                ValidationIssue(
                    severity=r.severity,
                    category="quality",
                    message=r.message,
                    path=f"{path_prefix}.{r.rule_id}",
                    expected=r.expected,
                    actual=r.actual,
                )
            )
    return issues


# ------------------------------------------------------------------
# Internal dispatch
# ------------------------------------------------------------------

# Rule types this engine can actually execute.
_IMPLEMENTED_RULE_TYPES = frozenset(
    {
        "completeness",
        "uniqueness",
        "accuracy",
        "validity",
        "valid_values",
        "freshness",
        "anomaly_detection",
    }
)

# Rule types ``$defs.dqRule.type`` accepts but this engine cannot run.
# They are reported as failures at the rule's declared severity so a gate
# nobody is enforcing can never read as green.
_UNIMPLEMENTED_RULE_TYPES = frozenset({"schema", "drift_detection"})


def _execute_single_rule(
    *,
    rule_id: str,
    rule_type: str,
    selector: str,
    severity: str,
    description: str,
    threshold,
    operator: str,
    valid_values: List[str],
    window: Optional[str],
    table_ref: str,
    execute_fn,
    dialect: str,
) -> QualityCheckResult:

    if rule_type == "completeness":
        return _check_completeness(
            rule_id,
            selector,
            severity,
            description,
            threshold,
            operator,
            table_ref,
            execute_fn,
        )
    elif rule_type == "uniqueness":
        return _check_uniqueness(
            rule_id,
            selector,
            severity,
            description,
            threshold,
            operator,
            table_ref,
            execute_fn,
        )
    elif rule_type == "accuracy":
        return _check_accuracy(
            rule_id,
            selector,
            severity,
            description,
            threshold,
            operator,
            table_ref,
            execute_fn,
        )
    elif rule_type == "validity":
        return _check_validity(
            rule_id,
            selector,
            severity,
            description,
            valid_values,
            table_ref,
            execute_fn,
        )
    elif rule_type == "freshness":
        return _check_freshness(
            rule_id,
            selector,
            severity,
            description,
            window,
            table_ref,
            execute_fn,
            dialect,
        )
    elif rule_type == "anomaly_detection":
        # Row-count anomaly detection: selector '*' means COUNT(*) >= threshold
        return _check_anomaly_detection(
            rule_id,
            selector,
            severity,
            description,
            threshold,
            operator,
            table_ref,
            execute_fn,
        )
    elif rule_type == "valid_values":
        # Alias for validity — checks that column values are in the validValues list
        return _check_validity(
            rule_id,
            selector,
            severity,
            description,
            valid_values,
            table_ref,
            execute_fn,
        )
    elif rule_type in _UNIMPLEMENTED_RULE_TYPES:
        # Schema-legal but not executable by the native engine. Reporting
        # this at a hardcoded 'warning' meant a governance gate the author
        # declared `severity: error` silently exited 0 — the author
        # believes the gate is enforced and nothing is enforcing it.
        #
        # The remedy deliberately does NOT offer `--engine soda`: the Soda
        # engine has no faithful SodaCL rendering for these two types either
        # (see exporters/sodacl._NO_SODACL_EQUIVALENT), so routing the author
        # there would just move the unenforced gate to a different command.
        return QualityCheckResult(
            rule_id=rule_id,
            rule_type=rule_type,
            selector=selector,
            passed=False,
            severity=severity,
            message=(
                f"Rule '{rule_id}' has type '{rule_type}', which the native "
                "quality engine does not implement — this gate is NOT being "
                "enforced. No fluid engine implements it (`--engine soda` "
                "reports it as unmapped too); express the check as one of: "
                f"{', '.join(sorted(_IMPLEMENTED_RULE_TYPES))}."
            ),
            expected=f"an implemented rule type ({rule_type} is not)",
            actual="not executed",
        )
    else:
        return QualityCheckResult(
            rule_id=rule_id,
            rule_type=rule_type,
            selector=selector,
            passed=False,
            severity=severity,
            message=(
                f"Unknown DQ rule type '{rule_type}' for rule '{rule_id}' — "
                f"expected one of: {', '.join(sorted(_IMPLEMENTED_RULE_TYPES))}"
            ),
        )


# ------------------------------------------------------------------
# Individual check implementations
# ------------------------------------------------------------------


def _check_completeness(
    rule_id,
    selector,
    severity,
    description,
    threshold,
    operator,
    table_ref,
    execute_fn,
) -> QualityCheckResult:
    sql = _completeness_sql(table_ref, selector)
    rows = execute_fn(sql)
    ratio = rows[0][0] if rows and rows[0][0] is not None else 0.0
    passed = _compare(ratio, threshold, operator) if threshold is not None else ratio == 1.0
    return QualityCheckResult(
        rule_id=rule_id,
        rule_type="completeness",
        selector=selector,
        passed=passed,
        severity=severity,
        message=(
            f"{description or rule_id} — completeness for '{selector}' is {float(ratio):.2%}"
            if not passed
            else f"Completeness OK for '{selector}'"
        ),
        expected=f"{operator} {threshold}" if threshold is not None else "1.0",
        actual=f"{float(ratio):.4f}",
    )


def _check_uniqueness(
    rule_id,
    selector,
    severity,
    description,
    threshold,
    operator,
    table_ref,
    execute_fn,
) -> QualityCheckResult:
    sql = _uniqueness_sql(table_ref, selector)
    rows = execute_fn(sql)
    ratio = rows[0][0] if rows and rows[0][0] is not None else 0.0
    passed = _compare(ratio, threshold, operator) if threshold is not None else ratio == 1.0
    return QualityCheckResult(
        rule_id=rule_id,
        rule_type="uniqueness",
        selector=selector,
        passed=passed,
        severity=severity,
        message=(
            f"{description or rule_id} — uniqueness for '{selector}' is {float(ratio):.2%}"
            if not passed
            else f"Uniqueness OK for '{selector}'"
        ),
        expected=f"{operator} {threshold}" if threshold is not None else "1.0",
        actual=f"{float(ratio):.4f}",
    )


def _check_accuracy(
    rule_id,
    selector,
    severity,
    description,
    threshold,
    operator,
    table_ref,
    execute_fn,
) -> QualityCheckResult:
    # A bound rule asserts something about EVERY row, so the aggregate
    # that decides it depends on the direction of the bound. Always
    # asking for MIN() made every upper bound ('<=', '<') trivially true
    # and every equality bound undecidable.
    if threshold is not None and operator in _NEGATED_OPERATORS:
        sql = _accuracy_violations_sql(table_ref, selector, operator, threshold)
        rows = execute_fn(sql)
        violations = rows[0][0] if rows and rows[0][0] is not None else 0
        passed = int(violations) == 0
        return QualityCheckResult(
            rule_id=rule_id,
            rule_type="accuracy",
            selector=selector,
            passed=passed,
            severity=severity,
            message=(
                f"{description or rule_id} — {violations} row(s) in '{selector}' "
                f"do not satisfy {operator} {threshold}"
                if not passed
                else f"Accuracy OK for '{selector}'"
            ),
            expected=f"0 rows violating {operator} {threshold}",
            actual=f"{violations} violating row(s)",
        )

    use_max = operator in _UPPER_BOUND_OPERATORS
    aggregate = "max" if use_max else "min"
    sql = (
        _accuracy_max_sql(table_ref, selector)
        if use_max
        else _accuracy_min_sql(table_ref, selector)
    )
    rows = execute_fn(sql)
    bound_val = rows[0][0] if rows and rows[0][0] is not None else None
    if bound_val is None:
        return QualityCheckResult(
            rule_id=rule_id,
            rule_type="accuracy",
            selector=selector,
            passed=False,
            severity=severity,
            message=f"No data to check accuracy for '{selector}'",
        )
    passed = _compare(bound_val, threshold, operator) if threshold is not None else True
    return QualityCheckResult(
        rule_id=rule_id,
        rule_type="accuracy",
        selector=selector,
        passed=passed,
        severity=severity,
        message=(
            f"{description or rule_id} — {aggregate} value of '{selector}' is {bound_val}"
            if not passed
            else f"Accuracy OK for '{selector}'"
        ),
        expected=f"{operator} {threshold}" if threshold is not None else "pass",
        actual=str(bound_val),
    )


def _check_validity(
    rule_id,
    selector,
    severity,
    description,
    valid_values,
    table_ref,
    execute_fn,
) -> QualityCheckResult:
    if not valid_values:
        # A misconfigured gate is a failed gate. Forcing 'warning' here
        # overrode the author's declared severity, so a rule declared
        # `severity: error` that checks nothing at all exited 0.
        return QualityCheckResult(
            rule_id=rule_id,
            rule_type="validity",
            selector=selector,
            passed=False,
            severity=severity,
            message=(
                f"Rule '{rule_id}' is a valid_values rule but declares no allowed "
                "values, so nothing was checked. Declare them in the description "
                f"(\"{selector or 'COLUMN'} valid values: A, B, C.\") or as a "
                "'validValues' list."
            ),
            expected="a non-empty list of allowed values",
            actual="no allowed values declared",
        )
    col = _validate_ident(selector)
    # Build SQL with quoted string literals for valid values
    escaped = ", ".join(quote_string_literal(str(v)) for v in valid_values)
    sql = (
        f"SELECT COUNT(*) AS invalid_count FROM {table_ref} "
        f'WHERE "{col}" IS NOT NULL AND "{col}" NOT IN ({escaped})'
    )
    rows = execute_fn(sql)
    invalid_count = rows[0][0] if rows and rows[0][0] is not None else 0
    passed = invalid_count == 0
    return QualityCheckResult(
        rule_id=rule_id,
        rule_type="validity",
        selector=selector,
        passed=passed,
        severity=severity,
        message=(
            f"{description or rule_id} — {invalid_count} invalid value(s) in '{selector}'"
            if not passed
            else f"Validity OK for '{selector}'"
        ),
        expected="0 invalid values",
        actual=f"{invalid_count} invalid value(s)",
    )


def _check_freshness(
    rule_id,
    selector,
    severity,
    description,
    window,
    table_ref,
    execute_fn,
    dialect,
) -> QualityCheckResult:
    sql = _freshness_sql(table_ref, selector, dialect)
    rows = execute_fn(sql)
    age_seconds = rows[0][0] if rows and rows[0][0] is not None else None

    if age_seconds is None:
        return QualityCheckResult(
            rule_id=rule_id,
            rule_type="freshness",
            selector=selector,
            passed=False,
            severity=severity,
            message=f"No data to check freshness for '{selector}'",
        )

    if window:
        max_age_seconds = _parse_duration_seconds(str(window))
        if max_age_seconds is None:
            # A window was declared but can't be turned into a bound.
            # Failing loudly beats silently running an unbounded check —
            # a typo'd window must not disable a quality gate.
            return QualityCheckResult(
                rule_id=rule_id,
                rule_type="freshness",
                selector=selector,
                passed=False,
                severity=severity,
                message=(
                    f"Unparseable freshness window '{window}' for '{selector}' — "
                    "use ISO-8601 ('PT6H', 'P2D') or shorthand ('6h', '2d')"
                ),
            )
    else:
        # No threshold specified — just report the age
        return QualityCheckResult(
            rule_id=rule_id,
            rule_type="freshness",
            selector=selector,
            passed=True,
            severity="info",
            message=f"Freshness for '{selector}': data is {float(age_seconds):.0f}s old (no threshold set)",
            actual=f"{float(age_seconds):.0f}s",
        )

    passed = float(age_seconds) <= float(max_age_seconds)
    return QualityCheckResult(
        rule_id=rule_id,
        rule_type="freshness",
        selector=selector,
        passed=passed,
        severity=severity,
        message=(
            f"{description or rule_id} — data is {float(age_seconds):.0f}s old, max allowed is {max_age_seconds}s"
            if not passed
            else f"Freshness OK for '{selector}' ({float(age_seconds):.0f}s old)"
        ),
        expected=f"<= {max_age_seconds}s",
        actual=f"{float(age_seconds):.0f}s",
    )


def _check_anomaly_detection(
    rule_id,
    selector,
    severity,
    description,
    threshold,
    operator,
    table_ref,
    execute_fn,
) -> QualityCheckResult:
    """Anomaly detection via row-count check (selector '*') or MIN/MAX check for a column."""
    if selector == "*" or selector == "":
        sql = f"SELECT COUNT(*) AS row_count FROM {table_ref}"
        rows = execute_fn(sql)
        row_count = rows[0][0] if rows and rows[0][0] is not None else 0
        passed = (
            _compare(row_count, threshold, operator) if threshold is not None else row_count > 0
        )
        return QualityCheckResult(
            rule_id=rule_id,
            rule_type="anomaly_detection",
            selector=selector,
            passed=passed,
            severity=severity,
            message=(
                f"{description or rule_id} — row count {row_count} does not satisfy {operator} {threshold}"
                if not passed
                else f"Anomaly detection OK: row count = {row_count}"
            ),
            expected=f"{operator} {threshold}" if threshold is not None else "> 0",
            actual=str(row_count),
        )
    else:
        # Column-level anomaly: delegate to the bound check, which picks
        # MIN or MAX (or a violating-row count) from the operator.
        return _check_accuracy(
            rule_id, selector, severity, description, threshold, operator, table_ref, execute_fn
        )
