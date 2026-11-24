# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A table's calendar and periods, and the structure of each table (PROFILE-SPEC format 1-3-0).

**Calendar.** For a date or timestamp column, how its known values fall by month of the year, day
of the week (ISO 8601, Monday is ``1``) and, on a timestamp column only, hour of the week (from
Monday 00:00), all read in UTC. Each is an ungrouped ``levelHistogram/1`` over the levels the
format fixes. They are counted in one integer pass over the column's cached epoch milliseconds,
with no date object per row.

The hour of the week and the day of the week count the same records, the day being the sum of
24 hours, so they are checked in **one** ``check_edge_counts`` call, as two ``marginalHistogram/1``
on integer edges (the second a subset of the first), and each is written as a level histogram
from its decision. The month of the year sums a span's months over the years, so it would give a
withheld cell of the column's own histogram or quantiles back: it is withheld whole beside one.

**Periods.** For a table whose expose declares ``semantics.defaultAggTimeDimension``: one
``mean/1`` of each number column in each calendar month of the time column, from the first month
of its published span (or of its declared range) to the last, every month a group, an empty one
included, so the groups never depend on the records. The rows with no time are one more group,
checked and never written, as the rows a ``groupBy`` null leaves out are. The groups are withheld
whole unless every edge statistic of the time column cuts only at month starts and none withholds.

**Edges.** A calendar histogram counts over months, days or hours, so a statistic on the column's
edges (its own histogram and quantiles, any statistic grouped by its bins, and a relationship's
children per parent grouped by them) must cut its records only at a start of that unit. It cuts at
an instant where a published count of the records below lies strictly between 0 and its ``k``: a
ladder's outer edges cut nothing. The relationships are written after the calendar, so
:func:`withhold_beside_relationships` checks it again once they are.

Nothing here names a value: a note names a table, columns and a reason.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from fluid_build.profiler import dates, defaults, disclosure, per_entity, sources
from fluid_build.profiler._stats import (
    DeclaredBlock,
    DeclaredStatistic,
    Grouping,
    MeasuredGroup,
    MeasuredStatistic,
    ReadRefused,
    RequestTable,
    StatisticsBackend,
    calendar_levels,
    most_calendar_entries,
)

if TYPE_CHECKING:
    from fluid_build.profiler.runner import ExposePlan, StageTimings

__all__ = [
    "Structure",
    "calendar_counts",
    "civil",
    "edge_fit",
    "form_structure",
    "withhold_beside_relationships",
]

_MS_PER_DAY = 86_400_000
_MS_PER_HOUR = 3_600_000
#: 1970-01-01 is a Thursday: the hour of the week (from Monday 00:00) of epoch hour 0.
_EPOCH_HOUR_OF_WEEK = 72
_GROUPED_KEYS = ("groupBy", "groupByLink", "groupByBins", "per")
_NO_VALUE = "no non-null record to form it over"
_KEY_COLUMN = "a key of a counted relationship, which its children per parent describe"
_NOT_PUBLISHED = "its months are not published"
_RANGE_BOUND = "its months are past the most a period holds"
_BINNED = (
    "beside a histogram over its declared range its sum would show what the histogram withholds"
)


def civil(day: int) -> Tuple[int, int]:
    """The ``(year, month)`` of a day number since 1970-01-01 (Hinnant's civil_from_days)."""
    shifted = day + 719_468
    era = shifted // 146_097
    day_of_era = shifted - era * 146_097
    year_of_era = (
        day_of_era - day_of_era // 1_460 + day_of_era // 36_524 - day_of_era // 146_096
    ) // 365
    day_of_year = day_of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)
    shifted_month = (5 * day_of_year + 2) // 153
    month = shifted_month + 3 if shifted_month < 10 else shifted_month - 9
    return year_of_era + era * 400 + (month <= 2), month


def calendar_counts(
    values: Sequence[int], timestamp: bool
) -> Tuple[List[int], List[int], Optional[List[int]]]:
    """The records of each month, day of the week and (on a timestamp) hour of the week.

    *values* are epoch milliseconds, none null. One integer pass counts the hours (or the days of
    a date); the rest is folded from those, and the month is read once for each distinct day.
    """
    week: Optional[List[int]] = None
    if timestamp:
        hours = Counter(each // _MS_PER_HOUR for each in values)
        week = [0] * 168
        days: Counter[int] = Counter()
        for hour, count in hours.items():
            week[(hour + _EPOCH_HOUR_OF_WEEK) % 168] += count
            days[hour // 24] += count
        weekdays = [sum(week[start : start + 24]) for start in range(0, 168, 24)]
    else:
        days = Counter(each // _MS_PER_DAY for each in values)
        weekdays = [0] * 7
        for day, count in days.items():
            weekdays[(day + 3) % 7] += count
    months = [0] * 12
    for day, count in days.items():
        months[civil(day)[1] - 1] += count
    return months, weekdays, week


def _month_start(milliseconds: int) -> bool:
    """Whether an instant is UTC midnight on the first day of a month."""
    day, rest = divmod(milliseconds, _MS_PER_DAY)
    return rest == 0 and civil(day)[1] != civil(day - 1)[1]


def _day_start(milliseconds: int) -> bool:
    return milliseconds % _MS_PER_DAY == 0


def _edge_statistics(
    table_doc: Mapping[str, Any], column: str
) -> List[Tuple[Mapping[str, Any], Sequence[Any]]]:
    """Each statistic the table's document holds on *column*'s edges, with those edges.

    The column's own ungrouped ``marginalHistogram/1`` and ``quantile/2`` (their bins), and every
    statistic of the table grouped by bins of the column (its ``groupByBins``): the cells the
    column's other counts are cut in.
    """
    found: List[Tuple[Mapping[str, Any], Sequence[Any]]] = []
    for name, entry in table_doc.get("columns", {}).items():
        for statistic in entry.get("statistics", ()):
            binned = statistic.get("groupByBins")
            if isinstance(binned, Mapping) and binned.get("column") == column:
                found.append((statistic, binned.get("bins", ())))
            elif (
                name == column
                and statistic["definition"] in ("marginalHistogram/1", "quantile/2")
                and not any(key in statistic for key in _GROUPED_KEYS)
            ):
                found.append((statistic, statistic.get("bins", ())))
    return found


def _withholds(statistic: Mapping[str, Any]) -> bool:
    """Whether a written statistic withholds a group or a cell."""
    return any(
        group["status"] != disclosure.FREE
        or any(each != disclosure.FREE for each in group.get("cellStatus", ()))
        for group in statistic["groups"]
    )


def _starts(edges: Sequence[Any], unit: Callable[[int], bool]) -> bool:
    """Whether every edge is a whole number of milliseconds that *unit* accepts."""
    for edge in edges:
        try:
            exact = Fraction(str(edge))
        except ValueError:
            return False
        if exact.denominator != 1 or not unit(int(exact)):
            return False
    return True


def _count(value: Any) -> Optional[int]:
    """A published count as written, or ``None`` when it is not one."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _cuts(statistic: Mapping[str, Any], edges: Sequence[Any]) -> Sequence[Any]:
    """The instants among *edges* at which a published count of the records below cuts them.

    A count of the records below an instant that is ``0`` or all of them says nothing at that
    instant. Counts are read as written (rounded; a published nonzero cell is at least the
    minimum cell, so a zero stays zero). A statistic whose counts are not all published as
    expected cuts at every edge. Only called for a statistic that withholds nothing.
    """
    groups = statistic.get("groups", ())
    if isinstance(statistic.get("groupByBins"), Mapping):
        return _binned_cuts(groups, edges)
    if len(groups) != 1:
        return edges
    group = groups[0]
    k = _count(group.get("k"))
    if k is None:
        return edges
    if statistic.get("definition") == "quantile/2":
        return _quantile_cuts(group, edges, k)
    return _histogram_cuts(group, edges)


def _binned_cuts(groups: Sequence[Mapping[str, Any]], edges: Sequence[Any]) -> Sequence[Any]:
    """The cuts of groups that are the bins of a column: one group per bin, keyed by its lower edge."""
    # A group is keyed by the lower edge of its bin and the groups are not in bin order.
    lowers = {str(edge): at for at, edge in enumerate(edges[:-1])}
    held: Dict[int, int] = {}
    for group in groups:
        at, size = lowers.get(str(group.get("group"))), _count(group.get("k"))
        if at is None or size is None or at in held:
            return edges
        held[at] = size
    if len(held) != len(lowers):
        return edges
    total = sum(held.values())
    return [
        edge
        for at, edge in enumerate(edges)
        if 0 < sum(size for lower, size in held.items() if lower < at) < total
    ]


def _quantile_cuts(group: Mapping[str, Any], edges: Sequence[Any], k: int) -> Sequence[Any]:
    """The cuts of a quantile's bin: the edges below and through which it counts, when they cut."""
    low, high, at = (
        _count(group.get("binBelow")),
        _count(group.get("binThrough")),
        _count(group.get("quantileBin")),
    )
    if low is None or high is None or at is None or not 0 <= at < len(edges) - 1:
        return edges
    return [edge for edge, count in ((edges[at], low), (edges[at + 1], high)) if 0 < count < k]


def _histogram_cuts(group: Mapping[str, Any], edges: Sequence[Any]) -> Sequence[Any]:
    """The cuts of a histogram's cells: the edges with a published count below and one above.

    "All of them" is the sum of every published cell, not the group's ``k``: each is rounded
    on its own, so the two can differ, and a cut that rounding would hide must still count.
    """
    cells = group.get("binCounts")
    if (
        not isinstance(cells, list)
        or len(cells) != len(edges) - 1
        or any(_count(each) is None for each in cells)
    ):
        return edges
    total = sum(cells)
    return [edge for at, edge in enumerate(edges) if 0 < sum(cells[:at]) < total]


def _misfit(
    statistics: Sequence[Tuple[Mapping[str, Any], Sequence[Any]]],
    column: str,
    unit: Callable[[int], bool],
    name: str,
) -> Optional[str]:
    """Why edge *statistics* of *column* do not let a count over *unit*s be published, or ``None``.

    The status test comes first and is decided from statuses alone, never from a withheld count.
    """
    for statistic, _ in statistics:
        if _withholds(statistic):
            return f"a statistic on the edges of {column} withholds a group or a cell"
    for statistic, edges in statistics:
        if not _starts(_cuts(statistic, edges), unit):
            return f"a statistic on the edges of {column} cuts at an instant that is not the start of a {name}"
    return None


def edge_fit(
    table_doc: Mapping[str, Any], column: str, unit: Callable[[int], bool], name: str
) -> Optional[str]:
    """Why a histogram counted over *unit*s (months, days, hours) of *column* is not published.

    ``None`` when no statistic on the column (:func:`_edge_statistics`) withholds a group or a
    cell and each cuts its records only at the start of a *unit* (:func:`_cuts`). Else the
    reason, naming the column and never a value. A column with no such statistic fits.
    """
    return _misfit(_edge_statistics(table_doc, column), column, unit, name)


def _binned_by(
    relationships: Sequence[Mapping[str, Any]], table: str, column: str
) -> List[Tuple[Mapping[str, Any], Sequence[Any]]]:
    """The relationship statistics grouped by bins of *column* of *table*, with those bins."""
    found: List[Tuple[Mapping[str, Any], Sequence[Any]]] = []
    for relationship in relationships:
        if relationship.get("parent", {}).get("table") != table:
            continue
        for statistic in relationship.get("statistics", ()):
            binned = statistic.get("groupByBins")
            if isinstance(binned, Mapping) and binned.get("column") == column:
                found.append((statistic, binned.get("bins", ())))
    return found


def _withhold_group(group: Dict[str, Any], status: str, tallies: Tuple[Any, Any]) -> Dict[str, Any]:
    """A published group as withheld whole, its tally moved to *status*."""
    if group["status"] != disclosure.FREE:
        return group
    tallies[0][disclosure.FREE] -= 1
    tallies[0][disclosure.SECONDARY] += 1
    return {"group": group["group"], "status": status}


def withhold_beside_relationships(
    tables_doc: Mapping[str, Dict[str, Any]],
    relationships: Sequence[Mapping[str, Any]],
    tallies: Tuple["Counter[str]", "Counter[str]"],
) -> None:
    """Withhold more of a published calendar or periods entry, now the relationships are written.

    A relationship's children per parent grouped by bins of a parent column hold a partition of
    that column in their groups, so they are edge statistics of the column too. They are written
    after the calendar is formed: each published month, day and hour histogram and each periods
    entry is checked again beside them, and withheld whole (``C``, tallied ``D``; the periods
    ``D``) where it fails. Withholding more after the first decision is always safe.
    """
    for expose_id, doc in tables_doc.items():
        for entry in doc.get("calendar", ()):
            _withhold_calendar_entry(
                entry, _binned_by(relationships, expose_id, entry["column"]), tallies
            )
        for period in doc.get("periods", ()):
            _withhold_period_entry(
                period, _binned_by(relationships, expose_id, period["column"]), tallies
            )


def _withhold_calendar_entry(
    entry: Dict[str, Any],
    beside: Sequence[Tuple[Mapping[str, Any], Sequence[Any]]],
    tallies: Tuple[Any, Any],
) -> None:
    """Withhold the month, day or hour histograms of one calendar entry that fail beside *beside*."""
    if not beside:
        return
    column = entry["column"]
    for keys, unit, name in (
        (("month",), _month_start, "month"),
        (("dayOfWeek", "hourOfWeek"), _day_start, "day"),
    ):
        if _misfit(beside, column, unit, name) is None:
            continue
        for key in keys:
            if key in entry:
                entry[key]["groups"] = [
                    _withhold_group(group, disclosure.WITHHELD, tallies)
                    for group in entry[key]["groups"]
                ]


def _withhold_period_entry(
    period: Dict[str, Any],
    beside: Sequence[Tuple[Mapping[str, Any], Sequence[Any]]],
    tallies: Tuple[Any, Any],
) -> None:
    """Withhold the statistics of one periods entry whole when they fail beside *beside*."""
    if not beside or _misfit(beside, period["column"], _month_start, "month") is None:
        return
    for statistic in period["statistics"]:
        statistic["groups"] = [
            _withhold_group(group, disclosure.SECONDARY, tallies) for group in statistic["groups"]
        ]


@dataclass(frozen=True)
class _Table:
    """What forming one table's structure reads."""

    expose_id: str
    expose: Mapping[str, Any]
    exposes: Mapping[str, Mapping[str, Any]]
    plan: "ExposePlan"
    read: sources.SourceColumns
    declared: Optional[DeclaredBlock]
    doc: Dict[str, Any]
    backend: StatisticsBackend
    rule: disclosure.DisclosureRule
    tallies: Tuple["Counter[str]", "Counter[str]"]
    bits: int
    held: FrozenSet[str]
    asked: Optional[RequestTable]
    timings: Optional["StageTimings"]

    @property
    def timed(self) -> Tuple[Optional["StageTimings"], str]:
        return self.timings, self.expose_id

    @property
    def runner(self) -> Any:
        from fluid_build.profiler import runner

        return runner


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def _level_statistic(column: str, key: str) -> DeclaredStatistic:
    return DeclaredStatistic(
        identifier="levelHistogram/1",
        kind="levelHistogram",
        column=column,
        group_by=None,
        shape={"levels": list(calendar_levels()[key])},
        columns=(),
        read_as={},
    )


def _month_entry(
    table: _Table, column: str, counts: Sequence[int], total: int, withheld: Optional[str]
) -> Dict[str, Any]:
    levels = calendar_levels()["month"]
    statistic = _level_statistic(column, "month")
    measured = MeasuredStatistic(groups=(table.backend.count_levels(counts, levels),))
    if withheld is not None:
        decision = table.runner._withheld_whole(measured, withheld)
    else:
        (decision,) = dates.checked(
            [(statistic, measured)],
            name=f"{table.expose_id}.{column} by month",
            total=total,
            rule=table.rule,
            backend=table.backend,
            timed=table.timed,
            what="the calendar of",
        )
    entry: Dict[str, Any] = table.runner._declared_entry(
        statistic, measured, table.rule, table.tallies, table.bits, decision
    )
    return entry


def _week_statistics(
    table: _Table, column: str, weekdays: Sequence[int], week: Optional[List[int]]
) -> Tuple[Dict[str, List[int]], List[Tuple[DeclaredStatistic, MeasuredStatistic]]]:
    """The counts of the day (and hour) of the week, and each as a statistic one call can check."""
    backend, bits = table.backend, table.bits
    levels = calendar_levels()
    if week is None:
        counts = {"dayOfWeek": list(weekdays)}
        group = backend.count_levels(counts["dayOfWeek"], levels["dayOfWeek"])
        return counts, [(_level_statistic(column, "dayOfWeek"), MeasuredStatistic(groups=(group,)))]
    counts = {"hourOfWeek": week, "dayOfWeek": list(weekdays)}
    edges = {"hourOfWeek": tuple(range(169)), "dayOfWeek": tuple(range(0, 169, 24))}
    encode = backend.encoder(bits)
    return counts, [
        (
            dates.histogram(column, edges[key], encode),
            MeasuredStatistic(groups=(backend.count_cells(counts[key], edges[key], bits),)),
        )
        for key in counts
    ]


def _week_entries(
    table: _Table,
    column: str,
    weekdays: Sequence[int],
    week: Optional[List[int]],
    total: int,
    withheld: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    """The day-of-week (and hour-of-week) entries, decided by one call that holds both.

    With a *withheld* reason both are withheld whole: an hour of the week sums to its day, so
    one cannot be published beside the other withheld.
    """
    backend, bits = table.backend, table.bits
    levels = calendar_levels()
    counts, asked = _week_statistics(table, column, weekdays, week)
    if withheld is None:
        decisions = dates.checked(
            asked,
            name=f"{table.expose_id}.{column} by week",
            total=total,
            rule=table.rule,
            backend=backend,
            timed=table.timed,
            what="the calendar of",
        )
    else:
        decisions = tuple(table.runner._withheld_whole(measured, withheld) for _, measured in asked)
    found: Dict[str, Dict[str, Any]] = {}
    for key, decision in zip(counts, decisions, strict=True):
        measured = MeasuredStatistic(groups=(backend.count_levels(counts[key], levels[key]),))
        found[key] = table.runner._declared_entry(
            _level_statistic(column, key), measured, table.rule, table.tallies, bits, decision
        )
    return found


def _calendar_columns(table: _Table, notes: List[Tuple[str, str]]) -> List[str]:
    """The columns a table's calendar covers, in order, at most the format's bound."""
    names = {each.name for each in table.plan.columns if each.domain == "timestamp"}
    if table.asked is None:
        listed = [
            each.name
            for each in table.plan.columns
            if each.domain == "timestamp" and each.name not in table.held
        ]
    else:
        listed = [each for each in table.asked.calendar if each in names]
        notes.extend(
            (f"calendar of {table.expose_id}.{each}", _KEY_COLUMN)
            for each in listed
            if each in table.held
        )
        listed = [each for each in listed if each not in table.held]
    limit = most_calendar_entries()
    if len(listed) > limit:
        notes.append(
            (
                f"calendar of {table.expose_id} past the first {limit}",
                f"a profile holds at most {limit} of them",
            )
        )
    return listed[:limit]


def table_calendar(table: _Table) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """A table's ``calendar`` entries, and each column left out with why."""
    notes: List[Tuple[str, str]] = []
    entries: List[Dict[str, Any]] = []
    dated = {each.name: each.date for each in table.plan.columns}
    for column in _calendar_columns(table, notes):
        what = f"calendar of {table.expose_id}.{column}"
        try:
            values = [
                each
                for each in dates.read_milliseconds(table.read, column, table.backend)
                if each is not None
            ]
        except ReadRefused:
            notes.append((what, "a value of the column is refused by the reader"))
            continue
        if not values:
            notes.append((what, _NO_VALUE))
            continue
        months, weekdays, week = calendar_counts(values, not dated[column])
        # Day-start edges are hour starts too, and an hour sums to its day: the day decides both.
        month_edges = edge_fit(table.doc, column, _month_start, "month")
        week_edges = edge_fit(table.doc, column, _day_start, "day")
        entry = {
            "column": column,
            "month": _month_entry(table, column, months, len(values), month_edges),
        }
        entry.update(_week_entries(table, column, weekdays, week, len(values), week_edges))
        entries.append(entry)
    return entries, notes


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------


def _month_index(milliseconds: int, cache: Dict[int, int]) -> int:
    day = milliseconds // _MS_PER_DAY
    found = cache.get(day)
    if found is None:
        year, month = civil(day)
        found = cache[day] = year * 12 + month - 1
    return found


def _dimension_column(
    semantics: Mapping[str, Any], name: str, columns: Mapping[str, Any]
) -> Tuple[str, Optional[str]]:
    """The column a time dimension *name* reads, or why it is no time dimension."""
    for each in semantics.get("dimensions") or ():
        if isinstance(each, Mapping) and each.get("name") == name:
            if each.get("type") != "time":
                return name, "its default time dimension is not a time dimension"
            expr = each.get("expr")
            return (
                expr.strip() if isinstance(expr, str) and expr.strip() in columns else name
            ), None
    return name, None


def _time_column(table: _Table) -> Tuple[Optional[str], Optional[str]]:
    """The column a table's default time dimension names, or why it names none."""
    semantics = table.expose.get("semantics")
    name = semantics.get("defaultAggTimeDimension") if isinstance(semantics, Mapping) else None
    if not isinstance(semantics, Mapping) or not isinstance(name, str):
        return None, None
    columns = {each.name: each for each in table.plan.columns}
    target, reason = _dimension_column(semantics, name, columns)
    if reason is not None:
        return None, reason
    column = columns.get(target)
    if column is None or column.domain != "timestamp":
        return None, "its default time dimension is no date or timestamp column fluid profile reads"
    return target, None


def _declared_range(table: _Table, column: str) -> Optional[Tuple[int, int]]:
    contract = table.expose.get("contract")
    schema = contract.get("schema") if isinstance(contract, Mapping) else None
    for each in schema if isinstance(schema, list) else ():
        if isinstance(each, Mapping) and each.get("name") == column:
            instants = defaults.declared_instants(each)
            return None if instants is None else (instants[0] * 1000, instants[1] * 1000)
    return None


def _month_window(table: _Table, column: str) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
    """The first and last month (as ``year * 12 + month - 1``) a time column's periods run over."""
    cache: Dict[int, int] = {}
    span: Optional[List[int]] = None
    for statistic in table.doc["columns"][column].get("statistics", ()):
        groups = statistic["groups"]
        if (
            statistic["definition"] == "marginalHistogram/1"
            and not any(key in statistic for key in _GROUPED_KEYS)
            and groups[0]["status"] == disclosure.FREE
        ):
            span = [int(each) for each in statistic["bins"]]
            break
    if span is not None:
        if len(span) < 2 or span[1] - span[0] > 31 * _MS_PER_DAY:
            return None, _NOT_PUBLISHED  # a span in calendar years
        window = (_month_index(span[0], cache), _month_index(span[-1], cache) - 1)
    else:
        declared = _declared_range(table, column)
        if declared is None:
            return None, _NOT_PUBLISHED
        window = (_month_index(declared[0], cache), _month_index(declared[1], cache))
    if window[1] - window[0] + 1 > defaults.MOST_SPAN_BINS:
        return None, _RANGE_BOUND
    return window, None


def _key_name(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _known(table: _Table, column: str) -> List[Any]:
    """The known values of a column of the table, as the registry reads them."""
    values = table.runner._keys((table.plan, table.read), (column,), table.backend)
    return [each for (each,) in values if each is not None]


def _period_statistic(
    table: _Table, time: str, column: str, rows: Sequence[Optional[int]], window: Tuple[int, int]
) -> Dict[str, Any]:
    """One number column's ``mean/1`` per month, every month of the window a group."""
    backend, bits = table.backend, table.bits
    values = table.runner._keys((table.plan, table.read), (column,), backend)
    encode = backend.encoder(bits)
    mean = next(each for each in backend.statistics() if each.identifier == "mean/1")
    members: Dict[int, List[Any]] = {index: [] for index in range(window[0], window[1] + 1)}
    outside: List[Any] = []  # no time, or a time outside the window: the group never written
    for month, (value,) in zip(rows, values, strict=True):
        if value is not None:
            members.get(month, outside).append(value)  # type: ignore[arg-type]

    def group(key: str, held: List[Any]) -> MeasuredGroup:
        encoded = [encode(each) for each in held]
        return MeasuredGroup(
            key=key,
            document=mean.measure(key, encoded),
            k=len(held),
            values=tuple(held),
            encoded=tuple(encoded),
        )

    statistic = DeclaredStatistic(
        identifier="mean/1",
        kind="mean",
        column=column,
        group_by=time,
        shape={"of": column},
        columns=(column,),
        read_as={column: "number"},
        grouping=Grouping(form="period", document={}, keys=tuple(_key_name(i) for i in members)),
    )
    measured = MeasuredStatistic(
        groups=tuple(group(_key_name(index), held) for index, held in members.items()),
        left_out=group("", outside) if outside else None,
    )
    known = sum(len(each) for each in members.values()) + len(outside)
    entry: Dict[str, Any] = table.runner._declared_entry(
        statistic, measured, table.rule, table.tallies, bits, None, records=known
    )
    return entry


def _withheld_period_statistic(
    table: _Table, column: str, window: Tuple[int, int], known: int
) -> Dict[str, Any]:
    """The statistic of a number column with every month withheld under secondary suppression."""
    keys = [_key_name(index) for index in range(window[0], window[1] + 1)]
    table.tallies[0][disclosure.SECONDARY] += len(keys)
    return {
        "definition": "mean/1",
        "sampleSize": disclosure.sample_size(known, table.rule),
        "disclosure": table.rule.name,
        "of": column,
        "groups": [{"group": key, "status": disclosure.SECONDARY} for key in keys],
    }


def table_periods(table: _Table) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """A table's ``periods`` (monthly means of its number columns) and each ask left out with why."""
    column, reason = _time_column(table)
    what = f"periods of {table.expose_id}"
    if column is None:
        return [], ([(what, reason)] if reason else [])
    window, reason = _month_window(table, column)
    if window is None:
        return [], [(what, reason or _NOT_PUBLISHED)]
    kept = per_entity.entity_keys(table.expose_id, table.exposes)[1] | table.held
    binned = table.runner._binned_numbers(table.declared, table.plan)
    notes = [(f"{what}.{name}", _BINNED) for name in binned]
    numbers = [
        each.name
        for each in table.plan.columns
        if each.domain == "number" and each.name not in kept and each.name not in binned
    ]
    if not numbers:
        return [], notes
    cache: Dict[int, int] = {}
    rows = [
        None if each is None else _month_index(each, cache)
        for each in dates.read_milliseconds(table.read, column, table.backend)
    ]
    withhold = edge_fit(table.doc, column, _month_start, "month") is not None
    statistics = [
        (
            _withheld_period_statistic(table, name, window, len(_known(table, name)))
            if withhold
            else _period_statistic(table, column, name, rows, window)
        )
        for name in numbers
    ]
    return [{"column": column, "period": "P1M", "statistics": statistics}], notes


# ---------------------------------------------------------------------------
# Every table
# ---------------------------------------------------------------------------


def _form_table(table: _Table) -> List[Tuple[str, str]]:
    notes: List[Tuple[str, str]] = []
    calendar, calendar_notes = table_calendar(table)
    notes.extend(calendar_notes)
    if calendar:
        table.doc["calendar"] = calendar
    if table.asked is None:
        periods, period_notes = table_periods(table)
        notes.extend(period_notes)
        if periods:
            table.doc["periods"] = periods
    notes.extend(
        per_entity.table_per(
            per_entity.PerTable(
                expose_id=table.expose_id,
                exposes=table.exposes,
                table=(table.plan, table.read),
                declared=table.declared,
                doc=table.doc,
                backend=table.backend,
                rule=table.rule,
                tallies=table.tallies,
                bits=table.bits,
                quiet=table.asked is not None,
                timed=table.timed,
            )
        )
    )
    return notes


@dataclass(frozen=True)
class Structure:
    """What forming every table's calendar, periods and per-entity statistics reads."""

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
    held: Mapping[str, FrozenSet[str]]
    asked: Mapping[str, RequestTable]
    timings: Optional["StageTimings"]


def form_structure(inputs: Structure) -> List[Tuple[str, str]]:
    """Form the calendar, periods and per-entity statistics of each table; return what is left out.

    A table a bundle's fidelity block answers is left alone. The time is counted as the table's
    own measure and check time.
    """
    from fluid_build.profiler import runner

    notes: List[Tuple[str, str]] = []
    for plan, read, found in zip(inputs.plans, inputs.reads, inputs.declared, strict=True):
        expose_id = plan.expose_id
        if expose_id in inputs.blocks:
            continue
        table = _Table(
            expose_id=expose_id,
            expose=inputs.exposes[expose_id],
            exposes=inputs.exposes,
            plan=plan,
            read=read,
            declared=found,
            doc=inputs.tables_doc[expose_id],
            backend=inputs.backend,
            rule=inputs.rule,
            tallies=inputs.tallies,
            bits=inputs.bits,
            held=inputs.held.get(expose_id, frozenset()),
            asked=inputs.asked.get(expose_id),
            timings=inputs.timings,
        )
        with runner._timed(inputs.timings, "measure", expose_id):
            notes.extend(_form_table(table))
    return notes
