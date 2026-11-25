# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The history of a table with a validity window (PROFILE-SPEC format 1-3-0, ``history``).

A FLUID contract declares a slowly changing dimension in ``exposes[].semantics``: one ``natural``
entity, two time dimensions that bound a version (``validityParams.isStart`` and ``isEnd``) and,
on the end dimension, ``openEnd`` and ``chain`` (RFC-versioned-relationships section 1). A default
profile (one without a request) writes the table's ``history`` when the profile reads the entity
column, the start and the end, and the start and the end are DATE columns, or timestamp columns, of
one type. ``fluxsim init`` reads it to write the bundle's history. A request has no ask for a
history, and a private release publishes nothing outside its noise, which PROFILE-SPEC refuses on a
history, so neither writes one.

**Versions.** The rows used are those whose entity column and start are known. The versions of an
entity are ordered by start, then end (an open end last), then row position. An end is open when
it is NULL, or when it equals the ``openEnd`` literal the contract declares, read as the column's
own values are read.

A table whose entity is the foreign key of a relationship the profile counts forms no history:
that relationship's children per parent are the entities by their versions with one more bin, the
parents with none, and its parents beside the history's entities (every member gives their number,
as a ``k`` or as the versions less the chain's pairs) would give that bin back, as a distinct count
of the key would (:func:`fluid_build.profiler.runner._key_columns`).

**Members.** Each is a statistic of the document, checked by the disclosure rule cell by cell:

* ``versionsPerEntity``: the entities by their number of versions, on the bins ``1, 2, 3, 5, 10,
  20`` and up. It is checked as children per parent are, and then audited the way they are
  (:func:`fluid_build.profiler.disclosure.pinned_children_cells`): a reader knows the entities and
  the versions exactly (the chain's size is their difference), and a first bin that starts at one
  because every entity has a version.
* ``firstStart``: the entities' first starts, written on the public year ladder and withheld
  whole. Where each end is the next version's start, the ends a column holds are the starts less
  the first starts (and the closed last ends), so the first starts beside the start and end
  columns' own histograms and calendars would give a withheld cell of any of them back, or the
  closed last ends, cell by cell.
* ``versionLength``: the whole days from a version's start to the next version's start, on the
  ladder of days trimmed as a date pair's are (:func:`fluid_build.profiler.dates.trimmed_span`).
  With no such span it is written on the ladder from day 0, withheld whole.
* ``chain``: each pair of consecutive versions as ``dayBefore``, ``gap``, ``nextStart`` or
  ``overlap``. An open end, or an end after the next start, is an overlap. The contract declares
  zeros: versions of one entity never overlap, and the declared chain says how an end meets the
  next start. A level the contract empties that the records leave empty is out of the check and is
  written 0, status ``F``, after it, as a date pair's order writes a level a rule empties
  (:mod:`fluid_build.profiler.dates`). A level the contract empties that holds records is checked
  like any cell.
* ``lastVersion``: the entities by how their last version ends, ``closed`` or ``open``.
* ``columns``: each categorical column the contract declares levels for (at most 32 columns of at
  most 64 levels each): ``initial``, the value on each entity's first version, and
  ``transitions``, grouped by the column itself. Group ``a`` counts, over the levels ``b``, each
  change from ``a`` to ``b`` between consecutive versions. A change from NULL is one more group,
  checked and never written. The diagonal (``a`` to ``a``) is zero by definition: it is written 0,
  status ``F``, and is not in the check. The check sees each group on the levels except its own
  (one cell fewer, in a placeholder order), which is exactly the cells a reader can form, and
  the group sizes and their total as it sees any grouped level histogram.
* ``reasons`` (with at least two tracked columns): the changes by the one tracked column that moves,
  or ``several``. Its cells and the transitions' group totals are linked: with two tracked columns
  ``several = M_a + M_b - k``, with ``M_a`` the changes of ``a`` (the sum of its transitions' group
  totals) and ``k`` the changes that move a column. A withheld group's total, or the moves from a
  NULL (a group never written), could be worked out from them. So ``reasons`` is published only
  when every one of its cells passes, every group of every tracked column's ``transitions`` is
  published, and no tracked column holds a NULL on a version used. Otherwise it is written
  withheld whole.

A member withheld whole by these rules is ``D`` with the open band other ``D`` statistics carry.

Nothing here names a value: a note names a table, columns and a reason.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from fluid_build.profiler import dates, defaults, disclosure, sources
from fluid_build.profiler._stats import (
    DeclaredBlock,
    DeclaredStatistic,
    EdgeDecision,
    MeasuredGroup,
    MeasuredStatistic,
    ReadRefused,
    StatisticsBackend,
)

if TYPE_CHECKING:  # the runner imports this module; its names are read inside functions
    from fluid_build.profiler.runner import ExposePlan, StageTimings

__all__ = [
    "HistoryStage",
    "MOST_COLUMNS",
    "MOST_LEVELS",
    "form_histories",
]

#: The most tracked columns a history holds, and the most levels one of them has (public caps).
MOST_COLUMNS = 32
MOST_LEVELS = 64

_MS_PER_DAY = 86_400_000
_FREE = disclosure.FREE
_CHAIN_LEVELS: Tuple[str, ...] = ("dayBefore", "gap", "nextStart", "overlap")
_LAST_LEVELS: Tuple[str, ...] = ("closed", "open")
_SEVERAL = "several"
_EMPTIED: Mapping[str, FrozenSet[str]] = {
    "nextStart": frozenset({"dayBefore", "gap", "overlap"}),
    "dayBefore": frozenset({"nextStart", "gap", "overlap"}),
    "none": frozenset({"nextStart", "overlap"}),
}
_PINNED = "the entities' and the versions' counts would give a withheld cell back"
_NO_WINDOW = "its semantics declare no validity window that is well formed"
_NOT_READ = "is not a column fluid profile reads"
_NOT_DATES = "its start and end are not DATE columns, or timestamp columns, of one type"
#: Why a table whose entity is a counted relationship's foreign key forms no history.
_COUNTED_KEY = (
    "its entity is the foreign key of a relationship this profile counts, and the history's "
    "entities beside that relationship's parents would give the parents with no version back"
)
_NO_ROWS = "no row holds both an entity and a start"
_NOT_DAYS = "a value of its start or end is not a whole day"
_NO_LEVELS = "the contract declares no levels for it"

# A row of a history: its start, whether its end is open, its end (0 when open) and its position.
_Key = Tuple[int, int, int, int]


@dataclass(frozen=True)
class HistoryStage:
    """What forming the histories of one profile reads."""

    exposes: Mapping[str, Mapping[str, Any]]
    plans: Sequence["ExposePlan"]
    reads: Sequence[sources.SourceColumns]
    declared: Sequence[Optional[DeclaredBlock]]
    #: The tables a bundle's fidelity block answers, which are left alone.
    blocks: Mapping[str, Any]
    tables_doc: Dict[str, Dict[str, Any]]
    backend: StatisticsBackend
    rule: disclosure.DisclosureRule
    tallies: Tuple["Counter[str]", "Counter[str]"]
    bits: int
    timings: Optional["StageTimings"]
    #: The ``(child table, child columns)`` of every relationship the profile counts.
    counted_keys: FrozenSet[Tuple[str, Tuple[str, ...]]] = frozenset()


@dataclass(frozen=True)
class _Version:
    start: int
    end: Optional[int]  # ``None``: open
    position: int


@dataclass(frozen=True)
class _Tracked:
    """A categorical column a history follows: its declared levels and each row's level."""

    name: str
    levels: Tuple[str, ...]
    values: Sequence[Optional[int]]  # the index of the row's level; ``None`` where null


class _Unformed(Exception):
    """A history that is not formed; the message is the reason, free of any count."""


@dataclass
class _Context:
    stage: HistoryStage
    plan: "ExposePlan"
    read: sources.SourceColumns
    expose_id: str

    @property
    def timed(self) -> Tuple[Optional["StageTimings"], str]:
        return self.stage.timings, self.expose_id

    def check(
        self,
        statistic: DeclaredStatistic,
        measured: MeasuredStatistic,
        *,
        name: str,
        total: int,
    ) -> EdgeDecision:
        (decision,) = dates.checked(
            [(statistic, measured)],
            name=name,
            total=total,
            rule=self.stage.rule,
            backend=self.stage.backend,
            timed=self.timed,
            what="the history of",
        )
        return decision

    def entry(
        self,
        statistic: DeclaredStatistic,
        measured: MeasuredStatistic,
        decision: EdgeDecision,
    ) -> Dict[str, Any]:
        from fluid_build.profiler import runner

        stage = self.stage
        entry: Dict[str, Any] = runner._declared_entry(
            statistic, measured, stage.rule, stage.tallies, stage.bits, decision
        )
        return entry

    def withheld(
        self,
        definition: str,
        shape: Mapping[str, Any],
        keys: Sequence[str],
        group_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """A statistic withheld whole by a rule of the history: ``D``, with the open band."""
        entry: Dict[str, Any] = {
            "definition": definition,
            "sampleSize": {"min": 0},
            "disclosure": self.stage.rule.name,
            **shape,
            "groups": [{"group": key, "status": disclosure.WITHHELD} for key in keys],
        }
        if group_by is not None:
            entry["groupBy"] = group_by
        self.stage.tallies[0][disclosure.SECONDARY] += len(keys)
        return entry


# ---------------------------------------------------------------------------
# What a decision over some of the cells says of all of them
# ---------------------------------------------------------------------------


def _expanded(decision: EdgeDecision, kept: Mapping[str, Sequence[int]], full: int) -> EdgeDecision:
    """*decision* of statistics over some of the cells, as one over all *full* cells.

    *kept* holds, by group key, the positions of the cells the check saw. Every other cell is one
    the contract or the definition makes zero: published, as ``F``. A group whose kept cells are
    all withheld says nothing but its size, and is withheld whole, as it is without the zeros.
    """
    group_status = dict(decision.group_status)
    group_reason = dict(decision.group_reason)
    cell_status: Dict[str, Tuple[str, ...]] = {}
    cell_reason: Dict[str, Tuple[str, ...]] = {}
    for key, statuses in decision.cell_status.items():
        reasons = decision.cell_reason[key]
        if all(status != _FREE for status in statuses):
            group_status[key] = disclosure.WITHHELD
            small = any(reason in ("small", "rounding") for reason in reasons)
            group_reason[key] = "small" if small else "differencing"
            continue
        shown = [_FREE] * full
        why = [""] * full
        for position, status, reason in zip(kept[key], statuses, reasons, strict=True):
            shown[position], why[position] = status, reason
        cell_status[key], cell_reason[key] = tuple(shown), tuple(why)
    return EdgeDecision(
        group_status=group_status,
        cell_status=cell_status,
        group_reason=group_reason,
        cell_reason=cell_reason,
        rounded={},
    )


def _level_statistic(
    name: str, levels: Sequence[str], group_by: Optional[str] = None
) -> DeclaredStatistic:
    return DeclaredStatistic(
        identifier="levelHistogram/1",
        kind="levelHistogram",
        column=name,
        group_by=group_by,
        shape={"levels": list(levels)},
        columns=(),
        read_as={},
    )


def _levels_entry(
    context: _Context,
    name: str,
    levels: Sequence[str],
    counts: Sequence[int],
    *,
    empty: FrozenSet[int] = frozenset(),
) -> Dict[str, Any]:
    """An ungrouped level histogram; the cells at *empty* are zero by the contract and unchecked."""
    backend = context.stage.backend
    statistic = _level_statistic(name, levels)
    measured = MeasuredStatistic(groups=(backend.count_levels(counts, levels),))
    kept = [position for position in range(len(levels)) if position not in empty]
    if not empty:
        return context.entry(
            statistic, measured, context.check(statistic, measured, name=name, total=sum(counts))
        )
    seen_levels = [levels[position] for position in kept]
    seen = MeasuredStatistic(
        groups=(backend.count_levels([counts[position] for position in kept], seen_levels),)
    )
    decision = context.check(
        _level_statistic(name, seen_levels), seen, name=name, total=sum(counts)
    )
    return context.entry(statistic, measured, _expanded(decision, {"": kept}, len(levels)))


def _histogram_entry(
    context: _Context,
    name: str,
    edges: Sequence[int],
    group: MeasuredGroup,
    *,
    total: int,
    audit: Optional[int] = None,
) -> Dict[str, Any]:
    """A histogram over whole-number *edges*; with *audit*, the versions behind its entities."""
    encode = context.stage.backend.encoder(context.stage.bits)
    statistic = dates.histogram(name, edges, encode)
    measured = MeasuredStatistic(groups=(group,))
    decision = context.check(statistic, measured, name=name, total=total)
    if audit is not None:
        decision = _audited(decision, group, audit, context.stage.rule)
    return context.entry(statistic, measured, decision)


def _audited(
    decision: EdgeDecision, group: MeasuredGroup, versions: int, rule: disclosure.DisclosureRule
) -> EdgeDecision:
    """*decision* of the versions per entity, with more cells withheld until none can be worked out.

    ``check_edge_counts`` sees the entities' cells and their total. A reader can know more: the
    versions held together, which each entity holds at least one of, bound the cells as the
    children of a parent bound a count of children per parent. So the audit reads the entities'
    number and the versions' number as known exactly, every entity as holding a version, and while
    a withheld cell can be pinned it withholds the published cell of the highest lower edge too
    (:func:`disclosure.pinned_children_cells`). It reads counts the profiler formed.
    """
    if decision.group_status[group.key] != _FREE:
        return decision
    lower = _version_edges()[:-1]
    statuses = list(decision.cell_status[group.key])
    reasons = list(decision.cell_reason[group.key])
    while any(status == _FREE for status in statuses):
        published = [status == _FREE for status in statuses]
        if not disclosure.pinned_children_cells(lower, group.cells, published, versions, rule, 0):
            break
        heaviest = max(
            (index for index, shown in enumerate(published) if shown),
            key=lambda index: lower[index],
        )
        statuses[heaviest] = disclosure.WITHHELD
        reasons[heaviest] = _PINNED
    return EdgeDecision(
        group_status=decision.group_status,
        cell_status={**decision.cell_status, group.key: tuple(statuses)},
        group_reason=decision.group_reason,
        cell_reason={**decision.cell_reason, group.key: tuple(reasons)},
        rounded=decision.rounded,
    )


def _version_edges() -> Tuple[int, ...]:
    """The bins of versions per entity: the children per parent's, from one."""
    from fluid_build.profiler import runner

    return runner.CHILDREN_EDGES[1:]


# ---------------------------------------------------------------------------
# Reading the table
# ---------------------------------------------------------------------------


def _instants(context: _Context, name: str, date_column: bool) -> List[Optional[int]]:
    """Each value of a start or end column in epoch milliseconds; ``None`` where null."""
    try:
        values = dates.read_milliseconds(context.read, name, context.stage.backend)
    except ReadRefused:
        raise _Unformed(
            f"a value of {context.expose_id}.{name} is not one the column reads"
        ) from None
    if date_column and any(value is not None and value % _MS_PER_DAY for value in values):
        raise _Unformed(_NOT_DAYS)
    return values


def _open_literal(
    context: _Context, name: str, date_column: bool, literal: Optional[str]
) -> Optional[int]:
    """The instant the ``openEnd`` literal is, read as the column's own values are."""
    if literal is None:
        return None
    from fluid_build.profiler import runner

    value: Any = runner._as_day(literal) if date_column else literal
    try:
        found = context.stage.backend.read_value("timestamp", value, column=name, row_index=0)
    except ReadRefused:
        raise _Unformed(f"its openEnd is not a value {context.expose_id}.{name} can hold") from None
    if date_column and found % _MS_PER_DAY:
        raise _Unformed(_NOT_DAYS)
    return int(found)


def _window(context: _Context, expose: Mapping[str, Any]) -> Any:
    """The expose's well formed window, and which columns it names: or why there is none."""
    from fluid_build.forge_datamodel.semantics_builder import version_window

    semantics = expose.get("semantics")
    window = version_window(semantics if isinstance(semantics, Mapping) else {})
    if window is None:
        raise _Unformed(_NO_WINDOW)
    columns = {column.name: column for column in context.plan.columns}
    for name in (window.key, window.start_column, window.end_column):
        if name not in columns:
            raise _Unformed(f"{context.expose_id}.{name} {_NOT_READ}")
    start, end = columns[window.start_column], columns[window.end_column]
    if start.domain != "timestamp" or end.domain != "timestamp" or start.date != end.date:
        raise _Unformed(_NOT_DATES)
    if len({window.key, window.start_column, window.end_column}) != 3:
        raise _Unformed("its entity, start and end are not three different columns")
    return window


def _ordered(
    context: _Context, window: Any
) -> Tuple[Dict[Tuple[Any, ...], List[_Version]], List[int]]:
    """Each entity's versions in order, and the position of each row used."""
    from fluid_build.profiler import runner

    date_column = next(
        column.date for column in context.plan.columns if column.name == window.start_column
    )
    keys = runner._keys((context.plan, context.read), (window.key,), context.stage.backend)
    starts = _instants(context, window.start_column, date_column)
    ends = _instants(context, window.end_column, date_column)
    literal = _open_literal(context, window.end_column, date_column, window.open_end)
    held: Dict[Tuple[Any, ...], List[Tuple[_Key, _Version]]] = {}
    used: List[int] = []
    for position, (key, start, end) in enumerate(zip(keys, starts, ends, strict=True)):
        if None in key or start is None:
            continue
        used.append(position)
        closed = end is not None and end != literal
        version = _Version(start, end if closed else None, position)
        order = (start, 0 if closed else 1, end if closed and end is not None else 0, position)
        held.setdefault(key, []).append((order, version))
    if not held:
        raise _Unformed(_NO_ROWS)
    return {
        key: [version for _, version in sorted(versions, key=lambda each: each[0])]
        for key, versions in held.items()
    }, used


# ---------------------------------------------------------------------------
# The members
# ---------------------------------------------------------------------------


def _versions_per_entity(
    context: _Context, stations: Mapping[Tuple[Any, ...], Sequence[_Version]], versions: int
) -> Dict[str, Any]:
    edges = _version_edges()
    counts = [len(held) for held in stations.values()]
    group = context.stage.backend.count_histogram(counts, edges, context.stage.bits)
    return _histogram_entry(
        context,
        f"{context.expose_id} versions per entity",
        edges,
        group,
        total=len(counts),
        audit=versions,
    )


def _first_start(context: _Context) -> Dict[str, Any]:
    """The first starts, on the public year ladder and withheld whole (see the module docstring)."""
    ladder = _year_ladder()
    return context.withheld("marginalHistogram/1", {"bins": [str(each) for each in ladder]}, [""])


def _year_ladder() -> List[int]:
    """The public ladder of the years ``defaults`` cuts a date on, in epoch milliseconds.

    From the first instant a timestamp holds, the start of each year from 1900 to 2100, to the
    last millisecond of year 9999.
    """
    return [
        defaults._FIRST_SECOND * 1000,
        *(defaults._year_start(year) * 1000 for year in defaults._LADDER_YEARS),
        defaults._LAST_SECOND * 1000 + 999,
    ]


def _version_length(context: _Context, lengths: "Counter[int]", pairs: int) -> Dict[str, Any]:
    backend, bits = context.stage.backend, context.stage.bits
    cells = dates.ladder_cells(lengths)
    span = dates.trimmed_span(cells, context.stage.rule.minimum_cell)
    ladder = dates.DAY_LADDER
    if span is None:
        edges = ladder[ladder.index(0) :]
        return context.withheld(
            "marginalHistogram/1", {"bins": [str(each) for each in edges]}, [""]
        )
    first, last = span
    edges = ladder[first : last + 2]
    group = backend.count_cells(cells[first : last + 1], edges, bits)
    return _histogram_entry(
        context, f"{context.expose_id} version length", edges, group, total=pairs
    )


def _chain(
    context: _Context, window: Any, stations: Mapping[Tuple[Any, ...], Sequence[_Version]]
) -> Dict[str, Any]:
    levels: Counter[str] = Counter()
    for held in stations.values():
        for current, following in zip(held, held[1:], strict=False):
            if current.end is None or current.end > following.start:
                levels["overlap"] += 1
            elif current.end == following.start:
                levels["nextStart"] += 1
            elif following.start - current.end == _MS_PER_DAY:
                levels["dayBefore"] += 1
            else:
                levels["gap"] += 1
    counts = [levels[each] for each in _CHAIN_LEVELS]
    emptied = _EMPTIED.get(window.chain, frozenset())
    empty = frozenset(
        position
        for position, level in enumerate(_CHAIN_LEVELS)
        if level in emptied and counts[position] == 0
    )
    return _levels_entry(context, f"{context.expose_id} chain", _CHAIN_LEVELS, counts, empty=empty)


def _last_version(
    context: _Context, stations: Mapping[Tuple[Any, ...], Sequence[_Version]]
) -> Dict[str, Any]:
    open_ = sum(1 for held in stations.values() if held[-1].end is None)
    return _levels_entry(
        context, f"{context.expose_id} last version", _LAST_LEVELS, [len(stations) - open_, open_]
    )


# ---------------------------------------------------------------------------
# Tracked columns
# ---------------------------------------------------------------------------


def _declared_levels(declared: Optional[DeclaredBlock], name: str) -> Optional[Tuple[str, ...]]:
    """The levels the contract declares for a column: those of its default level histogram."""
    for each in declared.statistics if declared is not None else ():
        if each.kind == "levelHistogram" and each.column == name and each.group_by is None:
            return tuple(each.shape["levels"])
    return None


def _tracked(
    context: _Context,
    window: Any,
    declared: Optional[DeclaredBlock],
) -> Tuple[List[_Tracked], List[Tuple[str, str]]]:
    """The categorical columns a history follows, and each one left out with why."""
    stage, read = context.stage, context.read
    taken = {window.key, window.start_column, window.end_column}
    found: List[_Tracked] = []
    notes: List[Tuple[str, str]] = []
    for column in context.plan.columns:
        if column.domain != "category" or column.name in taken:
            continue
        what = f"history of {context.expose_id}.{column.name}"
        levels = _declared_levels(declared, column.name)
        if levels is None:
            notes.append((what, _NO_LEVELS))
        elif len(levels) < 2:
            notes.append((what, "it declares fewer than two levels"))
        elif len(levels) > MOST_LEVELS:
            notes.append((what, f"it declares more than {MOST_LEVELS} levels"))
        elif len(found) >= MOST_COLUMNS:
            notes.append((what, f"a history follows at most {MOST_COLUMNS} columns"))
        else:
            index = {level: place for place, level in enumerate(levels)}
            try:
                values: List[Optional[int]] = []
                for position, raw in enumerate(read.values[column.name]):
                    value = stage.backend.read_value(
                        "category", raw, column=column.name, row_index=position
                    )
                    values.append(None if value is None else index[value])
            except (ReadRefused, KeyError):
                notes.append((what, "a value of it is outside its declared levels"))
                continue
            found.append(_Tracked(column.name, levels, values))
    return found, notes


def _initial(
    context: _Context, column: _Tracked, stations: Mapping[Tuple[Any, ...], Sequence[_Version]]
) -> Dict[str, Any]:
    counts = [0] * len(column.levels)
    for held in stations.values():
        value = column.values[held[0].position]
        if value is not None:
            counts[value] += 1
    return _levels_entry(
        context, f"{context.expose_id}.{column.name} initial", column.levels, counts
    )


def _moved(
    column: _Tracked, current: _Version, following: _Version
) -> Optional[Tuple[Optional[int], int]]:
    """``(the current level or None, the next level)`` when the column moves, else ``None``."""
    after = column.values[following.position]
    before = column.values[current.position]
    if after is None or after == before:
        return None
    return before, after


def _transitions(
    context: _Context, column: _Tracked, stations: Mapping[Tuple[Any, ...], Sequence[_Version]]
) -> Dict[str, Any]:
    backend = context.stage.backend
    levels, size = column.levels, len(column.levels)
    moves = [[0] * size for _ in range(size)]
    from_null = 0
    for held in stations.values():
        for current, following in zip(held, held[1:], strict=False):
            moved = _moved(column, current, following)
            if moved is None:
                continue
            if moved[0] is None:
                from_null += 1
            else:
                moves[moved[0]][moved[1]] += 1
    total = from_null + sum(sum(each) for each in moves)
    name = f"{context.expose_id}.{column.name} transitions"
    # The check sees each group on the levels but its own: one cell fewer, in a placeholder order.
    slots = [str(place) for place in range(size - 1)]
    kept = {
        level: [place for place in range(size) if place != at] for at, level in enumerate(levels)
    }
    seen = MeasuredStatistic(
        groups=tuple(
            backend.count_levels([moves[at][place] for place in kept[level]], slots, level)
            for at, level in enumerate(levels)
        ),
        left_out=(
            backend.count_levels([from_null, *([0] * (size - 2))], slots) if from_null else None
        ),
    )
    decision = context.check(
        _level_statistic(name, slots, column.name), seen, name=name, total=total
    )
    statistic = _level_statistic(name, levels, column.name)
    measured = MeasuredStatistic(
        groups=tuple(
            backend.count_levels(moves[at], levels, level) for at, level in enumerate(levels)
        )
    )
    return context.entry(statistic, measured, _expanded(decision, kept, size))


def _reasons(
    context: _Context,
    tracked: Sequence[_Tracked],
    stations: Mapping[Tuple[Any, ...], Sequence[_Version]],
    columns: Mapping[str, Mapping[str, Any]],
    used: Sequence[int],
) -> Dict[str, Any]:
    names = sorted([*(column.name for column in tracked), _SEVERAL])
    shape = {"levels": names}
    counts: Counter[str] = Counter()
    for held in stations.values():
        for current, following in zip(held, held[1:], strict=False):
            moving = [
                column.name for column in tracked if _moved(column, current, following) is not None
            ]
            if moving:
                counts[moving[0] if len(moving) == 1 else _SEVERAL] += 1
    holds_null = any(column.values[position] is None for column in tracked for position in used)
    unpublished = any(
        group["status"] != _FREE
        for entry in columns.values()
        for group in entry["transitions"]["groups"]
    )
    if not holds_null and not unpublished:
        values = [counts[each] for each in names]
        name = f"{context.expose_id} reasons"
        statistic = _level_statistic(name, names)
        measured = MeasuredStatistic(groups=(context.stage.backend.count_levels(values, names),))
        decision = context.check(statistic, measured, name=name, total=sum(values))
        group_status = decision.group_status[""]
        if group_status == _FREE and all(
            status == _FREE for status in decision.cell_status.get("", ("C",))
        ):
            return context.entry(statistic, measured, decision)
    return context.withheld("levelHistogram/1", shape, [""])


# ---------------------------------------------------------------------------
# A table, and every table
# ---------------------------------------------------------------------------


def _history(
    context: _Context,
    expose: Mapping[str, Any],
    declared: Optional[DeclaredBlock],
    notes: List[Tuple[str, str]],
) -> Dict[str, Any]:
    window = _window(context, expose)
    if (context.expose_id, (window.key,)) in context.stage.counted_keys:
        raise _Unformed(_COUNTED_KEY)
    stations, used = _ordered(context, window)
    versions = sum(len(held) for held in stations.values())
    lengths: Counter[int] = Counter(
        (following.start - current.start) // _MS_PER_DAY
        for held in stations.values()
        for current, following in zip(held, held[1:], strict=False)
    )
    found: Dict[str, Any] = {
        "entity": [window.key],
        "start": window.start_column,
        "end": window.end_column,
        "versionsPerEntity": _versions_per_entity(context, stations, versions),
        "firstStart": _first_start(context),
        "versionLength": _version_length(context, lengths, versions - len(stations)),
        "chain": _chain(context, window, stations),
        "lastVersion": _last_version(context, stations),
    }
    tracked, unfollowed = _tracked(context, window, declared)
    notes.extend(unfollowed)
    if tracked:
        columns = {
            column.name: {
                "initial": _initial(context, column, stations),
                "transitions": _transitions(context, column, stations),
            }
            for column in tracked
        }
        found["columns"] = columns
        if len(tracked) >= 2:
            found["reasons"] = _reasons(context, tracked, stations, columns, used)
    return found


def form_histories(inputs: HistoryStage) -> List[Tuple[str, str]]:
    """Write the ``history`` of each table that declares a validity window; the asks left out.

    A table a bundle's fidelity block answers is left alone, and so is a table whose contract
    declares no window. A table whose window the profile cannot form is named with why. The time
    is counted as the table's own measure and check time.
    """
    from fluid_build.data_rules._model import declares_validity_window
    from fluid_build.profiler import runner

    notes: List[Tuple[str, str]] = []
    for plan, read, found in zip(inputs.plans, inputs.reads, inputs.declared, strict=True):
        expose_id = plan.expose_id
        expose = inputs.exposes.get(expose_id)
        if expose_id in inputs.blocks or expose is None or not declares_validity_window(expose):
            continue
        context = _Context(inputs, plan, read, expose_id)
        with runner._timed(inputs.timings, "measure", expose_id):
            try:
                inputs.tables_doc[expose_id]["history"] = _history(context, expose, found, notes)
            except _Unformed as why:
                notes.append((f"history of {expose_id}", str(why)))
    return notes
