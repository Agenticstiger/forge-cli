# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Render a checked FLUID SQL/1 tree as SQL for one engine (``RFC-data-rules.md`` §2.8).

SQL is printed from the tree, never from the raw text: every name goes through
:func:`~fluid_build.providers._sql_safety.quote_identifier_for` and every string
literal through :func:`~fluid_build.providers._sql_safety.quote_string_literal_for`,
so a text cannot carry a second statement, a comment or a subquery into SQL.

:data:`DIALECTS` are the engines rendered: ``duckdb``, ``snowflake``, ``bigquery``,
``databricks``, ``postgres`` and ``athena`` (Athena's queries run on Trino). The
rendering is exact where FLUID SQL/1 and the engine agree:

- every comparison, ``IN`` and ``BETWEEN`` is wrapped in ``COALESCE(p, FALSE)``,
  and a bool column, ``CASE`` or ``COALESCE`` where a predicate is expected in
  ``COALESCE(e, FALSE)``, so ``NOT``, ``AND`` and ``OR`` only ever see TRUE or
  FALSE (SQL's ``x NOT BETWEEN a AND b`` is ``x < a OR x > b`` in three-valued
  logic too, so the wrapper is exact for it);
- ``a / b`` is ``CAST(a AS <exact type>) / NULLIF(b, 0)``, so ``x / 0`` is NULL;
- ``ROUND(x, s)`` is the engine's ``ROUND`` on a fixed-point value, which rounds ties
  away from zero; ``'HALF_TO_EVEN'`` is Snowflake's own mode, Databricks' ``bround``,
  and elsewhere the ``CASE`` form of §2.8 in fixed point;
- ``DAYS_BETWEEN`` is each engine's day difference; ``MONTHS_BETWEEN`` and
  ``YEARS_BETWEEN`` are DuckDB's ``date_sub`` (whole units) and elsewhere the
  ``EXTRACT`` form of §2.8, never an engine's ``DATEDIFF`` (unit boundaries crossed);
- a string compared with ``<``, ``<=``, ``>`` or ``>=`` gets ``COLLATE "C"`` on
  PostgreSQL, so text orders by code point as in the other engines.

Two constructs are exact in FLUID SQL/1 and approximate in the engines: a ``/``,
and arithmetic on a float column (:attr:`~fluid_build.fluid_sql.Checked.approximate`).

``column_sql(relationship, name)`` returns the SQL of a column reference: the
caller owns the aliases of the expose's row and of each relationship's target
(left-joined, so a path through a missing row reads NULL).

**Bounded output.** Some forms repeat an operand (the ``CASE`` form of half-to-even
names its value five times, the ``EXTRACT`` form of a month difference each date
eight times), so a deep text could print exponentially much SQL. Each node is
rendered once and its text reused, and a rendering longer than
:data:`MAX_RENDERED_CHARS` raises :class:`RenderTooLarge` instead.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from fluid_build.providers._sql_safety import quote_identifier_for, quote_string_literal_for

from . import _nodes as n
from ._checker import Checked
from ._types import NUMBER, STRING, SqlType

ColumnSql = Callable[[Optional[str], str], str]

#: The engines :func:`render_sql` writes SQL for.
DIALECTS = ("duckdb", "snowflake", "bigquery", "databricks", "postgres", "athena")

#: The longest SQL one rendering may print (a contract's text is at most 4,096 code points).
MAX_RENDERED_CHARS = 1_000_000

_PREDICATES = (n.Compare, n.In, n.Between, n.IsNull, n.DistinctFrom, n.Not, n.And, n.Or)
_ORDERING = frozenset({"<", "<=", ">", ">="})


class RenderTooLarge(ValueError):
    """The SQL of a text would be longer than :data:`MAX_RENDERED_CHARS`."""


def sql_name(dialect: str, name: str) -> str:
    """``name`` as a quoted identifier of ``dialect``."""
    return quote_identifier_for(dialect, name)


def sql_literal(dialect: str, value: str) -> str:
    """``value`` as a string literal of ``dialect``."""
    return quote_string_literal_for(dialect, value)


def exact_number_type(dialect: str, scale: Optional[int]) -> str:
    """The engine's exact decimal type with ``scale`` digits of scale, or its double when
    ``scale`` is ``None`` (unbounded: approximate in the engine either way)."""
    if scale is None:
        return {"bigquery": "FLOAT64", "postgres": "DOUBLE PRECISION"}.get(dialect, "DOUBLE")
    scale = min(max(scale, 0), 38)
    name = {"snowflake": "NUMBER", "bigquery": "BIGNUMERIC", "postgres": "NUMERIC"}.get(
        dialect, "DECIMAL"
    )
    return f"{name}(38, {scale})"


def column_reader(dialect: str, alias: str = "t") -> ColumnSql:
    """A ``column_sql`` that reads the expose's columns from ``alias`` and each
    relationship's target from an alias named like the relationship."""

    def column(relationship: Optional[str], name: str) -> str:
        return f"{sql_name(dialect, relationship or alias)}.{sql_name(dialect, name)}"

    return column


def duckdb_column(alias: str = "t") -> ColumnSql:
    """:func:`column_reader` for DuckDB."""
    return column_reader("duckdb", alias)


def render_sql(
    node: n.Node,
    checked: Checked,
    column_sql: ColumnSql,
    dialect: str,
    predicate: bool = False,
) -> str:
    """The SQL of ``node`` for ``dialect``; with ``predicate``, an expression that is never
    NULL. Raises :class:`ValueError` for a dialect not in :data:`DIALECTS`."""
    if dialect not in DIALECTS:
        raise ValueError(f"FLUID SQL/1 renders no SQL for dialect {dialect!r}")
    renderer = _Renderer(dialect, checked, column_sql)
    return renderer.truth(node) if predicate else renderer.value(node)


def render_duckdb(
    node: n.Node, checked: Checked, column_sql: ColumnSql, predicate: bool = False
) -> str:
    """The DuckDB SQL of ``node``; with ``predicate``, an expression that is never NULL."""
    return render_sql(node, checked, column_sql, "duckdb", predicate)


class _Renderer:
    def __init__(self, dialect: str, checked: Checked, column_sql: ColumnSql) -> None:
        self.dialect = dialect
        self.types = checked.types
        self.column_sql = column_sql
        self.done: Dict[Tuple[str, int], str] = {}
        #: DuckDB's ``date_sub`` counts whole months and years; elsewhere the EXTRACT form.
        self.whole_units = dialect == "duckdb"

    # -- bounded, memoised entry points ------------------------------------------------

    def truth(self, node: n.Node) -> str:
        """``node`` where a predicate is expected: TRUE or FALSE, never NULL."""
        return self._once(("truth", id(node)), lambda: self._truth(node))

    def value(self, node: n.Node) -> str:
        return self._once(("value", id(node)), lambda: self._value(node))

    def _once(self, key: Tuple[str, int], build: Callable[[], str]) -> str:
        done = self.done.get(key)
        if done is None:
            done = build()
            if len(done) > MAX_RENDERED_CHARS:
                raise RenderTooLarge(
                    f"the SQL of this text would be longer than {MAX_RENDERED_CHARS:,} characters"
                )
            self.done[key] = done
        return done

    # -- predicates ------------------------------------------------------------------

    def _truth(self, node: n.Node) -> str:
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
            if node.op in _ORDERING:
                left = self.collate(node.left, left)
            return f"({left} {node.op} {right})"
        if isinstance(node, n.In):
            left, *items = self.common(node.left, *node.items)
            keyword = "NOT IN" if node.negated else "IN"
            return f"({left} {keyword} ({', '.join(items)}))"
        if isinstance(node, n.Between):
            keyword = "NOT BETWEEN" if node.negated else "BETWEEN"
            left, low, high = self.common(node.left, node.low, node.high)
            return f"({self.collate(node.left, left)} {keyword} {low} AND {high})"
        if isinstance(node, n.IsNull):
            return f"({self.value(node.operand)} IS {'NOT ' if node.negated else ''}NULL)"
        assert isinstance(node, n.DistinctFrom)
        middle = "IS NOT DISTINCT FROM" if node.negated else "IS DISTINCT FROM"
        left, right = self.common(node.left, node.right)
        return f"({left} {middle} {right})"

    def collate(self, node: n.Node, sql: str) -> str:
        """PostgreSQL orders text by its database collation; ``COLLATE "C"`` orders it by
        code point, as Snowflake, BigQuery, Databricks, Trino and DuckDB do."""
        own = self.types.get(id(node))
        if self.dialect == "postgres" and own is not None and own.kind == STRING:
            return f'({sql} COLLATE "C")'
        return sql

    def common(self, *nodes: n.Node) -> List[str]:
        """The SQL of ``nodes``, numbers cast to one exact type.

        DuckDB unifies two DECIMALs by keeping both integer parts and the larger scale,
        and when that needs more than 38 digits it drops scale silently:
        ``COALESCE(a, b)`` of a ``DECIMAL(38,2)`` and a ``DECIMAL(38,4)`` is
        ``DECIMAL(38,2)``. An explicit ``DECIMAL(38, s)`` keeps every digit of scale,
        and a value too wide for it raises instead of comparing wrong. Every engine
        gets the same cast, so no engine's own unification rule is relied on.
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

    def number_type(self, types: List[SqlType]) -> str:
        """The exact type for the largest bounded scale, or the double when one of
        ``types`` is unbounded (approximate in the engine either way)."""
        if any(t.scale is None for t in types):
            return exact_number_type(self.dialect, None)
        return exact_number_type(self.dialect, max(t.scale or 0 for t in types))

    # -- values ----------------------------------------------------------------------

    def _value(self, node: n.Node) -> str:  # noqa: C901 - one branch per construct
        if isinstance(node, _PREDICATES):
            return self.truth(node)
        if isinstance(node, n.Number):
            return node.text
        if isinstance(node, n.String):
            return sql_literal(self.dialect, node.value)
        if isinstance(node, n.Bool):
            return "TRUE" if node.value else "FALSE"
        if isinstance(node, n.Null):
            return "NULL"
        if isinstance(node, n.Date):
            return f"DATE {sql_literal(self.dialect, node.text)}"
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
            return self.date_diff(node)
        raise TypeError(f"not a FLUID SQL/1 node: {node!r}")  # pragma: no cover

    def branches(self, node: n.Node, values: List[n.Node]) -> List[str]:
        """The value branches of a CASE or COALESCE, numbers cast to the node's own type
        (see :meth:`common`: an engine would otherwise unify them and may drop scale)."""
        own = self.types.get(id(node))
        if own is None or own.kind != NUMBER:
            return [self.value(value) for value in values]
        target = self.number_type([own])
        return [f"CAST({self.value(value)} AS {target})" for value in values]

    def exact_type(self, node: n.Node) -> str:
        """The engine's exact type for a dividend: a decimal of its scale, or the double
        when its scale is unbounded (it is a float or a quotient already)."""
        sql_type = self.types.get(id(node))
        if sql_type is None or sql_type.kind != NUMBER or sql_type.scale is None:
            return exact_number_type(self.dialect, None)
        return exact_number_type(self.dialect, sql_type.scale)

    def round(self, node: n.Round) -> str:
        value = self.value(node.arg)
        arg = self.types.get(id(node.arg))
        exact = arg is not None and arg.scale is not None and arg.scale <= node.scale
        if node.mode == n.DEFAULT_ROUND_MODE or exact:
            # The two modes differ only on a tie, and no digit is cut when the places
            # cover the value's scale: the CASE form's * 10^s could then only overflow.
            return f"ROUND({value}, {node.scale})"
        if self.dialect == "snowflake":
            return f"ROUND({value}, {node.scale}, 'HALF_TO_EVEN')"
        if self.dialect == "databricks":
            return f"bround({value}, {node.scale})"
        # Half to even in fixed point: y = x * 10^s, then back by * 10^-s (a DECIMAL
        # literal, so the engine stays in DECIMAL instead of dividing in DOUBLE).
        up = "1" + "0" * node.scale
        down = "1" if node.scale == 0 else "0." + "0" * (node.scale - 1) + "1"
        y = f"({value} * {up})"
        trunc = f"{'truncate' if self.dialect == 'athena' else 'TRUNC'}({y})"
        even = f"({trunc} % 2)" if self.dialect == "duckdb" else f"MOD({trunc}, 2)"
        return (
            f"((CASE WHEN ABS({y} - {trunc}) = 0.5 THEN {trunc} + "
            f"(CASE WHEN {even} = 0 THEN 0 ELSE SIGN({y}) END) "
            f"ELSE ROUND({y}) END) * {down})"
        )

    # -- date differences --------------------------------------------------------------

    def date_diff(self, node: n.DateDiff) -> str:
        a, b = self.value(node.from_), self.value(node.to)
        if node.unit == "day":
            return {
                "duckdb": f"date_sub('day', {a}, {b})",
                "snowflake": f"DATEDIFF(day, {a}, {b})",
                "bigquery": f"DATE_DIFF({b}, {a}, DAY)",
                "databricks": f"datediff({b}, {a})",
                "postgres": f"({b} - {a})",
                "athena": f"date_diff('day', {a}, {b})",
            }[self.dialect]
        if self.whole_units:
            return f"date_sub('{node.unit}', {a}, {b})"
        months = self.whole_months
        if node.unit == "month":
            return f"(CASE WHEN {b} >= {a} THEN {months(a, b)} ELSE -{months(b, a)} END)"
        return (
            f"(CASE WHEN {b} >= {a} THEN {self.div12(months(a, b))} "
            f"ELSE -{self.div12(months(b, a))} END)"
        )

    def whole_months(self, a: str, b: str) -> str:
        """The whole months from ``a`` to ``b`` when ``b`` is on or after ``a`` (§2.8)."""

        def part(unit: str, x: str) -> str:
            return f"EXTRACT({unit} FROM {x})"

        return (
            f"(12 * ({part('YEAR', b)} - {part('YEAR', a)}) "
            f"+ ({part('MONTH', b)} - {part('MONTH', a)}) "
            f"- (CASE WHEN {part('DAY', a)} > {part('DAY', b)} AND {b} < {self.last_day(b)} "
            "THEN 1 ELSE 0 END))"
        )

    def last_day(self, x: str) -> str:
        if self.dialect == "postgres":
            return f"CAST(DATE_TRUNC('month', {x}) + INTERVAL '1 month' - INTERVAL '1 day' AS DATE)"
        if self.dialect == "athena":
            return f"last_day_of_month({x})"
        return f"LAST_DAY({x})"

    def div12(self, months: str) -> str:
        """``months`` (never negative here) divided by 12, rounded toward zero, as an integer."""
        return {
            "snowflake": f"TRUNC({months} / 12)",
            "bigquery": f"DIV({months}, 12)",
            "databricks": f"({months} div 12)",
            "postgres": f"DIV({months}, 12)",
            "athena": f"({months} / 12)",
        }[self.dialect]
