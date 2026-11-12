# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A contract's data-quality rules, read as checks an engine can run.

``exposes[].contract.dq.rules[]`` (``$defs.dqRule``, closed in FLUID 0.7.x: ``id``, ``type``,
``selector``, ``threshold``, ``operator``, ``window``, ``severity``, ``description``, ``tags``,
``labels``) says what must hold. :func:`read_rules` reads each rule once, into
:class:`DqCheck` values that say how, for whichever engine runs them (the Databricks dbt job
first: ``iac/providers/databricks_dbt_tests.py``), and into a :class:`DqRefusal` for each rule
no check can run as written. A rule is never skipped in silence.

How each rule type reads:

* ``completeness``: ``not_null`` on each selected column. ``selector`` may list several,
  comma-separated; each gets its own check.
* ``uniqueness``: ``unique`` on the one selected column. A list is refused: it could mean each
  column or their combination, and ``fluid test`` reads one column too.
* ``valid_values``: the allowed values live on each selected column, as its
  ``validationRules`` (``enum`` ``"A | B"`` or ``"A,B"``; ``regex``; ``range`` ``">= 0"``,
  ``">= 0 and <= 100"`` or ``"between 0 and 100"``) or as its
  ``labels.references: <product id>.<column>``, the foreign-key convention the Unity Catalog
  keys read too; ``consumes[]`` says which of that product's exposes holds the column. Each
  declaration is a check. A column with none falls back to values the rule itself lists (``"<column> valid
  values: A, B."`` in its description, as ``fluid test`` reads it); with neither, the rule is
  refused.
* ``accuracy``: every value of each selected column meets ``<operator> <threshold>``, the bound
  ``fluid test`` checks (``providers/quality_engine.py::_check_accuracy``).
* ``freshness``: the newest value of the one selected date or timestamp column is no older than
  ``window`` (ISO 8601). ``operator`` ``<=`` (the default) allows exactly the window's age;
  ``<`` does not.
* ``schema``: each contract column, or each selected one, exists with its declared type.
  Columns the contract does not declare are left alone (additive evolution).
* ``anomaly_detection`` and ``drift_detection`` compare a table with its own history, which a
  check of the table as it stands cannot do: refused.

``threshold`` is the share of rows that pass, from 0 to 1: the non-null share for
completeness and the distinct share of non-null values for uniqueness, as ``fluid test``
computes them, and for valid_values the share of non-null values that are allowed (``fluid
test`` checks every value of a valid_values rule, whatever its threshold). ``1.0`` with ``>=``
or ``==`` (or no threshold at all) means every row, a per-row check; anything else is a
:attr:`DqCheck.share`. Accuracy's threshold is its bound, not a share; freshness and schema
rules take none.

``severity``: ``critical`` and ``error`` fail the run (``error``); ``warn`` and ``info`` are
reported and do not (``warn``), the two levels a dbt test has.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

#: ``dqRule.severity`` -> the level its checks run at.
SEVERITIES: Dict[str, str] = {"critical": "error", "error": "error", "warn": "warn", "info": "warn"}

#: The comparisons a threshold or a bound may use (``$defs.dqRule.operator``).
OPERATORS = (">=", ">", "<=", "<", "==", "!=")

#: What a check asserts; an engine renders each kind or refuses the check.
KINDS = (
    "not_null",
    "unique",
    "accepted_values",
    "regex",
    "bounds",
    "reference",
    "freshness",
    "schema",
)

#: The base types a freshness check can measure.
_TEMPORAL = ("date", "datetime", "smalldatetime", "timestamp")

_NUMBER = r"-?\d+(?:\.\d+)?"
_BETWEEN_RE = re.compile(rf"^\s*between\s+({_NUMBER})\s+and\s+({_NUMBER})\s*$", re.IGNORECASE)
_BOUND_RE = re.compile(rf"^\s*(>=|<=|>|<)\s*({_NUMBER})\s*$")
_AND_RE = re.compile(r"\s+and\s+", re.IGNORECASE)
_NAME_RE = re.compile(r"[^0-9a-z_]+")
#: A control character, or a UTF-16 surrogate with no partner (YAML's "\ud83d" escape
#: loads one), which no UTF-8 file can hold: refused wherever a rule's text reaches a check.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")


@dataclass(frozen=True)
class Reference:
    """Where a value must exist: ``column`` of a table of ``product_id``. ``expose_id`` is the
    expose ``consumes[]`` lists that product by, or ``None`` when it does not list it."""

    product_id: str
    expose_id: Optional[str]
    column: str


@dataclass(frozen=True)
class DqCheck:
    """One check of one rule, on one column or on the table."""

    #: Unique among the checks :func:`read_rules` returned together: the rule id (lower case,
    #: ``[0-9a-z_]``), then ``__<column>`` for one of several columns, then ``__<kind>`` for
    #: one of several declarations on a column.
    name: str
    rule_id: str
    rule_type: str
    expose_id: str
    #: One of :data:`KINDS`.
    kind: str
    #: ``error`` or ``warn`` (:data:`SEVERITIES`).
    severity: str
    column: Optional[str] = None
    #: ``(operator, threshold)``: the share of rows that must pass. ``None``: every row.
    share: Optional[Tuple[str, float]] = None
    #: ``accepted_values``: the allowed values, as text.
    values: Tuple[str, ...] = ()
    #: ``regex``: the pattern every value matches.
    pattern: Optional[str] = None
    #: ``bounds``: ``(operator, number)`` pairs every value meets.
    bounds: Tuple[Tuple[str, float], ...] = ()
    #: ``reference``: where every value must exist.
    reference: Optional[Reference] = None
    #: ``freshness``: the oldest the newest value may be, in seconds.
    max_age_seconds: Optional[int] = None
    #: ``freshness``: the newest value must be younger than the window, not at it.
    strict: bool = False
    #: ``schema``: ``(column, FLUID type)`` for each column that must exist.
    columns: Tuple[Tuple[str, str], ...] = ()
    #: The rule as written, for an engine with more levels than two (Lakeflow fails an
    #: update on ``critical`` and drops the row on ``error``) or a report with one row per
    #: rule: its ``severity`` (lower case), ``threshold`` and ``operator``.
    rule_severity: str = ""
    rule_threshold: Optional[float] = None
    rule_operator: Optional[str] = None


@dataclass(frozen=True)
class DqRefusal:
    """A rule that cannot run as written: what is wrong, and what to change."""

    rule_id: str
    expose_id: str
    message: str
    fix: str
    #: The rule's ``type``: an engine that runs some types another way (Databricks data quality
    #: monitoring learns ``anomaly_detection`` bounds itself) can tell which refusals are its own.
    rule_type: str = ""
    #: The one refusal such an engine takes over: the rule is judged against the table's
    #: history. A rule it cannot read either (a bad severity, an id holding a control
    #: character) is refused with ``needs_history`` false, as any other.
    needs_history: bool = False


@dataclass
class DqRules:
    """The checks for some exposes' rules, and every refusal."""

    checks: List[DqCheck] = field(default_factory=list)
    refusals: List[DqRefusal] = field(default_factory=list)


def read_rules(contract: Mapping[str, Any], exposes: Sequence[Mapping[str, Any]]) -> DqRules:
    """The checks for every ``contract.dq.rules[]`` entry of ``exposes``, named uniquely
    across them, and a refusal for each rule none can run."""
    out = DqRules()
    consumes = [c for c in contract.get("consumes") or [] if isinstance(c, Mapping)]
    product = str(contract.get("id") or "")
    taken: Set[str] = set()
    for exposure in exposes:
        expose_id, columns, rules = _parts(exposure)
        for rule in rules:
            reader = _Rule(rule, expose_id, columns, consumes, product)
            checks = reader.read()
            out.refusals.extend(reader.refusals)
            out.checks.extend(_unique(check, taken) for check in checks)
    return out


def has_rules(exposes: Sequence[Mapping[str, Any]]) -> bool:
    """Whether any of ``exposes`` declares a ``contract.dq.rules[]`` entry."""
    return any(_parts(exposure)[2] for exposure in exposes if isinstance(exposure, Mapping))


def _parts(
    exposure: Mapping[str, Any]
) -> Tuple[str, List[Mapping[str, Any]], List[Mapping[str, Any]]]:
    """An expose's id, its named schema columns and its DQ rules."""
    spec = exposure.get("contract")
    spec = spec if isinstance(spec, Mapping) else {}
    dq = spec.get("dq")
    dq = dq if isinstance(dq, Mapping) else {}
    columns = [
        c
        for c in spec.get("schema") or []
        if isinstance(c, Mapping) and isinstance(c.get("name"), str) and c["name"]
    ]
    rules = dq.get("rules")
    rules = [r for r in rules if isinstance(r, Mapping)] if isinstance(rules, list) else []
    return str(exposure.get("exposeId") or ""), columns, rules


def _base_name(rule_id: str) -> str:
    return _NAME_RE.sub("_", rule_id.lower()).strip("_") or "rule"


def _unique(check: DqCheck, taken: Set[str]) -> DqCheck:
    """``check``, renamed when another check already has its name: ``__<expose>``, then a
    count. Rule ids are unique per expose at most, and nothing promises even that."""
    name = check.name
    if name in taken:
        name = f"{check.name}__{_base_name(check.expose_id)}"
    count = 2
    while name in taken:
        name = f"{check.name}_{count}"
        count += 1
    taken.add(name)
    if name == check.name:
        return check
    return replace(check, name=name)


def _finite(value: Any) -> Optional[float]:
    """``value`` as a finite float; ``None`` for a bool, a non-number, an infinity, NaN, or
    an integer too large for a float (YAML loads any number of digits)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _empty(bounds: Sequence[Tuple[str, float]]) -> bool:
    """Whether no number meets every bound (``between 100 and 0``, ``> 5 and < 1``)."""
    lows = [(v, op == ">") for op, v in bounds if op in (">", ">=")]
    highs = [(v, op == "<") for op, v in bounds if op in ("<", "<=")]
    if not lows or not highs:
        return False
    low = max(lows, key=lambda b: (b[0], b[1]))
    high = min(highs, key=lambda b: (b[0], not b[1]))
    return low[0] > high[0] or (low[0] == high[0] and (low[1] or high[1]))


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def parse_enum(constraint: Any) -> List[str]:
    """An ``enum`` constraint's values: ``"A | B"`` (as the industry packs write it) or
    ``"A,B"`` (as ``fluid import dbt`` writes it), each stripped, empty ones dropped."""
    if not isinstance(constraint, str):
        return []
    parts = constraint.split("|") if "|" in constraint else constraint.split(",")
    return [part.strip() for part in parts if part.strip()]


def parse_range(constraint: Any) -> Optional[Tuple[Tuple[str, float], ...]]:
    """A ``range`` constraint's bounds, or ``None`` when it is not one of ``">= 0"``,
    ``">= 0 and <= 100"`` (any of ``>= > <= <``) or ``"between 0 and 100"``."""
    if not isinstance(constraint, str) or not constraint.strip():
        return None
    between = _BETWEEN_RE.match(constraint)
    if between is not None:
        pairs = [(">=", between.group(1)), ("<=", between.group(2))]
    else:
        pairs = []
        for part in _AND_RE.split(constraint.strip()):
            match = _BOUND_RE.match(part)
            if match is None:
                return None
            pairs.append((match.group(1), match.group(2)))
    bounds = [(op, float(text)) for op, text in pairs]
    if not all(math.isfinite(value) for _op, value in bounds):
        return None  # digits past a float's range
    return tuple(bounds)


def describe(check: DqCheck) -> str:
    """What ``check`` asserts, in a few words, for a report."""
    column = f" {check.column}" if check.column else ""
    if check.share is not None:
        return f"{check.rule_type}{column} (share {check.share[0]} {_number(check.share[1])})"
    return f"{check.rule_type}{column}"


class _Rule:
    """One rule being read; its refusals collect in :attr:`refusals`."""

    def __init__(
        self,
        rule: Mapping[str, Any],
        expose_id: str,
        columns: Sequence[Mapping[str, Any]],
        consumes: Sequence[Mapping[str, Any]],
        product: str,
    ) -> None:
        self.rule = rule
        self.rule_id = str(rule.get("id") or "")
        self.type = str(rule.get("type") or "").strip().lower()
        self.expose_id = expose_id
        self.columns = {str(c["name"]): c for c in columns}
        self.consumes = consumes
        self.product = product
        self.refusals: List[DqRefusal] = []

    # ── reading ───────────────────────────────────────────────────────────────

    def read(self) -> List[DqCheck]:
        if _CONTROL_RE.search(self.rule_id) or _CONTROL_RE.search(self.expose_id):
            self.refuse(
                "has an id (or its expose's) holding a control character or an unpaired "
                "surrogate",
                "Write the id in plain text.",
            )
            return []
        severity = SEVERITIES.get(str(self.rule.get("severity") or "").strip().lower())
        if severity is None:
            self.refuse(
                f"has severity {self.rule.get('severity')!r}",
                "Set severity to critical, error, warn or info.",
            )
            return []
        reader = {
            "completeness": self._completeness,
            "uniqueness": self._uniqueness,
            "valid_values": self._valid_values,
            "accuracy": self._accuracy,
            "freshness": self._freshness,
            "schema": self._schema,
        }.get(self.type)
        if reader is None:
            if self.type in ("anomaly_detection", "drift_detection"):
                self.refuse(
                    "has no check of the table as it stands here: an anomaly or a drift is "
                    "judged against the table's history",
                    "Check it with fluid test against the deployed table (it reads an "
                    "anomaly_detection threshold as a row count), or remove it from this expose.",
                    needs_history=True,
                )
            else:
                self.refuse(
                    "is not a type a check can run",
                    "Use completeness, uniqueness, valid_values, accuracy, freshness or schema.",
                )
            return []
        found = reader()
        if self.refusals:
            return []
        return self._named(found, severity)

    def _named(self, found: List[Dict[str, Any]], severity: str) -> List[DqCheck]:
        base = _base_name(self.rule_id)
        columns = {item.get("column") for item in found}
        per_column: Dict[Any, int] = {}
        for item in found:
            per_column[item.get("column")] = per_column.get(item.get("column"), 0) + 1
        checks = []
        for item in found:
            parts = [base]
            if len(columns) > 1 and item.get("column"):
                parts.append(_base_name(item["column"]))
            if per_column[item.get("column")] > 1:
                parts.append(item["kind"])
            checks.append(
                DqCheck(
                    name="__".join(parts),
                    rule_id=self.rule_id,
                    rule_type=self.type,
                    expose_id=self.expose_id,
                    severity=severity,
                    rule_severity=str(self.rule.get("severity") or "").strip().lower(),
                    rule_threshold=self._number_or_none(self.rule.get("threshold")),
                    rule_operator=self._operator_or_none(),
                    **item,
                )
            )
        return checks

    def _completeness(self) -> List[Dict[str, Any]]:
        columns = self._selected(required=True)
        share = self._share()
        return [{"kind": "not_null", "column": c, "share": share} for c in columns]

    def _uniqueness(self) -> List[Dict[str, Any]]:
        columns = self._selected(required=True)
        share = self._share()
        if len(columns) > 1:
            self.refuse(
                f"selects {len(columns)} columns ({', '.join(columns)}); a uniqueness rule checks "
                "one column, as fluid test reads it, and a list could mean each column or their "
                "combination",
                "Write one uniqueness rule per column.",
            )
            return []
        return [{"kind": "unique", "column": c, "share": share} for c in columns]

    def _valid_values(self) -> List[Dict[str, Any]]:
        columns = self._selected(required=True)
        share = self._share()
        found: List[Dict[str, Any]] = []
        for name in columns:
            declared = self._allowed(name)
            if declared is None:  # refused
                continue
            if not declared:
                declared = self._listed_values(name)
            for item in declared:
                found.append({**item, "column": name, "share": share})
        return found

    def _accuracy(self) -> List[Dict[str, Any]]:
        columns = self._selected(required=True)
        threshold = _finite(self.rule.get("threshold"))
        operator = self._operator()
        if threshold is None:
            self.refuse(
                "has no finite numeric threshold, the bound each value of an accuracy rule "
                "meets",
                "Set threshold to the bound and operator to the comparison (>= 0, say).",
            )
            return []
        if operator is None:
            return []
        return [
            {"kind": "bounds", "column": c, "bounds": ((operator, threshold),)} for c in columns
        ]

    def _freshness(self) -> List[Dict[str, Any]]:
        columns = self._selected(required=True)
        if self.rule.get("threshold") is not None:
            self.refuse(
                "has a threshold, which a freshness rule does not take: its window is the limit",
                "Remove threshold and put the age limit in window (PT2H, say).",
            )
        operator = str(self.rule.get("operator") or "<=").strip()
        if operator not in ("<=", "<"):
            self.refuse(
                f"has operator {operator!r}; a freshness rule bounds the newest value's age from "
                "above",
                "Use <= (no older than the window) or < (younger than it).",
            )
        if len(columns) > 1:
            self.refuse(
                f"selects {len(columns)} columns; a freshness rule measures one date or "
                "timestamp column",
                "Select the one column the age is measured on.",
            )
        window = self.rule.get("window")
        from fluid_build.providers.quality_engine import parse_duration_seconds

        try:
            seconds = parse_duration_seconds(str(window)) if window else None
        except (OverflowError, ValueError):
            seconds = None  # more digits than a duration holds
        if seconds is None:
            self.refuse(
                f"has window {window!r}, which is not a fixed ISO 8601 duration",
                "Set window to a duration such as PT15M, PT2H or P1D (months and years have "
                "no fixed length).",
            )
        for name in columns:
            declared = str(self.columns[name].get("type") or "").strip().lower()
            if not declared.startswith(_TEMPORAL):
                self.refuse(
                    f"measures column {name!r}, of type {declared or 'unknown'}, and the age of "
                    "a value needs a date or timestamp column",
                    "Select a DATE or TIMESTAMP column.",
                )
        if self.refusals:
            return []
        return [
            {
                "kind": "freshness",
                "column": columns[0],
                "max_age_seconds": seconds,
                "strict": operator == "<",
            }
        ]

    def _schema(self) -> List[Dict[str, Any]]:
        if self.rule.get("threshold") is not None:
            self.refuse(
                "has a threshold, which a schema rule does not take",
                "Remove threshold from the rule.",
            )
            return []
        columns = self._selected(required=False)
        if self.refusals:
            return []
        names = columns if columns else list(self.columns)
        if not names:
            self.refuse(
                "checks the expose's schema, and exposes[].contract.schema declares no column",
                "Declare the expose's columns.",
            )
            return []
        return [
            {
                "kind": "schema",
                "columns": tuple((n, str(self.columns[n].get("type") or "")) for n in names),
            }
        ]

    # ── parts ─────────────────────────────────────────────────────────────────

    def _selected(self, *, required: bool) -> List[str]:
        """The contract columns ``selector`` names, in its order; ``[]`` for ``*`` or none
        (a refusal when ``required``)."""
        raw = self.rule.get("selector")
        text = raw.strip() if isinstance(raw, str) else ""
        if not text or text == "*":
            if required:
                self.refuse(
                    "selects no column, and it checks columns",
                    "Set selector to the column, or to several, comma-separated.",
                )
            return []
        names: List[str] = []
        for part in text.split(","):
            name = self._column(part.strip())
            if name is None:
                self.refuse(
                    f"selects {part.strip()!r}, which exposes[].contract.schema does not "
                    "declare",
                    "Name a column of the expose's schema, or declare the column there.",
                )
            elif name not in names:
                names.append(name)
        return names

    def _column(self, name: str) -> Optional[str]:
        """The schema's spelling of column ``name``: exact, else the one that differs only
        in case (Databricks and most warehouses fold column names)."""
        if name in self.columns:
            return name
        folded = [c for c in self.columns if c.lower() == name.lower()]
        return folded[0] if len(folded) == 1 else None

    @staticmethod
    def _number_or_none(value: Any) -> Optional[float]:
        return _finite(value)

    def _operator_or_none(self) -> Optional[str]:
        raw = self.rule.get("operator")
        return str(raw).strip() if raw is not None else None

    def _operator(self) -> Optional[str]:
        operator = str(self.rule.get("operator") or ">=").strip()
        if operator not in OPERATORS:
            self.refuse(
                f"has operator {operator!r}",
                f"Use one of {', '.join(OPERATORS)}.",
            )
            return None
        return operator

    def _share(self) -> Optional[Tuple[str, float]]:
        threshold = self.rule.get("threshold")
        if threshold is None:
            return None
        value = _finite(threshold)
        if value is None:
            self.refuse(
                f"has threshold {threshold!r}, which is not a finite number",
                "Set threshold to the share of rows that must pass, from 0 to 1.",
            )
            return None
        operator = self._operator()
        if not 0.0 <= value <= 1.0:
            self.refuse(
                f"has threshold {threshold}, and a threshold is the share of rows that pass, "
                "from 0 to 1 (1.0: every row)",
                "Write the share as a fraction: 0.99, not 99.",
            )
            return None
        if operator is None or (value == 1.0 and operator in (">=", "==")):
            return None
        return (operator, value)

    def _allowed(self, name: str) -> Optional[List[Dict[str, Any]]]:
        """What column ``name`` declares as its allowed values; ``None`` after a refusal.
        A ``custom`` validationRule is no allowed-values declaration and is left alone."""
        column = self.columns[name]
        refused = len(self.refusals)
        readers = {"enum": self._enum, "regex": self._regex, "range": self._range}
        found: List[Dict[str, Any]] = []
        rules = column.get("validationRules")
        for entry in rules if isinstance(rules, list) else []:
            kind = (
                str(entry.get("type") or "").strip().lower() if isinstance(entry, Mapping) else ""
            )
            item = readers[kind](name, entry.get("constraint")) if kind in readers else None
            if item is not None:
                found.append(item)
        labels = column.get("labels")
        target = labels.get("references") if isinstance(labels, Mapping) else None
        reference = self._reference(name, target) if target is not None else None
        if reference is not None:
            found.append({"kind": "reference", "reference": reference})
        return None if len(self.refusals) > refused else found

    def _enum(self, name: str, constraint: Any) -> Optional[Dict[str, Any]]:
        values = parse_enum(constraint)
        if not values:
            self.refuse(
                f"reads column {name!r}, whose enum validationRule lists no value",
                "Write the values as the constraint: 'A | B | C'.",
            )
        elif any(_CONTROL_RE.search(v) for v in values):
            self.refuse(
                f"reads column {name!r}, whose enum holds a control character",
                "Remove it from the value.",
            )
        else:
            return {"kind": "accepted_values", "values": tuple(values)}
        return None

    def _regex(self, name: str, constraint: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(constraint, str) or not constraint:
            self.refuse(
                f"reads column {name!r}, whose regex validationRule has no pattern",
                "Write the pattern as the constraint: '^[A-Z]{2}$'.",
            )
        elif _CONTROL_RE.search(constraint):
            self.refuse(
                f"reads column {name!r}, whose regex holds a control character or an unpaired "
                "surrogate",
                "Write it with an escape (\\t, \\n) instead.",
            )
        else:
            return {"kind": "regex", "pattern": constraint}
        return None

    def _range(self, name: str, constraint: Any) -> Optional[Dict[str, Any]]:
        bounds = parse_range(constraint)
        if bounds is not None and _empty(bounds):
            self.refuse(
                f"reads column {name!r}, whose range {constraint!r} no value can meet",
                "Write the lower bound first: 'between 0 and 100', '>= 0 and <= 100'.",
            )
            return None
        if bounds is not None:
            return {"kind": "bounds", "bounds": bounds}
        self.refuse(
            f"reads column {name!r}, whose range {constraint!r} is not '>= 0', "
            "'>= 0 and <= 100' or 'between 0 and 100'",
            "Write the range in one of those forms.",
        )
        return None

    def _listed_values(self, name: str) -> List[Dict[str, Any]]:
        """Values the rule itself lists, for a column that declares none; a refusal when the
        rule lists none either."""
        from fluid_build.engines.dbt._test_mapping import valid_values

        values = [str(v).strip() for v in valid_values(self.rule) if str(v).strip()]
        if values and not any(_CONTROL_RE.search(v) for v in values):
            return [{"kind": "accepted_values", "values": tuple(values)}]
        self.refuse(
            f"reads column {name!r}, which declares no allowed values, and the rule lists none",
            f"Declare them on column {name!r}: validationRules [{{type: enum, constraint: "
            "'A | B'}] (or regex, or range), or labels.references: <product id>.<column> for "
            "a value that must exist in another product's column.",
        )
        return []

    def _reference(self, name: str, target: Any) -> Optional[Reference]:
        text = target.strip() if isinstance(target, str) else ""
        product_id, _, column = text.rpartition(".")
        if not product_id or not column:
            self.refuse(
                f"reads column {name!r}, whose labels.references {target!r} is not "
                "<product id>.<column>",
                "Write it as the referenced product's id, a dot, and its column.",
            )
            return None
        if product_id == self.product:
            self.refuse(
                f"reads column {name!r}, which references {text!r} in this same product",
                "Reference another product's column; a key within the product is not checked "
                "here.",
            )
            return None
        exposes = sorted(
            {
                str(c.get("exposeId"))
                for c in self.consumes
                if c.get("productId") == product_id and c.get("exposeId")
            }
        )
        if len(exposes) > 1:
            self.refuse(
                f"reads column {name!r}, which references {text!r}, and consumes[] lists "
                f"{product_id} by {len(exposes)} exposes ({', '.join(exposes)}), so which table "
                "holds the column is ambiguous",
                f"List {product_id} in consumes[] once, with the exposeId of the table that "
                f"holds {column}.",
            )
            return None
        # Not consumed (a source-aligned product consumes nothing): an engine that finds the
        # product's table another way can still check it; one that cannot refuses.
        return Reference(product_id, exposes[0] if exposes else None, column)

    def refuse(self, problem: str, fix: str, *, needs_history: bool = False) -> None:
        self.refusals.append(
            DqRefusal(
                rule_id=self.rule_id,
                expose_id=self.expose_id,
                message=f"rule {self.rule_id!r} ({self.type or 'no type'}) {problem}",
                fix=fix,
                rule_type=self.type,
                needs_history=needs_history,
            )
        )


__all__ = [
    "DqCheck",
    "DqRefusal",
    "DqRules",
    "KINDS",
    "OPERATORS",
    "Reference",
    "SEVERITIES",
    "describe",
    "has_rules",
    "parse_enum",
    "parse_range",
    "read_rules",
]
