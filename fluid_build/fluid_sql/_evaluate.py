# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Evaluate a checked FLUID SQL/1 tree for one row (``RFC-data-rules.md`` §2.4 to §2.6).

The reference meaning the conformance vectors pin, and the one a renderer's SQL
must agree with:

- Numbers are exact rationals (:class:`fractions.Fraction`); ``x / 0`` is NULL.
- Two-valued truth: a comparison with a NULL operand is FALSE, ``NOT IN`` is one
  comparison, ``BETWEEN`` and ``NOT BETWEEN`` are their two comparisons, and a
  NULL bool where a predicate is expected counts as FALSE.
- ``ROUND(x, s)`` rounds ties away from zero; ``'HALF_TO_EVEN'`` to the even
  neighbour.
- ``DAYS_BETWEEN``, ``MONTHS_BETWEEN`` and ``YEARS_BETWEEN`` count whole units from
  the first date to the second (EXPR-SPEC-2 §6; DuckDB's ``date_sub``): moving
  forward by months keeps the day of the month or lands on a shorter month's
  last day; from a later date to an earlier one the result is negative.

- ``ADD_DAYS(d, n)`` moves ``d`` by ``n`` days and ``ADD_MONTHS(d, n)`` by ``n`` months,
  keeping the day of the month or clamping it to a shorter month's last day (no
  month-end preservation: 2001-02-28 plus a month is 2001-03-28); ``ADD_YEARS(d, n)`` is
  ``ADD_MONTHS(d, 12 * n)``. A result before 0001-01-01 or after 9999-12-31 is NULL, for
  every ``n``, however large (EXPR-SPEC-3).

A row maps column names to Python values (``None``, ``bool``, ``Fraction``,
``str``, ``datetime.date``, ``datetime.datetime``); ``links`` maps each
relationship to the row it reaches, or ``None`` when it reaches none.
"""

from __future__ import annotations

import calendar
import datetime
from decimal import Decimal
from fractions import Fraction
from typing import Any, Mapping, Optional

from . import _nodes as n

_HALF = Fraction(1, 2)


def evaluate(
    node: n.Node,
    row: Mapping[str, Any],
    links: Optional[Mapping[str, Optional[Mapping[str, Any]]]] = None,
    predicate: bool = False,
) -> Any:
    """The value of ``node`` for ``row``; with ``predicate``, TRUE or FALSE."""
    value = _Evaluator(row, links or {}).value(node)
    return value is True if predicate else value


def round_fraction(x: Fraction, places: int, mode: str = n.DEFAULT_ROUND_MODE) -> Fraction:
    """``x`` rounded to ``places`` decimal places in ``mode``."""
    scaled = x * 10**places
    sign = -1 if scaled < 0 else 1
    magnitude = abs(scaled)
    whole = magnitude.numerator // magnitude.denominator
    rest = magnitude - whole
    if rest > _HALF or (rest == _HALF and (mode == n.DEFAULT_ROUND_MODE or whole % 2 == 1)):
        whole += 1
    return Fraction(sign * whole, 10**places)


def add_months(day: datetime.date, months: int) -> datetime.date:
    """``day`` moved forward by ``months``: the same day of the month, or the last day of
    a shorter month."""
    years, month0 = divmod(day.month - 1 + months, 12)
    year, month = day.year + years, month0 + 1
    return datetime.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def months_between(start: datetime.date, end: datetime.date) -> int:
    """The whole months from ``start`` to ``end`` (negative when ``end`` is earlier)."""
    if end < start:
        return -months_between(end, start)
    months = 12 * (end.year - start.year) + (end.month - start.month)
    if add_months(start, months) > end:
        months -= 1
    return months


def years_between(start: datetime.date, end: datetime.date) -> int:
    """The whole years from ``start`` to ``end``: whole months divided by 12, toward zero."""
    if end < start:
        return -years_between(end, start)
    return months_between(start, end) // 12


def number(text: str) -> Fraction:
    """The exact value of a decimal text (``"1.50"``, ``"-3"``)."""
    return Fraction(Decimal(text))


class _Evaluator:
    def __init__(self, row: Mapping[str, Any], links: Mapping[str, Optional[Mapping[str, Any]]]):
        self.row = row
        self.links = links

    def truth(self, node: n.Node) -> bool:
        """``node`` where a predicate is expected: a NULL counts as FALSE."""
        return self.value(node) is True

    def value(self, node: n.Node) -> Any:  # noqa: C901 - one branch per construct
        if isinstance(node, n.Number):
            return number(node.text)
        if isinstance(node, n.String):
            return node.value
        if isinstance(node, n.Bool):
            return node.value
        if isinstance(node, n.Null):
            return None
        if isinstance(node, n.Date):
            return datetime.date.fromisoformat(node.text)
        if isinstance(node, n.Column):
            if node.relationship is None:
                return self.row.get(node.name)
            reached = self.links.get(node.relationship)
            return None if reached is None else reached.get(node.name)
        if isinstance(node, n.Neg):
            operand = self.value(node.operand)
            return None if operand is None else -operand
        if isinstance(node, n.Arith):
            left, right = self.value(node.left), self.value(node.right)
            if left is None or right is None:
                return None
            if node.op == "+":
                return left + right
            if node.op == "-":
                return left - right
            if node.op == "*":
                return left * right
            return None if right == 0 else Fraction(left) / Fraction(right)
        if isinstance(node, n.Compare):
            return _compare(node.op, self.value(node.left), self.value(node.right))
        if isinstance(node, n.In):
            left = self.value(node.left)
            if left is None:
                return False
            found = any(left == self.value(item) for item in node.items)
            return not found if node.negated else found
        if isinstance(node, n.Between):
            x, low, high = (self.value(p) for p in (node.left, node.low, node.high))
            if node.negated:
                return _compare("<", x, low) or _compare(">", x, high)
            return _compare(">=", x, low) and _compare("<=", x, high)
        if isinstance(node, n.IsNull):
            is_null = self.value(node.operand) is None
            return not is_null if node.negated else is_null
        if isinstance(node, n.DistinctFrom):
            left, right = self.value(node.left), self.value(node.right)
            same = (left is None and right is None) or (
                left is not None and right is not None and left == right
            )
            return same if node.negated else not same
        if isinstance(node, n.Not):
            return not self.truth(node.operand)
        if isinstance(node, n.And):
            return self.truth(node.left) and self.truth(node.right)
        if isinstance(node, n.Or):
            return self.truth(node.left) or self.truth(node.right)
        if isinstance(node, n.Case):
            for when, then in node.whens:
                if self.truth(when):
                    return self.value(then)
            return None if node.else_ is None else self.value(node.else_)
        if isinstance(node, n.Coalesce):
            for arg in node.args:
                value = self.value(arg)
                if value is not None:
                    return value
            return None
        if isinstance(node, n.Round):
            x = self.value(node.arg)
            return None if x is None else round_fraction(Fraction(x), node.scale, node.mode)
        if isinstance(node, n.DateDiff):
            start, end = self.value(node.from_), self.value(node.to)
            if start is None or end is None:
                return None
            if node.unit == "day":
                return Fraction((end - start).days)
            if node.unit == "month":
                return Fraction(months_between(start, end))
            return Fraction(years_between(start, end))
        if isinstance(node, n.DateAdd):
            return self.date_add(node)
        raise TypeError(f"not a FLUID SQL/1 node: {node!r}")  # pragma: no cover

    def date_add(self, node: n.DateAdd) -> Optional[datetime.date]:
        """``ADD_DAYS``, ``ADD_MONTHS`` or ``ADD_YEARS``: NULL in, NULL out."""
        date, count = self.value(node.date), self.value(node.count)
        if date is None or count is None:
            return None
        return add_to_date(date, node.unit, count)


def add_to_date(date: datetime.date, unit: str, count: Any) -> Optional[datetime.date]:
    """``date`` moved by ``count`` days, months or years (``unit``), or ``None``.

    Total: a result outside ``date.min``..``date.max`` is ``None``, and so is a count that
    is not a whole number (the checker only lets scale 0 through).
    """
    count = Fraction(count)
    if count.denominator != 1:
        return None
    k = count.numerator
    if unit == "day":
        # Bound k first: a huge k would overflow timedelta before the range check.
        if not (date.min - date).days <= k <= (date.max - date).days:
            return None
        return date + datetime.timedelta(days=k)
    months = k * 12 if unit == "year" else k
    # Bound the month index first: a huge k would make add_months build a year past 9999.
    index = date.year * 12 + date.month - 1 + months
    if not 12 <= index <= 9999 * 12 + 11:
        return None
    return add_months(date, months)


def _compare(op: str, left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    if op == "=":
        return bool(left == right)
    if op == "<>":
        return bool(left != right)
    if op == "<":
        return bool(left < right)
    if op == "<=":
        return bool(left <= right)
    if op == ">":
        return bool(left > right)
    return bool(left >= right)
