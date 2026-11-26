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

**Members.** Each is a statistic of the document, checked by the disclosure rule cell by cell, and
withheld a cell at a time. The three histograms are counted on public ladders trimmed to the bins
that hold records. An end bin of fewer than ``minimumCell`` records is joined to the bins beside it
until the run holds that many, and the run then reaches out to a public bound: the ladder's own end,
or for the first starts and the version lengths the start column's published extremes
(:func:`fluid_build.profiler.dates.coded_span`, top and bottom coding). So no outer edge is placed
by fewer than ``minimumCell`` records. Such a member is written on its whole ladder,
withheld whole, only when its records together are fewer than ``minimumCell``, or when the check
withholds its group: its bins then say nothing of where records lie.

* ``versionsPerEntity``: the entities by their number of versions, on the bins ``1, 2, 3, 5, 10,
  20`` and up, coded. It is checked as children per parent are, and then audited the way they are
  (:func:`fluid_build.profiler.disclosure.pinned_children_cells`): a reader knows the entities and
  the versions exactly (the chain's size is their difference), a first bin that starts at one
  because every entity has a version, and a trimmed top edge as a bound.
* ``firstStart``: the entities' first starts, on the public year ladder, coded. Each end that is
  not an entity's last is the next version's start, so the start column's records below an edge,
  less the first starts', are the later versions' starts (L), and the end column's less those are
  the closed last ends (C). A reader who knows the start and end columns' published counts at an
  edge (a histogram's published cells, a quantile's bin, the extremes) and the first starts there
  forms L and C: any two of the start column, the first starts and L give the third, and any two
  of the end column, L and C. So the first starts are also checked beside the two columns
  (:func:`_linked_cells`), every one of those cells as a cell of any statistic: a small cell they
  let a reader form withholds the two cells of the first starts beside the edge that sets it
  apart, until none is left; one that only an outer edge sets apart withholds the member whole.
  L is 0 at every edge, known without the first starts, where the published versions per entity
  show every entity with one version. A calendar's cells are not intervals of the year ladder,
  and are not held beside them.
* ``versionLength``: the whole days from a version's start to the next version's start, on the
  ladder of days from day 0, coded.
* ``chain``: each pair of consecutive versions as ``dayBefore``, ``gap``, ``nextStart`` or
  ``overlap``. An open end, or an end after the next start, is an overlap. The contract declares
  zeros: versions of one entity never overlap, and the declared chain says how an end meets the
  next start. A level the contract empties that the records leave empty is out of the check and is
  written 0, status ``F``, after it, as a date pair's order writes a level a rule empties
  (:mod:`fluid_build.profiler.dates`). A level the contract empties that holds records is checked
  like any cell.
* ``lastVersion``: the entities by how their last version ends, ``closed`` or ``open``. A declared
  ``endMarker`` ends an entity with a marked version, so ``closed`` is a level the contract empties,
  written 0 as the chain's are. Beside the end column's NULLs it gives the rows with no entity and
  a NULL end; when those are few, both levels are withheld.
* ``columns``: each categorical column the contract declares levels for (at most 32 columns of at
  most 64 levels each): ``initial``, the value on each entity's first version, and
  ``transitions``, grouped by the column itself. Group ``a`` counts, over the levels ``b``, each
  change from ``a`` to ``b`` between consecutive versions. A change from NULL is one more group,
  checked and never written. The diagonal (``a`` to ``a``) is zero by definition: it is written 0,
  status ``F``, and is not in the check. The check sees each group on the levels except its own
  (one cell fewer, in a placeholder order), which is exactly the cells a reader can form, and
  the group sizes and their total as it sees any grouped level histogram. The group of a value
  that a declared ``endMarker`` or ``currentFlag`` puts on the last version alone has no change by
  the contract: where the records leave it empty it is out of the check and written as zeros.
* ``reasons`` (with at least two tracked columns): the changes by the one tracked column that moves,
  or ``several``. Its cells and the transitions' group totals are linked: with two tracked columns
  ``several = M_a + M_b - k``, with ``M_a`` the changes of ``a`` (the sum of its transitions' group
  totals) and ``k`` the changes that move a column. A withheld group's total, or the moves from a
  NULL (a group never written), could be worked out from them. So ``reasons`` is published only
  when every one of its cells passes, every group of every tracked column's ``transitions`` is
  published, and no tracked column holds a NULL on a version used. Otherwise it is written
  withheld whole.

A member withheld whole by these rules is ``D`` with the open band other ``D`` statistics carry.

The closed last versions in total are the end column's count less the start column's plus the
entities, where every row holds an entity and the versions chain. Those are published rounded,
each, and no check here holds the three together.

Nothing here names a value: a note names a table, columns and a reason.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
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
        withheld: Optional[Mapping[str, Sequence[bool]]] = None,
    ) -> EdgeDecision:
        (decision,) = dates.checked(
            [(statistic, measured)],
            name=name,
            total=total,
            rule=self.stage.rule,
            backend=self.stage.backend,
            timed=self.timed,
            what="the history of",
            withheld=(withheld,) if withheld else (),
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
    for key, positions in kept.items():
        if not positions:  # every cell of the group is one the contract empties: all published
            group_status[key], group_reason[key] = _FREE, ""
            cell_status[key], cell_reason[key] = (_FREE,) * full, ("",) * full
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
    withheld: FrozenSet[int] = frozenset(),
) -> Dict[str, Any]:
    """An ungrouped level histogram; the cells at *empty* are zero by the contract and unchecked.

    The cells at *withheld* are withheld before the output check, which withholds what they would
    otherwise give back.
    """
    backend = context.stage.backend
    statistic = _level_statistic(name, levels)
    measured = MeasuredStatistic(groups=(backend.count_levels(counts, levels),))
    kept = [position for position in range(len(levels)) if position not in empty]
    marks = {"": [position in withheld for position in kept]}
    if not empty:
        decision = context.check(statistic, measured, name=name, total=sum(counts), withheld=marks)
        return context.entry(statistic, measured, decision)
    seen_levels = [levels[position] for position in kept]
    seen = MeasuredStatistic(
        groups=(backend.count_levels([counts[position] for position in kept], seen_levels),)
    )
    decision = context.check(
        _level_statistic(name, seen_levels), seen, name=name, total=sum(counts), withheld=marks
    )
    return context.entry(statistic, measured, _expanded(decision, {"": kept}, len(levels)))


@dataclass(frozen=True)
class _Coded:
    """A member counted on a coded ladder (:func:`dates.coded_span`), checked and not yet written."""

    edges: List[int]
    statistic: DeclaredStatistic
    measured: MeasuredStatistic
    decision: EdgeDecision


def _checked_histogram(
    context: _Context,
    name: str,
    edges: Sequence[int],
    group: MeasuredGroup,
    *,
    total: int,
    audit: Optional[int] = None,
    withheld: FrozenSet[int] = frozenset(),
) -> _Coded:
    """A histogram over whole-number *edges*, checked; nothing is written or counted yet.

    With *audit*, the versions behind its entities; the cells at *withheld* are withheld before
    the output check, which withholds what they would otherwise give back.
    """
    encode = context.stage.backend.encoder(context.stage.bits)
    statistic = dates.histogram(name, edges, encode)
    measured = MeasuredStatistic(groups=(group,))
    marks = {group.key: [cell in withheld for cell in range(len(edges) - 1)]}
    decision = context.check(statistic, measured, name=name, total=total, withheld=marks)
    if audit is not None:
        decision = _audited(decision, group, audit, context.stage.rule, edges)
    return _Coded(list(edges), statistic, measured, decision)


def _audited(
    decision: EdgeDecision,
    group: MeasuredGroup,
    versions: int,
    rule: disclosure.DisclosureRule,
    edges: Sequence[int],
) -> EdgeDecision:
    """*decision* of the versions per entity, with more cells withheld until none can be worked out.

    ``check_edge_counts`` sees the entities' cells and their total. A reader can know more: the
    versions held together, which each entity holds at least one of, bound the cells as the
    children of a parent bound a count of children per parent. So the audit reads the entities'
    number and the versions' number as known exactly, every entity as holding a version, and while
    a withheld cell can be pinned it withholds the published cell of the highest lower edge too
    (:func:`disclosure.pinned_children_cells`). A trimmed top edge bounds the last bin, which the
    ladder's own top does not. It reads counts the profiler formed.
    """
    if decision.group_status[group.key] != _FREE:
        return decision
    lower = list(edges[:-1])
    upper = None if edges[-1] == _version_edges()[-1] else edges[-1]
    statuses = list(decision.cell_status[group.key])
    reasons = list(decision.cell_reason[group.key])
    while any(status == _FREE for status in statuses):
        published = [status == _FREE for status in statuses]
        if not disclosure.pinned_children_cells(
            lower, group.cells, published, versions, rule, 0, upper
        ):
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


def _binned(values: Sequence[int], ladder: Sequence[int]) -> List[int]:
    """The records of *values* in each bin of *ladder*; a value outside it in the nearest end bin."""
    inner = ladder[1:-1]
    cells = [0] * (len(ladder) - 1)
    for value in values:
        cells[bisect_right(inner, value)] += 1
    return cells


def _coded(
    context: _Context,
    name: str,
    ladder: Sequence[int],
    values: Sequence[int],
    *,
    total: int,
    audit: Optional[int] = None,
    withheld: FrozenSet[int] = frozenset(),
    bounds: Tuple[Optional[int], Optional[int]] = (None, None),
) -> Optional[_Coded]:
    """A member counted on *ladder* trimmed and coded at its ends (:func:`dates.coded_span`).

    *bounds* are the public values a coded end reaches out to, on the ladder's scale (``None``:
    the ladder's own end). ``None`` when no bins can stand: the records are fewer than the least
    cell in all.
    """
    cells = _binned(values, ladder)
    inner = ladder[1:-1]
    low, high = (None if bound is None else bisect_right(inner, bound) for bound in bounds)
    runs = dates.coded_span(cells, context.stage.rule.minimum_cell, low, high)
    if runs is None:
        return None
    edges = dates.coded_edges(ladder, runs)
    group = context.stage.backend.count_cells(
        dates.coded_cells(cells, runs), edges, context.stage.bits
    )
    return _checked_histogram(
        context, name, edges, group, total=total, audit=audit, withheld=withheld
    )


def _written(context: _Context, coded: Optional[_Coded], ladder: Sequence[int]) -> Dict[str, Any]:
    """The entry of a coded member, or, with none, the member on *ladder* withheld whole."""
    statuses = () if coded is None else coded.decision.cell_status.get("", ())
    if coded is None or coded.decision.group_status.get("") != _FREE or _FREE not in statuses:
        # a coded ladder's outer edges are the records' own: with no group published, the
        # member is written on the whole ladder, so its bins say nothing of where records lie
        return context.withheld("marginalHistogram/1", {"bins": [str(e) for e in ladder]}, [""])
    return context.entry(coded.statistic, coded.measured, coded.decision)


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
    counts = [len(held) for held in stations.values()]
    ladder = _version_edges()
    name = f"{context.expose_id} versions per entity"
    return _written(
        context, _coded(context, name, ladder, counts, total=len(counts), audit=versions), ladder
    )


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


# ---------------------------------------------------------------------------
# First starts beside the start and end columns
# ---------------------------------------------------------------------------

#: The series a reader holds beside the first starts, S = F + L and N = L + C: any two of each three
#: give the third.
_TRIOS = (("S", "F", "L"), ("N", "L", "C"))


@dataclass(frozen=True)
class _Published:
    """Where a reader knows a date column's records below an edge, from what the column publishes.

    *edges* are the edges of every published count of the column (a histogram's published cells,
    a ``quantile/2`` group's two edges), read as known outright, which errs toward the reader;
    *below* and *above* the published minimum and maximum, below and beyond which every record
    is known to lie. The column's total is read as known.
    """

    edges: FrozenSet[int]
    below: Optional[int]
    above: Optional[int]

    def knows(self, edge: Optional[int]) -> bool:
        if edge is None:  # the ends of the line: no record below the first, all below the last
            return True
        return (
            edge in self.edges
            or (self.below is not None and edge <= self.below)
            or (self.above is not None and edge > self.above)
        )


def _number(text: Any) -> Optional[int]:
    try:
        value = Fraction(str(text))
    except (ValueError, ZeroDivisionError):
        return None
    return int(value) if value.denominator == 1 else None


def _published(context: _Context, name: str) -> _Published:
    """What the column *name* publishes of its records' places (:class:`_Published`)."""
    entry = context.stage.tables_doc.get(context.expose_id, {}).get("columns", {}).get(name, {})
    edges: Set[Optional[int]] = set()
    for statistic in entry.get("statistics") or ():
        bins = [_number(each) for each in statistic.get("bins") or ()]
        if statistic.get("groupBy") is not None or None in bins:
            continue
        for group in statistic.get("groups") or ():
            if group.get("status") != _FREE:
                continue
            if statistic.get("definition") == "quantile/2":
                place = group.get("quantileBin")
                if isinstance(place, int) and 0 <= place < len(bins) - 1:
                    edges.update((bins[place], bins[place + 1]))
                continue
            statuses = group.get("cellStatus") or [_FREE] * (len(bins) - 1)
            for cell, status in enumerate(statuses):
                if status == _FREE and cell + 1 < len(bins):
                    edges.update((bins[cell], bins[cell + 1]))
    metrics = entry.get("metrics") or {}

    def extreme(key: str) -> Optional[int]:
        metric = metrics.get(key)
        if isinstance(metric, Mapping) and metric.get("status") == _FREE:
            return _number(metric.get("value"))
        return None

    return _Published(frozenset(e for e in edges if e is not None), extreme("min"), extreme("max"))


def _below(ordered: Sequence[int], edge: Optional[int], last: bool = False) -> int:
    """The values of *ordered* (sorted) below *edge*; all of them at the line's last end."""
    if edge is None:
        return len(ordered) if last else 0
    return bisect_left(ordered, edge)


def _formed_cells(
    places: Sequence[Optional[int]],
    series: Mapping[str, Sequence[int]],
    knows: Mapping[str, Sequence[bool]],
) -> Dict[Tuple[str, int, int], int]:
    """Every cell a reader forms: for each series, between consecutive places it knows.

    *places* are the edges in order, the first and last being the ends of the line. A series'
    counts below each place are *series*, and *knows* says where the reader knows them. The series
    are the start column (S), the end column (N), the first starts (F), the later versions' starts
    L = S - F and the closed last ends C = N - L. At a place where two of S, F and L are known the
    third is too (S = F + L), and where two of N, L and C are (N = L + C); and where S is 0 or its
    total, F is 0 or its total, since a first start is a start.
    """
    known = {name: list(flags) for name, flags in knows.items()}
    totals = {name: values[-1] for name, values in series.items()}
    changed = True
    while changed:
        changed = False
        for at in range(len(places)):
            if known["S"][at] and not known["F"][at] and series["S"][at] in (0, totals["S"]):
                if series["F"][at] in (0, totals["F"]):
                    known["F"][at] = changed = True
            for trio in _TRIOS:
                if sum(known[name][at] for name in trio) == 2:
                    for name in trio:
                        known[name][at] = True
                    changed = True
    formed: Dict[Tuple[str, int, int], int] = {}
    for name, values in series.items():
        held = [at for at in range(len(places)) if known[name][at]]
        for low, high in zip(held, held[1:], strict=False):
            formed[(name, low, high)] = values[high] - values[low]
    return formed


def _linked_cells(
    context: _Context,
    window: Any,
    firsts: Sequence[int],
    edges: Sequence[int],
    decision: EdgeDecision,
    single: bool = False,
) -> Optional[FrozenSet[int]]:
    """The cells of the first starts to withhold so that no small cell is formed beside the columns.

    Each end a version holds that is not its entity's last is the next version's start, so the
    start column's records below an edge, less the first starts', are the later versions' starts,
    and the end column's less those are the closed last ends (and, where the chain leaves a gap, a
    difference of the two). A reader who knows two of the start column, the first starts and the
    later versions' starts at one edge knows the third, and two of the end column, the later
    versions' starts and the closed last ends likewise (:func:`_formed_cells`). Every cell they
    form with the first starts that they could not form without them, and that is small (1 to
    ``minimumCell - 1``, or 0 where zeros are disclosive), is protected by withholding the two
    cells of the first starts beside the edge that sets it apart. Empty when nothing more is
    withheld; ``None`` when an outer edge sets it apart, which no cell's withholding hides, and
    the member is withheld whole.

    With *single*, the versions per entity as published show every entity with one version: the
    later versions' starts are then 0 at every edge, known without the first starts.
    """
    stage = context.stage
    statuses = decision.cell_status.get("", ())
    if decision.group_status.get("") != _FREE or not statuses:
        return frozenset()
    starts = sorted(
        value
        for value in dates.read_milliseconds(context.read, window.start_column, stage.backend)
        if value is not None
    )
    ends = sorted(
        value
        for value in dates.read_milliseconds(context.read, window.end_column, stage.backend)
        if value is not None
    )
    ordered = sorted(firsts)
    start_known, end_known = (
        _published(context, window.start_column),
        _published(context, window.end_column),
    )
    # the edges a first start's count is known at: from below and from above through published cells
    outright = {0}
    while max(outright) < len(statuses) and statuses[max(outright)] == _FREE:
        outright.add(max(outright) + 1)
    top = {len(statuses)}
    while min(top) > 0 and statuses[min(top) - 1] == _FREE:
        top.add(min(top) - 1)
    first_known = {edges[at] for at in outright | top}
    inner = sorted({*start_known.edges, *end_known.edges, *edges})
    places: List[Optional[int]] = [None, *inner, None]
    last = len(places) - 1
    series = {
        "S": [_below(starts, edge, at == last) for at, edge in enumerate(places)],
        "N": [_below(ends, edge, at == last) for at, edge in enumerate(places)],
        "F": [_below(ordered, edge, at == last) for at, edge in enumerate(places)],
    }
    series["L"] = [s - f for s, f in zip(series["S"], series["F"], strict=True)]
    series["C"] = [n - later for n, later in zip(series["N"], series["L"], strict=True)]

    def f_known(at: int, edge: Optional[int]) -> bool:
        return edge is None or edge <= edges[0] or edge >= edges[-1] or edge in first_known

    ends_known = [at in (0, last) for at in range(len(places))]
    # Where no closed last version ends (C is 0 at every edge), a reader who takes the chain at its
    # word knows C everywhere, without the first starts.
    linked_zero = not any(series["C"])
    # Likewise where the published versions per entity show one version each, and no row with a
    # start lacks an entity: L is 0 at every edge.
    single_known = single and not any(series["L"])
    base = {
        "S": [start_known.knows(edge) for edge in places],
        "N": [end_known.knows(edge) for edge in places],
        "F": list(ends_known),
        "L": [single_known or flag for flag in ends_known],
        "C": [linked_zero or flag for flag in ends_known],
    }
    without = _formed_cells(places, series, base)
    with_first = _formed_cells(
        places, series, {**base, "F": [f_known(at, edge) for at, edge in enumerate(places)]}
    )
    rule = stage.rule
    for key, count in sorted(
        with_first.items(), key=lambda item: (item[0][1], item[0][2], item[0][0])
    ):
        if key in without or (key[0] == "F" and _own_cell(places, key, edges)):
            continue  # formed without the first starts, or their own cells, which the check saw
        if count >= 0 and disclosure.check_count(count, rule).published:
            continue
        apart = [places[at] for at in key[1:] if not base["F"][at] and f_known(at, places[at])]
        inner_edges = [edge for edge in apart if edge in edges[1:-1]]
        if not inner_edges:
            return None  # set apart by an outer edge, or by nothing a cell's withholding hides
        found = {
            each
            for edge in inner_edges
            for each in (edges.index(edge) - 1, edges.index(edge))
            if statuses[each] == _FREE
        }
        return frozenset(found) if found else None
    return frozenset()


def _own_cell(
    places: Sequence[Optional[int]], key: Tuple[str, int, int], edges: Sequence[int]
) -> bool:
    """Whether a formed cell of the first starts is one they publish, or lies outside their edges."""
    low, high = places[key[1]], places[key[2]]
    if low is not None and high is not None and low in edges and high in edges:
        return True
    below = high is not None and high <= edges[0]
    beyond = low is not None and low >= edges[-1]
    return below or beyond


def _shows_one_version_each(versions_per_entity: Mapping[str, Any]) -> bool:
    """Whether the published versions per entity show every entity with one version.

    A group published on bins that end at 2: no entity holds two versions, and each holds one.
    """
    groups = versions_per_entity.get("groups") or ()
    bins = [_number(each) for each in versions_per_entity.get("bins") or ()]
    top = bins[-1] if bins else None
    return (
        bool(groups)
        and all(group.get("status") == _FREE for group in groups)
        and None not in bins
        and len(bins) >= 2
        and bins[0] == 1
        and top is not None
        and top <= 2
    )


def _first_start(
    context: _Context,
    window: Any,
    stations: Mapping[Tuple[Any, ...], Sequence[_Version]],
    versions_per_entity: Mapping[str, Any],
) -> Dict[str, Any]:
    """The first starts, on the public year ladder trimmed and coded at its ends.

    Checked cell by cell, and beside the start and end columns (:func:`_linked_cells`) until they
    form no small cell with them. *versions_per_entity* is the entry already written.
    """
    single = _shows_one_version_each(versions_per_entity)
    firsts = [held[0].start for held in stations.values()]
    name = f"{context.expose_id} first start"
    ladder = _year_ladder()
    withheld: FrozenSet[int] = frozenset()
    while True:
        coded = _coded(
            context,
            name,
            ladder,
            firsts,
            total=len(firsts),
            withheld=withheld,
            bounds=_start_extremes(context, window),
        )
        if coded is None:
            return _written(context, None, ladder)
        more = _linked_cells(context, window, firsts, coded.edges, coded.decision, single)
        if more is None:
            return _written(context, None, ladder)
        if not more - withheld:
            return _written(context, coded, ladder)
        withheld |= more


def _start_extremes(context: _Context, window: Any) -> Tuple[Optional[int], Optional[int]]:
    """Public bounds of the start column, in epoch milliseconds: what it publishes, else ``None``.

    Its published minimum and maximum, else the outer edges of a histogram of it whose group is
    published (no record lies outside them, as a date span's edges say).
    """
    found = _published(context, window.start_column)
    low, high = found.below, found.above
    columns = context.stage.tables_doc.get(context.expose_id, {}).get("columns", {})
    for statistic in columns.get(window.start_column, {}).get("statistics") or ():
        groups = statistic.get("groups") or ()
        if (
            statistic.get("definition") != "marginalHistogram/1"
            or statistic.get("groupBy") is not None
            or not groups
            or groups[0].get("status") != _FREE
        ):
            continue
        bins = [_number(each) for each in statistic.get("bins") or ()]
        if len(bins) >= 2 and None not in bins:
            low = bins[0] if low is None else low
            high = bins[-1] if high is None else high
    return low, high


def _version_length(context: _Context, window: Any, lengths: "Counter[int]") -> Dict[str, Any]:
    ladder = dates.DAY_LADDER[dates.DAY_LADDER.index(0) :]
    name = f"{context.expose_id} version length"
    days = list(lengths.elements())
    low, high = _start_extremes(context, window)
    # no version lasts longer than the published starts lie apart: a public bound for the top
    longest = None if low is None or high is None else (high - low) // _MS_PER_DAY
    coded = _coded(context, name, ladder, days, total=len(days), bounds=(None, longest))
    return _written(context, coded, ladder)


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
    context: _Context, window: Any, stations: Mapping[Tuple[Any, ...], Sequence[_Version]]
) -> Dict[str, Any]:
    """The entities by how their last version ends.

    A declared ``endMarker`` ends an entity with a marked version, never by closing its last one,
    so ``closed`` is zero by the contract: published 0 when the records leave it empty, and
    checked like any cell when they do not. The end column's NULL ends less the open last
    versions that hold one is a count a reader forms beside the column's ``nullCount`` (or its
    ``count`` and the table's rows): the other rows with a NULL end (rows with no entity or no
    start, and any version before the last that has one). When that count
    is small, both levels are withheld, since the entities' number beside ``closed`` gives
    ``open``. A zero is the window's own rule (only a version's last end is open), as a declared
    zero is.
    """
    open_ = sum(1 for held in stations.values() if held[-1].end is None)
    counts = [len(stations) - open_, open_]
    empty = frozenset({0}) if window.end_marker is not None and counts[0] == 0 else frozenset()
    ends = dates.read_milliseconds(context.read, window.end_column, context.stage.backend)
    nulls = sum(1 for value in ends if value is None)
    open_null = sum(
        1 for held in stations.values() if held[-1].end is None and ends[held[-1].position] is None
    )
    apart = nulls - open_null
    small = apart != 0 and not disclosure.check_count(apart, context.stage.rule).published
    return _levels_entry(
        context,
        f"{context.expose_id} last version",
        _LAST_LEVELS,
        counts,
        empty=empty,
        withheld=frozenset(range(len(_LAST_LEVELS))) if small else frozenset(),
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


def _final_levels(window: Any, column: _Tracked) -> FrozenSet[int]:
    """The levels of *column* the contract puts on an entity's last version alone.

    A declared ``endMarker`` holds its value on the final version only, and a ``currentFlag`` on
    the last version only, so no change leaves that value: its group of ``transitions`` is zero
    by the contract.
    """
    found: Set[int] = set()
    for declared in (window.end_marker, window.current_flag):
        if declared is None or declared[0] != column.name:
            continue
        value = declared[1]
        spelled = {str(value)} | (
            {"true" if value else "false"} if isinstance(value, bool) else set()
        )
        found.update(at for at, level in enumerate(column.levels) if level in spelled)
    return frozenset(found)


def _transitions(
    context: _Context,
    window: Any,
    column: _Tracked,
    stations: Mapping[Tuple[Any, ...], Sequence[_Version]],
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
    final = _final_levels(window, column)
    kept = {
        level: (
            []
            if at in final and not any(moves[at])
            else [place for place in range(size) if place != at]
        )
        for at, level in enumerate(levels)
    }
    seen = MeasuredStatistic(
        groups=tuple(
            backend.count_levels([moves[at][place] for place in kept[level]], slots, level)
            for at, level in enumerate(levels)
            if kept[level]
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
    from fluid_build.profiler import runner

    if runner.counts_the_entity(context.stage.counted_keys, context.expose_id, (window.key,)):
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
    }
    found["firstStart"] = _first_start(context, window, stations, found["versionsPerEntity"])
    found["versionLength"] = _version_length(context, window, lengths)
    found["chain"] = _chain(context, window, stations)
    found["lastVersion"] = _last_version(context, window, stations)
    tracked, unfollowed = _tracked(context, window, declared)
    notes.extend(unfollowed)
    if tracked:
        columns = {
            column.name: {
                "initial": _initial(context, column, stations),
                "transitions": _transitions(context, window, column, stations),
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
