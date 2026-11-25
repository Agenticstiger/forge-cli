# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A column's statistic counted per entity (PROFILE-SPEC format 1-3-0, ``per``).

A contract ``dependency`` says a column is set once for each value of the columns it follows. When
those columns are an entity's key (a declared key of the table, or the child side of a
relationship the table declares), the distribution the column is drawn from is the one over
entities, not over rows. For each such column that has a default row-level histogram, a statistic
with the same definition, levels or bins and a ``per`` is written beside it.

The entities are the distinct key tuples with no NULL part. Each counts once, at the known value
its rows most often hold (on a tie the first in code point order of the value's text); an entity
whose rows hold no known value is not counted. ``k`` and every cell are entities, and the
statistic is checked in a call of its own, as a row-level histogram is.

Nothing here names a value: a note names a table, columns and a reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from fluid_build.profiler import dates, disclosure, sources
from fluid_build.profiler._stats import (
    DeclaredBlock,
    DeclaredStatistic,
    EdgeDecision,
    MeasuredStatistic,
    StatisticsBackend,
)

if TYPE_CHECKING:  # the runner imports this module's caller
    from collections import Counter

    from fluid_build.profiler.runner import ExposePlan, StageTimings

__all__ = ["PerTable", "dependencies", "entity_keys", "table_per"]

_HISTOGRAMS = ("levelHistogram", "marginalHistogram")
_GROUPED = ("groupBy", "groupByLink", "groupByBins", "per")
_NO_HISTOGRAM = "its column has no default histogram on declared levels or public bins"
_UNREAD = "a column of its key is not one fluid profile reads"
_ROW_LEVEL = "it withholds other cells than the row-level histogram on the same cells does"


def _mappings(value: Any) -> List[Mapping[str, Any]]:
    return [each for each in value if isinstance(each, Mapping)] if isinstance(value, list) else []


def _names(value: Any) -> Tuple[str, ...]:
    return tuple(each for each in value if isinstance(each, str)) if isinstance(value, list) else ()


def _declared_keys(semantics: Mapping[str, Any]) -> Set[str]:
    """The columns of the ``primary`` and ``unique`` entities a table declares."""
    found: Set[str] = set()
    for entity in _mappings(semantics.get("entities")):
        expr = entity.get("expr", entity.get("name"))
        if entity.get("type") in ("primary", "unique") and isinstance(expr, str):
            found.add(expr)
    return found


def _linked_keys(
    expose_id: str, exposes: Mapping[str, Mapping[str, Any]], versioned: bool
) -> Tuple[Set[FrozenSet[str]], Set[str]]:
    """The keys the relationships of the contract name for a table, and the columns of them.

    A table's own relationships give their ``fromColumns``, the others that target it their
    ``toColumns``; a table with a validity window repeats its parents, so those are no key, though
    their columns still belong to one.
    """
    keys: Set[FrozenSet[str]] = set()
    held: Set[str] = set()
    for other_id, other in exposes.items():
        other_semantics = other.get("semantics")
        for link in _mappings(
            other_semantics.get("relationships") if isinstance(other_semantics, Mapping) else None
        ):
            if other_id == expose_id:
                found = frozenset(_names(link.get("fromColumns")))
            elif link.get("to") == expose_id:
                found = frozenset(_names(link.get("toColumns")))
                if versioned:
                    held.update(found)
                    continue
            else:
                continue
            if found:
                keys.add(found)
                held.update(found)
    return keys, held


def entity_keys(
    expose_id: str, exposes: Mapping[str, Mapping[str, Any]]
) -> Tuple[Set[FrozenSet[str]], FrozenSet[str]]:
    """The entity keys of a table and every column that is part of a key of it.

    An entity key is a declared ``primary`` or ``unique`` entity's column, the ``toColumns`` of a
    relationship that targets the table (not on a table with a validity window, whose parents
    repeat), and the ``fromColumns`` of each relationship the table declares. The second value
    also holds the columns of the keys a validity window repeats, which are keys all the same.
    """
    from fluid_build.data_rules._model import declares_validity_window

    expose = exposes[expose_id]
    semantics = expose.get("semantics")
    declared = _declared_keys(semantics if isinstance(semantics, Mapping) else {})
    keys, held = _linked_keys(expose_id, exposes, declares_validity_window(expose))
    keys.update(frozenset({each}) for each in declared)
    return keys, frozenset(held | declared)


def dependencies(expose: Mapping[str, Any]) -> List[Tuple[Tuple[str, ...], str]]:
    """Each ``(from columns, dependent column)`` the contract's dependencies set, in its order.

    A ``bothWays`` dependency (one column a side) also sets the column it follows.
    """
    contract = expose.get("contract")
    found: List[Tuple[Tuple[str, ...], str]] = []
    for rule in _mappings(contract.get("invariants") if isinstance(contract, Mapping) else None):
        dependency = rule.get("dependency")
        if not isinstance(dependency, Mapping):
            continue
        source, targets = _names(dependency.get("from")), _names(dependency.get("to"))
        found.extend((source, each) for each in targets)
        if dependency.get("bothWays") is True and len(source) == 1 and len(targets) == 1:
            found.append((targets, source[0]))
    return found


def _text(value: Any) -> str:
    """A value as text, for the tie-break: a category as it is, a number as its decimal."""
    if not isinstance(value, Fraction):
        return str(value)
    magnitude, scale = abs(value), 0
    while (magnitude * 10**scale).denominator != 1:
        scale += 1
    digits = str(int(magnitude * 10**scale)).rjust(scale + 1, "0")
    body = digits if scale == 0 else f"{digits[:-scale]}.{digits[-scale:]}"
    return ("-" if value < 0 else "") + body


def _mode(tally: Mapping[Any, int]) -> Any:
    if len(tally) == 1:
        return next(iter(tally))
    top = max(tally.values())
    return min((value for value, count in tally.items() if count == top), key=_text)


def _entity_ids(
    table: Tuple["ExposePlan", sources.SourceColumns],
    key: Sequence[str],
    backend: StatisticsBackend,
) -> Tuple[List[int], int]:
    """Each row's entity number (``-1``: a NULL part) and the number of entities; formed once."""
    from fluid_build.profiler import runner

    read = table[1]
    kept = ("entities", tuple(key))
    found: Optional[Tuple[List[int], int]] = read.decoded.get(kept)
    if found is None:
        index: Dict[Tuple[Any, ...], int] = {}
        ids = [
            -1 if None in each else index.setdefault(each, len(index))
            for each in runner._keys(table, key, backend)
        ]
        found = read.decoded[kept] = (ids, len(index))
    return found


def _entity_values(ids: Sequence[int], count: int, values: Sequence[Tuple[Any, ...]]) -> List[Any]:
    """The value each entity most often holds, for the entities that hold a known one."""
    tallies: List[Optional[Dict[Any, int]]] = [None] * count
    for entity, (value,) in zip(ids, values, strict=True):
        if entity < 0 or value is None:
            continue
        found = tallies[entity]
        if found is None:
            tallies[entity] = {value: 1}
        else:
            found[value] = found.get(value, 0) + 1
    return [_mode(each) for each in tallies if each is not None]


def _cells(
    statistic: DeclaredStatistic, modes: Sequence[Any], backend: StatisticsBackend, bits: int
) -> MeasuredStatistic:
    """The entities' counts in the statistic's levels or bins."""
    if statistic.kind == "levelHistogram":
        levels = list(statistic.shape["levels"])
        position = {level: index for index, level in enumerate(levels)}
        cells = [0] * len(levels)
        for value in modes:
            cells[position[value]] += 1
        return MeasuredStatistic(groups=(backend.count_levels(cells, levels),))
    edges = [Fraction(each) for each in statistic.shape["bins"]]
    cells = [0] * (len(edges) - 1)
    for index in backend.bin_cells(list(modes), edges, bits):
        cells[index] += 1
    return MeasuredStatistic(groups=(backend.count_cells(cells, statistic.edges, bits),))


def _row_histograms(declared: Optional[DeclaredBlock]) -> Dict[str, DeclaredStatistic]:
    """The ungrouped row-level histogram of each column the block gives one."""
    return {
        each.column: each
        for each in (declared.statistics if declared is not None else ())
        if each.kind in _HISTOGRAMS and each.group_by is None and each.grouping is None
    }


@dataclass(frozen=True)
class PerTable:
    """What forming one table's per-entity statistics reads."""

    expose_id: str
    exposes: Mapping[str, Mapping[str, Any]]
    table: Tuple["ExposePlan", sources.SourceColumns]
    declared: Optional[DeclaredBlock]
    doc: Dict[str, Any]
    backend: StatisticsBackend
    rule: disclosure.DisclosureRule
    tallies: Tuple["Counter[str]", "Counter[str]"]
    bits: int
    #: With a request, a column whose histogram is not listed is not named.
    quiet: bool
    timed: Tuple[Optional["StageTimings"], str]


def _row_level(
    doc: Mapping[str, Any], name: str, statistic: DeclaredStatistic
) -> Optional[Mapping[str, Any]]:
    """The document's own row-level histogram of a column on the cells of *statistic*, if written."""
    key = "levels" if statistic.kind == "levelHistogram" else "bins"
    written: Mapping[str, Any]
    for written in doc["columns"][name].get("statistics", ()):
        if (
            written.get("definition") == statistic.identifier
            and not any(each in written for each in _GROUPED)
            and written.get(key) == statistic.shape[key]
        ):
            return written
    return None


def _withheld_there(row: Optional[Mapping[str, Any]], cell: int) -> bool:
    """Whether a cell of a written row-level histogram is withheld (or nothing is written)."""
    if row is None:
        return True
    (group,) = row["groups"]
    if group["status"] != disclosure.FREE:
        return True
    statuses = group.get("cellStatus")
    return statuses is not None and statuses[cell] != disclosure.FREE


def _beside_published(
    row: Optional[Mapping[str, Any]], decision: EdgeDecision, measured: MeasuredStatistic
) -> bool:
    """Whether the per statistic withholds other cells than the row-level histogram does.

    With the rows per entity known (a balanced panel) a withheld per cell would be the row cell
    over that many, and a published per cell beside a withheld row cell would give that cell
    back; so a per statistic is published only when it withholds exactly the cells the
    row-level histogram does (the same levels or bins), and withheld whole when the two
    differ either way. With no row-level histogram written there is nothing to differ from.
    """
    (group,) = measured.groups
    if row is None or decision.group_status[group.key] != disclosure.FREE:
        return False
    statuses = decision.cell_status[group.key]
    return any(
        (status != disclosure.FREE) != _withheld_there(row, cell)
        for cell, status in enumerate(statuses)
    )


def _per_entry(
    inputs: PerTable,
    source: Sequence[str],
    name: str,
    statistic: DeclaredStatistic,
    modes: Sequence[Any],
) -> Dict[str, Any]:
    """The ``per`` statistic of a column over its entities' *modes*: checked in a call of its own.

    With no entity holding a known value it is written withheld whole, as one with few entities
    is. It is withheld whole too when it withholds other cells than the column's row-level
    histogram on the same cells does (:func:`_beside_published`).
    """
    from fluid_build.profiler import runner

    measured = _cells(statistic, modes, inputs.backend, inputs.bits)
    if not modes:
        decision = runner._withheld_whole(measured, "small")
    else:
        (decision,) = dates.checked(
            [(statistic, measured)],
            name=f"{inputs.expose_id}.{name} per {', '.join(source)}",
            total=len(modes),
            rule=inputs.rule,
            backend=inputs.backend,
            timed=inputs.timed,
            what="the counts per entity of",
        )
        row = _row_level(inputs.doc, name, statistic)
        if _beside_published(row, decision, measured):
            decision = runner._withheld_whole(measured, _ROW_LEVEL)
    entry: Dict[str, Any] = runner._declared_entry(
        statistic, measured, inputs.rule, inputs.tallies, inputs.bits, decision
    )
    entry["per"] = list(source)
    return entry


def table_per(inputs: PerTable) -> List[Tuple[str, str]]:
    """Add each ``per`` statistic of a table to its column's statistics; return what is left out."""
    from fluid_build.profiler import runner

    plan, _ = inputs.table
    keys, keyed = entity_keys(inputs.expose_id, inputs.exposes)
    histograms = _row_histograms(inputs.declared)
    readable = {each.name for each in plan.columns}
    notes: List[Tuple[str, str]] = []
    done: Set[str] = set()
    for source, name in dependencies(inputs.exposes[inputs.expose_id]):
        if frozenset(source) not in keys or name in keyed or name in source or name in done:
            continue
        statistic = histograms.get(name)
        if statistic is None or not set(source) <= readable:
            if not inputs.quiet and name in readable:
                what = f"per {', '.join(source)} statistic of {inputs.expose_id}.{name}"
                notes.append((what, _NO_HISTOGRAM if statistic is None else _UNREAD))
            continue
        done.add(name)
        ids, count = _entity_ids(inputs.table, source, inputs.backend)
        modes = _entity_values(ids, count, runner._keys(inputs.table, (name,), inputs.backend))
        entry = _per_entry(inputs, source, name, statistic, modes)
        inputs.doc["columns"][name].setdefault("statistics", []).append(entry)
    return notes
