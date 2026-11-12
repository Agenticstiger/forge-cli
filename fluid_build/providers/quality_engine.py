# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from fluid_build.providers._sql_safety import quote_string_literal_for
from fluid_build.providers.validation_provider import ValidationIssue

LOG = logging.getLogger("fluid.providers.quality_engine")

# Safe SQL identifier regex (letters, digits, underscores only)
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")


def _validate_ident(name: str) -> str:
    if not _SAFE_IDENT.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


# Dialects that read ``"x"`` as a string literal, not as a column: an
# identifier there needs backticks. ``COUNT(DISTINCT "col")`` on Databricks
# or BigQuery counts one constant, so a uniqueness or completeness rule
# quoted the ANSI way passes whatever the data holds.
_BACKTICK_DIALECTS = frozenset({"bigquery", "databricks"})

# The floating-point type the ratio casts use. ``DOUBLE PRECISION`` is ANSI,
# DuckDB and Snowflake; Spark SQL and BigQuery do not accept it.
_DOUBLE_TYPES = {"bigquery": "FLOAT64", "databricks": "DOUBLE"}


def _dialect(dialect: Optional[str]) -> str:
    return (dialect or "ansi").strip().lower()


def _qi(column: str, dialect: Optional[str] = "ansi") -> str:
    """``column`` validated and quoted as an identifier of ``dialect``."""
    col = _validate_ident(column)
    return f"`{col}`" if _dialect(dialect) in _BACKTICK_DIALECTS else f'"{col}"'


def _double(dialect: Optional[str]) -> str:
    return _DOUBLE_TYPES.get(_dialect(dialect), "DOUBLE PRECISION")


def rule_columns(selector: Any) -> List[str]:
    """The columns a rule's ``selector`` names, in order.

    ``dqRule.selector`` is one column, ``"*"``, or a comma-separated list of
    columns (the industry packs write ``site_id,meter_id,reading_ts`` for
    "all mandatory attributes"). Completeness and accuracy rules hold for
    every listed column; the other column rules take exactly one.
    """
    if not isinstance(selector, str):
        return []
    return [part.strip() for part in selector.split(",") if part.strip()]


def _column_constraint(columns: Any, name: str, kind: str) -> Optional[str]:
    """The ``constraint`` of the first ``validationRules[]`` entry of ``kind`` on
    the contract column called ``name`` (case-insensitive), if any."""
    if not isinstance(columns, (list, tuple)) or not isinstance(name, str):
        return None
    for col in columns:
        if not isinstance(col, dict) or str(col.get("name", "")).lower() != name.lower():
            continue
        for entry in col.get("validationRules") or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("type") or "").strip().lower() != kind:
                continue
            constraint = entry.get("constraint")
            if isinstance(constraint, str) and constraint.strip():
                return constraint.strip()
    return None


def expose_columns(expose: Any) -> List[Dict[str, Any]]:
    """The expose's declared columns (``contract.schema``, else ``schema``)."""
    if not isinstance(expose, dict):
        return []
    contract = expose.get("contract")
    columns = contract.get("schema") if isinstance(contract, dict) else None
    if not columns:
        columns = expose.get("schema")
    return [c for c in columns if isinstance(c, dict)] if isinstance(columns, list) else []


def extract_reference(rule: Dict[str, Any], columns: Any = None) -> Optional[Tuple[str, str]]:
    """``(product id, column)`` that a ``valid_values`` rule's column references.

    Used when the rule names no values and its column declares no ``enum`` or
    ``regex``: the column's ``labels.references: <product id>.<column>`` (the
    foreign-key convention the Unity Catalog keys read too) makes a valid value
    one that the referenced product's column holds.
    """
    name = rule.get("selector") or ""
    if not isinstance(columns, (list, tuple)) or not isinstance(name, str):
        return None
    for col in columns:
        if not isinstance(col, dict) or str(col.get("name", "")).lower() != name.lower():
            continue
        labels = col.get("labels")
        target = labels.get("references") if isinstance(labels, dict) else None
        if isinstance(target, str):
            product, _, column = target.strip().rpartition(".")
            if product and column:
                return product, column
    return None


_NUMBER = r"-?\d+(?:\.\d+)?"
_RANGE_BETWEEN = re.compile(rf"^\s*between\s+({_NUMBER})\s+and\s+({_NUMBER})\s*$", re.IGNORECASE)
_RANGE_BOUND = re.compile(rf"^\s*(>=|<=|>|<)\s*({_NUMBER})\s*$")
_RANGE_AND = re.compile(r"\s+and\s+", re.IGNORECASE)


def parse_range(constraint: Any) -> List[Tuple[str, float]]:
    """The bounds of a ``range`` validationRule: ``">= 0"``, ``">= 0 and <= 100"``
    or ``"between 0 and 100"`` (``[]`` for anything else)."""
    if not isinstance(constraint, str):
        return []
    m = _RANGE_BETWEEN.match(constraint)
    if m:
        bounds = [(">=", float(m.group(1))), ("<=", float(m.group(2)))]
    else:
        bounds = []
        for part in _RANGE_AND.split(constraint.strip()):
            m = _RANGE_BOUND.match(part)
            if not m:
                return []
            bounds.append((m.group(1), float(m.group(2))))
    # A bound too large for a float would reach SQL as ``inf``: not a number.
    return bounds if all(math.isfinite(n) for _, n in bounds) else []


@dataclass(frozen=True)
class ValueDomain:
    """What a valid value of one column is: the rule's own value list, or else
    every declaration the column makes (its ``enum``, ``regex`` and ``range``
    validationRules and its ``labels.references``), all of which must hold."""

    values: Tuple[str, ...] = ()
    pattern: Optional[str] = None
    bounds: Tuple[Tuple[str, float], ...] = ()
    reference: Optional[Tuple[str, str]] = None


def value_domain(rule: Dict[str, Any], columns: Any = None, column: Optional[str] = None):
    """The :class:`ValueDomain` of ``column`` (default: the rule's selector)."""
    sub = rule if column is None else {**rule, "selector": column}
    listed = extract_valid_values(sub)
    if listed:
        return ValueDomain(values=tuple(listed))
    name = sub.get("selector") or ""
    enum = _column_constraint(columns, name, "enum")
    return ValueDomain(
        values=tuple(v.strip() for v in re.split(r"[|,]", enum) if v.strip()) if enum else (),
        pattern=extract_value_pattern(sub, columns),
        bounds=tuple(parse_range(_column_constraint(columns, name, "range"))),
        reference=extract_reference(sub, columns),
    )


#: ``resolve_reference(product id, column) -> table reference for SQL``; raises
#: ``LookupError`` (with a message that says why) when the table cannot be found.
ReferenceResolver = Callable[[str, str], str]


def extract_value_pattern(rule: Dict[str, Any], columns: Any = None) -> Optional[str]:
    """The regular expression a ``valid_values`` rule's column must match.

    Used only when the rule names no allowed values (see
    :func:`extract_valid_values`): the selected column's own ``regex``
    validationRule is then what "a valid value" means
    (``country_code: ^[A-Z]{2}$``).
    """
    return _column_constraint(columns, rule.get("selector") or "", "regex")


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


def _completeness_expr(column: str, dialect: str) -> str:
    return (
        f"CAST(SUM(CASE WHEN {_qi(column, dialect)} IS NOT NULL THEN 1 ELSE 0 END) "
        f"AS {_double(dialect)}) / NULLIF(COUNT(*), 0)"
    )


def _completeness_sql(table_ref: str, column: str, dialect: str = "ansi") -> str:
    """Generate SQL to compute non-null fraction for a column."""
    return f"SELECT {_completeness_expr(column, dialect)} AS completeness_ratio FROM {table_ref}"


def _completeness_columns_sql(table_ref: str, columns: List[str], dialect: str) -> str:
    """One statement: the non-null fraction of each of ``columns``, in order."""
    ratios = ", ".join(_completeness_expr(c, dialect) for c in columns)
    return f"SELECT {ratios} FROM {table_ref}"


def _uniqueness_sql(table_ref: str, column: str, dialect: str = "ansi") -> str:
    """Generate SQL to compute distinct-value fraction for a column."""
    col = _qi(column, dialect)
    return (
        "SELECT "
        f"CAST(COUNT(DISTINCT {col}) AS {_double(dialect)}) "
        f"/ NULLIF(COUNT({col}), 0) AS uniqueness_ratio "
        f"FROM {table_ref}"
    )


def _accuracy_min_sql(table_ref: str, column: str, dialect: str = "ansi") -> str:
    """Generate SQL to get the minimum value of a numeric column."""
    return f"SELECT MIN({_qi(column, dialect)}) AS min_val FROM {table_ref}"


def _accuracy_max_sql(table_ref: str, column: str, dialect: str = "ansi") -> str:
    """Generate SQL to get the maximum value of a numeric column."""
    return f"SELECT MAX({_qi(column, dialect)}) AS max_val FROM {table_ref}"


def _accuracy_bound_columns_sql(
    table_ref: str, columns: List[str], aggregate: str, dialect: str
) -> str:
    """One statement: ``MIN`` (or ``MAX``) of each of ``columns``, in order."""
    func = "MAX" if aggregate == "max" else "MIN"
    bounds = ", ".join(f"{func}({_qi(c, dialect)})" for c in columns)
    return f"SELECT {bounds} FROM {table_ref}"


def _violation_predicate(column: str, operator: str, threshold, dialect: str) -> str:
    col = _qi(column, dialect)
    negated = _NEGATED_OPERATORS[operator]
    # ``threshold`` is schema-typed ``number``; coerce so nothing but a
    # numeric literal can reach the SQL text.
    literal = float(threshold)
    return f"{col} IS NOT NULL AND {col} {negated} {literal}"


def _accuracy_violations_sql(
    table_ref: str, column: str, operator: str, threshold, dialect: str = "ansi"
) -> str:
    """Count rows whose value does NOT satisfy ``value <operator> threshold``.

    Used for equality/inequality bounds, where no single aggregate can
    decide the rule: ``MIN(col) == t`` says nothing about the rest of
    the column.
    """
    return (
        f"SELECT COUNT(*) AS violation_count FROM {table_ref} "
        f"WHERE {_violation_predicate(column, operator, threshold, dialect)}"
    )


def _accuracy_violations_columns_sql(
    table_ref: str, columns: List[str], operator: str, threshold, dialect: str
) -> str:
    """One statement: the violating-row count of each of ``columns``, in order."""
    counts = ", ".join(
        f"SUM(CASE WHEN {_violation_predicate(c, operator, threshold, dialect)} "
        "THEN 1 ELSE 0 END)"
        for c in columns
    )
    return f"SELECT {counts} FROM {table_ref}"


def _validity_pattern_sql(table_ref: str, column: str, pattern: str, dialect: str) -> str:
    """Count non-null values of ``column`` that do not match ``pattern`` anywhere.

    Each dialect's search-anywhere regex test (Snowflake's ``REGEXP_LIKE``
    matches the whole value, so ``REGEXP_INSTR`` stands in for it); an
    anchored pattern (``^[A-Z]{2}$``) means the same on all of them.
    """
    col = _qi(column, dialect)
    return (
        f"SELECT COUNT(*) AS invalid_count FROM {table_ref} "
        f"WHERE {col} IS NOT NULL AND {_pattern_mismatch(col, pattern, dialect)}"
    )


def _pattern_mismatch(col: str, pattern: str, dialect: str) -> str:
    """The predicate "the (quoted) column ``col`` does not match ``pattern``"."""
    literal = quote_string_literal_for(dialect, pattern)
    name = _dialect(dialect)
    if name == "databricks":
        return f"NOT ({col} RLIKE {literal})"
    if name == "bigquery":
        return f"NOT REGEXP_CONTAINS({col}, {literal})"
    if name == "snowflake":
        return f"REGEXP_INSTR({col}, {literal}) = 0"
    if name in ("ansi", "duckdb"):
        return f"NOT regexp_matches({col}, {literal})"
    raise ValueError(f"no regular-expression test for SQL dialect {dialect!r}")


def _range_violation(col: str, bounds: Tuple[Tuple[str, float], ...]) -> str:
    """The predicate "the (quoted) column ``col`` is outside ``bounds``"."""
    inside = " AND ".join(f"{col} {op} {float(n)!r}" for op, n in bounds if op in _OPERATORS)
    return f"NOT ({inside})"


def _validity_range_sql(
    table_ref: str, column: str, bounds: Tuple[Tuple[str, float], ...], dialect: str
) -> str:
    """Count non-null values of ``column`` outside the declared bounds."""
    col = _qi(column, dialect)
    return (
        f"SELECT COUNT(*) AS invalid_count FROM {table_ref} "
        f"WHERE {col} IS NOT NULL AND {_range_violation(col, bounds)}"
    )


def _freshness_sql(table_ref: str, column: str, dialect: str = "ansi") -> str:
    """Generate SQL to get age of most-recent timestamp value (in seconds)."""
    col = _validate_ident(column)
    if dialect == "databricks":
        return (
            f"SELECT timestampdiff(SECOND, MAX(`{col}`), current_timestamp()) AS age_seconds "
            f"FROM {table_ref}"
        )
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


def extract_valid_values(rule: Dict[str, Any], columns: Any = None) -> List[str]:
    """Allowed values for a ``validity`` / ``valid_values`` rule.

    ``$defs.dqRule`` is ``additionalProperties: false`` and has no
    ``validValues`` key, so schema-valid contracts declare the list inside
    the description: ``"COLUMN valid values: A, B, C."``. An explicit
    ``validValues`` list still wins when present (hand-written files).

    With ``columns`` (the expose's ``contract.schema``), a rule that names no
    values takes them from its column's own ``enum`` validationRule
    (``constraint: "GOOD | ESTIMATED | SUSPECT"``, ``|`` or ``,`` separated):
    that is where a contract already declares them, and where the industry
    packs do.

    Shared by the native engine and the SodaCL exporter so the two cannot
    disagree about which values a rule allows.
    """
    explicit = rule.get("validValues") or []
    if explicit:
        return [str(v) for v in explicit]
    description = rule.get("description") or ""
    if isinstance(description, str) and " valid values:" in description.lower():
        m = re.search(r"valid values:\s*([^.]+)", description, re.IGNORECASE)
        if m:
            values = [v.strip() for v in m.group(1).split(",") if v.strip()]
            if values:
                return values
    enum = _column_constraint(columns, rule.get("selector") or "", "enum")
    if enum:
        return [v.strip() for v in re.split(r"[|,]", enum) if v.strip()]
    return []


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def execute_quality_checks(
    rules: List[Dict[str, Any]],
    table_ref: str,
    execute_fn,
    dialect: str = "ansi",
    columns: Any = None,
    resolve_reference: Optional[ReferenceResolver] = None,
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
        SQL dialect hint: ``"ansi"``, ``"snowflake"``, ``"bigquery"``,
        ``"databricks"``.
    columns : list[dict], optional
        The expose's ``contract.schema``. A ``valid_values`` rule that names
        no values reads its column's ``enum`` (or ``regex``) validationRule, or
        its ``labels.references``.
    resolve_reference : callable, optional
        ``(product id, column) -> table reference`` of a referenced product;
        without it a reference rule is reported as not executed.

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
            if rule_type in _VALIDITY_TYPES and len(rule_columns(selector)) > 1:
                result = _check_validity_columns(
                    rule, columns, table_ref, execute_fn, dialect, resolve_reference
                )
            else:
                result = _execute_single_rule(
                    rule_id=rule_id,
                    rule_type=rule_type,
                    selector=selector,
                    severity=severity,
                    description=description,
                    threshold=threshold,
                    operator=operator,
                    domain=value_domain(rule, columns),
                    resolve_reference=resolve_reference,
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

# Rule types whose comma-separated selector means "every listed column" (a
# valid_values rule checks each listed column against its own declaration).
_MULTI_COLUMN_RULE_TYPES = frozenset(
    {"completeness", "accuracy", "anomaly_detection", "validity", "valid_values"}
)
_VALIDITY_TYPES = frozenset({"validity", "valid_values"})

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
    domain: ValueDomain,
    window: Optional[str],
    table_ref: str,
    execute_fn,
    dialect: str,
    resolve_reference: Optional[ReferenceResolver] = None,
) -> QualityCheckResult:

    columns = rule_columns(selector)
    if len(columns) > 1 and rule_type not in _MULTI_COLUMN_RULE_TYPES:
        # ``uniqueness`` over a list would mean a composite key and
        # ``valid_values`` / ``freshness`` one column's domain or clock;
        # guessing either would check something the author did not write.
        return QualityCheckResult(
            rule_id=rule_id,
            rule_type=rule_type,
            selector=selector,
            passed=False,
            severity=severity,
            message=(
                f"Rule '{rule_id}' is a {rule_type} rule over {len(columns)} columns; "
                f"a {rule_type} rule takes one column, so nothing was checked. "
                "Write one rule per column."
            ),
            expected="one column",
            actual="not executed",
        )

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
            dialect,
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
            dialect,
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
            dialect,
        )
    elif rule_type in _VALIDITY_TYPES:
        # ``valid_values`` is the schema's name; ``validity`` the older alias.
        return _check_validity(
            rule_id,
            selector,
            severity,
            description,
            domain,
            table_ref,
            execute_fn,
            dialect,
            rule_type=rule_type,
            resolve_reference=resolve_reference,
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
            dialect,
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
    dialect: str = "ansi",
) -> QualityCheckResult:
    columns = rule_columns(selector)
    if len(columns) > 1:
        return _check_completeness_columns(
            rule_id,
            selector,
            columns,
            severity,
            description,
            threshold,
            operator,
            table_ref,
            execute_fn,
            dialect,
        )
    sql = _completeness_sql(table_ref, selector, dialect)
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


def _row_values(rows, width: int) -> List[Any]:
    """The first result row padded to ``width`` (an empty result reads as NULLs)."""
    first = list(rows[0]) if rows else []
    return [first[i] if i < len(first) else None for i in range(width)]


def _listed(pairs: List[tuple], fmt) -> str:
    """``'a' (x), 'b' (y)`` for at most five pairs, then how many more."""
    shown = ", ".join(f"'{name}' ({fmt(value)})" for name, value in pairs[:5])
    return shown + (f" and {len(pairs) - 5} more" if len(pairs) > 5 else "")


def _check_completeness_columns(
    rule_id,
    selector,
    columns: List[str],
    severity,
    description,
    threshold,
    operator,
    table_ref,
    execute_fn,
    dialect: str,
) -> QualityCheckResult:
    """A completeness rule over several columns: each must meet the bound.

    One statement measures every column. The observed value is the lowest
    ratio, and the message names each column below the bound.
    """
    rows = execute_fn(_completeness_columns_sql(table_ref, columns, dialect))
    ratios = [float(v) if v is not None else 0.0 for v in _row_values(rows, len(columns))]

    def meets(ratio: float) -> bool:
        return _compare(ratio, threshold, operator) if threshold is not None else ratio == 1.0

    failing = [(c, r) for c, r in zip(columns, ratios, strict=True) if not meets(r)]
    return QualityCheckResult(
        rule_id=rule_id,
        rule_type="completeness",
        selector=selector,
        passed=not failing,
        severity=severity,
        message=(
            f"{description or rule_id} — completeness is below the bound for "
            f"{_listed(failing, lambda r: f'{r:.2%}')}"
            if failing
            else f"Completeness OK for all {len(columns)} columns"
        ),
        expected=f"{operator} {threshold}" if threshold is not None else "1.0",
        actual=f"{min(ratios):.4f}",
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
    dialect: str = "ansi",
) -> QualityCheckResult:
    sql = _uniqueness_sql(table_ref, selector, dialect)
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
    dialect: str = "ansi",
) -> QualityCheckResult:
    if threshold is None:
        # A bound with nothing to compare against checks nothing; reading it as
        # passed whenever the column holds a value made a gate read green.
        return QualityCheckResult(
            rule_id=rule_id,
            rule_type="accuracy",
            selector=selector,
            passed=False,
            severity=severity,
            message=(
                f"Rule '{rule_id}' is an accuracy rule with no threshold, so nothing was "
                "checked. Declare a threshold and an operator (>=, >, <=, <, == or !=)."
            ),
            expected="a threshold to compare with",
            actual="not executed",
        )
    columns = rule_columns(selector)
    if len(columns) > 1:
        return _check_accuracy_columns(
            rule_id,
            selector,
            columns,
            severity,
            description,
            threshold,
            operator,
            table_ref,
            execute_fn,
            dialect,
        )
    # A bound rule asserts something about EVERY row, so the aggregate
    # that decides it depends on the direction of the bound. Always
    # asking for MIN() made every upper bound ('<=', '<') trivially true
    # and every equality bound undecidable.
    if threshold is not None and operator in _NEGATED_OPERATORS:
        sql = _accuracy_violations_sql(table_ref, selector, operator, threshold, dialect)
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
        _accuracy_max_sql(table_ref, selector, dialect)
        if use_max
        else _accuracy_min_sql(table_ref, selector, dialect)
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


def _check_accuracy_columns(
    rule_id,
    selector,
    columns: List[str],
    severity,
    description,
    threshold,
    operator,
    table_ref,
    execute_fn,
    dialect: str,
) -> QualityCheckResult:
    """An accuracy rule over several columns: every column must hold the bound.

    One statement measures every column, with the same aggregate the
    single-column rule uses (``MIN`` for a lower bound, ``MAX`` for an upper
    one, a violating-row count for ``==`` / ``!=``). A column with no values
    fails as "no data", as it does on its own.
    """
    if threshold is not None and operator in _NEGATED_OPERATORS:
        rows = execute_fn(
            _accuracy_violations_columns_sql(table_ref, columns, operator, threshold, dialect)
        )
        counts = [int(v) if v is not None else 0 for v in _row_values(rows, len(columns))]
        failing = [(c, n) for c, n in zip(columns, counts, strict=True) if n]
        return QualityCheckResult(
            rule_id=rule_id,
            rule_type="accuracy",
            selector=selector,
            passed=not failing,
            severity=severity,
            message=(
                f"{description or rule_id} — rows that do not satisfy {operator} {threshold} "
                f"in {_listed(failing, str)}"
                if failing
                else f"Accuracy OK for all {len(columns)} columns"
            ),
            expected=f"0 rows violating {operator} {threshold}",
            actual=f"{sum(counts)} violating row(s)",
        )

    aggregate = "max" if operator in _UPPER_BOUND_OPERATORS else "min"
    rows = execute_fn(_accuracy_bound_columns_sql(table_ref, columns, aggregate, dialect))
    bounds = _row_values(rows, len(columns))
    empty = [c for c, v in zip(columns, bounds, strict=True) if v is None]
    failing = [
        (c, v)
        for c, v in zip(columns, bounds, strict=True)
        if v is not None and threshold is not None and not _compare(v, threshold, operator)
    ]
    problems = []
    if failing:
        problems.append(f"{aggregate} value is outside the bound for {_listed(failing, str)}")
    if empty:
        problems.append("no data in " + ", ".join(f"'{c}'" for c in empty[:5]))
    present = [v for v in bounds if v is not None]
    worst = None
    if present:
        try:
            worst = (max if aggregate == "max" else min)(float(v) for v in present)
        except (TypeError, ValueError):
            worst = None
    return QualityCheckResult(
        rule_id=rule_id,
        rule_type="accuracy",
        selector=selector,
        passed=not problems,
        severity=severity,
        message=(
            f"{description or rule_id} — " + "; ".join(problems)
            if problems
            else f"Accuracy OK for all {len(columns)} columns"
        ),
        expected=f"{operator} {threshold}" if threshold is not None else "pass",
        actual=None if worst is None else f"{worst:g}",
    )


def _check_validity(
    rule_id,
    selector,
    severity,
    description,
    domain: ValueDomain,
    table_ref,
    execute_fn,
    dialect: str = "ansi",
    *,
    rule_type: str = "validity",
    resolve_reference: Optional[ReferenceResolver] = None,
) -> QualityCheckResult:
    col = _qi(selector, dialect)
    violations: List[str] = []
    kinds: List[str] = []
    if domain.values:
        # The literals are contract-supplied, so the quoting must hold on the
        # *target* dialect (a value ending in a backslash must not end the
        # literal early and append live SQL to this predicate).
        escaped = ", ".join(quote_string_literal_for(dialect, str(v)) for v in domain.values)
        violations.append(f"{col} NOT IN ({escaped})")
        kinds.append("invalid values")
    if domain.pattern:
        violations.append(_pattern_mismatch(col, domain.pattern, dialect))
        kinds.append(f"values not matching {domain.pattern}")
    if domain.bounds:
        violations.append(_range_violation(col, domain.bounds))
        kinds.append("values outside " + " and ".join(f"{op} {n:g}" for op, n in domain.bounds))
    if not violations and domain.reference is not None:
        return _check_reference(
            rule_id,
            selector,
            severity,
            description,
            domain.reference,
            resolve_reference,
            table_ref,
            execute_fn,
            dialect,
            rule_type,
        )
    if violations:
        invalid = violations[0] if len(violations) == 1 else f"({' OR '.join(violations)})"
        sql = (
            f"SELECT COUNT(*) AS invalid_count FROM {table_ref} "
            f"WHERE {col} IS NOT NULL AND {invalid}"
        )
        expected = "0 " + kinds[0] if len(kinds) == 1 else "0 " + "; 0 ".join(kinds)
    else:
        # A misconfigured gate is a failed gate. Forcing 'warning' here
        # overrode the author's declared severity, so a rule declared
        # `severity: error` that checks nothing at all exited 0.
        return QualityCheckResult(
            rule_id=rule_id,
            rule_type=rule_type,
            selector=selector,
            passed=False,
            severity=severity,
            message=(
                f"Rule '{rule_id}' is a valid_values rule but declares no allowed "
                "values, so nothing was checked. Declare them in the description "
                f"(\"{selector or 'COLUMN'} valid values: A, B, C.\"), as an enum, regex "
                "or range validationRule on the column, as the column's "
                "labels.references, or as a 'validValues' list."
            ),
            expected="a non-empty list of allowed values",
            actual="no allowed values declared",
        )
    rows = execute_fn(sql)
    invalid_count = rows[0][0] if rows and rows[0][0] is not None else 0
    passed = int(invalid_count) == 0
    result = QualityCheckResult(
        rule_id=rule_id,
        rule_type=rule_type,
        selector=selector,
        passed=passed,
        severity=severity,
        message=(
            f"{description or rule_id} — {invalid_count} invalid value(s) in '{selector}'"
            if not passed
            else f"Validity OK for '{selector}'"
        ),
        expected=expected,
        actual=f"{invalid_count} invalid value(s)",
    )
    if domain.reference is None:
        return result
    # The column also references another product: that must hold too.
    reference = _check_reference(
        rule_id,
        selector,
        severity,
        description,
        domain.reference,
        resolve_reference,
        table_ref,
        execute_fn,
        dialect,
        rule_type,
    )
    failed = [r.message for r in (result, reference) if not r.passed]
    return QualityCheckResult(
        rule_id=rule_id,
        rule_type=rule_type,
        selector=selector,
        passed=not failed,
        severity=severity,
        message="; ".join(failed) if failed else result.message,
        expected=f"{result.expected}; {reference.expected}",
        actual=(
            "not executed"
            if reference.actual == "not executed"
            else f"{result.actual}; {reference.actual}"
        ),
    )


_NOT_RUN = ("not executed", "no allowed values declared")


def _check_validity_columns(
    rule: Dict[str, Any],
    columns: Any,
    table_ref,
    execute_fn,
    dialect: str,
    resolve_reference: Optional[ReferenceResolver],
) -> QualityCheckResult:
    """A valid_values rule over several columns: each column against its own
    declared values (the rule's list, else each column's validationRule or
    reference). Passes when every column was checked and holds no invalid value."""
    rule_id = rule.get("id", "unnamed")
    rule_type = rule.get("type", "").lower()
    selector = rule.get("selector", "")
    severity = rule.get("severity", "warning")
    names = rule_columns(selector)
    parts = [
        _check_validity(
            rule_id,
            name,
            severity,
            rule.get("description", ""),
            value_domain(rule, columns, name),
            table_ref,
            execute_fn,
            dialect,
            rule_type=rule_type,
            resolve_reference=resolve_reference,
        )
        for name in names
    ]
    skipped = [n for n, r in zip(names, parts, strict=True) if r.actual in _NOT_RUN]
    failing = [
        (n, r.actual)
        for n, r in zip(names, parts, strict=True)
        if not r.passed and n not in skipped
    ]
    problems = []
    if failing:
        problems.append("invalid values in " + _listed(failing, str))
    if skipped:
        problems.append("nothing checked for " + ", ".join(f"'{n}'" for n in skipped[:5]))
    total = 0
    for result in parts:
        head = str(result.actual or "").split(" ", 1)[0]
        total += int(head) if head.isdigit() else 0
    return QualityCheckResult(
        rule_id=rule_id,
        rule_type=rule_type,
        selector=selector,
        passed=not problems,
        severity=severity,
        message=(
            f"{rule.get('description') or rule_id} — " + "; ".join(problems)
            if problems
            else f"Validity OK for all {len(names)} columns"
        ),
        expected="0 invalid values in every column",
        actual="not executed" if skipped else f"{total} invalid value(s)",
    )


def _check_reference(
    rule_id,
    selector,
    severity,
    description,
    reference: Tuple[str, str],
    resolve_reference: Optional[ReferenceResolver],
    table_ref,
    execute_fn,
    dialect: str,
    rule_type: str,
) -> QualityCheckResult:
    """Every non-null value of ``selector`` exists in the referenced column."""
    product, column = reference
    target = f"{product}.{column}"
    expected = f"every value in {target}"

    def not_run(why: str) -> QualityCheckResult:
        return QualityCheckResult(
            rule_id=rule_id,
            rule_type=rule_type,
            selector=selector,
            passed=False,
            severity=severity,
            message=f"Rule '{rule_id}' checks '{selector}' against {target}, but {why}, "
            "so nothing was checked",
            expected=expected,
            actual="not executed",
        )

    if resolve_reference is None:
        return not_run("this platform cannot look up another product's table")
    try:
        parent = resolve_reference(product, column)
    except LookupError as exc:
        return not_run(str(exc))
    col, pcol = _qi(selector, dialect), _qi(column, dialect)
    sql = (
        f"SELECT COUNT(*) AS missing_count FROM {table_ref} AS c "
        f"WHERE c.{col} IS NOT NULL AND NOT EXISTS "
        f"(SELECT 1 FROM {parent} AS p WHERE p.{pcol} = c.{col})"
    )
    rows = execute_fn(sql)
    missing = int(rows[0][0]) if rows and rows[0][0] is not None else 0
    return QualityCheckResult(
        rule_id=rule_id,
        rule_type=rule_type,
        selector=selector,
        passed=missing == 0,
        severity=severity,
        message=(
            f"{description or rule_id} — {missing} value(s) of '{selector}' are not in {target}"
            if missing
            else f"Every '{selector}' value is in {target}"
        ),
        expected=f"0 values missing from {target}",
        actual=f"{missing} value(s) missing",
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
    dialect: str = "ansi",
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
            rule_id,
            selector,
            severity,
            description,
            threshold,
            operator,
            table_ref,
            execute_fn,
            dialect,
        )
