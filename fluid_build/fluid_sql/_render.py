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

from typing import Callable, Optional

from fluid_build.providers._sql_safety import quote_identifier_for, quote_string_literal_for

from . import _nodes as n
from ._checker import Checked
from ._types import NUMBER

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
            return f"({self.value(node.left)} {node.op} {self.value(node.right)})"
        if isinstance(node, n.In):
            items = ", ".join(self.value(item) for item in node.items)
            keyword = "NOT IN" if node.negated else "IN"
            return f"({self.value(node.left)} {keyword} ({items}))"
        if isinstance(node, n.Between):
            keyword = "NOT BETWEEN" if node.negated else "BETWEEN"
            return (
                f"({self.value(node.left)} {keyword} {self.value(node.low)} "
                f"AND {self.value(node.high)})"
            )
        if isinstance(node, n.IsNull):
            return f"({self.value(node.operand)} IS {'NOT ' if node.negated else ''}NULL)"
        assert isinstance(node, n.DistinctFrom)
        middle = "IS NOT DISTINCT FROM" if node.negated else "IS DISTINCT FROM"
        return f"({self.value(node.left)} {middle} {self.value(node.right)})"

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
            parts = ["CASE"]
            for when, then in node.whens:
                parts.append(f"WHEN {self.truth(when)} THEN {self.value(then)}")
            if node.else_ is not None:
                parts.append(f"ELSE {self.value(node.else_)}")
            parts.append("END")
            return "(" + " ".join(parts) + ")"
        if isinstance(node, n.Coalesce):
            return "COALESCE(" + ", ".join(self.value(arg) for arg in node.args) + ")"
        if isinstance(node, n.Round):
            return self.round(node)
        if isinstance(node, n.DateDiff):
            part = quote_string_literal_for("duckdb", _DATE_PARTS[node.unit])
            return f"date_sub({part}, {self.value(node.from_)}, {self.value(node.to)})"
        raise TypeError(f"not a FLUID SQL/1 node: {node!r}")  # pragma: no cover

    def exact_type(self, node: n.Node) -> str:
        """DuckDB's exact type for a dividend: ``DECIMAL(38, s)``, or ``DOUBLE`` when its
        scale is unbounded (it is a float or a quotient already)."""
        sql_type = self.types.get(id(node))
        if sql_type is None or sql_type.kind != NUMBER or sql_type.scale is None:
            return "DOUBLE"
        return f"DECIMAL(38, {min(sql_type.scale, 38)})"

    def round(self, node: n.Round) -> str:
        value = self.value(node.arg)
        if node.mode == n.DEFAULT_ROUND_MODE:
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
