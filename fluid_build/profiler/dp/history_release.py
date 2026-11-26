# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The noisy history of a table a private release follows into (profile format 1-5-0).

A request of format 1-3-0 may ask a table's ``history``. When the release's unit follows into that
table, and the table is not the unit's own expose (one unit is one row there, and an entity's
versions are several rows), the release writes the history with every member noisy
(PROFILE-SPEC section 3a, "A noisy history"). Each member is a count table over the kept rows of
the table, with a share of the budget of its own and the noise of the stream of its own label:

* ``versionsPerEntity``: the entities by their number of versions, on the bins of the exact path;
* ``firstStart``: the entities' first starts, on the public year ladder;
* ``versionLength``: the whole days from a version's start to the next version's start, on the
  public ladder of days from day 0;
* ``chain``: each pair of consecutive versions as ``dayBefore``, ``gap``, ``nextStart`` or
  ``overlap``;
* ``lastVersion``: the entities by how their last version ends, ``closed`` or ``open``;
* for each tracked column ``initial``, its level on each entity's first version, and
  ``transitions``, grouped by its current level with one cell for each next level (every group is
  published, and the diagonal is a cell of its own, always 0 before noise);
* ``reasons`` (with two tracked columns or more): the changes by the one tracked column that moves,
  or ``several``.

The versions, the ladders and the levels are those of the exact history
(:mod:`fluid_build.profiler.history`): a version is ordered by start, then end (an open end last),
then row position, and an end is open when it is NULL or equals the contract's ``openEnd``. The
ladders are the whole public ones, never trimmed or coded to the data; a value beyond one is counted
in its end bin, so every record lands in one cell. Nothing is withheld, and nothing here reads a
cell to decide whether to publish another.

The charge is that of any count table of the table: ``m_T`` for one row, so a squared L2 of
``m_T**2``. One unit holds at most ``m_T`` rows, so at most ``m_T`` entities and ``m_T - 1`` pairs
of consecutive versions; the pair members are charged ``m_T`` as well, the slack of one. That holds
when one unit holds a whole entity: the history's entity is the child column of the followed
relationship into the table (true by construction), or the release refuses to be written
(:func:`~fluid_build.profiler.dp.follow_release.encode_follow_rows`, ``profile_release_entity_units``)
when an entity's versions follow two rows of the parent table.

The history is released whole or not at all: the five core members are one unit, each tracked column
(its ``initial`` and ``transitions``) is one unit, and ``reasons`` is the last. The units are fitted
under the cell cap in that order, after the null counts and before the statistics grouped through a
link (:func:`~fluid_build.profiler.dp.follow_release.admit`).

Truncation is per entity: a release that follows keeps an entity's versions under one parent row
whole or drops them whole (:func:`~fluid_build.profiler.dp.bounding.bound_entities`), by a keyed
priority that reads only that parent's rows, within the cap of the followed relationship. An entity
with more versions than the cap under one parent row is dropped whole, so it is not described.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import (
    AbstractSet,
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from fluid_build.profiler import history, runner
from fluid_build.profiler._stats import ReadRefused, RequestHistory, StatisticsBackend
from fluid_build.profiler.dp import bounding
from fluid_build.profiler.dp.release import (
    _OUTSIDE,
    HISTORY,
    NO_CELL,
    RULE_NAME,
    Part,
    ReleasedColumn,
    ReleasedTable,
    ReleaseRefused,
    _noise_block,
    place_known,
)

__all__ = [
    "CHAIN_LEVELS",
    "CORE_MEMBERS",
    "EXPOSE_REASON",
    "HistoryPlan",
    "HistoryRows",
    "LAST_LEVELS",
    "UNREACHED_REASON",
    "admitted",
    "encode_history",
    "history_block",
    "history_counts",
    "history_parts",
    "history_units",
    "ladder_of",
    "member_label",
    "plan_history",
]

VERSIONS, FIRST_START, VERSION_LENGTH, CHAIN, LAST_VERSION = (
    "versionsPerEntity",
    "firstStart",
    "versionLength",
    "chain",
    "lastVersion",
)
#: The five members every history holds, in the order of the document.
CORE_MEMBERS = (VERSIONS, FIRST_START, VERSION_LENGTH, CHAIN, LAST_VERSION)
CHAIN_LEVELS = history._CHAIN_LEVELS
LAST_LEVELS = history._LAST_LEVELS
_MS_PER_DAY = history._MS_PER_DAY
_PREFIX = "history"

_NOT_CATEGORICAL = "it is not a categorical column, and a history follows categorical columns"
_NO_PUBLIC_LEVELS = (
    "only the levels an enum(...) or a boolean column declares are public, and the contract "
    "declares none for it"
)
EXPOSE_REASON = (
    "the unit of privacy is one row of this table, and an entity's versions are several rows, so "
    "one unit does not hold a whole entity"
)
UNREACHED_REASON = "the unit does not reach this table, so its rows are fixed"


def member_label(member: str, column: Optional[str] = None) -> str:
    """The label of a member's noise stream: ``history/<member>`` or ``history/<member>/<column>``."""
    return f"{_PREFIX}/{member}" if column is None else f"{_PREFIX}/{member}/{column}"


def ladder_of(member: str) -> Tuple[int, ...]:
    """The public ladder of a core member that counts on bins; the exact history's own."""
    if member == VERSIONS:
        return tuple(history._version_edges())
    if member == FIRST_START:
        return tuple(history._year_ladder())
    if member == VERSION_LENGTH:
        return tuple(history.length_ladder())
    raise KeyError(member)


def _core_shape(member: str) -> Tuple[str, Tuple[Any, ...]]:
    """What a core member is cut on: ``("bins", edges)`` or ``("levels", levels)``."""
    if member == CHAIN:
        return "levels", tuple(CHAIN_LEVELS)
    if member == LAST_VERSION:
        return "levels", tuple(LAST_LEVELS)
    return "bins", ladder_of(member)


def _core_cells(member: str) -> int:
    kind, shape = _core_shape(member)
    return len(shape) - 1 if kind == "bins" else len(shape)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryPlan:
    """What the release holds of a table's history, decided from the contract and the request."""

    table: str
    #: The one column that names an entity (the contract's natural entity), the start and the end.
    entity: str
    start: str
    end: str
    #: The start and the end are DATE columns: a value is a whole day.
    date: bool
    #: The instant (milliseconds) of the ``openEnd`` literal the contract declares, else ``None``.
    open_end: Optional[int]
    #: The tracked columns, in the contract's order, with their public levels.
    columns: Tuple[ReleasedColumn, ...]
    #: ``reasons`` is released: at least two tracked columns.
    with_reasons: bool

    @property
    def read(self) -> Tuple[str, ...]:
        """The columns the history reads, each once: the entity, the start, the end, the tracked."""
        return tuple(
            dict.fromkeys(
                [self.entity, self.start, self.end, *(each.name for each in self.columns)]
            )
        )


def _note(table: str, why: str) -> Tuple[str, str]:
    return f"history of {table}", why


def plan_history(
    table: str,
    expose: Mapping[str, Any],
    expose_plan: runner.ExposePlan,
    asked: RequestHistory,
    structure: Callable[[str], Tuple[Optional[ReleasedColumn], str]],
    backend: StatisticsBackend,
) -> Tuple[Optional[HistoryPlan], List[Tuple[str, str]]]:
    """The history of *table* the release holds, and each ask it leaves out, with why.

    The plan is ``None``, and the first note says why, when the contract declares no well formed
    window, the window's columns are not all read, its start and end are not DATE columns or
    timestamp columns of one type, or its ``openEnd`` is no value of the end column. A candidate
    tracked column the release cannot follow is named, and the history is released without it.
    *structure* gives a column's public cells (or why it has none).
    """
    from fluid_build.forge_datamodel.semantics_builder import version_window

    semantics = expose.get("semantics")
    window = version_window(semantics if isinstance(semantics, Mapping) else {})
    if window is None:
        return None, [_note(table, history._NO_WINDOW)]
    by_name = {column.name: column for column in expose_plan.columns}
    for name in (window.key, window.start_column, window.end_column):
        if name not in by_name:
            return None, [_note(table, f"{table}.{name} {history._NOT_READ}")]
    start, end = by_name[window.start_column], by_name[window.end_column]
    if start.domain != "timestamp" or end.domain != "timestamp" or start.date != end.date:
        return None, [_note(table, history._NOT_DATES)]
    if len({window.key, window.start_column, window.end_column}) != 3:
        return None, [_note(table, "its entity, start and end are not three different columns")]
    open_end: Optional[int] = None
    if window.open_end is not None:
        value: Any = runner._as_day(window.open_end) if start.date else window.open_end
        try:
            open_end = int(
                backend.read_value("timestamp", value, column=window.end_column, row_index=0)
            )
        except ReadRefused:
            return None, [_note(table, f"its openEnd is not a value {table}.{end.name} can hold")]
        if start.date and open_end % _MS_PER_DAY:
            return None, [_note(table, history._NOT_DAYS)]
    taken = {window.key, window.start_column, window.end_column}
    named = None if asked.columns is None else tuple(asked.columns)
    notes: List[Tuple[str, str]] = []
    for name in named or ():
        what = f"history of {table}.{name}"
        if name in taken:
            notes.append((what, "it is the entity, the start or the end of the window"))
        elif name not in by_name:
            notes.append((what, f"it {history._NOT_READ}"))
        elif by_name[name].domain != "category":
            notes.append((what, _NOT_CATEGORICAL))
    tracked: List[ReleasedColumn] = []
    for column in expose_plan.columns:
        if column.name in taken or column.domain != "category":
            continue
        if named is not None and column.name not in named:
            continue
        what = f"history of {table}.{column.name}"
        made, why = structure(column.name)
        if made is None:
            notes.append((what, why))
        elif not made.categorical:
            notes.append((what, _NO_PUBLIC_LEVELS))
        elif len(made.levels) < 2:
            notes.append((what, "it declares fewer than two levels"))
        elif len(made.levels) > history.MOST_LEVELS:
            notes.append((what, f"it declares more than {history.MOST_LEVELS} levels"))
        elif len(tracked) >= history.MOST_COLUMNS:
            notes.append((what, f"a history follows at most {history.MOST_COLUMNS} columns"))
        else:
            tracked.append(replace(made, nullable=True))
    # ``reasons`` names a tracked column as a level beside ``several``; a column of that name would
    # make the two levels one, so ``reasons`` is left out.
    reasons = len(tracked) >= 2
    if reasons and history._SEVERAL in (column.name for column in tracked):
        reasons = False
        notes.append(
            (
                f"history of {table} reasons",
                f"a tracked column is named {history._SEVERAL}, which is also a level of reasons",
            )
        )
    plan = HistoryPlan(
        table=table,
        entity=window.key,
        start=window.start_column,
        end=window.end_column,
        date=bool(start.date),
        open_end=open_end,
        columns=tuple(tracked),
        with_reasons=reasons,
    )
    return plan, notes


# ---------------------------------------------------------------------------
# The parts
# ---------------------------------------------------------------------------


def history_parts(plan: HistoryPlan) -> Tuple[Part, ...]:
    """Every noisy member of the history, in document order: the one source of the parts.

    The five core members, then for each tracked column its ``initial`` and its ``transitions``,
    then ``reasons`` when it is released. Each has the label of its noise stream.
    """
    parts = [
        Part(HISTORY, member_label(member), (), (_core_cells(member),)) for member in CORE_MEMBERS
    ]
    for column in plan.columns:
        size = len(column.levels)
        parts.append(Part(HISTORY, member_label("initial", column.name), (column.name,), (size,)))
        parts.append(
            Part(HISTORY, member_label("transitions", column.name), (column.name,), (size, size))
        )
    if plan.with_reasons:
        parts.append(Part(HISTORY, member_label("reasons"), (), (len(plan.columns) + 1,)))
    return tuple(parts)


def history_units(plan: HistoryPlan) -> List[Tuple[str, ...]]:
    """The labels of the history's parts as units, each released whole or not at all.

    The core members; then each tracked column's ``initial`` and ``transitions``; then ``reasons``
    when it is released.
    """
    units: List[Tuple[str, ...]] = [tuple(member_label(member) for member in CORE_MEMBERS)]
    units.extend(
        (member_label("initial", column.name), member_label("transitions", column.name))
        for column in plan.columns
    )
    if plan.with_reasons:
        units.append((member_label("reasons"),))
    return units


def admitted(plan: HistoryPlan, gone: AbstractSet[str], prefix: str = "") -> Optional[HistoryPlan]:
    """The history that stays when the parts labelled in *gone* (each with *prefix*) are left out.

    ``None`` when the core members are. A tracked column stays with both its members, and
    ``reasons`` only when it was released and is not left out.
    """
    if f"{prefix}{member_label(VERSIONS)}" in gone:
        return None
    return replace(
        plan,
        columns=tuple(
            each
            for each in plan.columns
            if f"{prefix}{member_label('initial', each.name)}" not in gone
        ),
        with_reasons=plan.with_reasons and f"{prefix}{member_label('reasons')}" not in gone,
    )


# ---------------------------------------------------------------------------
# The records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryRows:
    """What a history reads of each row of its table. Nothing here leaves the process."""

    #: Each row's entity, encoded; ``None``: no entity.
    entities: Sequence[Optional[bytes]]
    #: Each row's start and end in epoch milliseconds; ``None``: no start, or no end (open).
    starts: Sequence[Optional[int]]
    ends: Sequence[Optional[int]]
    #: The instant of the contract's ``openEnd`` literal, else ``None``.
    open_end: Optional[int]
    #: For each tracked column, the level of each row; :data:`NO_CELL` where it holds none.
    tracked: Mapping[str, Sequence[int]]


def _instants(
    plan: HistoryPlan, name: str, values: Sequence[Any], table: Optional[str]
) -> List[Optional[int]]:
    found: List[Optional[int]] = []
    for value in values:
        if value is None:
            found.append(None)
        elif plan.date and value % _MS_PER_DAY:
            where = f"column {name!r}" if table is None else f"table {table!r} column {name!r}"
            raise ReleaseRefused(
                _OUTSIDE,
                f"{where}: a value that is not a whole day",
                {
                    "next": "fix the records, or the contract's column type, enum levels or "
                    "validationRules range, then release"
                },
            )
        else:
            found.append(int(value))
    return found


def encode_history(
    plan: HistoryPlan,
    exact: Mapping[str, Sequence[Any]],
    backend: StatisticsBackend,
    bits: int,
    *,
    table: Optional[str] = None,
) -> HistoryRows:
    """Read the columns of the history of every row of its table; refuse what it cannot hold.

    *exact* are the values read of each column. A value of a DATE column that is no whole day
    refuses, naming the table and the column; a tracked column's value outside its declared levels
    refuses, naming the column and the row. Neither names a value. A NULL is no refusal: a row with no entity or no start is no version, a NULL end
    is an open one, and a tracked column that is NULL holds no level.
    """
    return HistoryRows(
        entities=[
            None if value is None else bounding.encode_values([value])
            for value in exact[plan.entity]
        ],
        starts=_instants(plan, plan.start, exact[plan.start], table),
        ends=_instants(plan, plan.end, exact[plan.end], table),
        open_end=plan.open_end,
        tracked={
            column.name: place_known(
                column.name, column, exact[column.name], backend, bits, table=table
            )
            for column in plan.columns
        },
    )


# ---------------------------------------------------------------------------
# The counts
# ---------------------------------------------------------------------------


def _binned(values: Sequence[int], member: str) -> List[int]:
    return history._binned(values, ladder_of(member))


def history_counts(
    plan: HistoryPlan, rows: HistoryRows, kept: Sequence[int]
) -> Dict[str, List[int]]:
    """The exact cells of every member of the history over the *kept* rows, by label.

    A version is a kept row with an entity and a start. Each member counts as the exact history does
    (:mod:`fluid_build.profiler.history`) on a public ladder that is never trimmed. A pair member
    counts consecutive versions of one entity. A row that holds no level of a tracked column lands in
    no cell of that column's members, and a version of a column that moves from NULL is no cell of its
    ``transitions``.
    """
    stations, _ = history.order_versions(
        [rows.entities[at] for at in kept],
        [rows.starts[at] for at in kept],
        [rows.ends[at] for at in kept],
        rows.open_end,
    )
    tracked = [
        history._Tracked(
            column.name,
            column.levels,
            [
                None if rows.tracked[column.name][at] == NO_CELL else rows.tracked[column.name][at]
                for at in kept
            ],
        )
        for column in plan.columns
    ]
    pairs = [
        (current, following)
        for held in stations.values()
        for current, following in zip(held, held[1:], strict=False)
    ]
    found: Dict[str, List[int]] = {
        member_label(VERSIONS): _binned([len(held) for held in stations.values()], VERSIONS),
        member_label(FIRST_START): _binned(
            [held[0].start for held in stations.values()], FIRST_START
        ),
        member_label(VERSION_LENGTH): _binned(
            [(following.start - current.start) // _MS_PER_DAY for current, following in pairs],
            VERSION_LENGTH,
        ),
    }
    levels = [0] * len(CHAIN_LEVELS)
    for current, following in pairs:
        levels[CHAIN_LEVELS.index(history.chain_level(current, following))] += 1
    found[member_label(CHAIN)] = levels
    open_ = sum(1 for held in stations.values() if held[-1].end is None)
    found[member_label(LAST_VERSION)] = [len(stations) - open_, open_]
    for column in tracked:
        size = len(column.levels)
        initial = [0] * size
        for held in stations.values():
            level = column.values[held[0].position]
            if level is not None:
                initial[level] += 1
        moves = [0] * (size * size)
        for current, following in pairs:
            moved = history._moved(column, current, following)
            if moved is not None and moved[0] is not None:
                moves[moved[0] * size + moved[1]] += 1
        found[member_label("initial", column.name)] = initial
        found[member_label("transitions", column.name)] = moves
    if plan.with_reasons:
        names = sorted([*(column.name for column in tracked), history._SEVERAL])
        reasons = {name: 0 for name in names}
        for current, following in pairs:
            moving = [
                column.name
                for column in tracked
                if history._moved(column, current, following) is not None
            ]
            if moving:
                reasons[moving[0] if len(moving) == 1 else history._SEVERAL] += 1
        found[member_label("reasons")] = [reasons[name] for name in names]
    return found


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def _entry(
    item: ReleasedTable,
    cells: Sequence[str],
    shape: Mapping[str, Any],
    *,
    groups: Optional[Sequence[str]] = None,
    group_by: Optional[str] = None,
) -> Dict[str, Any]:
    """One noisy member: a histogram over *shape*, ungrouped or one group of cells for each key."""
    width = len(cells) if groups is None else len(cells) // len(groups)
    keys = [""] if groups is None else list(groups)
    entry: Dict[str, Any] = {
        "definition": "marginalHistogram/1" if "bins" in shape else "levelHistogram/1",
        "sampleSize": {"min": 0},
        "disclosure": RULE_NAME,
        "noise": _noise_block(item),
        "groups": [
            {
                "group": key,
                "status": "F",
                "noisyCounts": list(cells[position * width : (position + 1) * width]),
            }
            for position, key in enumerate(keys)
        ],
        **shape,
    }
    if group_by is not None:
        entry["groupBy"] = group_by
    return entry


def history_block(
    plan: HistoryPlan,
    items: Mapping[str, ReleasedTable],
    values_of: Mapping[str, Sequence[str]],
) -> Dict[str, Any]:
    """The table's ``history``: its columns, every noisy member and the tracked columns'.

    *items* are the history's parts by their own label (without a table prefix), *values_of* the
    noisy counts of each part by the part's label.
    """

    def cells(label: str) -> Tuple[ReleasedTable, Sequence[str]]:
        item = items[label]
        return item, values_of[item.label]

    found: Dict[str, Any] = {"entity": [plan.entity], "start": plan.start, "end": plan.end}
    for member in CORE_MEMBERS:
        kind, shape = _core_shape(member)
        item, values = cells(member_label(member))
        found[member] = _entry(
            item,
            values,
            {"bins": [str(edge) for edge in shape]} if kind == "bins" else {"levels": list(shape)},
        )
    if plan.columns:
        tracked: Dict[str, Any] = {}
        for column in plan.columns:
            levels = list(column.levels)
            initial, initial_values = cells(member_label("initial", column.name))
            moves, move_values = cells(member_label("transitions", column.name))
            tracked[column.name] = {
                "initial": _entry(initial, initial_values, {"levels": levels}),
                "transitions": _entry(
                    moves, move_values, {"levels": levels}, groups=levels, group_by=column.name
                ),
            }
        found["columns"] = tracked
    if plan.with_reasons:
        names = sorted([*(column.name for column in plan.columns), history._SEVERAL])
        item, values = cells(member_label("reasons"))
        found["reasons"] = _entry(item, values, {"levels": names})
    return found
