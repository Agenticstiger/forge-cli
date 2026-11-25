# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0
# Adapted from FLUX flux_engine/consumers/_bounds.py at 19dfe48, Apache-2.0

"""Bounds on the cost of reading a consumer query, applied before the work they bound.

Two refusals live here because each must come before sqlglot does the work it prevents:

* **A projection alias read in its own SELECT**. ``qualify`` writes the expression
  of an alias in place of each reference to it, so ``SELECT x AS a0, a0 + a0 AS a1, a1 + a1 AS a2,
  ...`` doubles its tree at every step, far past the node cap and before any cap is counted. A text
  is refused when a column it writes is the alias of a projection of the same ``SELECT``, whether
  it is read in a later projection, ``WHERE``, ``GROUP BY``, ``HAVING`` or ``QUALIFY``, or inside an
  expression of an ``ORDER BY``. The one use kept is a whole ``ORDER BY`` key that names an output
  column (``ORDER BY n DESC``), which the total-order rule reads as written and which expands to one
  projection at most. The fix the message gives is to write the expression out.
* **A LIKE pattern past 512 characters**. The pattern is written into the text
  that runs, so its length is bounded.
* **Stars and positional GROUP BY keys written out past the canonical cap**.
  ``qualify`` writes each star as the columns of its sources and each positional ``GROUP BY`` key
  as a copy of the projection it names. The source caps bound the stars, the keys and the columns
  one by one, never their product, so ``SELECT *, *, ...`` over a wide expose, or ``GROUP BY 1, 1,
  ...`` of a long projection, grows as the product. The nodes these add are counted first, scope
  by scope from the innermost, and a text that adds more than the canonical tree may hold is
  refused before ``qualify`` writes any of them.
"""

from __future__ import annotations

from bisect import bisect_right
from typing import Dict, Iterator, List, Mapping, Optional, Tuple

from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, build_scope, walk_in_scope

from fluid_build.profiler.consumer_queries._types import Refusal

__all__ = [
    "MOST_PATTERN_CHARACTERS",
    "NODES_PER_STAR_COLUMN",
    "expansion_nodes",
    "refuse_alias_references",
    "refuse_long_patterns",
    "refuse_wide_expansion",
]

#: The longest LIKE or ILIKE pattern a consumer text may write, in characters.
MOST_PATTERN_CHARACTERS = 512

#: The nodes each column a star becomes holds at the least once qualified: the column and the two
#: names it is written with, its table's and its own.
NODES_PER_STAR_COLUMN = 3

#: A positional key of more digits than this names no projection, and sqlglot refuses it.
_POSITION_DIGITS = 18

_DUCKDB = "duckdb"

_CLAUSES = ("where", "group", "having", "qualify")


def _fold(name: object) -> str:
    return str(name).lower()


def _identity(item: exp.Expr) -> bool:
    """Whether *item* is ``c AS c``, an alias whose expansion changes nothing."""
    return (
        isinstance(item, exp.Alias)
        and isinstance(item.this, exp.Column)
        and not item.this.table
        and _fold(item.this.name) == _fold(item.alias)
    )


def _columns(node: exp.Expr) -> Iterator[exp.Column]:
    for found in walk_in_scope(node):
        if isinstance(found, exp.Column) and not found.table:
            yield found


def _refusal(label: str, name: str) -> Refusal:
    return Refusal(
        "alias",
        f"{label} reads {name!r}, the alias of a projection of the same SELECT; "
        f"write the expression out instead of its alias",
    )


def _refuse_in_projections(select: exp.Select, label: str) -> None:
    """A projection may read only columns of its sources, never an alias an earlier one wrote."""
    earlier: Dict[str, bool] = {}
    for item in select.args.get("expressions") or []:
        for column in _columns(item):
            if earlier.get(_fold(column.name)):
                raise _refusal(label, str(column.name))
        if isinstance(item, exp.Alias):
            earlier[_fold(item.alias)] = not _identity(item)


def _refuse_in_clauses(select: exp.Select, aliases: Dict[str, bool], label: str) -> None:
    for key in _CLAUSES:
        clause = select.args.get(key)
        if clause is None:
            continue
        for column in _columns(clause):
            if aliases.get(_fold(column.name)):
                raise _refusal(label, str(column.name))


def _refuse_in_order(select: exp.Select, aliases: Dict[str, bool], label: str) -> None:
    order = select.args.get("order")
    if order is None:
        return
    for key in order.expressions:
        expression = key.this
        for column in _columns(expression):
            if column is expression:
                continue  # a whole key naming an output column is read as written
            if aliases.get(_fold(column.name)):
                raise _refusal(label, str(column.name))


def refuse_alias_references(tree: exp.Expr, *, label: str) -> None:
    """Refuse a column that reads a projection alias of its own SELECT.

    Read before ``qualify``, so that no alias is ever expanded. *label* names the query or view.
    """
    selects: List[exp.Select] = [node for node in tree.walk() if isinstance(node, exp.Select)]
    for select in selects:
        _refuse_in_projections(select, label)
        aliases = {
            _fold(item.alias): not _identity(item)
            for item in select.args.get("expressions") or []
            if isinstance(item, exp.Alias)
        }
        if not any(aliases.values()):
            continue
        _refuse_in_clauses(select, aliases, label)
        _refuse_in_order(select, aliases, label)


def _node_count(node: exp.Expr) -> int:
    """The nodes of the tree under *node*, itself included."""
    return sum(1 for _ in node.walk())


class _Projections:
    """The output columns of one SELECT once its stars are written out, and what each copies."""

    def __init__(self) -> None:
        self.ends: List[int] = []  # the output columns up to and with each projection
        self.nodes: List[Optional[exp.Expr]] = []  # the projection, or None for a star
        self._copied: Dict[int, int] = {}  # position -> the nodes one copy of it holds

    def add(self, count: int, projection: Optional[exp.Expr]) -> None:
        self.ends.append((self.ends[-1] if self.ends else 0) + count)
        self.nodes.append(projection)

    @property
    def columns(self) -> int:
        return self.ends[-1] if self.ends else 0

    def copied(self, key: exp.Literal) -> int:
        """The nodes sqlglot copies for the positional key *key*: the projection it names.

        sqlglot reads the key as Python reads an index less one, so ``0`` names the last output
        column; a key past the columns names none, and sqlglot refuses it.
        """
        digits = str(key.this)
        if not self.ends or len(digits) > _POSITION_DIGITS:
            return 0
        index = int(digits) - 1
        if index == -1:
            index = self.columns - 1
        if not 0 <= index < self.columns:
            return 0
        position = bisect_right(self.ends, index)
        if position not in self._copied:
            self._copied[position] = self._count(self.nodes[position])
        return self._copied[position]

    @staticmethod
    def _count(projection: Optional[exp.Expr]) -> int:
        """The nodes one copy of *projection* holds, read once per projection."""
        if projection is None:
            return NODES_PER_STAR_COLUMN
        copy = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(copy, (exp.Literal, exp.Boolean, exp.Null)):
            return 0  # sqlglot keeps the key and copies no constant
        return _node_count(copy)


class _Widths:
    """The columns a star reads from each source: an expose's, or a WITH entry's or subquery's."""

    def __init__(self, widths: Mapping[str, int]) -> None:
        self.exposes = {name.lower(): count for name, count in widths.items()}
        self.widest = max(self.exposes.values(), default=0)
        #: The output columns of each scope counted so far, by the id of its expression.
        self.outputs: Dict[int, int] = {}

    def of(self, source: object) -> int:
        if isinstance(source, Scope):
            return self.outputs.get(id(source.expression), self.widest)
        if isinstance(source, exp.Table):
            return self.exposes.get(source.name, self.widest)
        return self.widest


def _select_expansion(scope: Scope, widths: _Widths) -> Tuple[int, int]:
    """The output columns of the SELECT of *scope*, and the nodes its stars and keys add."""
    select = scope.expression
    sources = {name: source for name, (_, source) in scope.selected_sources.items()}
    every = sum(widths.of(source) for source in sources.values())
    projections = _Projections()
    added = 0
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            count = every
        elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            found = sources.get(projection.table)
            count = every if found is None else widths.of(found)
        else:
            projections.add(1, projection)
            continue
        projections.add(count, None)
        added += count * NODES_PER_STAR_COLUMN
    group = select.args.get("group")
    for key in group.expressions if group is not None else ():
        if isinstance(key, exp.Literal) and key.is_int:
            added += projections.copied(key)
    return projections.columns, added


def _scope_expansion(scope: Scope, widths: _Widths) -> Tuple[int, int]:
    """The output columns of *scope*, and the nodes ``qualify`` adds to it."""
    if isinstance(scope.expression, exp.Select):
        return _select_expansion(scope, widths)
    if isinstance(scope.expression, exp.SetOperation):
        branches = [widths.outputs.get(id(b.expression), 0) for b in scope.set_operation_scopes]
        return max(branches, default=0), 0
    return widths.widest, 0


def expansion_nodes(tree: exp.Expr, widths: Mapping[str, int], *, most: int) -> int:
    """The nodes ``qualify`` adds to *tree* by writing out its stars and positional GROUP BY keys.

    Counted on a copy whose names are folded as ``qualify`` folds them, scope by scope from the
    innermost, so that a star over a WITH entry or a subquery reads the columns that entry returns
    once its own stars are written out. *widths* gives each expose's visible columns. The count is
    a floor of what ``qualify`` writes, and it stops once it passes *most*.
    """
    root = build_scope(normalize_identifiers(tree.copy(), dialect=_DUCKDB))
    if root is None:
        return 0
    counted = _Widths(widths)
    added = 0
    for scope in root.traverse():
        columns, nodes = _scope_expansion(scope, counted)
        counted.outputs[id(scope.expression)] = columns
        added += nodes
        if added > most:
            break
    return added


def refuse_wide_expansion(
    tree: exp.Expr, widths: Mapping[str, int], *, label: str, most: int
) -> None:
    """Refuse a text whose stars and positional GROUP BY keys add more than *most* nodes.

    The canonical tree may hold *most* nodes, and each node counted here is one the
    qualified tree holds, so a text refused here is one ``qualify`` would make too large.
    """
    added = expansion_nodes(tree, widths, most=most)
    if added > most:
        raise Refusal(
            "bounds",
            f"{label} writes its stars and positional GROUP BY keys out to more than {most} "
            f"expression nodes; name the columns it reads",
        )


def refuse_long_patterns(tree: exp.Expr, *, label: str) -> None:
    """Refuse a LIKE or ILIKE pattern of more than 512 characters."""
    for node in tree.find_all(exp.Like, exp.ILike):
        pattern = node.expression
        if (
            isinstance(pattern, exp.Literal)
            and pattern.is_string
            and len(str(pattern.this)) > MOST_PATTERN_CHARACTERS
        ):
            raise Refusal(
                "pattern",
                f"{label} writes a LIKE pattern of {len(str(pattern.this))} characters; "
                f"at most {MOST_PATTERN_CHARACTERS} are read",
            )
