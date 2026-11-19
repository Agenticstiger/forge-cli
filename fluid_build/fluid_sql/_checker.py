# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Check a FLUID SQL/1 tree against the expose it is written for.

Names (§2.2), types (§2.3) and static scales (§2.4) of ``RFC-data-rules.md``:

- A bare name is a column of the model; ``rel.col`` is a column of the target of
  the model's relationship ``rel``, never through a target that declares a
  validity window. A column with no FLUID SQL/1 type cannot be named.
- ``=``, ``<>``, ``IS [NOT] DISTINCT FROM`` and ``IN`` take one type;
  ``<``, ``<=``, ``>``, ``>=`` and ``BETWEEN`` take one orderable type;
  arithmetic takes numbers; the date differences take dates; the branches of
  ``CASE`` and ``COALESCE`` share one type; a predicate position takes a bool.
- The NULL literal takes its type from a typed operand beside it: an arithmetic
  operand, a date-difference argument, an ``IS [NOT] DISTINCT FROM`` operand, a
  ``CASE`` or ``COALESCE`` branch. Anywhere else (an ``IN`` item, an operand of
  a comparison or ``BETWEEN``, ``IS [NOT] NULL``, unary minus, ``ROUND``'s
  argument, a predicate position, a whole value) it is refused, and so is a
  construct whose operands are all NULL literals.
- ``ADD_DAYS``, ``ADD_MONTHS`` and ``ADD_YEARS`` take a date and a number of scale
  0 (a float, a decimal without ``(p,s)`` and a ``/`` have no static scale), and
  neither argument may be the NULL literal: it would give the call no type to
  check the other against, and the count's scale could not be known.
- A string literal compared with a column that declares an enum is one of its
  values, or one of the column's placeholders.
- Scales: a literal has its written scale and a column its declared one; ``+``
  and ``-`` take the larger, ``*`` the sum, ``ROUND(x, s)`` is ``s``, a date
  difference 0, ``CASE`` and ``COALESCE`` the largest of their value branches;
  ``/``, a float and a decimal without ``(p,s)`` are unbounded.

A ``derive`` target takes a value of its own type whose scale fits its own.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from fluid_build.util.column_type import write_enum_list

from . import _nodes as n
from ._canonical import canonical
from ._errors import FluidSqlError
from ._types import BOOL, DATE, NUMBER, ORDERABLE, STRING, Column, Model, SqlType, decimal_hint

PREDICATE = "predicate"
VALUE = "value"

_CLOCK_WORDS = frozenset(
    {"CURRENT_DATE", "CURRENT_TIMESTAMP", "NOW", "GETDATE", "SYSDATE", "TODAY", "SYSDATETIME"}
)


@dataclass(frozen=True)
class _T:
    """A node's type while checking: ``kind`` ``None`` is the untyped NULL literal."""

    kind: Optional[str]
    scale: Optional[int] = None
    column: Optional[Column] = None
    from_float: bool = False


@dataclass(frozen=True)
class Checked:
    """What a checked text is and reads.

    ``reads`` holds ``(relationship or None, column)`` for every name the text
    reads. ``division`` is a ``/`` outside ``ROUND`` and ``float_arithmetic`` an
    arithmetic or ``ROUND`` over a float column: both are exact in FLUID SQL/1 and
    approximate in SQL engines (§2.8). ``types`` maps each node's ``id`` to its
    type (``None`` for the NULL literal), for renderers.
    """

    type: SqlType
    reads: FrozenSet[Tuple[Optional[str], str]]
    division: bool = False
    float_arithmetic: bool = False
    types: Dict[int, Optional[SqlType]] = field(default_factory=dict, compare=False, repr=False)

    @property
    def approximate(self) -> bool:
        return self.division or self.float_arithmetic


def check(
    node: n.Node, model: Model, site: str = PREDICATE, derive: Optional[Column] = None
) -> Checked:
    """Check ``node`` as written at ``site`` (``predicate`` or ``value``) of ``model``.

    With ``derive``, the text is the ``expr`` of a ``derive`` on that column, and the
    value must fit it. Raises :class:`FluidSqlError` with the reason it is refused.
    """
    checker = _Checker(model)
    if site == PREDICATE:
        checker.predicate(node, "a check, a when or a filter")
        result = SqlType(BOOL)
    elif site == VALUE:
        t = checker.typed(node)
        if t.kind is None:
            raise FluidSqlError(
                "null-literal",
                "a value that is only the NULL literal has no type",
                node.pos,
            )
        result = SqlType(t.kind, t.scale)
        if derive is not None:
            check_derive_fit(derive, result)  # the whole value: no one character is at fault
    else:  # pragma: no cover - a programming error
        raise ValueError(f"unknown site {site!r}")
    return Checked(
        type=result,
        reads=frozenset(checker.reads),
        division=checker.division,
        float_arithmetic=checker.float_arithmetic,
        types={
            key: None if t.kind is None else SqlType(t.kind, t.scale)
            for key, t in checker.types.items()
        },
    )


def check_derive_target(target: Column) -> None:
    """Refuse a derive target that no value can fit (reason ``derive-target``)."""
    column_type = target.type
    if column_type is None:
        raise FluidSqlError(
            "derive-target",
            f"the derive target {target.name} ({target.declared}) has no FLUID SQL/1 type"
            + decimal_hint(target.declared),
        )
    if column_type.is_float:
        raise FluidSqlError(
            "derive-target",
            f"the derive target {target.name} is a float ({target.declared}): a derived value is "
            "exact, so declare decimal(p,s)",
        )
    if column_type.type.kind == NUMBER and not column_type.declared_scale:
        raise FluidSqlError(
            "derive-target",
            f"the derive target {target.name} is declared {target.declared} with no scale: "
            "declare decimal(p,s)",
        )


def check_derive_fit(target: Column, value: SqlType, pos: Optional[int] = None) -> None:
    """Refuse ``value`` as the value of the derive target ``target``."""
    check_derive_target(target)
    assert target.type is not None
    own = target.type.type
    if value.kind != own.kind:
        raise FluidSqlError(
            "derive-type",
            f"the derive target {target.name} is a {own.kind} ({target.declared}), but the value "
            f"is a {value.describe()}",
            pos,
        )
    if own.kind == NUMBER:
        assert own.scale is not None
        if value.scale is None:
            raise FluidSqlError(
                "derive-scale",
                f"the value's scale is unbounded (a division or a float) and "
                f"{target.name} has scale {own.scale}: wrap it in ROUND(x, {own.scale})",
                pos,
            )
        if value.scale > own.scale:
            raise FluidSqlError(
                "derive-scale",
                f"the value has scale {value.scale} and {target.name} has scale {own.scale}: wrap "
                f"it in ROUND(x, {own.scale})",
                pos,
            )


def _short(node: n.Node) -> str:
    text = canonical(node)
    return text if len(text) <= 60 else text[:57] + "..."


def _hint(name: str, choices: List[str]) -> str:
    folded = [c for c in choices if c.lower() == name.lower()]
    if folded:
        return f" (names are case-sensitive: did you mean {folded[0]!r}?)"
    close = difflib.get_close_matches(name, choices, n=1)
    return f" (did you mean {close[0]!r}?)" if close else ""


class _Checker:
    def __init__(self, model: Model) -> None:
        self.model = model
        self.reads: Set[Tuple[Optional[str], str]] = set()
        self.division = False
        self.float_arithmetic = False
        self.types: Dict[int, _T] = {}
        self.in_round = 0

    def typed(self, node: n.Node) -> _T:
        t = self._typed(node)
        self.types[id(node)] = t
        return t

    def predicate(self, node: n.Node, where: str) -> None:
        t = self.typed(node)
        if t.kind is None:
            raise FluidSqlError(
                "null-literal",
                f"the NULL literal in {where} is always FALSE: write a condition",
                node.pos,
            )
        if t.kind != BOOL:
            raise FluidSqlError(
                "type-mismatch",
                f"{where} takes a bool, but {_short(node)} is a {t.kind}",
                node.pos,
            )

    # -- names -----------------------------------------------------------------

    def column(self, node: n.Column) -> _T:
        model = self.model
        if node.relationship is not None:
            link = model.links.get(node.relationship)
            if link is None:
                raise FluidSqlError(
                    "unknown-relationship",
                    f"{node.relationship} is not a relationship of {model.name}"
                    + _hint(node.relationship, sorted(model.links)),
                    node.pos,
                )
            if link.target is None:
                raise FluidSqlError(
                    "unknown-column",
                    f"the relationship {link.name} goes to {link.target_name!r}, which is not an "
                    "expose of the contract",
                    node.pos,
                )
            if link.target.validity_window:
                raise FluidSqlError(
                    "validity-window",
                    f"{link.name} goes to {link.target.name}, which declares a validity window: "
                    "FLUID SQL/1 v1 reads no slowly changing dimension through a relationship",
                    node.pos,
                )
            model = link.target
        column = model.columns.get(node.name)
        if column is None:
            where = (
                model.name
                if node.relationship is None
                else f"{model.name} (via {node.relationship})"
            )
            hint = _hint(node.name, sorted(model.columns))
            if not hint and node.relationship is None and node.name.upper() in _CLOCK_WORDS:
                hint = " (FLUID SQL/1 never reads the clock: use a column or a DATE literal)"
            raise FluidSqlError(
                "unknown-column", f"{node.name} is not a column of {where}{hint}", node.pos
            )
        if column.type is None:
            raise FluidSqlError(
                "untyped-column",
                f"{node.name} ({column.declared}) has no FLUID SQL/1 type, so no expression can "
                "name it" + decimal_hint(column.declared),
                node.pos,
            )
        self.reads.add((node.relationship, node.name))
        return _T(
            column.type.type.kind,
            column.type.type.scale,
            column=column,
            from_float=column.type.is_float,
        )

    # -- types -----------------------------------------------------------------

    def _typed(self, node: n.Node) -> _T:  # noqa: C901 - one branch per construct
        if isinstance(node, n.Number):
            return _T(NUMBER, node.scale)
        if isinstance(node, n.String):
            return _T(STRING)
        if isinstance(node, n.Bool):
            return _T(BOOL)
        if isinstance(node, n.Null):
            return _T(None)
        if isinstance(node, n.Date):
            return _T(DATE)
        if isinstance(node, n.Column):
            return self.column(node)
        if isinstance(node, n.Neg):
            t = self.typed(node.operand)
            if t.kind is None:
                raise FluidSqlError("null-literal", "-NULL has no type", node.pos)
            self._need(t, NUMBER, node.operand, "unary minus")
            return _T(NUMBER, t.scale, from_float=t.from_float)
        if isinstance(node, n.Arith):
            return self._arith(node)
        if isinstance(node, n.Compare):
            return self._compare(node)
        if isinstance(node, n.In):
            return self._in(node)
        if isinstance(node, n.Between):
            return self._between(node)
        if isinstance(node, n.IsNull):
            t = self.typed(node.operand)
            if t.kind is None:
                raise FluidSqlError(
                    "null-literal", "NULL IS NULL is always TRUE: name a value", node.pos
                )
            return _T(BOOL)
        if isinstance(node, n.DistinctFrom):
            left, right = self.typed(node.left), self.typed(node.right)
            if left.kind is None and right.kind is None:
                raise FluidSqlError(
                    "null-literal", "both sides of IS DISTINCT FROM are NULL literals", node.pos
                )
            self._same(left, right, node, "IS DISTINCT FROM")
            self._enum(left, node.right)
            self._enum(right, node.left)
            return _T(BOOL)
        if isinstance(node, n.Not):
            self.predicate(node.operand, "NOT")
            return _T(BOOL)
        if isinstance(node, (n.And, n.Or)):
            word = "AND" if isinstance(node, n.And) else "OR"
            self.predicate(node.left, word)
            self.predicate(node.right, word)
            return _T(BOOL)
        if isinstance(node, n.Case):
            for when, _then in node.whens:
                self.predicate(when, "CASE WHEN")
            values = [then for _when, then in node.whens]
            if node.else_ is not None:
                values.append(node.else_)
            return self._branches(node, values, "CASE")
        if isinstance(node, n.Coalesce):
            return self._branches(node, list(node.args), "COALESCE")
        if isinstance(node, n.Round):
            self.in_round += 1
            t = self.typed(node.arg)
            self.in_round -= 1
            if t.kind is None:
                raise FluidSqlError("null-literal", "ROUND(NULL, s) has no type", node.pos)
            self._need(t, NUMBER, node.arg, "ROUND")
            if t.from_float:
                self.float_arithmetic = True
            return _T(NUMBER, node.scale)
        if isinstance(node, n.DateDiff):
            name = n.DATE_DIFF_FUNCTIONS[node.unit]
            start, end = self.typed(node.from_), self.typed(node.to)
            if start.kind is None and end.kind is None:
                raise FluidSqlError(
                    "null-literal", f"both arguments of {name} are NULL literals", node.pos
                )
            for t, arg in ((start, node.from_), (end, node.to)):
                if t.kind is not None:
                    self._need(t, DATE, arg, name)
            return _T(NUMBER, 0)
        if isinstance(node, n.DateAdd):
            return self._date_add(node)
        raise TypeError(f"not a FLUID SQL/1 node: {node!r}")  # pragma: no cover

    def _date_add(self, node: n.DateAdd) -> _T:
        """``ADD_DAYS``, ``ADD_MONTHS`` or ``ADD_YEARS``: a date and a count of scale 0."""
        name = n.DATE_ADD_FUNCTIONS[node.unit]
        date, count = self.typed(node.date), self.typed(node.count)
        for t, arg in ((date, node.date), (count, node.count)):
            if t.kind is None:
                raise FluidSqlError(
                    "null-literal", f"{name} takes a value, not the NULL literal", arg.pos
                )
        self._need(date, DATE, node.date, name)
        self._need(count, NUMBER, node.count, name)
        if count.scale != 0:
            raise FluidSqlError(
                "type-mismatch",
                f"{name} takes a count of scale 0, but {_short(node.count)} has "
                + ("no static scale" if count.scale is None else f"scale {count.scale}")
                + " (ROUND(x, 0) makes a whole count)",
                node.count.pos,
            )
        return _T(DATE)

    def _need(self, t: _T, kind: str, node: n.Node, construct: str) -> None:
        if t.kind != kind:
            article = "an" if kind[0] in "aeiou" else "a"
            raise FluidSqlError(
                "type-mismatch",
                f"{construct} takes {article} {kind}, but {_short(node)} is a {t.kind}",
                node.pos,
            )

    def _same(self, left: _T, right: _T, node: n.Node, construct: str) -> None:
        if left.kind is not None and right.kind is not None and left.kind != right.kind:
            raise FluidSqlError(
                "type-mismatch",
                f"{construct} compares one type, but {_short(node)} compares a {left.kind} "
                f"with a {right.kind}",
                node.pos,
            )

    def _enum(self, t: _T, literal: n.Node) -> None:
        column = t.column
        if column is None or not column.enum or not isinstance(literal, n.String):
            return
        if literal.value in column.enum or column.is_placeholder(literal.value):
            return
        values = write_enum_list(column.enum)
        raise FluidSqlError(
            "enum-literal",
            f"'{literal.value}' is not one of {column.name}'s values ({values})"
            + _hint(literal.value, list(column.enum)),
            literal.pos,
        )

    def _arith(self, node: n.Arith) -> _T:
        left, right = self.typed(node.left), self.typed(node.right)
        if left.kind is None and right.kind is None:
            raise FluidSqlError(
                "null-literal", f"both operands of {node.op} are NULL literals", node.pos
            )
        for t, operand in ((left, node.left), (right, node.right)):
            if t.kind is not None:
                self._need(t, NUMBER, operand, node.op)
        if node.op == "/" and not self.in_round:
            self.division = True
        from_float = left.from_float or right.from_float
        if from_float:
            self.float_arithmetic = True
        if node.op == "/":
            return _T(NUMBER, None, from_float=from_float)
        scales = [t.scale for t in (left, right) if t.kind is not None]
        if any(scale is None for scale in scales):
            scale: Optional[int] = None
        elif node.op == "*":
            scale = sum(s for s in scales if s is not None)
        else:
            scale = max(s for s in scales if s is not None)
        return _T(NUMBER, scale, from_float=from_float)

    def _compare(self, node: n.Compare) -> _T:
        left, right = self.typed(node.left), self.typed(node.right)
        for t, operand in ((left, node.left), (right, node.right)):
            if t.kind is None:
                raise FluidSqlError(
                    "null-literal",
                    f"a comparison with NULL is always FALSE: write "
                    f"{_short(node.right if operand is node.left else node.left)} IS NULL",
                    operand.pos,
                )
        self._same(left, right, node, node.op)
        if node.op not in ("=", "<>") and left.kind not in ORDERABLE:
            raise FluidSqlError(
                "type-mismatch",
                f"{node.op} orders numbers, strings, dates and timestamps, " f"not a {left.kind}",
                node.pos,
            )
        self._enum(left, node.right)
        self._enum(right, node.left)
        return _T(BOOL)

    def _in(self, node: n.In) -> _T:
        left = self.typed(node.left)
        if left.kind is None:
            raise FluidSqlError(
                "null-literal", "NULL IN (...) is always FALSE: name a value", node.left.pos
            )
        for item in node.items:
            t = self.typed(item)
            if t.kind != left.kind:
                raise FluidSqlError(
                    "type-mismatch",
                    f"an IN list holds literals of the left side's type ({left.kind}), but "
                    f"{_short(item)} is a {t.kind}",
                    item.pos,
                )
            self._enum(left, item)
        return _T(BOOL)

    def _between(self, node: n.Between) -> _T:
        parts = [(self.typed(p), p) for p in (node.left, node.low, node.high)]
        for t, operand in parts:
            if t.kind is None:
                raise FluidSqlError(
                    "null-literal",
                    "BETWEEN with a NULL literal is always FALSE: write the comparison you mean",
                    operand.pos,
                )
        kinds = {t.kind for t, _ in parts}
        if len(kinds) > 1:
            raise FluidSqlError(
                "type-mismatch",
                f"BETWEEN takes one type, but {_short(node)} mixes "
                + " and ".join(sorted(str(k) for k in kinds)),
                node.pos,
            )
        if parts[0][0].kind not in ORDERABLE:
            raise FluidSqlError(
                "type-mismatch",
                f"BETWEEN orders numbers, strings, dates and timestamps, not a {parts[0][0].kind}",
                node.pos,
            )
        self._enum(parts[0][0], node.low)
        self._enum(parts[0][0], node.high)
        return _T(BOOL)

    def _branches(self, node: n.Node, values: List[n.Node], construct: str) -> _T:
        typed = [(self.typed(v), v) for v in values]
        kinds = {t.kind for t, _ in typed if t.kind is not None}
        if not kinds:
            raise FluidSqlError(
                "null-literal",
                f"every value of {construct} is the NULL literal, so it has no type",
                node.pos,
            )
        if len(kinds) > 1:
            raise FluidSqlError(
                "type-mismatch",
                f"the values of {construct} share one type, but {_short(node)} mixes "
                + " and ".join(sorted(kinds)),
                node.pos,
            )
        kind = kinds.pop()
        scale: Optional[int] = None
        if kind == NUMBER:
            scales = [t.scale for t, _ in typed if t.kind == NUMBER]
            scale = (
                None if any(s is None for s in scales) else max(s for s in scales if s is not None)
            )
        from_float = any(t.from_float for t, _ in typed)
        return _T(kind, scale, from_float=from_float)
