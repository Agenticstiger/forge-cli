# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Dates in a profile: a table's date pairs, a relationship's dates, its structural classes and exposure.

PROFILE-SPEC format 1-3-0 (section 5) lets a profile say how far apart two dates of one row, or
of a parent and its child, lie, and how many children a parent lifetime has per day it is active.
``fluxsim init`` reads those to keep generated dates inside their parents' lifetimes.

**Date pairs and relationship dates.** Each pair of DATE columns (a request lists the pairs it
asks) gets two statistics over the rows where both are known: a ``marginalHistogram/1`` of
``DAYS_BETWEEN(start, end)`` and ``order``, a ``levelHistogram/1`` over ``after``, ``before`` and
``same``. The days are counted on one fixed, public ladder of whole days (:data:`DAY_LADDER`),
**trimmed** to the bins from the first one holding a record to the last one holding one
(:func:`trimmed_span`), with the ladder's interior edges kept: the far tails and the days before
a start no record precedes are not bins of zeros the check has to protect. The two outer edges
are then the records' own, so the histogram is published only when each of the two end bins holds
at least ``minimumCell`` records: no single record places an edge, the rule a date span follows.
Otherwise the pair has no day histogram, and is named in the report.

The order is the trimmed ladder cut at day 0 and day 1, on the same outer edges, so the two
statistics count the same rows at edges they share, and **one** ``check_edge_counts`` call holds
both: the order goes in as the 3-cell (or fewer) ``marginalHistogram/1`` on those edges (the check
counts a column over its levels or over its bins, never both) and is written from that decision.
**An order level wholly outside the outer edges** (every level below day 0 when the first bin
starts at 0 or later, "same" when it starts at 1 or later, "after" when the last bin ends at 1 or
earlier) **is implied empty by them and is written as 0, status F, when the order group is
published.** Those edges stand on ``minimumCell`` records in each end bin, as a date span's do,
and that is the one reason a zero is published here. With no day histogram the order is checked
alone on :data:`ORDER_EDGES`.

**A cell a contract rule empties, with no day histogram.** ``end IS NULL OR end >= start`` says
no row is *before*. A zero cell is disclosive where the rule says so, and the check would
withhold that cell, and with it the cells it cannot tell from it. A cell a declared rule empties
tells a reader nothing the contract does not, so :func:`declared_empty_levels` names the levels a
rule empties **and the records leave empty**. The order is then checked on the ladder without
those cells (the edges inside the rule's range), and they are written afterwards as published
zeros. Never any other zero, and none inferred from the records: a rule the records break (any
level it empties holds a record) changes nothing, and neither does a rule that is not there.

**Structural classes.** ``structural: ["childless"]`` is written only where the contract declares
the parent's aggregate ``count`` of the child through the relationship and checks it ``>= 1``
(and no parent has no child, which a record counts), so the class of parents with no child is
empty by the contract and not by a count.

**Exposure.** ``children`` (the child rows whose parent's start is known) and ``activeDays``
(the whole days those parents are active inside a window) are ``count/1`` metrics under the
count and sum rules. The window comes from the parent's start column's published span histogram
(its outer edges) or from a range the contract declares for it, never from the records
otherwise; with neither the exposure is not formed. The days follow the engine's own reading
(``flux_engine/concordance/exposure_lines.py``): a DATE start is its midnight and a DATE end the
last millisecond of its day, both ends of the window count their day, and a parent whose end is
missing or before its start is active to the window's last day.

Nothing here names a value: a reason names a table, columns, a relationship and a rule.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import combinations
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from fluid_build import fluid_sql as fs
from fluid_build.fluid_sql import _nodes as nodes
from fluid_build.profiler import defaults, disclosure, sources
from fluid_build.profiler._stats import (
    DeclaredStatistic,
    EdgeDecision,
    MeasuredGroup,
    MeasuredStatistic,
    ReadRefused,
    RequestTable,
    StatisticsBackend,
)

if TYPE_CHECKING:  # the runner imports this module; its names are read inside functions
    from fluid_build.profiler.runner import ExposePlan, Relationship, StageTimings

__all__ = [
    "DAY_LADDER",
    "MOST_ENTRIES",
    "ORDER_EDGES",
    "ORDER_LEVELS",
    "DateStage",
    "PairContext",
    "RelationshipAsks",
    "declared_empty_levels",
    "read_milliseconds",
    "relationship_extras",
    "table_date_pairs",
]

#: The whole days a pair is counted in, from 10 000 years before to 10 000 years after: fixed and
#: public. Every date from year 1 to year 9999 lies inside it.
DAY_LADDER: Tuple[int, ...] = (
    -3652060,
    -36525,
    -3653,
    -366,
    -91,
    -31,
    -8,
    -1,
    0,
    1,
    2,
    8,
    31,
    91,
    181,
    366,
    731,
    1461,
    3653,
    36525,
    3652061,
)
#: The ladder cut at day 0 and day 1: before (days below 0), same (0) and after, with the
#: ladder's outer edges.
ORDER_EDGES: Tuple[int, ...] = (DAY_LADDER[0], 0, 1, DAY_LADDER[-1])
#: The levels of ``order``, in the code point order a level histogram counts in.
ORDER_LEVELS: Tuple[str, ...] = ("after", "before", "same")
#: The most entries (pairs, dates, exposures) a table or a relationship holds (PROFILE-SPEC section 5).
MOST_ENTRIES = 1024

_MS_PER_DAY = 86_400_000
_FREE = disclosure.FREE

_TIMESTAMP_PAIR = (
    "its order is on the millisecond and its days are whole, which one check cannot hold together"
)
_KEY_COLUMN = "a key of a counted relationship, which its children per parent describe"
_EXPOSURE_DATE = "an exposure is formed over DATE columns, whose days are whole"
_NO_DAY_HISTOGRAM = "its first or last day bin holds fewer than the least cell, so its days have no outer edge to publish"
_TOO_MANY = f"a profile holds at most {MOST_ENTRIES} of them"

# ---------------------------------------------------------------------------
# What a contract rule empties
# ---------------------------------------------------------------------------

#: A column as a rule names it: the relationship it is read through (``None``: the row's own) and
#: its name.
Ref = Tuple[Optional[str], str]

_EMPTIED = {
    ">=": frozenset({"before"}),
    ">": frozenset({"before", "same"}),
    "<=": frozenset({"after"}),
    "<": frozenset({"after", "same"}),
}
_FLIPPED = {">=": "<=", ">": "<", "<=": ">=", "<": ">"}
_EVERY_LEVEL = frozenset({"before", "same", "after"})


def _ref(node: nodes.Node) -> Optional[Ref]:
    return (node.relationship, node.name) if isinstance(node, nodes.Column) else None


def _guards(node: nodes.Node) -> Optional[Set[Ref]]:
    """The columns an ``IS NULL`` (or an ``OR`` of them) tests; ``None`` for any other text."""
    if isinstance(node, nodes.IsNull) and not node.negated:
        found = _ref(node.operand)
        return {found} if found is not None else None
    if isinstance(node, nodes.Or):
        left, right = _guards(node.left), _guards(node.right)
        return left | right if left is not None and right is not None else None
    return None


def _emptied(node: nodes.Node, start: Ref, end: Ref) -> FrozenSet[str]:
    """The levels of ``order`` that *node*, a rule's ``check``, says no row holds."""
    if isinstance(node, nodes.Or):
        # ``end IS NULL OR end >= start``: a null guard on either column of the pair.
        for guard, test in ((node.left, node.right), (node.right, node.left)):
            guarded = _guards(guard)
            if guarded is not None and guarded <= {start, end}:
                return _emptied(test, start, end)
        return frozenset()
    if isinstance(node, nodes.Compare):
        sides = (_ref(node.left), _ref(node.right))
        if sides == (end, start):
            operator = node.op
        elif sides == (start, end) and node.op in _FLIPPED:
            operator = _FLIPPED[node.op]
        else:
            return frozenset()
        return _EMPTIED.get(operator, frozenset())
    return frozenset()


def declared_empty_levels(
    checks: Sequence[str], start: Ref, end: Ref, counts: Mapping[str, int]
) -> FrozenSet[str]:
    """The levels of ``order`` a contract rule empties and the records leave empty.

    *checks* are the texts of the unconditional ``check`` rules of the table the pair is
    read in; *start* and *end* name its two columns, and *counts* are the measured rows
    ``{"before": ..., "same": ..., "after": ...}`` (days below 0, 0 and above 0). A rule empties a
    level when it is exactly ``end >= start`` (empties ``before``), ``end > start``
    (``before`` and ``same``), ``end <= start`` (``after``) or ``end < start`` (``after`` and
    ``same``), in either order of the two sides and under an ``IS NULL`` guard on either column.
    The levels a rule empties are returned only when **every** one of them holds no record: a rule
    the records break is not a declaration the profile may lean on, so nothing is returned.
    Nothing is returned either without a rule, or for rules that between them empty every level.
    """
    declared: Set[str] = set()
    for text in checks:
        try:
            declared |= _emptied(fs.parse(text), start, end)
        except fs.FluidSqlError:
            continue
    if not declared or declared >= _EVERY_LEVEL:
        return frozenset()
    if any(counts.get(level, 0) != 0 for level in declared):
        return frozenset()
    return frozenset(declared)


def _check_texts(expose: Mapping[str, Any]) -> List[str]:
    """The ``check`` of each unconditional rule of an expose: a rule with a ``when`` empties nothing."""
    contract = expose.get("contract")
    rules = contract.get("invariants") if isinstance(contract, Mapping) else None
    return [
        rule["check"]
        for rule in (rules if isinstance(rules, list) else ())
        if isinstance(rule, Mapping) and isinstance(rule.get("check"), str) and "when" not in rule
    ]


# ---------------------------------------------------------------------------
# One pair: days and order, checked together
# ---------------------------------------------------------------------------


def _kept_range(empty: FrozenSet[str]) -> Tuple[int, int]:
    """The ladder edges that stay in the check once the levels *empty* are taken out of it."""
    low, high = DAY_LADDER[0], DAY_LADDER[-1]
    if "before" in empty:
        low = 1 if "same" in empty else 0
    if "after" in empty:
        high = 0 if "same" in empty else 1
    return low, high


def _histogram(name: str, edges: Sequence[int], encode: Any) -> DeclaredStatistic:
    return DeclaredStatistic(
        identifier="marginalHistogram/1",
        kind="marginalHistogram",
        column=name,
        group_by=None,
        shape={"bins": [str(edge) for edge in edges]},
        columns=(),
        read_as={},
        edges=tuple(encode(Fraction(edge)) for edge in edges),
    )


def _widened(decision: EdgeDecision, offset: int, kept: int, full: int) -> EdgeDecision:
    """*decision* of a statistic with the declared-empty cells taken out, on all *full* cells.

    The *kept* cells starting at *offset* keep what the check decided; each cell outside them is
    one a rule empties and the records leave empty: published, as zero. A group whose kept cells
    are all withheld says nothing but its size, and is withheld whole, as it is without the rule.
    """
    reasons = decision.cell_reason[""]
    statuses = decision.cell_status[""]
    group_status = decision.group_status[""]
    group_reason = decision.group_reason.get("", "")
    if group_status == _FREE and all(status != _FREE for status in statuses):
        group_status = disclosure.WITHHELD
        group_reason = (
            "small"
            if any(reason in ("small", "rounding") for reason in reasons)
            else ("differencing")
        )
    before, after = offset, full - offset - kept
    return EdgeDecision(
        group_status={"": group_status},
        cell_status={"": (_FREE,) * before + tuple(statuses) + (_FREE,) * after},
        group_reason={"": group_reason},
        cell_reason={"": ("",) * before + tuple(reasons) + ("",) * after},
        rounded={},
    )


def _level(left: int) -> str:
    """The level of the order cell whose lower edge is *left*."""
    return "before" if left < 0 else ("same" if left == 0 else "after")


def ladder_cells(days: Counter[int]) -> List[int]:
    """The records of *days* (each day's number of records) in each bin of :data:`DAY_LADDER`.

    A day outside the ladder is placed in the nearest end bin, as ``marginalHistogram/1`` places it.
    """
    inner = DAY_LADDER[1:-1]
    cells = [0] * (len(DAY_LADDER) - 1)
    for day, count in days.items():
        cells[bisect_right(inner, day)] += count
    return cells


def trimmed_span(cells: Sequence[int], minimum_cell: int) -> Optional[Tuple[int, int]]:
    """The ladder bins a pair's histogram runs over, as ``(first, last)`` bin numbers, or ``None``.

    From the first bin of the ladder holding a record to the last one holding a record, so the
    ladder's always-empty bins (the far tails, the days before the start when none is) are not
    zeros the check must protect. The outer edges are the records' own, so they are published
    only when each of the two end bins holds at least *minimum_cell* records: no single record
    then places an edge, the rule a date span follows. With no such bins the pair has no day
    histogram.
    """
    held = [position for position, count in enumerate(cells) if count]
    if not held:
        return None
    first, last = held[0], held[-1]
    if cells[first] < minimum_cell or cells[last] < minimum_cell:
        return None
    return first, last


def _order_cells(cells: Sequence[int], edges: Sequence[int]) -> List[int]:
    """The records of each cell between *edges* (ladder edges), from the ladder's *cells*."""
    return [
        sum(
            count
            for count, left in zip(cells, DAY_LADDER, strict=False)
            if edges[position] <= left < edges[position + 1]
        )
        for position in range(len(edges) - 1)
    ]


def _levelled(decision: EdgeDecision, lefts: Sequence[int]) -> EdgeDecision:
    """The decision of an order's cells (lower edges *lefts*) as one of its three levels.

    A level whose cell the edges leave out lies wholly outside the published outer edges, which
    say no record is there, and is published as zero **when the group is**: those edges stand on
    at least ``minimumCell`` records in each end bin, the same rule as a date span.
    """
    if "" not in decision.cell_status:  # the group is withheld whole: it has no cells to order
        return decision
    group_status = decision.group_status[""]
    by_level = {_level(left): position for position, left in enumerate(lefts)}
    statuses, reasons = [], []
    for level in ORDER_LEVELS:
        position = by_level.get(level)
        if position is None:
            statuses.append(_FREE)
            reasons.append("")
        else:
            statuses.append(decision.cell_status[""][position])
            reasons.append(decision.cell_reason[""][position])
    return EdgeDecision(
        group_status={"": group_status},
        cell_status={"": tuple(statuses)},
        group_reason=decision.group_reason,
        cell_reason={"": tuple(reasons)},
        rounded={},
    )


def _checked(
    statistics: Sequence[Tuple[DeclaredStatistic, MeasuredStatistic]],
    *,
    name: str,
    total: int,
    rule: disclosure.DisclosureRule,
    backend: StatisticsBackend,
    timed: Tuple[Optional["StageTimings"], str],
) -> Tuple[EdgeDecision, ...]:
    from fluid_build.profiler import runner

    try:
        with runner._timed(timed[0], "check", timed[1]):
            return backend.check_edge_counts(
                [runner._edge_statistic(each, measured, rule) for each, measured in statistics],
                total=total,
                minimum_cell=rule.minimum_cell,
                zeros_disclosive=rule.zeros_disclosive,
                round_count=lambda count: disclosure.round_count(count, rule),
            )
    except ValueError as exc:
        raise runner.ProfileError(
            "profile_edge_counts_refused",
            f"the days of {name} cannot describe one set of records: {exc}",
            {"written": "nothing", "next": "report this: the output check refused its input"},
        ) from None


@dataclass(frozen=True)
class PairContext:
    """What every pair of one profile is counted, checked and written with."""

    rule: disclosure.DisclosureRule
    backend: StatisticsBackend
    fractional_bits: int
    tallies: Tuple["Counter[str]", "Counter[str]"]
    timed: Tuple[Optional["StageTimings"], str] = (None, "")


@dataclass(frozen=True)
class _Counted:
    """One pair's days counted on the ladder, with its ``order`` statistic and what it counts."""

    name: str
    total: int
    cells: Sequence[int]
    order: DeclaredStatistic
    order_measured: MeasuredStatistic


def _counted(days: "Counter[int]", name: str) -> _Counted:
    """The ladder cells of *days* and the ``order`` statistic they give (before, same, after)."""
    cells = ladder_cells(days)
    order = DeclaredStatistic(
        identifier="levelHistogram/1",
        kind="levelHistogram",
        column=name,
        group_by=None,
        shape={"levels": list(ORDER_LEVELS)},
        columns=(),
        read_as={},
    )
    counted = _order_cells(cells, ORDER_EDGES)
    by_level = {"before": counted[0], "same": counted[1], "after": counted[2]}
    total = sum(days.values())
    order_measured = MeasuredStatistic(
        groups=(
            MeasuredGroup(
                key="",
                document={"group": ""},
                k=total,
                cells=tuple(by_level[level] for level in ORDER_LEVELS),
            ),
        )
    )
    return _Counted(name, total, cells, order, order_measured)


def pair_entries(
    days: Counter[int],
    *,
    name: str,
    empty: FrozenSet[str],
    context: PairContext,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """The ``statistics`` entry of a pair's days (``None`` when it has none) and its ``order`` entry.

    *days* count the records of each whole-day offset from the pair's start to its end over the
    rows where both are known, and *empty* the levels a rule empties that the records leave empty
    (:func:`declared_empty_levels`; none unless the rule holds zeros disclosive).

    The days are on :data:`DAY_LADDER` trimmed to the bins from the first holding a record to the
    last holding one (:func:`trimmed_span`), published only when each of those two bins holds at
    least ``minimumCell`` records. The ladder's interior edges stay. The order is the same
    partition coarsened at day 0 and day 1, on the same outer edges, and both go through **one**
    ``check_edge_counts`` call. A level of the order wholly outside the outer edges is implied
    empty by them (those edges stand on ``minimumCell`` records each, as a date span's do) and is
    published as 0 when the order is. With no day histogram the order is checked alone on
    :data:`ORDER_EDGES`, and a level a rule empties and the records leave empty is published as 0.
    """
    counted = _counted(days, name)
    span = trimmed_span(counted.cells, context.rule.minimum_cell)
    if span is not None:
        return _trimmed_entries(counted, span, context)
    return None, _fallback_entry(counted, empty, context)


def _trimmed_entries(
    counted: _Counted, span: Tuple[int, int], context: PairContext
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """The days and the order on the trimmed outer edges, checked by one call."""
    from fluid_build.profiler import runner

    rule, backend, bits, tallies = (
        context.rule,
        context.backend,
        context.fractional_bits,
        context.tallies,
    )
    encode = backend.encoder(bits)
    first, last = span
    day_edges = DAY_LADDER[first : last + 2]
    lo, hi = day_edges[0], day_edges[-1]
    order_edges = (lo, *(edge for edge in (0, 1) if lo < edge < hi), hi)
    day_stat = _histogram(counted.name, day_edges, encode)
    day_measured = MeasuredStatistic(
        groups=(backend.count_cells(counted.cells[first : last + 1], day_edges, bits),)
    )
    coarse_stat = _histogram(counted.name, order_edges, encode)
    coarse_measured = MeasuredStatistic(
        groups=(backend.count_cells(_order_cells(counted.cells, order_edges), order_edges, bits),)
    )
    day_decision, coarse_decision = _checked(
        [(day_stat, day_measured), (coarse_stat, coarse_measured)],
        name=counted.name,
        total=counted.total,
        rule=rule,
        backend=backend,
        timed=context.timed,
    )
    return (
        runner._declared_entry(day_stat, day_measured, rule, tallies, bits, day_decision),
        runner._declared_entry(
            counted.order,
            counted.order_measured,
            rule,
            tallies,
            bits,
            _levelled(coarse_decision, order_edges[:-1]),
        ),
    )


def _fallback_entry(
    counted: _Counted, empty: FrozenSet[str], context: PairContext
) -> Dict[str, Any]:
    """The order alone, checked on :data:`ORDER_EDGES` without the levels a rule empties."""
    from fluid_build.profiler import runner

    rule, backend, bits = context.rule, context.backend, context.fractional_bits
    low, high = _kept_range(empty)
    kept_order = tuple(edge for edge in ORDER_EDGES if low <= edge <= high)
    kept_stat = _histogram(counted.name, kept_order, backend.encoder(bits))
    kept_measured = MeasuredStatistic(
        groups=(backend.count_cells(_order_cells(counted.cells, kept_order), kept_order, bits),)
    )
    (decision,) = _checked(
        [(kept_stat, kept_measured)],
        name=counted.name,
        total=counted.total,
        rule=rule,
        backend=backend,
        timed=context.timed,
    )
    levelled = decision  # a group withheld whole has no cells to widen or order
    if "" in decision.cell_status:
        widened = _widened(
            decision, ORDER_EDGES.index(kept_order[0]), len(kept_order) - 1, len(ORDER_EDGES) - 1
        )
        # The order is the 3-cell histogram's cells (before, same, after) in the order's levels.
        levelled = EdgeDecision(
            group_status=widened.group_status,
            cell_status={"": _permuted(widened.cell_status[""])},
            group_reason=widened.group_reason,
            cell_reason={"": _permuted(widened.cell_reason[""])},
            rounded={},
        )
    return runner._declared_entry(
        counted.order, counted.order_measured, rule, context.tallies, bits, levelled
    )


def _permuted(values: Sequence[Any]) -> Tuple[Any, ...]:
    """A 3-cell histogram's values (before, same, after) in the order's levels."""
    return (values[2], values[0], values[1])


def _levels(days: "Counter[int]") -> Dict[str, int]:
    return {
        "before": sum(count for day, count in days.items() if day < 0),
        "same": days.get(0, 0),
        "after": sum(count for day, count in days.items() if day > 0),
    }


def _declared_for(
    days: "Counter[int]",
    checks: Sequence[str],
    start: Ref,
    end: Ref,
    rule: disclosure.DisclosureRule,
) -> FrozenSet[str]:
    """The levels a rule of the contract empties, for a rule that holds zeros disclosive."""
    if not rule.zeros_disclosive:
        return frozenset()
    return declared_empty_levels(checks, start, end, _levels(days))


# ---------------------------------------------------------------------------
# Reading a table's dates
# ---------------------------------------------------------------------------


def read_milliseconds(
    read: sources.SourceColumns, name: str, backend: StatisticsBackend
) -> List[Optional[int]]:
    """Each value of a date or timestamp column as the registry reads it; ``None`` where null.

    Read once per table and kept (a date span, every pair and every relationship that holds the
    column share it). Raises :class:`ReadRefused` for a value the registry refuses, naming its row.
    """
    key = ("timestamp", name)
    found: Optional[List[Optional[int]]] = read.decoded.get(key)
    if found is None:
        found = [
            backend.read_value("timestamp", value, column=name, row_index=index)
            for index, value in enumerate(read.values[name])
        ]
        read.decoded[key] = found
    return found


def _milliseconds(
    read: sources.SourceColumns, name: str, backend: StatisticsBackend
) -> Optional[List[Optional[int]]]:
    """Each value of a date column as the registry reads it; ``None`` if one is not a whole day."""
    try:
        values = read_milliseconds(read, name, backend)
    except ReadRefused:
        return None
    if any(value is not None and value % _MS_PER_DAY for value in values):
        return None
    return values


@dataclass
class TableDates:
    """A table's date columns read once: ``None`` for a column with a value that is not a whole day."""

    plan: "ExposePlan"
    read: sources.SourceColumns
    backend: StatisticsBackend
    cache: Dict[str, Optional[List[Optional[int]]]] = field(default_factory=dict)
    whole_days: Dict[str, Optional[List[Optional[int]]]] = field(default_factory=dict)

    def of(self, name: str) -> Optional[List[Optional[int]]]:
        if name not in self.cache:
            self.cache[name] = _milliseconds(self.read, name, self.backend)
        return self.cache[name]

    def days_of(self, name: str) -> Optional[List[Optional[int]]]:
        """Each value of the column as the day number since the epoch (read once)."""
        if name not in self.whole_days:
            milliseconds = self.of(name)
            self.whole_days[name] = (
                None
                if milliseconds is None
                else [None if value is None else value // _MS_PER_DAY for value in milliseconds]
            )
        return self.whole_days[name]

    def why_not(
        self, name: str, held: FrozenSet[str], table: str, timestamp: str = _TIMESTAMP_PAIR
    ) -> Optional[str]:
        """Why the column takes no date statistic, or ``None`` when it may.

        *timestamp* is the reason a column that is not a DATE gives.
        """
        column = next((each for each in self.plan.columns if each.name == name), None)
        if column is None:
            return f"{table}.{name} is not a column fluid profile reads"
        if name in held:
            return _KEY_COLUMN
        if not (column.domain == "timestamp" and column.date):
            return timestamp
        if self.of(name) is None:
            return f"a value of {table}.{name} is not a whole day"
        return None


def table_dates(
    shared: MutableMapping[str, TableDates],
    plan: "ExposePlan",
    read: sources.SourceColumns,
    backend: StatisticsBackend,
) -> TableDates:
    """The one :class:`TableDates` of a table, which a pair, a relationship and an exposure share.

    Each date column is read through the registry once for the whole run, not once per pair.
    """
    if plan.expose_id not in shared:
        shared[plan.expose_id] = TableDates(plan, read, backend)
    return shared[plan.expose_id]


@dataclass(frozen=True)
class DateStage:
    """What the date pairs, relationship dates and exposures of one profile read and share."""

    raw_exposes: Mapping[str, Mapping[str, Any]]
    by_expose: Mapping[str, Tuple["ExposePlan", sources.SourceColumns]]
    tables_doc: Dict[str, Dict[str, Any]]
    backend: StatisticsBackend
    rule: disclosure.DisclosureRule
    tallies: Tuple["Counter[str]", "Counter[str]"]
    fractional_bits: int
    held: Mapping[str, FrozenSet[str]]
    timings: Optional["StageTimings"]
    #: Each table's date columns, read once for the pairs, relationships and exposures.
    shared: Dict[str, TableDates] = field(default_factory=dict)
    #: Each parent table's published ``activeDays`` by parent row (see :func:`_differences_pass`).
    exposed: Dict[str, List[Dict[int, int]]] = field(default_factory=dict)

    def pair_context(self, table: str) -> PairContext:
        """What a pair of *table* is counted and written with."""
        return PairContext(
            self.rule,
            self.backend,
            self.fractional_bits,
            self.tallies,
            (self.timings, table),
        )

    def held_of(self, table: str) -> FrozenSet[str]:
        """The columns of *table* a pair, date or exposure leaves to another statistic."""
        return self.held.get(table, frozenset())


def _candidate_pairs(
    plan: "ExposePlan", held: FrozenSet[str], asked: Optional[RequestTable]
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """The pairs a table is asked to form, and what is left out of them before any is read."""
    table = plan.expose_id
    notes: List[Tuple[str, str]] = []
    if asked is None:
        stamped = [
            each for each in plan.columns if each.domain == "timestamp" and each.name not in held
        ]
        names = [each.name for each in stamped if each.date]
        pairs = list(combinations(names, 2))
        notes.extend(
            (f"date pairs of {table}.{each.name}", _TIMESTAMP_PAIR)
            for each in stamped
            if not each.date and len(stamped) > 1
        )
    else:
        pairs = [(each.start, each.end) for each in asked.date_pairs]
    if len(pairs) > MOST_ENTRIES:
        notes.append((f"date pairs of {table} past the first {MOST_ENTRIES}", _TOO_MANY))
        pairs = pairs[:MOST_ENTRIES]
    return pairs, notes


def _same_row_days(table: TableDates, start: str, end: str) -> "Counter[int]":
    """The whole days from *start* to *end* of each row where both are known."""
    starts, ends = table.days_of(start), table.days_of(end)
    assert starts is not None and ends is not None
    return Counter(
        stop - first
        for first, stop in zip(starts, ends, strict=True)
        if first is not None and stop is not None
    )


def table_date_pairs(
    stage: DateStage,
    plan: "ExposePlan",
    read: sources.SourceColumns,
    asked: Optional[RequestTable],
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """A table's ``datePairs``, and each pair left out with why: ``(what, reason)``.

    Without a request, each pair of DATE columns in the contract's order, the earlier as the
    start; with one, the pairs it lists, in its order and its direction. A pair with a timestamp
    column is named and not formed. Never a value.
    """
    table = plan.expose_id
    held = stage.held_of(table)
    dates = table_dates(stage.shared, plan, read, stage.backend)
    pairs, notes = _candidate_pairs(plan, held, asked)
    checks = _check_texts(stage.raw_exposes[table])
    context = stage.pair_context(table)
    entries: List[Dict[str, Any]] = []
    for start, end in pairs:
        what = f"date pair {start} to {end} of {table}"
        reason = dates.why_not(start, held, table) or dates.why_not(end, held, table)
        if reason is not None:
            notes.append((what, reason))
            continue
        days = _same_row_days(dates, start, end)
        statistics, order = pair_entries(
            days,
            name=f"{start} to {end}",
            empty=_declared_for(days, checks, (None, start), (None, end), context.rule),
            context=context,
        )
        if statistics is None:
            notes.append((f"days of {what}", _NO_DAY_HISTOGRAM))
        entries.append(
            {
                "start": start,
                "end": end,
                "statistics": [] if statistics is None else [statistics],
                "order": order,
            }
        )
    return entries, notes


# ---------------------------------------------------------------------------
# A relationship: dates, structural classes, exposure
# ---------------------------------------------------------------------------


def _unique_parents(keys: Sequence[Tuple[Any, ...]]) -> Optional[Dict[Tuple[Any, ...], int]]:
    """The row of each parent key, or ``None`` when a key names two parents."""
    found: Dict[Tuple[Any, ...], int] = {}
    for position, key in enumerate(keys):
        if None in key:
            continue
        if key in found:
            return None
        found[key] = position
    return found


def _numbered(value: Any) -> Optional[Fraction]:
    try:
        return Fraction(value.text) if isinstance(value, nodes.Number) else None
    except ValueError:
        return None


def _is_at_least_one(node: nodes.Node, column: str) -> bool:
    """Whether *node* is ``column >= 1`` (or ``column > 0``, or either with the sides swapped)."""
    if not isinstance(node, nodes.Compare):
        return False
    operator, left, right = node.op, node.left, node.right
    if isinstance(right, nodes.Column) and not isinstance(left, nodes.Column):
        operator, left, right = _FLIPPED.get(operator, operator), right, left
    if _ref(left) != (None, column):
        return False
    bound = _numbered(right)
    return (operator == ">=" and bound == 1) or (operator == ">" and bound == 0)


def childless_declared(parent: Mapping[str, Any], child: str, relationship: str) -> bool:
    """Whether the contract declares every parent has a child through *relationship*.

    The parent expose holds an unconditional ``derive`` of an aggregate ``count`` of the *child*
    expose through *relationship* with no ``filter`` (every child row counts), and an
    unconditional ``check`` that the derived column is at least 1.
    """
    contract = parent.get("contract")
    rules = contract.get("invariants") if isinstance(contract, Mapping) else None
    counted: Set[str] = set()
    for rule in rules if isinstance(rules, list) else ():
        aggregate = rule.get("aggregate") if isinstance(rule, Mapping) else None
        if (
            isinstance(aggregate, Mapping)
            and isinstance(rule.get("derive"), str)
            and "when" not in rule
            and aggregate.get("function") == "count"
            and aggregate.get("from") == child
            and aggregate.get("via") == relationship
            and "filter" not in aggregate
        ):
            counted.add(rule["derive"])
    for text in _check_texts(parent):
        try:
            tree = fs.parse(text)
        except fs.FluidSqlError:
            continue
        if any(_is_at_least_one(tree, column) for column in counted):
            return True
    return False


def _window(
    expose: Mapping[str, Any], table_doc: Mapping[str, Any], column: str
) -> Optional[Tuple[int, int]]:
    """The window an exposure over *column* states: its published span's outer edges, else its range."""
    for statistic in table_doc.get("columns", {}).get(column, {}).get("statistics", ()):
        groups = statistic.get("groups", ())
        if (
            statistic.get("definition") == "marginalHistogram/1"
            and "groupBy" not in statistic
            and len(groups) == 1
            and groups[0].get("status") == _FREE
        ):
            low, high = int(statistic["bins"][0]), int(statistic["bins"][-1])
            return (low, high) if low <= high else None
    contract = expose.get("contract")
    schema = contract.get("schema") if isinstance(contract, Mapping) else None
    for entry in schema if isinstance(schema, list) else ():
        if isinstance(entry, Mapping) and entry.get("name") == column:
            declared = defaults.declared_instants(entry)
            return None if declared is None else (declared[0] * 1000, declared[1] * 1000)
    return None


def _active_days(start: int, end: Optional[int], window: Tuple[int, int]) -> int:
    """The whole days a lifetime is active in *window*, both ends' days counted (engine's reading)."""
    low, high = window[0] // _MS_PER_DAY, window[1] // _MS_PER_DAY
    first = max(start // _MS_PER_DAY, low)
    last = high if end is None or end < start else min(end // _MS_PER_DAY, high)
    return max(last - first + 1, 0)


def _count_metric(
    backend: StatisticsBackend,
    rule: disclosure.DisclosureRule,
    tally: "Counter[str]",
    value: int,
    verdict: disclosure.Verdict,
    shown: int,
    behind: int,
) -> Dict[str, Any]:
    """A ``count/1`` metric as the document spells it: withheld when *verdict* does not publish."""
    metric = next(each for each in backend.metrics() if each.name == "count")
    entry: Dict[str, Any] = {
        "definition": metric.identifier,
        "status": verdict.status,
        "sampleSize": disclosure.sample_size(behind, rule),
        "disclosure": rule.name,
    }
    if verdict.published:
        entry.update(metric.spell(shown))
    tally[verdict.status] += 1
    return entry


@dataclass(frozen=True)
class RelationshipAsks:
    """What one relationship is asked for: a request's lists (``None``: the defaults) and whether
    its children per parent are published, which ``structural`` describes."""

    dates: Optional[Sequence[Tuple[str, str]]]
    exposure: Optional[Sequence[Tuple[str, Optional[str]]]]
    counted: bool


@dataclass(frozen=True)
class _Sides:
    """A relationship's two tables, read once."""

    relationship: "Relationship"
    parent_dates: TableDates
    child_dates: TableDates
    parent_keys: Sequence[Tuple[Any, ...]]
    child_keys: Sequence[Tuple[Any, ...]]
    link: str

    @property
    def parent(self) -> str:
        return self.relationship.parent

    @property
    def child(self) -> str:
        return self.relationship.child


def _sides(stage: DateStage, relationship: "Relationship") -> _Sides:
    from fluid_build.profiler import runner

    parent_id, child_id = relationship.parent, relationship.child
    parent_plan, parent_read = stage.by_expose[parent_id]
    child_plan, child_read = stage.by_expose[child_id]
    return _Sides(
        relationship,
        table_dates(stage.shared, parent_plan, parent_read, stage.backend),
        table_dates(stage.shared, child_plan, child_read, stage.backend),
        runner._keys(stage.by_expose[parent_id], relationship.parent_columns, stage.backend),
        runner._keys(stage.by_expose[child_id], relationship.child_columns, stage.backend),
        f"{child_id}({', '.join(relationship.child_columns)}) to {parent_id}",
    )


def _date_columns(plan: "ExposePlan", held: FrozenSet[str]) -> List[str]:
    return [
        each.name
        for each in plan.columns
        if each.domain == "timestamp" and each.date and each.name not in held
    ]


def _relationship_pairs(
    stage: DateStage, sides: _Sides, asked: Optional[Sequence[Tuple[str, str]]]
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """The parent and child columns a relationship's dates are formed over, and what is left out."""
    if asked is None:
        parents = _date_columns(sides.parent_dates.plan, stage.held_of(sides.parent))
        children = _date_columns(sides.child_dates.plan, stage.held_of(sides.child))
        pairs = [(parent, child) for parent in parents for child in children]
    else:
        pairs = list(asked)
    notes: List[Tuple[str, str]] = []
    if len(pairs) > MOST_ENTRIES:
        notes.append((f"dates of {sides.link} past the first {MOST_ENTRIES}", _TOO_MANY))
        pairs = pairs[:MOST_ENTRIES]
    return pairs, notes


def _dates_reason(
    stage: DateStage,
    sides: _Sides,
    columns: Tuple[str, str],
    index: Optional[Mapping[Tuple[Any, ...], int]],
) -> Optional[str]:
    """Why a parent and child date take no pair, or ``None`` when they do."""
    parent_column, child_column = columns
    reason = sides.parent_dates.why_not(
        parent_column, stage.held_of(sides.parent), sides.parent
    ) or sides.child_dates.why_not(child_column, stage.held_of(sides.child), sides.child)
    if reason is None and index is None:
        reason = f"a key of {sides.parent} names more than one parent, so a child has no one parent"
    return reason


def _joined_days(
    sides: _Sides, columns: Tuple[str, str], joined: Sequence[Tuple[int, int]]
) -> "Counter[int]":
    """The whole days from each child's parent date to its own, over the rows where both are known."""
    parent_days = sides.parent_dates.days_of(columns[0])
    child_days = sides.child_dates.days_of(columns[1])
    assert parent_days is not None and child_days is not None
    return Counter(
        child_days[row] - parent_days[parent_row]  # type: ignore[operator]
        for row, parent_row in joined
        if child_days[row] is not None and parent_days[parent_row] is not None
    )


def _date_entries(
    stage: DateStage, sides: _Sides, asked: Optional[Sequence[Tuple[str, str]]]
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """A relationship's ``dates`` entries, and each pair left out with why."""
    pairs, notes = _relationship_pairs(stage, sides, asked)
    index = _unique_parents(sides.parent_keys) if pairs else {}
    checks = _check_texts(stage.raw_exposes[sides.child])
    context = stage.pair_context(sides.child)
    entries: List[Dict[str, Any]] = []
    joined: Optional[List[Tuple[int, int]]] = None  # each child row and its parent's, formed once
    for columns in pairs:
        parent_column, child_column = columns
        what = (
            f"dates {sides.parent}.{parent_column} to {sides.child}.{child_column} of {sides.link}"
        )
        reason = _dates_reason(stage, sides, columns, index)
        if reason is not None:
            notes.append((what, reason))
            continue
        assert index is not None
        if joined is None:
            joined = [
                (row, index[key])
                for row, key in enumerate(sides.child_keys)
                if None not in key and key in index
            ]
        days = _joined_days(sides, columns, joined)
        statistics, order = pair_entries(
            days,
            name=f"{sides.parent}.{parent_column} to {sides.child}.{child_column}",
            empty=_declared_for(
                days,
                checks,
                (sides.relationship.name, parent_column),
                (None, child_column),
                context.rule,
            ),
            context=context,
        )
        if statistics is None:
            notes.append((f"days of {what}", _NO_DAY_HISTOGRAM))
        entries.append(
            {
                "parent": parent_column,
                "child": child_column,
                "statistics": [] if statistics is None else [statistics],
                "order": order,
            }
        )
    return entries, notes


def _is_childless(stage: DateStage, sides: _Sides) -> bool:
    """Whether the class of parents with no child is empty by the contract."""
    if not (
        stage.rule.zeros_disclosive
        and childless_declared(
            stage.raw_exposes[sides.parent], sides.child, sides.relationship.name
        )
    ):
        return False
    children = Counter(key for key in sides.child_keys if None not in key)
    return all(None not in key and children.get(key, 0) > 0 for key in sides.parent_keys)


def relationship_extras(
    stage: DateStage, relationship: "Relationship", asks: RelationshipAsks
) -> Tuple[Dict[str, Any], List[Tuple[str, str]]]:
    """What a relationship adds to its ``relationships[]`` entry: ``dates``, ``structural``, ``exposure``.

    With *asks*' dates and exposure (a request's lists) only those are formed, else every
    parent and child DATE pair and each parent lifetime the contract orders. ``asks.counted`` says
    the relationship's children per parent are published, which ``structural`` describes. The
    second value is each ask left out with why: ``(what, reason)``. ``stage.exposed`` holds, for
    each parent table, the active days of each parent in every ``activeDays`` published so far
    (the exposures of every relationship share it: see :func:`_differences_pass`).
    """
    sides = _sides(stage, relationship)
    extras: Dict[str, Any] = {}
    entries, notes = _date_entries(stage, sides, asks.dates)
    if entries:
        extras["dates"] = entries
    if asks.counted and _is_childless(stage, sides):
        extras["structural"] = ["childless"]
    exposure, exposure_notes = _exposure(stage, sides, asks.exposure)
    notes.extend(exposure_notes)
    if exposure:
        extras["exposure"] = exposure
    return extras, notes


def _lifetimes(
    dates: TableDates, held: FrozenSet[str], checks: Sequence[str]
) -> List[Tuple[str, Optional[str]]]:
    """The parent lifetimes a profile states without a request: each DATE column alone, and each
    ordered pair of DATE columns a rule says does not end before it starts."""
    names = [
        each.name
        for each in dates.plan.columns
        if each.domain == "timestamp" and each.date and each.name not in held
    ]
    found: List[Tuple[str, Optional[str]]] = [(name, None) for name in names]
    for first, second in combinations(names, 2):
        for start, end in ((first, second), (second, first)):
            says = frozenset().union(
                *(_safe_emptied(text, (None, start), (None, end)) for text in checks)
            )
            if "before" in says and not says >= _EVERY_LEVEL:
                found.append((start, end))
    return found


def _safe_emptied(text: str, start: Ref, end: Ref) -> FrozenSet[str]:
    try:
        return _emptied(fs.parse(text), start, end)
    except fs.FluidSqlError:
        return frozenset()


def _exposure_reason(
    stage: DateStage, sides: _Sides, start: str, end: Optional[str]
) -> Tuple[Optional[str], Optional[Tuple[int, int]]]:
    """Why a lifetime forms no exposure (or its window when it does): ``(reason, window)``."""
    table, held, dates = sides.parent, stage.held_of(sides.parent), sides.parent_dates
    reason = dates.why_not(start, held, table, _EXPOSURE_DATE) or (
        dates.why_not(end, held, table, _EXPOSURE_DATE) if end is not None else None
    )
    if reason is not None:
        return reason, None
    window = _window(stage.raw_exposes[table], stage.tables_doc.get(table, {}), start)
    if window is None:
        return (
            "an exposure states its window, and the parent's start has no published span "
            "or declared range"
        ), None
    return None, window


def _active_by_parent(
    sides: _Sides, start: str, end: Optional[str], window: Tuple[int, int]
) -> Tuple[Dict[int, int], Set[Tuple[Any, ...]]]:
    """Each parent row's active days in *window*, and the keys of the parents with a start."""
    starts = sides.parent_dates.of(start)
    ends = sides.parent_dates.of(end) if end is not None else None
    assert starts is not None
    days: Dict[int, int] = {}
    known: Set[Tuple[Any, ...]] = set()
    for row, first in enumerate(starts):
        if first is None:
            continue
        # A DATE end is the last millisecond of its day (the engine's reading).
        last = None if ends is None or ends[row] is None else ends[row] + _MS_PER_DAY - 1  # type: ignore[operator]
        days[row] = _active_days(first, last, window)
        if None not in sides.parent_keys[row]:
            known.add(sides.parent_keys[row])
    return days, known


def _exposure_entry(
    stage: DateStage,
    sides: _Sides,
    lifetime: Tuple[str, Optional[str]],
    window: Tuple[int, int],
) -> Dict[str, Any]:
    """One lifetime's exposure: the children and active days inside *window*."""
    start, end = lifetime
    days, known = _active_by_parent(sides, start, end, window)
    children = sum(1 for key in sides.child_keys if None not in key and key in known)
    return {
        "start": start,
        **({"end": end} if end is not None else {}),
        "children": _children_metric(stage.backend, stage.rule, stage.tallies[0], children),
        "activeDays": _days_metric(
            stage.backend,
            stage.rule,
            stage.tallies[0],
            days,
            stage.exposed.setdefault(sides.parent, []),
        ),
        "window": {"start": str(window[0]), "end": str(window[1])},
    }


def _exposure(
    stage: DateStage, sides: _Sides, asked: Optional[Sequence[Tuple[str, Optional[str]]]]
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """The relationship's ``exposure`` entries and each lifetime left out, with why.

    ``stage.exposed`` holds the active days of each parent (by row) in every ``activeDays`` of
    this parent table published so far, and gains this relationship's.
    """
    lifetimes = (
        _lifetimes(
            sides.parent_dates,
            stage.held_of(sides.parent),
            _check_texts(stage.raw_exposes[sides.parent]),
        )
        if asked is None
        else list(asked)
    )
    notes: List[Tuple[str, str]] = []
    if len(lifetimes) > MOST_ENTRIES:
        notes.append((f"exposure of {sides.link} past the first {MOST_ENTRIES}", _TOO_MANY))
        lifetimes = lifetimes[:MOST_ENTRIES]
    entries: List[Dict[str, Any]] = []
    for start, end in lifetimes:
        span = start if end is None else f"{start} to {end}"
        reason, window = _exposure_reason(stage, sides, start, end)
        if reason is not None:
            notes.append((f"exposure {sides.parent}.{span} of {sides.link}", reason))
            continue
        assert window is not None
        entries.append(_exposure_entry(stage, sides, (start, end), window))
    return entries, notes


def _children_metric(
    backend: StatisticsBackend, rule: disclosure.DisclosureRule, tally: "Counter[str]", value: int
) -> Dict[str, Any]:
    """``children``: a count of records, so the count rules and the rounding."""
    verdict = disclosure.check_count(value, rule)
    if verdict.published:
        verdict = disclosure.check_rounding(value, rule)
    shown = disclosure.round_count(value, rule) if verdict.published else 0
    return _count_metric(backend, rule, tally, value, verdict, shown, value)


def _differences_pass(
    days: Mapping[int, int], published: Sequence[Mapping[int, int]], rule: disclosure.DisclosureRule
) -> disclosure.Verdict:
    """Whether *days* may be published beside the ``activeDays`` already published for a parent table.

    Two exposures of one parent table, on the same window, publish sums whose difference is the
    sum over the parents they treat differently of the days between them: with one such parent
    that is one record's date. So against each one already published, the parents whose active
    days differ must pass the count rules and their absolute differences the sum rule, the
    profile's own. An identical exposure (every difference 0) says nothing new and passes.
    Returns the verdict of the first check that fails.
    """
    for earlier in published:
        differences = [
            abs(days.get(row, 0) - earlier.get(row, 0)) for row in days.keys() | earlier.keys()
        ]
        differing = [each for each in differences if each]
        if not differing:
            continue
        verdict = disclosure.check_count(len(differing), rule)
        if verdict.published:
            verdict = disclosure.check_rounding(len(differing), rule)
        if verdict.published:
            verdict = disclosure.check_sum([Fraction(each) for each in differing], rule)
        if not verdict.published:
            return verdict
    return disclosure.PUBLISHED


def _days_metric(
    backend: StatisticsBackend,
    rule: disclosure.DisclosureRule,
    tally: "Counter[str]",
    days: Mapping[int, int],
    published: List[Dict[int, int]],
) -> Dict[str, Any]:
    """``activeDays``: a sum over the parents with a start, so the count of them and the sum rule.

    *days* is each such parent's (by row) days active. Published only when those parents pass
    the count rules, the days they are each active do not let one or two of them make up most
    of the sum, and it differs from each ``activeDays`` of the same parent table already
    published (*published*, which a published one joins) only as :func:`_differences_pass` allows.
    The sum is exact, as a ``sum`` metric is.
    """
    parents = len(days)
    verdict = disclosure.check_count(parents, rule)
    if verdict.published:
        verdict = disclosure.check_rounding(parents, rule)
    if verdict.published:
        verdict = disclosure.check_sum([Fraction(each) for each in days.values()], rule)
    if verdict.published:
        verdict = _differences_pass(days, published, rule)
    if verdict.published:
        published.append(dict(days))
    total = sum(days.values())
    return _count_metric(backend, rule, tally, total, verdict, total, parents)
