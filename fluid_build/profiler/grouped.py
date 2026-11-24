# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Statistics grouped by public groups: the answers to ``cliqueColumns``, ``groupBy`` and ``linked``.

A request (format 1-2-0) asks for three groupings, and a profile that answers them writes each
as a statistic whose groups are keys the contract fixes, never keys read from a record:

* ``cliqueColumns`` of a table: for each unordered pair of the first
  :data:`MOST_PAIR_COLUMNS` columns listed, the first column's histogram grouped by the second,
  by ``groupBy`` where the second is a category and by ``groupByBins`` where it is a number.
  An exact profile has no cliques, so it answers the columns with these two-way statistics
  and says which pairs it leaves out.
* a relationship's ``groupBy``: the relationship's children per parent, grouped by a column of
  the parent table, in the same ways.
* a relationship's ``linked``: a child column's histogram grouped by a category column of the
  parent row the relationship names (``groupByLink``).

The structure of a column is the one its default statistic has (:mod:`~fluid_build.profiler.defaults`):
the levels an ``enum(...)`` or a boolean declares, or the bins over the range its
``validationRules`` declare. A column with neither has no public groups, so no statistic
groups by it or is grouped by it, and the pair is left out with that reason. The bins of a
grouping are the bins of the histogram, so a number is grouped on the edges any reader of the
contract can state. The group keys of a ``groupByBins`` are the canonical decimal text of each
bin's lower edge, sorted as text, and every bin is a group, one no record holds included, so the
groups listed never depend on the records. The check for counts at a column's edges decides what
each group may publish (:mod:`~fluid_build.profiler.runner`).

A record outside the levels or the range its column declares refuses the profile, as it does
for the column's own statistics. Nothing here names a value: a message names a table, a column
and a row's index.
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from fluid_build.profiler import defaults, sources
from fluid_build.profiler._stats import (
    DeclaredBlock,
    DeclaredStatistic,
    Grouping,
    RequestTable,
    StatisticsBackend,
)

__all__ = [
    "MOST_PAIR_COLUMNS",
    "assignment",
    "child_groupings",
    "histogram_of",
    "link_assignment",
    "pair_statistics",
    "parent_groupings",
]

#: The most columns a ``cliqueColumns`` is answered over: its first 32, in the order listed.
MOST_PAIR_COLUMNS = 32

_NO_STRUCTURE = (
    "it has no public levels or bins: it is neither an enum(...) or a boolean column nor a "
    "number whose validationRules declare both ends of a range"
)
_NOT_READ = "it is a column fluid profile does not read"
_KEY = "a key of a counted relationship, which its children per parent describe"


def histogram_of(declared: Optional[DeclaredBlock], column: str) -> Optional[DeclaredStatistic]:
    """The ungrouped histogram the contract's declarations give *column*, or ``None``.

    Its levels or bins are public, so they serve as the groups of a grouping by the column
    and as the bins of a histogram of it.
    """
    if declared is None:
        return None
    for each in declared.statistics:
        if (
            each.column == column
            and each.group_by is None
            and each.kind in ("levelHistogram", "marginalHistogram")
        ):
            return each
    return None


def _keys(structure: DeclaredStatistic) -> Tuple[str, ...]:
    """Every group a grouping by the column has: its levels, or each bin's lower edge, sorted."""
    if structure.kind == "levelHistogram":
        return tuple(sorted(structure.shape["levels"]))
    return tuple(sorted(structure.shape["bins"][:-1]))


def _cells(structure: DeclaredStatistic) -> int:
    key = "levels" if structure.kind == "levelHistogram" else "bins"
    return len(structure.shape[key]) - (0 if key == "levels" else 1)


def _refused(message: str) -> Exception:
    from fluid_build.profiler.runner import ProfileError

    return ProfileError(
        "profile_record_outside_declared_domain",
        message,
        {
            "written": "nothing",
            "next": "the records and the contract disagree: fix the records, or the contract's "
            "column type, enum levels or validationRules range, then profile",
        },
    )


def assignment(
    structure: DeclaredStatistic,
    domain: str,
    values: Sequence[Any],
    *,
    name: str,
    backend: StatisticsBackend,
    fractional_bits: int,
) -> Tuple[Optional[str], ...]:
    """The group of each row when a column is the grouping: a level, a bin's lower edge, or ``None``.

    *values* are the column's raw values. A null is in no group. A value that is not a level
    the contract declares refuses the profile; a number is placed in the bin that holds it
    (the registry's rule), and one outside the declared range is refused where the column's
    own statistics are formed.
    """
    read = [
        backend.read_value(domain, value, column=name, row_index=index)
        for index, value in enumerate(values)
    ]
    if structure.kind == "levelHistogram":
        levels = frozenset(structure.shape["levels"])
        for index, value in enumerate(read):
            if value is not None and value not in levels:
                raise _refused(f"column {name!r} row {index}: not a level the contract declares")
        return tuple(read)
    edges = [Fraction(edge) for edge in structure.shape["bins"]]
    lowers = structure.shape["bins"]
    present = [(index, value) for index, value in enumerate(read) if value is not None]
    cells = backend.bin_cells([value for _, value in present], edges, fractional_bits)
    placed: List[Optional[str]] = [None] * len(read)
    for (index, _), cell in zip(present, cells, strict=True):
        placed[index] = lowers[cell]
    return tuple(placed)


def _grouping(
    structure: DeclaredStatistic, column: str, assigned: Sequence[Optional[str]]
) -> Grouping:
    """The :class:`Grouping` by *column*, whose public structure is *structure*."""
    if structure.kind == "levelHistogram":
        document: Dict[str, Any] = {"groupBy": column}
        form = "groupBy"
    else:
        document = {"groupByBins": {"column": column, "bins": list(structure.shape["bins"])}}
        form = "groupByBins"
    return Grouping(form, document, _keys(structure), tuple(assigned))


def pair_statistics(
    table: RequestTable,
    domains: Mapping[str, str],
    read: sources.SourceColumns,
    structures: Optional[DeclaredBlock],
    *,
    backend: StatisticsBackend,
    fractional_bits: int,
    held: FrozenSet[str] = frozenset(),
) -> Tuple[List[DeclaredStatistic], List[Tuple[str, str]]]:
    """The two-way statistics answering a table's ``cliqueColumns``, and what is left out.

    *structures* is the table's default block, which holds the public levels and bins.
    Each statistic is the histogram of the first column of a pair grouped by the second.
    One column's groupings hold at most :data:`~fluid_build.profiler.defaults.MOST_GROUPED_CELLS`
    cells together, as the contract's own groupings do.
    """
    listed = table.clique_columns
    left_out: List[Tuple[str, str]] = [
        (
            f"{name} of the cliqueColumns of {table.expose_id}",
            f"past the first {MOST_PAIR_COLUMNS} columns listed",
        )
        for name in listed[MOST_PAIR_COLUMNS:]
    ]
    used = listed[:MOST_PAIR_COLUMNS]
    assigned: Dict[str, Tuple[Optional[str], ...]] = {}
    cells: Dict[str, int] = {}
    found: List[DeclaredStatistic] = []
    for position, first in enumerate(used):
        for second in used[position + 1 :]:
            what = f"{first} grouped by {second} of {table.expose_id}"
            reason = _pair_unavailable(first, second, domains, held, structures)
            if reason is not None:
                left_out.append((what, reason))
                continue
            base = histogram_of(structures, first)
            by = histogram_of(structures, second)
            assert base is not None and by is not None  # _pair_unavailable checked both
            held_cells = cells.get(first, 0) + _cells(base) * _cells(by)
            if held_cells > defaults.MOST_GROUPED_CELLS:
                left_out.append(
                    (
                        what,
                        f"past the {defaults.MOST_GROUPED_CELLS:,} cells one column's "
                        "groupings may hold",
                    )
                )
                continue
            cells[first] = held_cells
            if second not in assigned:
                assigned[second] = assignment(
                    by,
                    domains[second],
                    read.values[second],
                    name=second,
                    backend=backend,
                    fractional_bits=fractional_bits,
                )
            found.append(
                replace(base, group_by=second, grouping=_grouping(by, second, assigned[second]))
            )
    return found, left_out


def _pair_unavailable(
    first: str,
    second: str,
    domains: Mapping[str, str],
    held: FrozenSet[str],
    structures: Optional[DeclaredBlock],
) -> Optional[str]:
    for name in (first, second):
        if name not in domains:
            return f"{name}: {_NOT_READ}"
        if name in held:
            return f"{name}: {_KEY}"
        if histogram_of(structures, name) is None:
            return f"{name}: {_NO_STRUCTURE}"
    return None


def parent_groupings(
    names: Sequence[str],
    *,
    parent: str,
    domains: Mapping[str, str],
    read: sources.SourceColumns,
    structures: Optional[DeclaredBlock],
    backend: StatisticsBackend,
    fractional_bits: int,
) -> Tuple[List[Tuple[str, Grouping]], List[Tuple[str, str]]]:
    """The groupings of a relationship's parents by the columns *names*, and each one left out.

    Each parent row is in the group of its value of the column: a level, or the bin that holds
    it. A parent whose value is null is in none.
    """
    found: List[Tuple[str, Grouping]] = []
    left_out: List[Tuple[str, str]] = []
    for name in names:
        structure = histogram_of(structures, name)
        what = f"grouped by {parent}.{name}"
        if name not in domains:
            left_out.append((what, f"{name}: {_NOT_READ}"))
        elif structure is None:
            left_out.append((what, f"{name}: {_NO_STRUCTURE}"))
        else:
            assigned = assignment(
                structure,
                domains[name],
                read.values[name],
                name=name,
                backend=backend,
                fractional_bits=fractional_bits,
            )
            found.append((name, _grouping(structure, name, assigned)))
    return found, left_out


def link_assignment(
    child_keys: Sequence[Tuple[Any, ...]],
    parent_keys: Sequence[Tuple[Any, ...]],
    parent_groups: Sequence[Optional[str]],
) -> Tuple[Optional[str], ...]:
    """The group of each child row: that of the parent row its key reaches, or ``None``.

    A child with a null key part reaches no parent, and neither does one whose key no parent
    holds. A key that several parent rows hold reaches a group only when all of them are in
    the same one, so the link never picks a parent by the order of the records.
    """
    reached: Dict[Tuple[Any, ...], Optional[str]] = {}
    for key, group in zip(parent_keys, parent_groups, strict=True):
        if None in key:
            continue
        if key in reached and reached[key] != group:
            reached[key] = None
        else:
            reached.setdefault(key, group)
    return tuple(None if None in key else reached.get(key) for key in child_keys)


def child_groupings(
    structure: DeclaredStatistic,
    parent_structure: DeclaredStatistic,
    *,
    parent: str,
    parent_column: str,
    parent_columns: Sequence[str],
    child_columns: Sequence[str],
    assigned: Sequence[Optional[str]],
) -> DeclaredStatistic:
    """*structure*, the child column's histogram, grouped by *parent_column* through a link."""
    document = {
        "groupByLink": {
            "parent": {"table": parent, "columns": list(parent_columns)},
            "columns": list(child_columns),
            "column": parent_column,
        }
    }
    label = f"{parent}.{parent_column} via {','.join(child_columns)}"
    return replace(
        structure,
        group_by=label,
        grouping=Grouping("groupByLink", document, _keys(parent_structure), tuple(assigned)),
    )
