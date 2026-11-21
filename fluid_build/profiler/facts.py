# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The share of a table's rows that satisfy one contract rule: a requested fact.

Why. A fact is a rule of ``exposes[].contract.invariants[]`` that the requester wants
measured on the table's records. The engine scores a run's rule against the share this
module measures, and the profile states it in the ``invariants`` section
(PROFILE-SPEC section 5). So the count here has to be the one the engine would count on
the same rows.

Semantics (EXPR-SPEC-2 section 4, as the engine's rule shares read them):

* A row is *covered* when the rule has no ``when``, or its ``when`` is TRUE. Truth is
  two-valued: a ``when`` that is NULL on a row is FALSE, so that row is not covered.
* A covered row *satisfies* a ``check`` when its predicate is TRUE (a NULL in a compared
  column makes a comparison FALSE).
* A covered row satisfies a ``derive`` with ``expr`` when the column IS NOT DISTINCT FROM
  the value the expression gives: NULL and NULL agree, NULL and a value do not.
* An aggregate ``derive``, a ``dependency`` and any rule that reads a related row
  through a relationship are not measured; :func:`plan_fact` says why.

Each text is parsed and checked by :mod:`fluid_build.fluid_sql` against the expose's
model, built as :mod:`fluid_build.data_rules` builds it, and evaluated by that package's
evaluator: there is no second one. The raw values of a source (CSV text, Parquet and
database typed values, a JSON slice's values, as
:func:`fluid_build.profiler.sources.read_columns` hands them on) are converted exactly to
the values the evaluator takes for the column's contract type; a value that cannot be
read refuses, naming the column and the row and never the value.

The counts :func:`count_fact` returns are exact. They must pass the output checks
(small cells, zeros, rounding) before any of them leaves the data owner's boundary;
that is the caller's job.

Standard library plus :mod:`fluid_build.fluid_sql`.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build import fluid_sql as fs
from fluid_build.data_rules._model import Expose, as_list, build, nfc
from fluid_build.fluid_sql import _nodes as nodes
from fluid_build.fluid_sql._types import BOOL, DATE, NUMBER, STRING, TIMESTAMP, TIMESTAMPTZ

__all__ = ["FactCount", "FactPlan", "FactRefused", "count_fact", "plan_fact"]

#: The most a decimal's exponent may be before its value is refused (it would be a number
#: of thousands of digits).
_MAX_EXPONENT = 1000
_NUMBER_TEXT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_DATE_TEXT = re.compile(r"\d{4}-\d{2}-\d{2}")
_MIDNIGHT_UTC = re.compile(r"(\d{4}-\d{2}-\d{2})T00:00:00(?:\.0+)?Z")


class FactRefused(ValueError):
    """A fact that cannot be planned or counted. ``event`` is a stable code, ``context`` its
    details (a name, a column, a row number; never a record's value)."""

    def __init__(self, event: str, message: str, context: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.event = event
        self.context = dict(context or {})


@dataclass(frozen=True)
class FactPlan:
    """What one requested fact needs, decided from the contract alone."""

    #: ``<exposeId>.<ruleId>``.
    name: str
    table: str
    rule: str
    #: The expose's columns the rule reads, in the expose's order (to read with the rest).
    columns: Tuple[str, ...]
    #: Set when the fact cannot be measured; then ``columns`` is empty.
    reason: Optional[str] = None


@dataclass(frozen=True)
class FactCount:
    """A rule's share, as counts of rows."""

    #: Rows the rule covers.
    covered: int
    #: Covered rows that satisfy it.
    satisfied: int


@dataclass(frozen=True)
class _Measure:
    """A rule ready to count: its trees and the model's columns it reads."""

    kind: str  # "check" or "derive"
    when: Optional[nodes.Node]
    test: nodes.Node
    columns: Tuple[str, ...]
    expose: Expose


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _expose_of(contract: Mapping[str, Any], name: str, table: str, rule: str) -> Expose:
    if name != f"{table}.{rule}":
        raise FactRefused(
            "fact-name-mismatch",
            "a fact's name is <table>.<rule>, and this one is not its table and rule joined",
            {"fact": name},
        )
    for expose in build(contract):
        if expose.expose_id and nfc(expose.expose_id) == nfc(table):
            return expose
    raise FactRefused(
        "fact-table-unknown",
        f"fact {name!r}: the contract declares no expose {table!r}",
        {"fact": name, "table": table},
    )


def _invariant(expose: Expose, name: str, table: str, rule: str) -> List[Mapping[str, Any]]:
    found = [
        inv
        for inv in as_list(expose.contract.get("invariants"))
        if isinstance(inv, Mapping) and inv.get("id") == rule
    ]
    if not found:
        raise FactRefused(
            "fact-rule-unknown",
            f"fact {name!r}: expose {table!r} declares no rule {rule!r}",
            {"fact": name, "table": table, "rule": rule},
        )
    return found


def _prepare(expose: Expose, found: List[Mapping[str, Any]]) -> "_Measure | str":
    """The rule ready to count, or the reason it cannot be measured."""
    if len(found) > 1:
        return "the rule id is declared more than once, so which rule is meant is unknown"
    inv = found[0]
    forms = [key for key in ("check", "derive", "dependency") if key in inv]
    if len(forms) != 1:
        return "the entry has no form, or two (check, derive or dependency)"
    form = forms[0]
    if form == "dependency":
        return (
            "a dependency's share depends on the row order when two values tie for a key, "
            "so it is not measured"
        )
    if form == "derive" and "aggregate" in inv:
        return "an aggregate reads child rows, which this table's records do not hold"
    model = expose.model
    try:
        reads: List[Tuple[Optional[str], str]] = []
        when: Optional[nodes.Node] = None
        if "when" in inv:
            if not isinstance(inv["when"], str):
                return "the when is not a text"
            when = fs.parse(inv["when"])
            reads += fs.check(when, model, fs.PREDICATE).reads
        if form == "check":
            if not isinstance(inv["check"], str):
                return "the check is not a text"
            test = fs.parse(inv["check"])
            reads += fs.check(test, model, fs.PREDICATE).reads
        else:
            target = nfc(inv["derive"]) if isinstance(inv["derive"], str) else ""
            if target not in model.columns:
                return "the derive names no column of the expose"
            if not isinstance(inv.get("expr"), str):
                return "a derive takes an expr or an aggregate"
            value = fs.parse(inv["expr"])
            reads += fs.check(value, model, fs.VALUE, model.columns[target]).reads
            # IS NOT DISTINCT FROM: NULL and NULL agree.
            test = nodes.DistinctFrom(nodes.Column(target), value, negated=True)
            reads += fs.check(test, model, fs.PREDICATE).reads
    except fs.FluidSqlError as exc:
        return (
            f"a text of the rule is not FLUID SQL/1 for this expose ({exc.reason}); "
            "the contract's own lint refuses it"
        )
    if any(relationship is not None for relationship, _ in reads):
        return (
            "the rule reads a related row through a relationship, which needs the related "
            "table's rows"
        )
    wanted = {column for _, column in reads}
    if not wanted:
        return "the rule reads no column, so the rows it covers are not known from the table"
    ordered = tuple(column for column in model.columns if column in wanted)
    floats = [
        column
        for column in ordered
        if (declared := model.columns[column].type) is not None and declared.is_float
    ]
    if floats:
        return (
            f"the rule reads {floats[0]!r}, a binary floating point column, which has no "
            "exact reading, so the share is not measured"
        )
    return _Measure(form, when, test, ordered, expose)


def plan_fact(contract: Mapping[str, Any], name: str, table: str, rule: str) -> FactPlan:
    """What the fact ``name`` (the ``rule`` of the expose ``table``) needs.

    Refuses (:class:`FactRefused`) a name that is not ``<table>.<rule>``, a table that is
    not an expose and a rule the expose does not declare. A rule that exists but cannot be
    measured on one table's records comes back with a ``reason``.
    """
    expose = _expose_of(contract, name, table, rule)
    prepared = _prepare(expose, _invariant(expose, name, table, rule))
    if isinstance(prepared, str):
        return FactPlan(name, table, rule, (), prepared)
    return FactPlan(name, table, rule, prepared.columns)


# ---------------------------------------------------------------------------
# Reading a source's values as the evaluator holds them
# ---------------------------------------------------------------------------


class _Unreadable(Exception):
    """A value that is not its column's type (never carries the value)."""


def _number(value: Any) -> Fraction:
    if isinstance(value, bool):
        raise _Unreadable
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Fraction):
        return value
    if isinstance(value, float):
        # A binary float has no exact reading, and the profiler refuses one everywhere else
        # (flux-stats reads no float), so a fact is not measured on one either.
        raise _Unreadable
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, str) and _NUMBER_TEXT.fullmatch(value):
        return _decimal(Decimal(value))
    raise _Unreadable


def _decimal(value: Decimal) -> Fraction:
    if not value.is_finite():
        raise _Unreadable
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or abs(exponent) > _MAX_EXPONENT:
        raise _Unreadable
    return Fraction(value)


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    # Exactly the two levels a boolean column is read as; "True" or "TRUE" is refused there too.
    if isinstance(value, str) and value in ("true", "false"):
        return value == "true"
    raise _Unreadable


def _string(value: Any) -> str:
    if isinstance(value, str):
        return value
    raise _Unreadable


def _date(value: Any) -> datetime.date:
    try:
        if isinstance(value, datetime.datetime):
            raise _Unreadable
        if isinstance(value, datetime.date):
            return value
        if isinstance(value, str):
            midnight = _MIDNIGHT_UTC.fullmatch(value)
            if midnight:
                return datetime.date.fromisoformat(midnight.group(1))
            if _DATE_TEXT.fullmatch(value):
                return datetime.date.fromisoformat(value)
    except ValueError:
        pass
    raise _Unreadable


def _moment(value: Any) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, str) and ("T" in value or " " in value):
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    raise _Unreadable


def _timestamp(value: Any) -> datetime.datetime:
    """A timestamp is zone-less; an instant a source labels UTC loses its label."""
    moment = _moment(value)
    if moment.tzinfo is not None:
        moment = moment.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return moment


def _timestamptz(value: Any) -> datetime.datetime:
    """A timestamp with time zone is an instant; a source's zone-less one is taken as UTC."""
    moment = _moment(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment


_READERS: Mapping[str, Callable[[Any], Any]] = {
    NUMBER: _number,
    BOOL: _boolean,
    STRING: _string,
    DATE: _date,
    TIMESTAMP: _timestamp,
    TIMESTAMPTZ: _timestamptz,
}


def _converted(expose: Expose, name: str, plan_name: str, values: Sequence[Any]) -> List[Any]:
    column = expose.model.columns[name]
    if column.type is None:
        raise FactRefused(
            "fact-column-untyped",
            f"fact {plan_name!r}: column {name!r} has no FLUID SQL/1 type",
            {"fact": plan_name, "column": name},
        )
    read = _READERS[column.type.type.kind]
    out: List[Any] = []
    for index, value in enumerate(values):
        if value is None:
            out.append(None)
            continue
        try:
            out.append(read(value))
        except _Unreadable:
            raise FactRefused(
                "fact-value-unreadable",
                f"fact {plan_name!r}: row {index + 1} of column {name!r} is not a "
                f"{column.type.type.kind} (the value is not shown)",
                {"fact": plan_name, "column": name, "row": str(index + 1)},
            ) from None
    return out


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


def count_fact(
    contract: Mapping[str, Any], plan: FactPlan, values: Mapping[str, Sequence[Any]]
) -> FactCount:
    """The rows ``plan``'s rule covers and the covered rows that satisfy it.

    ``values`` holds, for each column of ``plan.columns``, its raw values as
    :func:`fluid_build.profiler.sources.read_columns` returns them, one list per column, all
    of one length. Refuses a plan that carries a reason, a plan the contract no longer
    gives, a column that is missing or of another length, and a value that is not its
    column's type.
    """
    if plan.reason is not None:
        raise FactRefused(
            "fact-not-measured",
            f"fact {plan.name!r} is not measured: {plan.reason}",
            {"fact": plan.name},
        )
    expose = _expose_of(contract, plan.name, plan.table, plan.rule)
    prepared = _prepare(expose, _invariant(expose, plan.name, plan.table, plan.rule))
    if isinstance(prepared, str) or prepared.columns != plan.columns:
        raise FactRefused(
            "fact-plan-stale",
            f"fact {plan.name!r}: the plan is not the one this contract gives",
            {"fact": plan.name},
        )
    lengths = set()
    for name in plan.columns:
        if name not in values:
            raise FactRefused(
                "fact-column-missing",
                f"fact {plan.name!r}: column {name!r} was not read",
                {"fact": plan.name, "column": name},
            )
        lengths.add(len(values[name]))
    if len(lengths) > 1:
        raise FactRefused(
            "fact-column-length",
            f"fact {plan.name!r}: the columns read hold different numbers of rows",
            {"fact": plan.name},
        )
    converted = {name: _converted(expose, name, plan.name, values[name]) for name in plan.columns}
    covered = satisfied = 0
    for index in range(lengths.pop() if lengths else 0):
        row = {name: converted[name][index] for name in plan.columns}
        if prepared.when is not None and not fs.evaluate(prepared.when, row, predicate=True):
            continue
        covered += 1
        if fs.evaluate(prepared.test, row, predicate=True):
            satisfied += 1
    return FactCount(covered, satisfied)
