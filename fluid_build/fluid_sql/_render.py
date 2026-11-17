# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Render a checked FLUID SQL/1 tree as DuckDB SQL (``RFC-data-rules.md`` §2.8).

SQL is printed from the tree, never from the raw text: every name goes through
:func:`~fluid_build.providers._sql_safety.quote_identifier_for` and every string
literal through :func:`~fluid_build.providers._sql_safety.quote_string_literal_for`,
so a text cannot carry a second statement, a comment or a subquery into SQL.

The rendering is exact where FLUID SQL/1 and DuckDB agree:

- every comparison, ``IN`` and ``BETWEEN`` is wrapped in ``COALESCE(p, FALSE)``,
  and a bool column, ``CASE`` or ``COALESCE`` where a predicate is expected in
  ``COALESCE(e, FALSE)``, so ``NOT``, ``AND`` and ``OR`` only ever see TRUE or
  FALSE (SQL's ``x NOT BETWEEN a AND b`` is ``x < a OR x > b`` in three-valued
  logic too, so the wrapper is exact for it);
- ``a / b`` is ``CAST(a AS <exact type>) / NULLIF(b, 0)``, so ``x / 0`` is NULL;
- ``ROUND(x, s)`` is DuckDB's ``ROUND`` on a fixed-point value, which rounds ties
  away from zero; ``'HALF_TO_EVEN'`` is the ``CASE`` form of §2.8 in fixed point;
- the date differences are DuckDB's ``date_sub`` (whole units), never
  ``date_diff`` (unit boundaries crossed).

Two constructs are exact in FLUID SQL/1 and approximate in DuckDB, which divides
``DECIMAL`` values in ``DOUBLE``: a ``/``, and arithmetic on a float column
(:attr:`~fluid_build.fluid_sql.Checked.approximate`).

``column_sql(relationship, name)`` returns the SQL of a column reference: the
caller owns the aliases of the expose's row and of each relationship's target
(left-joined, so a path through a missing row reads NULL).
"""

from __future__ import annotations

from typing import Callable, List, Optional

from fluid_build.providers._sql_safety import quote_identifier_for, quote_string_literal_for

from . import _nodes as n
from ._checker import Checked
from ._types import NUMBER, SqlType

ColumnSql = Callable[[Optional[str], str], str]

_PREDICATES = (n.Compare, n.In, n.Between, n.IsNull, n.DistinctFrom, n.Not, n.And, n.Or)
_DATE_PARTS = {"day": "day", "month": "month", "year": "year"}


def duckdb_column(alias: str = "t") -> ColumnSql:
    """A ``column_sql`` that reads the expose's columns from ``alias`` and each
    relationship's target from an alias named like the relationship."""

    def column(relationship: Optional[str], name: str) -> str:
        table = quote_identifier_for("duckdb", relationship or alias)
        return f"{table}.{quote_identifier_for('duckdb', name)}"

    return column


def render_duckdb(
    node: n.Node, checked: Checked, column_sql: ColumnSql, predicate: bool = False
) -> str:
    """The DuckDB SQL of ``node``; with ``predicate``, an expression that is never NULL."""
    renderer = _DuckDB(checked, column_sql)
    return renderer.truth(node) if predicate else renderer.value(node)


class _DuckDB:
    def __init__(self, checked: Checked, column_sql: ColumnSql) -> None:
        self.types = checked.types
        self.column_sql = column_sql

    def truth(self, node: n.Node) -> str:
        """``node`` where a predicate is expected: TRUE or FALSE, never NULL."""
        if isinstance(node, (n.Compare, n.In, n.Between)):
            return f"COALESCE({self.comparison(node)}, FALSE)"
        if isinstance(node, (n.IsNull, n.DistinctFrom)):
            return self.comparison(node)
        if isinstance(node, n.Not):
            return f"(NOT {self.truth(node.operand)})"
        if isinstance(node, (n.And, n.Or)):
            word = "AND" if isinstance(node, n.And) else "OR"
            return f"({self.truth(node.left)} {word} {self.truth(node.right)})"
        if isinstance(node, n.Bool):
            return "TRUE" if node.value else "FALSE"
        if isinstance(node, n.Null):
            return "FALSE"
        return f"COALESCE({self.value(node)}, FALSE)"

    def comparison(self, node: n.Node) -> str:
        if isinstance(node, n.Compare):
            left, right = self.common(node.left, node.right)
            return f"({left} {node.op} {right})"
        if isinstance(node, n.In):
            left, *items = self.common(node.left, *node.items)
            keyword = "NOT IN" if node.negated else "IN"
            return f"({left} {keyword} ({', '.join(items)}))"
        if isinstance(node, n.Between):
            keyword = "NOT BETWEEN" if node.negated else "BETWEEN"
            left, low, high = self.common(node.left, node.low, node.high)
            return f"({left} {keyword} {low} AND {high})"
        if isinstance(node, n.IsNull):
            return f"({self.value(node.operand)} IS {'NOT ' if node.negated else ''}NULL)"
        assert isinstance(node, n.DistinctFrom)
        middle = "IS NOT DISTINCT FROM" if node.negated else "IS DISTINCT FROM"
        left, right = self.common(node.left, node.right)
        return f"({left} {middle} {right})"

    def common(self, *nodes: n.Node) -> List[str]:
        """The SQL of ``nodes``, numbers cast to one exact type.

        DuckDB unifies two DECIMALs by keeping both integer parts and the larger scale,
        and when that needs more than 38 digits it drops scale silently:
        ``COALESCE(a, b)`` of a ``DECIMAL(38,2)`` and a ``DECIMAL(38,4)`` is
        ``DECIMAL(38,2)``. An explicit ``DECIMAL(38, s)`` keeps every digit of scale,
        and a value too wide for it raises instead of comparing wrong.
        """
        types = [self.types.get(id(node)) for node in nodes]
        numbers = [t for t in types if t is not None and t.kind == NUMBER]
        if not numbers:
            return [self.value(node) for node in nodes]
        target = self.number_type(numbers)
        return [
            (
                f"CAST({self.value(node)} AS {target})"
                if t is None or t.kind == NUMBER
                else self.value(node)
            )
            for node, t in zip(nodes, types, strict=True)
        ]

    @staticmethod
    def number_type(types: List[SqlType]) -> str:
        """``DECIMAL(38, s)`` for the largest bounded scale, or ``DOUBLE`` when one of
        ``types`` is unbounded (approximate in DuckDB either way)."""
        if any(t.scale is None for t in types):
            return "DOUBLE"
        return f"DECIMAL(38, {min(max(t.scale or 0 for t in types), 38)})"

    def value(self, node: n.Node) -> str:  # noqa: C901 - one branch per construct
        if isinstance(node, _PREDICATES):
            return self.truth(node)
        if isinstance(node, n.Number):
            return node.text
        if isinstance(node, n.String):
            return quote_string_literal_for("duckdb", node.value)
        if isinstance(node, n.Bool):
            return "TRUE" if node.value else "FALSE"
        if isinstance(node, n.Null):
            return "NULL"
        if isinstance(node, n.Date):
            return f"DATE {quote_string_literal_for('duckdb', node.text)}"
        if isinstance(node, n.Column):
            return self.column_sql(node.relationship, node.name)
        if isinstance(node, n.Neg):
            return f"(-{self.value(node.operand)})"
        if isinstance(node, n.Arith):
            if node.op == "/":
                return (
                    f"(CAST({self.value(node.left)} AS {self.exact_type(node.left)}) "
                    f"/ NULLIF({self.value(node.right)}, 0))"
                )
            return f"({self.value(node.left)} {node.op} {self.value(node.right)})"
        if isinstance(node, n.Case):
            values = [then for _when, then in node.whens]
            if node.else_ is not None:
                values.append(node.else_)
            rendered = self.branches(node, values)
            parts = ["CASE"]
            for (when, _then), sql in zip(node.whens, rendered, strict=False):
                parts.append(f"WHEN {self.truth(when)} THEN {sql}")
            if node.else_ is not None:
                parts.append(f"ELSE {rendered[-1]}")
            parts.append("END")
            return "(" + " ".join(parts) + ")"
        if isinstance(node, n.Coalesce):
            return "COALESCE(" + ", ".join(self.branches(node, list(node.args))) + ")"
        if isinstance(node, n.Round):
            return self.round(node)
        if isinstance(node, n.DateDiff):
            part = quote_string_literal_for("duckdb", _DATE_PARTS[node.unit])
            return f"date_sub({part}, {self.value(node.from_)}, {self.value(node.to)})"
        raise TypeError(f"not a FLUID SQL/1 node: {node!r}")  # pragma: no cover

    def branches(self, node: n.Node, values: List[n.Node]) -> List[str]:
        """The value branches of a CASE or COALESCE, numbers cast to the node's own type
        (see :meth:`common`: DuckDB would otherwise unify them and may drop scale)."""
        own = self.types.get(id(node))
        if own is None or own.kind != NUMBER:
            return [self.value(value) for value in values]
        target = self.number_type([own])
        return [f"CAST({self.value(value)} AS {target})" for value in values]

    def exact_type(self, node: n.Node) -> str:
        """DuckDB's exact type for a dividend: ``DECIMAL(38, s)``, or ``DOUBLE`` when its
        scale is unbounded (it is a float or a quotient already)."""
        sql_type = self.types.get(id(node))
        if sql_type is None or sql_type.kind != NUMBER or sql_type.scale is None:
            return "DOUBLE"
        return f"DECIMAL(38, {min(sql_type.scale, 38)})"

    def round(self, node: n.Round) -> str:
        value = self.value(node.arg)
        arg = self.types.get(id(node.arg))
        exact = arg is not None and arg.scale is not None and arg.scale <= node.scale
        if node.mode == n.DEFAULT_ROUND_MODE or exact:
            # The two modes differ only on a tie, and no digit is cut when the places
            # cover the value's scale: the CASE form's * 10^s could then only overflow.
            return f"ROUND({value}, {node.scale})"
        # Half to even in fixed point: y = x * 10^s, then back by * 10^-s (a DECIMAL
        # literal, so DuckDB stays in DECIMAL instead of dividing in DOUBLE).
        up = "1" + "0" * node.scale
        down = "1" if node.scale == 0 else "0." + "0" * (node.scale - 1) + "1"
        y = f"({value} * {up})"
        return (
            f"((CASE WHEN ABS({y} - TRUNC({y})) = 0.5 THEN TRUNC({y}) + "
            f"(CASE WHEN ({'TRUNC(' + y + ')'} % 2) = 0 THEN 0 ELSE SIGN({y}) END) "
            f"ELSE ROUND({y}) END) * {down})"
        )
