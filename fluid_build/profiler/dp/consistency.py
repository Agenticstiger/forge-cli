# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Make noisy count tables agree exactly, and write them as decimal text that still agrees.

A profiler that releases clique marginals under differential privacy draws each table's noise
on its own, so two tables that share a column disagree about that column's counts. The
consumer (flux-engine ``PROFILE-SPEC.md`` sections 3a and 5a) requires that cliques sharing
columns agree **exactly**, as rationals read from decimal strings, and that the noise covariance
of each table's cells stays at most its variance times the identity.

:func:`make_consistent` does this in exact :class:`~fractions.Fraction` arithmetic. Each step is
the least-squares projection of the tables onto agreement on one set of columns, weighted by
inverse variance, so the result agrees exactly and keeps every table's noise covariance at most
its variance times the identity (PROFILE-SPEC section 5a): a projection in the metric the noise
defines only shrinks the covariance. The sets are processed smaller first, which keeps the
agreements already reached (a step on a set A changes each table by an amount that sums to zero
over every smaller set). This is borrowed as a pattern from the consistency step of PriView
(Qardaji, Yang and Li, "PriView: practical differentially private release of marginal
contingency tables", SIGMOD 2014), whose order is the same.

:func:`junction_forest` arranges the cliques the way the engine does (ADR 0021,
``flux_engine/calibrate/junction.py``): a maximum-weight spanning forest of the graph of cliques
weighted by the columns they share, with the engine's tie-breaking, refused when a column's
cliques are not connected (the running intersection property).

:func:`to_decimal_text` rounds the exact values to a decimal grid without breaking an
agreement: it rounds the common total, then walks the forest so that every clique's cells,
summed onto the separator it shares with its parent, equal the parent's rounded margin.

All of this is post-processing: it reads only noisy numbers and column names.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

__all__ = [
    "CountTable",
    "Forest",
    "ForestNode",
    "junction_forest",
    "make_consistent",
    "to_decimal_text",
]


@dataclass(frozen=True)
class CountTable:
    """One noisy count table.

    ``values`` is row-major over ``columns`` (the first column varies slowest), one value per
    combination of the columns' cells, and ``variance`` is the noise variance of each cell.
    """

    label: str
    columns: Tuple[str, ...]
    sizes: Tuple[int, ...]
    values: Tuple[Fraction, ...]
    variance: Fraction


@dataclass(frozen=True)
class ForestNode:
    """A clique's place in the forest: its parent, and the columns it shares with it.

    ``separator`` is in the clique's own column order and is empty for a root.
    """

    label: str
    parent: Optional[str]
    separator: Tuple[str, ...]


@dataclass(frozen=True)
class Forest:
    """The cliques in processing order: each root, then its tree breadth first."""

    nodes: Tuple[ForestNode, ...]


def _validate(tables: Sequence[CountTable]) -> None:
    if not tables:
        raise ValueError("no count tables")
    seen: Set[str] = set()
    for table in tables:
        if table.label in seen:
            raise ValueError(f"two count tables are both labelled {table.label!r}")
        seen.add(table.label)
    sizes: Dict[str, Tuple[int, str]] = {}
    for table in tables:
        label = table.label
        if not table.columns:
            raise ValueError(f"table {label!r} has no columns")
        if len(set(table.columns)) != len(table.columns):
            raise ValueError(f"table {label!r} repeats a column")
        if len(table.sizes) != len(table.columns):
            raise ValueError(f"table {label!r} has a different number of sizes and columns")
        if any(isinstance(s, bool) or not isinstance(s, int) or s < 1 for s in table.sizes):
            raise ValueError(f"table {label!r} has a column of fewer than one cell")
        if len(table.values) != math.prod(table.sizes):
            raise ValueError(f"table {label!r} has the wrong number of values for its sizes")
        for value in itertools.chain(table.values, (table.variance,)):
            if isinstance(value, bool) or not isinstance(value, (Fraction, int)):
                raise ValueError(f"table {label!r} holds a value that is not an exact number")
        if table.variance <= 0:
            raise ValueError(f"table {label!r} has a variance that is not above zero")
        for column, size in zip(table.columns, table.sizes, strict=False):
            if column in sizes and sizes[column][0] != size:
                raise ValueError(
                    f"column {column!r} has a different size in tables "
                    f"{sizes[column][1]!r} and {label!r}"
                )
            sizes.setdefault(column, (size, label))


def _cell_keys(table: CountTable, names: Sequence[str]) -> List[Tuple[int, ...]]:
    """For each cell of *table* (row-major), its cell of the sub-table over *names*."""
    positions = [table.columns.index(name) for name in names]
    return [
        tuple(digits[p] for p in positions)
        for digits in itertools.product(*(range(size) for size in table.sizes))
    ]


def _closure(tables: Sequence[CountTable]) -> List[Tuple[str, ...]]:
    """Sets of columns held by two or more tables, smaller first, ties by sorted names.

    The sets are the closure of the tables' column sets under intersection, with the empty set.
    A set's cells are ordered row-major over its columns sorted by name.
    """
    sets: Set[FrozenSet[str]] = {frozenset(t.columns) for t in tables}
    sets.add(frozenset())
    grew = True
    while grew:
        grew = False
        for one, other in itertools.combinations(list(sets), 2):
            both = one & other
            if both not in sets:
                sets.add(both)
                grew = True
    held = [tuple(sorted(a)) for a in sets if sum(1 for t in tables if a <= set(t.columns)) >= 2]
    return sorted(held, key=lambda names: (len(names), names))


def make_consistent(tables: Sequence[CountTable]) -> Tuple[Tuple[Fraction, ...], ...]:
    """New values for each table, in order, so that tables agree exactly on shared columns.

    For each set A of the closure (see :func:`_closure`), every table i that holds A is moved by
    ``(target - P_i y_i) / n_i`` per cell, where ``P_i y_i`` is the table summed onto A, ``n_i``
    the number of its cells per A-cell, and ``target = sum(w_i P_i y_i) / sum(w_i)`` with
    ``w_i = 1 / (n_i * variance_i)``. For the empty set this makes every total equal.
    """
    _validate(tables)
    values: List[List[Fraction]] = [[Fraction(v) for v in t.values] for t in tables]
    for names in _closure(tables):
        wanted = set(names)
        holders = [i for i, t in enumerate(tables) if wanted <= set(t.columns)]
        sums: Dict[int, Dict[Tuple[int, ...], Fraction]] = {}
        keys: Dict[int, List[Tuple[int, ...]]] = {}
        weight: Dict[int, Fraction] = {}
        per_cell: Dict[int, int] = {}
        for i in holders:
            table = tables[i]
            keys[i] = _cell_keys(table, names)
            projected: Dict[Tuple[int, ...], Fraction] = {}
            for key, value in zip(keys[i], values[i], strict=False):
                projected[key] = projected.get(key, Fraction(0)) + value
            sums[i] = projected
            per_cell[i] = math.prod(
                size
                for column, size in zip(table.columns, table.sizes, strict=False)
                if column not in wanted
            )
            weight[i] = 1 / (per_cell[i] * Fraction(table.variance))
        total_weight = sum(weight.values(), Fraction(0))
        target: Dict[Tuple[int, ...], Fraction] = {
            key: sum((weight[i] * sums[i][key] for i in holders), Fraction(0)) / total_weight
            for key in sums[holders[0]]
        }
        for i in holders:
            shift = {key: (target[key] - sums[i][key]) / per_cell[i] for key in target}
            values[i] = [value + shift[key] for key, value in zip(keys[i], values[i], strict=False)]
    return tuple(tuple(v) for v in values)


def junction_forest(tables: Sequence[CountTable]) -> Forest:
    """The engine's junction forest over the tables of two or more columns (the cliques).

    A maximum-weight spanning forest of the graph of cliques, weighted by the number of columns
    two cliques share. Cliques are ranked by most columns, then sorted column names, then
    position; edges are taken by most shared columns, then by the ranks of their ends. The
    root of each tree is its best-ranked clique, and trees are walked breadth first, children by
    rank. A set of cliques in which some column's cliques are not connected is refused, and so
    is a one-column table that shares its column with another table.
    """
    _validate(tables)
    for table in tables:
        if len(table.columns) != 1:
            continue
        for other in tables:
            if other is not table and table.columns[0] in other.columns:
                raise ValueError(
                    f"table {table.label!r} has one column, {table.columns[0]!r}, which "
                    f"table {other.label!r} also holds"
                )
    cliques = [t for t in tables if len(t.columns) >= 2]
    sets = [frozenset(t.columns) for t in cliques]
    ranking = sorted(range(len(sets)), key=lambda i: (-len(sets[i]), tuple(sorted(sets[i])), i))
    rank = {index: place for place, index in enumerate(ranking)}
    edges = sorted(
        (
            (-len(sets[i] & sets[j]), min(rank[i], rank[j]), max(rank[i], rank[j]))
            for i in range(len(sets))
            for j in range(i + 1, len(sets))
            if sets[i] & sets[j]
        )
    )
    group = list(range(len(sets)))

    def find(x: int) -> int:
        while group[x] != x:
            group[x] = group[group[x]]
            x = group[x]
        return x

    adjacency: Dict[int, List[int]] = {i: [] for i in range(len(sets))}
    for _, low, high in edges:
        a, b = ranking[low], ranking[high]
        if find(a) != find(b):
            group[find(a)] = find(b)
            adjacency[a].append(b)
            adjacency[b].append(a)

    parent: Dict[int, int] = {}
    depth: Dict[int, int] = {}
    roots: List[int] = []
    for start in ranking:  # best rank first, so a tree's root is its best-ranked clique
        if start in depth:
            continue
        roots.append(start)
        depth[start] = 0
        queue = [start]
        for current in queue:
            for near in sorted(adjacency[current], key=lambda i: rank[i]):
                if near not in depth:
                    depth[near] = depth[current] + 1
                    parent[near] = current
                    queue.append(near)
    _running_intersection(cliques, sets, parent)

    def root_of(index: int) -> int:
        while index in parent:
            index = parent[index]
        return index

    nodes: List[ForestNode] = []
    below = sorted(parent, key=lambda i: (depth[i], rank[i]))
    for root in roots:
        nodes.append(ForestNode(cliques[root].label, None, ()))
        for child in below:
            if root_of(child) == root:
                shared = sets[child] & sets[parent[child]]
                nodes.append(
                    ForestNode(
                        cliques[child].label,
                        cliques[parent[child]].label,
                        tuple(c for c in cliques[child].columns if c in shared),
                    )
                )
    return Forest(tuple(nodes))


def _running_intersection(
    cliques: Sequence[CountTable], sets: Sequence[FrozenSet[str]], parent: Dict[int, int]
) -> None:
    """Refuse a forest in which the cliques holding some column are not connected."""
    for column in sorted(set().union(*sets) if sets else set()):
        tops: Dict[int, int] = {}
        for index, columns in enumerate(sets):
            if column not in columns:
                continue
            top = index
            while top in parent and column in sets[parent[top]]:
                top = parent[top]
            tops.setdefault(top, index)
        if len(tops) > 1:
            first, second = list(tops.values())[:2]
            raise ValueError(
                f"cliques {cliques[first].label!r} and {cliques[second].label!r} both hold "
                f"column {column!r} and no chain of cliques that hold it joins them, so the "
                "cliques have no junction tree (the running intersection property fails)"
            )


def _round_to_target(cells: Sequence[Fraction], target: int) -> List[int]:
    """Whole grid steps for *cells* (in grid steps) that add up to *target* exactly.

    Floor each cell, then hand the left-over steps out, whole rounds first, then one step each:
    largest remainder first for a positive balance, smallest first for a negative one, lowest
    position first among equals.
    """
    floors = [math.floor(c) for c in cells]
    remainders = [c - f for c, f in zip(cells, floors, strict=False)]
    count = len(cells)
    balance = target - sum(floors)
    if balance >= 0:
        rounds, extra = divmod(balance, count)
        order = sorted(range(count), key=lambda k: (-remainders[k], k))[:extra]
        step = 1
    else:
        rounds, extra = divmod(-balance, count)
        order = sorted(range(count), key=lambda k: (remainders[k], k))[:extra]
        rounds, step = -rounds, -1
    result = [f + rounds for f in floors]
    for k in order:
        result[k] += step
    return result


def _spell(units: int, places: int) -> str:
    """Decimal text of ``units * 10**-places``: no exponent or plus, no trailing fraction zeros."""
    if units == 0:
        return "0"
    digits = str(abs(units)).rjust(places + 1, "0")
    whole, fraction = digits[: len(digits) - places], digits[len(digits) - places :]
    fraction = fraction.rstrip("0")
    text = f"{whole}.{fraction}" if fraction else whole
    return f"-{text}" if units < 0 else text


def to_decimal_text(
    tables: Sequence[CountTable],
    values: Sequence[Sequence[Fraction]],
    forest: Forest,
    places: int = 12,
) -> Tuple[Tuple[str, ...], ...]:
    """Decimal strings of at most *places* fractional digits whose agreements stay exact.

    *values* are the tables' consistent values (from :func:`make_consistent`) and *forest* the
    result of :func:`junction_forest` over *tables*. The common total is rounded to the grid
    ``10**-places``, half up. Every root clique and every one-column table is rounded to add up
    to it; every other clique, parent first, is rounded so that each cell of its separator sums
    to the parent's rounded margin there. See :func:`_round_to_target` for the rounding.
    """
    _validate(tables)
    if places < 0:
        raise ValueError("places is below zero")
    if len(values) != len(tables) or any(
        len(v) != len(t.values) for v, t in zip(values, tables, strict=False)
    ):
        raise ValueError("the values do not match the tables")
    index = {t.label: i for i, t in enumerate(tables)}
    cliques = {t.label for t in tables if len(t.columns) >= 2}
    if sorted(n.label for n in forest.nodes) != sorted(cliques):
        raise ValueError("the forest does not hold exactly the tables of two or more columns")
    scale = 10**places
    totals = [sum((Fraction(x) for x in v), Fraction(0)) for v in values]
    for table, total in zip(tables, totals, strict=False):
        if total != totals[0]:
            raise ValueError(f"tables {tables[0].label!r} and {table.label!r} differ in total")
    total_units = math.floor(totals[0] * scale + Fraction(1, 2))

    grid = [[Fraction(x) * scale for x in v] for v in values]
    rounded: Dict[str, List[int]] = {}
    for table in tables:
        if len(table.columns) == 1:
            rounded[table.label] = _round_to_target(grid[index[table.label]], total_units)
    for node in forest.nodes:
        table = tables[index[node.label]]
        cells = grid[index[node.label]]
        if node.parent is None:
            rounded[node.label] = _round_to_target(cells, total_units)
            continue
        if node.parent not in rounded:
            raise ValueError(f"clique {node.label!r} comes before its parent {node.parent!r}")
        parent = tables[index[node.parent]]
        names = tuple(sorted(node.separator))
        margin: Dict[Tuple[int, ...], int] = {}
        for key, value in zip(_cell_keys(parent, names), rounded[node.parent], strict=False):
            margin[key] = margin.get(key, 0) + value
        groups: Dict[Tuple[int, ...], List[int]] = {}
        for position, key in enumerate(_cell_keys(table, names)):
            groups.setdefault(key, []).append(position)
        out = [0] * len(cells)
        for key, positions in groups.items():
            fixed = _round_to_target([cells[p] for p in positions], margin[key])
            for position, value in zip(positions, fixed, strict=False):
                out[position] = value
        rounded[node.label] = out
    return tuple(tuple(_spell(u, places) for u in rounded[t.label]) for t in tables)
