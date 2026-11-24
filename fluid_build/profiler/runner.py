# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Profile one expose: read its columns, form the metrics, check them, write the document.

The order is fixed, and nothing is written until every step has passed:

1. :func:`plan_expose` reads the contract: the expose, and the domain each of its
   columns is read in (``category``, ``number`` or ``timestamp``). A column whose
   FLUID type has no domain is left out and named, never guessed at.
2. :func:`fluid_build.profiler.sources.read_columns` reads every record of the
   table, once.
3. The statistic registry reads each value and forms each of the seven
   OpenLineage column metrics (``count`` ... ``quantiles``), through the one seam
   in :mod:`fluid_build.profiler._stats`. For a number column it also forms
   ``mean/1``, the statistic the fidelity check compares a ``mean`` property
   with, over the values placed on the document's grid (``fractionalBits``).
   With a bundle (:mod:`fluid_build.profiler.bundle`) it forms, besides, each
   statistic the Simulation's fidelity block declares (a histogram, a support, a
   quantile, a grouped mean), from the same one read of the table.
4. :mod:`fluid_build.profiler.disclosure` decides what each metric, mean and support
   may publish, group by group, and the registry's ``check_edge_counts`` (FLUX ADR
   0012) what the histograms and quantiles of one column may publish together.
5. :mod:`fluid_build.profiler.document` writes the canonical document, the
   registry reads it back, and its digest is taken.

The domains follow flux-engine's FLUID type table (``flux_engine/seam/contract.py``):
the types FLUX refuses (binary floats, a bare time, an interval, geospatial
values, sketches) and those with no profile domain (nested values, bytes) are
not profiled. A boolean (``boolean``, ``bool``, ``bit``) is read in the ``category``
domain, as the levels ``false`` and ``true``, which is how flux-stats reads one.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from fluid_build.policy._common import iter_exposes
from fluid_build.profiler import defaults, disclosure, document, facts, grouped, ranks, sources
from fluid_build.profiler._stats import (
    WITHHELD_ROUNDING,
    BlockRefused,
    DeclaredBlock,
    DeclaredStatistic,
    EdgeDecision,
    EdgeStatistic,
    Grouping,
    MeasuredGroup,
    MeasuredStatistic,
    MeasureRefused,
    MetricDefinition,
    ReadRefused,
    Request,
    RequestColumn,
    RequestTable,
    StatisticDefinition,
    StatisticsBackend,
)
from fluid_build.profiler.bundle import BundleBlock
from fluid_build.profiler.document import DEFAULT_FRACTIONAL_BITS

__all__ = [
    "DEFAULT_QUANTILE_LEVELS",
    "ColumnPlan",
    "ExposePlan",
    "FactResult",
    "CHILDREN_EDGES",
    "ExposeSource",
    "ProfileError",
    "ProfileResult",
    "Relationship",
    "RelationshipResult",
    "TableResult",
    "column_domain",
    "expose_ids",
    "plan_expose",
    "profile_expose",
    "profile_exposes",
    "resolve_expose_id",
]

CATEGORY, NUMBER, TIMESTAMP = "category", "number", "timestamp"

_CATEGORY_TYPES = (
    frozenset(
        "string text varchar varchar2 nvarchar char nchar character clob enum uuid "
        "uniqueidentifier guid".split()
    )
    | defaults.BOOLEAN_TYPES
)
_NUMBER_TYPES = frozenset(
    "int integer int2 int4 int8 int16 int32 int64 tinyint smallint mediumint bigint long "
    "longint serial bigserial year decimal dec numeric number bignumeric money".split()
)
_TIMESTAMP_TYPES = frozenset(
    "date timestamp timestamptz timestamp_tz timestamp_ntz timestamp_ltz timestampntz "
    "datetime datetime2 smalldatetime".split()
) | {"timestamp with time zone", "timestamp without time zone"}

_BINARY_FLOAT = "binary floating point has no exact reading (KERNEL-SPEC §2.1)"
_NESTED = "a nested value has no profile domain"
_NOT_PROFILED: Mapping[str, str] = {
    **{name: _BINARY_FLOAT for name in "float float4 float8 float32 float64 double real".split()},
    "double precision": _BINARY_FLOAT,
    "time": "a bare time of day has no profile domain",
    "interval": "a duration has no profile domain",
    **{
        name: _NESTED
        for name in "json jsonb super variant object array struct map record row".split()
    },
    **{
        name: "bytes have no profile domain"
        for name in "binary varbinary bytes blob bytea raw".split()
    },
    **{
        name: "a geospatial value has no profile domain"
        for name in "geography geometry geom point".split()
    },
    "hll": "a sketch has no profile domain",
}

#: The quantile levels ``quantiles`` is measured at.
DEFAULT_QUANTILE_LEVELS: Tuple[Fraction, ...] = (Fraction(1, 4), Fraction(1, 2), Fraction(3, 4))

#: The metrics published, by their OpenLineage name, in this order. A metric the
#: registry adds that is not named here is not published: nothing leaves before a
#: disclosure rule for it is written.
_METRICS = ("count", "nullCount", "distinctCount", "sum", "min", "max", "quantiles")
#: Counts of records: checked as cells, and read over every row of the table.
_RECORD_COUNTS = frozenset({"count", "nullCount"})

#: The fidelity statistics published, each with the domains it is formed for. As
#: with :data:`_METRICS`, a statistic not named here is not published. ``mean/1``
#: is formed ungrouped, over a number column only: there it publishes where the
#: column's ``count`` and ``sum`` do, and what it adds, the values' residues below
#: the grid, passes the sum checks (:func:`disclosure.check_mean`). Over a
#: timestamp it would publish a sum that no metric publishes. The rest
#: are formed only from a Simulation's fidelity block, which names the column each
#: one reads. A histogram's bin edges, a quantile's level and a ``groupBy`` are
#: shapes only that block holds, while a ``support`` needs the column and nothing
#: more. A correlation is not answered at all (its moments need the exact record
#: count, which the output checks round).
_STATISTICS: Tuple[Tuple[str, FrozenSet[str]], ...] = (("mean/1", frozenset({NUMBER})),)


class ProfileError(Exception):
    """A profile that cannot be formed. ``event`` is a stable code, ``context`` its details."""

    def __init__(self, event: str, message: str, context: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.event = event
        self.context = dict(context or {})


@dataclass(frozen=True)
class ColumnPlan:
    name: str
    domain: str
    #: The contract types the column ``date``: a ``YYYY-MM-DD`` text in it is that day.
    date: bool = False


@dataclass(frozen=True)
class ExposePlan:
    expose_id: str
    columns: Tuple[ColumnPlan, ...]
    #: Columns left out, each with the reason.
    skipped: Tuple[Tuple[str, str], ...] = ()


#: A fidelity statistic as the report names it: column, definition, groupBy and shape
#: (a quantile's level or a histogram's bins, which tell two of one column apart).
NamedStatistic = Tuple[str, str, Optional[str], Optional[str]]
#: One with some groups withheld: also the number of groups published, and of groups.
PartlyNamedStatistic = Tuple[str, str, Optional[str], Optional[str], int, int]


@dataclass(frozen=True)
class ExposeSource:
    """One expose a profile reads, and where its records live (``--source``, ``--table``)."""

    expose_id: str
    source: str
    table: Optional[str] = None


@dataclass(frozen=True)
class TableResult:
    """What the profile says about one expose, for the owner's summary."""

    plan: ExposePlan
    #: Each published fidelity statistic (:data:`NamedStatistic`).
    statistics: Tuple[NamedStatistic, ...] = ()
    #: Each fidelity statistic written with every group withheld: a run that checks
    #: it scores it censored.
    withheld_statistics: Tuple[NamedStatistic, ...] = ()
    #: Each fidelity statistic with some groups published and some withheld: a run
    #: that checks it scores only the published groups.
    partly_withheld_statistics: Tuple[PartlyNamedStatistic, ...] = ()
    #: Each declared property the document does not answer, with the reason.
    left_out: Tuple[Tuple[str, str], ...] = ()
    #: Each number, date or timestamp column the contract gives no default statistic,
    #: with why (:mod:`fluid_build.profiler.defaults`).
    uncovered: Tuple[Tuple[str, str], ...] = ()
    #: Each date or timestamp column with no declared range that has no date span, with why:
    #: it keeps the ladder's quantiles (:func:`_date_spans`).
    no_span: Tuple[Tuple[str, str], ...] = ()


#: The bins a parent's number of children is counted in: 0, 1, 2, 3 to 4, 5 to 9, 10 to 19,
#: and 20 or more. The last bin ends at the largest count a profile holds, 2^53 - 1, so it
#: holds every parent with 20 children or more and no count is cut to fit.
CHILDREN_EDGES: Tuple[int, ...] = (0, 1, 2, 3, 5, 10, 20, disclosure.MAX_COUNT)


@dataclass(frozen=True)
class Relationship:
    """A declared relationship whose two exposes one profile reads: a child's foreign key."""

    child: str
    name: str
    parent: str
    child_columns: Tuple[str, ...]
    parent_columns: Tuple[str, ...]
    #: Answering a request (format 1-2-0): whether its children per parent are published, the
    #: parent columns they are grouped by, and the ``(child column, parent column)`` pairs
    #: whose child histogram is grouped by the parent column through the relationship.
    counted: bool = True
    group_by: Tuple[str, ...] = ()
    linked: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RelationshipResult:
    """What the profile says about one relationship's children per parent, for the summary."""

    relationship: Relationship
    #: The cells published, of the histogram's cells; 0 when it is withheld whole.
    published: int
    cells: int


@dataclass(frozen=True)
class FactResult:
    """What the profile says about one measured fact, for the summary: never a count."""

    name: str
    #: Whether the share is published (as rounded counts); else the group is withheld whole.
    published: bool
    #: The ``CL_CONF_STATUS`` code of the check that withheld it; ``None`` when published.
    code: Optional[str] = None


@dataclass(frozen=True)
class ProfileResult:
    document: Dict[str, Any]
    canonical: bytes
    digest: str
    #: One result per expose, in the order they were profiled.
    tables: Tuple[TableResult, ...]
    published: int
    #: Withheld values per ``CL_CONF_STATUS`` code of the check that withheld them: a
    #: histogram's or a quantile's withheld groups are written ``C`` whatever the check, and
    #: counted here by the check (:func:`_code`).
    withheld: Mapping[str, int] = field(default_factory=dict)
    #: The histogram cells withheld inside published groups, per code of the check
    #: that withheld them; the document writes each ``C``.
    withheld_cells: Mapping[str, int] = field(default_factory=dict)
    #: The grid the document declares and its on-grid statistics were formed on.
    fractional_bits: int = DEFAULT_FRACTIONAL_BITS
    #: Each relationship counted, children per parent.
    relationships: Tuple[RelationshipResult, ...] = ()
    #: Each declared relationship not counted: the child, the relationship, and why.
    relationships_left_out: Tuple[Tuple[str, str, str], ...] = ()
    #: Answering a request: each listed item the profile does not hold, and why.
    not_measured: Tuple[Tuple[str, str], ...] = ()
    #: Answering a request: each fact measured, published or withheld.
    facts: Tuple[FactResult, ...] = ()


def _base_type(type_name: Any) -> str:
    """A FLUID column type in lower case, without its parameters: ``decimal(10,2)`` is ``decimal``."""
    text = re.sub(r"\s+", " ", str(type_name or "").strip().lower())
    return re.sub(r"\s*\(.*\)$", "", text)


def column_domain(type_name: Any) -> Tuple[Optional[str], Optional[str]]:
    """The profile domain a FLUID column type is read in, or ``None`` and the reason."""
    base = _base_type(type_name)
    if base in _CATEGORY_TYPES:
        return CATEGORY, None
    if base in _NUMBER_TYPES:
        return NUMBER, None
    if base in _TIMESTAMP_TYPES:
        return TIMESTAMP, None
    if base in _NOT_PROFILED:
        return None, _NOT_PROFILED[base]
    return None, "the type is not one fluid profile reads"


def expose_ids(contract: Mapping[str, Any]) -> List[str]:
    """The contract's exposeIds, in the order it declares them."""
    return [str(item["exposeId"]) for item in iter_exposes(dict(contract)) if item.get("exposeId")]


def resolve_expose_id(contract: Mapping[str, Any], requested: Optional[str]) -> str:
    """The expose to profile: *requested*, or the contract's only expose."""
    ids = expose_ids(contract)
    if requested:
        if requested in ids:
            return requested
        raise ProfileError(
            "profile_expose_not_found",
            f"the contract has no expose {requested!r}",
            {"exposes": ", ".join(ids) or "(none)", "next": "pass --expose with one of them"},
        )
    if len(ids) == 1:
        return ids[0]
    raise ProfileError(
        "profile_expose_required",
        "the contract has more than one expose" if ids else "the contract has no expose",
        {"exposes": ", ".join(ids) or "(none)", "next": "name one with --expose"},
    )


def plan_expose(contract: Mapping[str, Any], expose_id: str) -> ExposePlan:
    """The columns of *expose_id* that are profiled, and those left out with why."""
    exposes = iter_exposes(dict(contract))
    expose = next((item for item in exposes if item.get("exposeId") == expose_id), None)
    if expose is None:
        raise ProfileError("profile_expose_not_found", f"the contract has no expose {expose_id!r}")
    body = expose.get("contract")
    schema = body.get("schema") if isinstance(body, Mapping) else None
    if not isinstance(schema, list) or not schema:
        raise ProfileError(
            "profile_expose_has_no_schema",
            f"exposes[{expose_id}] declares no contract.schema columns",
            {"next": "declare the expose's columns, then profile it"},
        )
    columns: List[ColumnPlan] = []
    skipped: List[Tuple[str, str]] = []
    seen = set()
    for entry in schema:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
            raise ProfileError(
                "profile_expose_has_no_schema",
                f"exposes[{expose_id}].contract.schema holds an entry with no name",
            )
        name = entry["name"]
        if name in seen:
            raise ProfileError(
                "profile_column_declared_twice",
                f"exposes[{expose_id}] declares column {name!r} twice",
            )
        seen.add(name)
        domain, reason = column_domain(entry.get("type"))
        if domain is None:
            skipped.append((name, reason or ""))
        else:
            is_date = _base_type(entry.get("type")) == "date"
            columns.append(ColumnPlan(name=name, domain=domain, date=is_date))
    return ExposePlan(expose_id=expose_id, columns=tuple(columns), skipped=tuple(skipped))


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, Mapping) and not value)


def _column(
    column: ColumnPlan,
    raw: Sequence[Any],
    row_count: int,
    backend: StatisticsBackend,
    rule: disclosure.DisclosureRule,
    tally: "Counter[str]",
    fractional_bits: int,
    declares_mean: bool = False,
    withhold: FrozenSet[str] = frozenset(),
    asked: Optional[RequestColumn] = None,
) -> Dict[str, Any]:
    """One column of the table: its domain, then its checked metrics and statistics.

    *asked* is the request's entry for the column: only the metrics and the ``mean/1`` it lists.

    *declares_mean* says the fidelity block declares an ungrouped ``mean`` of this
    column: that statistic replaces the fixed ``mean/1``, which is not formed.
    *withhold* names the metrics withheld whatever their value (:func:`_counted_levels`).
    """
    read = [
        backend.read_value(column.domain, value, column=column.name, row_index=index)
        for index, value in enumerate(raw)
    ]
    entry: Dict[str, Any] = {
        "domain": column.domain,
        "metrics": _column_metrics(
            column,
            read,
            row_count,
            backend,
            rule,
            tally,
            withhold,
            None if asked is None else frozenset(asked.metrics),
        ),
    }
    statistics = (
        []
        if declares_mean or (asked is not None and "mean/1" not in asked.statistics)
        else _column_statistics(column, read, backend, rule, tally, fractional_bits)
    )
    if statistics:
        entry["statistics"] = statistics
    return entry


def _column_metrics(
    column: ColumnPlan,
    read: Sequence[Any],
    row_count: int,
    backend: StatisticsBackend,
    rule: disclosure.DisclosureRule,
    tally: "Counter[str]",
    withhold: FrozenSet[str] = frozenset(),
    listed: Optional[FrozenSet[str]] = None,
) -> Dict[str, Any]:
    """The checked ``metrics`` object of one column, from its values as read.

    A request's *listed* metrics are the only ones formed.

    A metric *withhold* names is withheld under ``D`` whatever its value and its own
    check, so its status says nothing about the records either.
    """
    present = [value for value in read if value is not None]
    holders = Counter(present)
    counted = disclosure.check_count(len(present), rule)
    available: Dict[str, MetricDefinition] = {
        metric.name: metric for metric in backend.metrics() if column.domain in metric.domains
    }
    metrics: Dict[str, Any] = {}
    for name in _METRICS:
        metric = available.get(name)
        if metric is None or (listed is not None and name not in listed):
            continue
        levels = DEFAULT_QUANTILE_LEVELS if name == "quantiles" else ()
        value = metric.measure(read, levels)
        if name in _RECORD_COUNTS:
            verdict = disclosure.check_count(value, rule)
            if verdict.published:
                verdict = disclosure.check_rounding(value, rule)
            band = disclosure.sample_size(row_count, rule)
            shown = disclosure.round_count(value, rule)
        elif counted.published and _empty(value):
            continue  # no such value: the column holds no non-null record
        else:
            band = disclosure.sample_size(len(present), rule)
            verdict, shown = counted, value
            if counted.published:
                verdict, shown = _check_described(name, value, present, holders, rule)
        if name in withhold:
            verdict = disclosure.Verdict(disclosure.SECONDARY, "counts what a histogram withholds")
        entry: Dict[str, Any] = {
            "definition": metric.identifier,
            "status": verdict.status,
            "sampleSize": band,
            "disclosure": rule.name,
        }
        if verdict.published:
            entry.update(metric.spell(shown))
            tally[disclosure.FREE] += 1
        else:
            tally[verdict.status] += 1
        metrics[name] = entry
    return metrics


def _check_described(
    name: str,
    value: Any,
    present: Sequence[Any],
    holders: "Counter[Any]",
    rule: disclosure.DisclosureRule,
) -> Tuple[disclosure.Verdict, Any]:
    """Check a metric of the non-null records, once their count has passed."""
    if name == "distinctCount":
        return disclosure.check_rounding(value, rule), disclosure.round_count(value, rule)
    if name == "sum":
        return disclosure.check_sum(present, rule), value
    if name in ("min", "max"):
        return disclosure.check_order_statistic(holders[value], rule), value
    if name == "quantiles":
        for found in value.values():
            verdict = disclosure.check_order_statistic(holders[found], rule)
            if not verdict.published:
                # One level's value too few records hold withholds the whole metric:
                # a document cannot say which level was left out (PROFILE-SPEC §4).
                return verdict, value
        return disclosure.PUBLISHED, value
    raise AssertionError(f"no disclosure rule for metric {name!r}")


#: PROFILE-SPEC §3: an ungrouped statistic has one group, keyed by the empty string.
_UNGROUPED = ""


def _column_statistics(
    column: ColumnPlan,
    read: Sequence[Any],
    backend: StatisticsBackend,
    rule: disclosure.DisclosureRule,
    tally: "Counter[str]",
    fractional_bits: int,
) -> List[Dict[str, Any]]:
    """The checked fidelity ``statistics`` of one column, each with its one group.

    A group the rule withholds keeps its status and nothing else, which the
    fidelity check reads as censored, never as a zero (PROFILE-SPEC §7).
    """
    available: Dict[str, StatisticDefinition] = {
        definition.identifier: definition for definition in backend.statistics()
    }
    present = [value for value in read if value is not None]
    statistics: List[Dict[str, Any]] = []
    for identifier, domains in _STATISTICS:
        definition = available.get(identifier)
        if definition is None or column.domain not in domains:
            continue
        encode = backend.encoder(fractional_bits)
        encoded = [encode(value) for value in present]
        verdict = _check_statistic(identifier, present, encoded, fractional_bits, rule)
        if verdict.published and not present:
            continue  # no record to form it over, and the rule publishes a zero
        if verdict.published:
            group = definition.measure(_UNGROUPED, encoded)
            # k is a count of records: published rounded, as the column's count is.
            group["k"] = disclosure.round_count(len(present), rule)
            group["status"] = disclosure.FREE
        else:
            group = {"group": _UNGROUPED, "status": verdict.status}
        tally[verdict.status] += 1
        statistics.append(
            {
                "definition": identifier,
                "sampleSize": disclosure.sample_size(len(present), rule),
                "disclosure": rule.name,
                "groups": [group],
            }
        )
    return statistics


def _check_statistic(
    identifier: str,
    present: Sequence[Any],
    encoded: Sequence[int],
    fractional_bits: int,
    rule: disclosure.DisclosureRule,
) -> disclosure.Verdict:
    """Check one ungrouped statistic of a column's non-null values and their grid points."""
    if identifier == "mean/1":
        return disclosure.check_mean(present, encoded, fractional_bits, rule)
    raise AssertionError(f"no disclosure rule for statistic {identifier!r}")


def profile_expose(
    contract: Mapping[str, Any],
    expose_id: str,
    *,
    source: str,
    backend: StatisticsBackend,
    table: Optional[str] = None,
    cwd: Optional[Path] = None,
    rule: disclosure.DisclosureRule = disclosure.ACRO_DEFAULT,
    producer: Optional[str] = None,
    fractional_bits: Optional[int] = None,
    bundle: Optional[BundleBlock] = None,
) -> ProfileResult:
    """Profile *expose_id* of *contract* from the records *source* names.

    :func:`profile_exposes` of this one expose: *bundle* is the fidelity block it answers.
    """
    return profile_exposes(
        contract,
        [ExposeSource(expose_id, source, table)],
        backend=backend,
        cwd=cwd,
        rule=rule,
        producer=producer,
        fractional_bits=fractional_bits,
        bundles=None if bundle is None else {expose_id: bundle},
    )


def profile_exposes(
    contract: Mapping[str, Any],
    exposes: Sequence[ExposeSource],
    *,
    backend: StatisticsBackend,
    cwd: Optional[Path] = None,
    rule: disclosure.DisclosureRule = disclosure.ACRO_DEFAULT,
    producer: Optional[str] = None,
    fractional_bits: Optional[int] = None,
    bundles: Optional[Mapping[str, BundleBlock]] = None,
    request: Optional[Request] = None,
    row_floor: Optional[int] = None,
) -> ProfileResult:
    """Profile each of *exposes* of *contract*, each from its own source, into one document.

    *fractional_bits* is the grid ``mean/1`` is formed on (16 unless given),
    checked before any record is read; the fidelity block must declare the same
    (PROFILE-SPEC §7). An expose *bundles* holds a block for also answers that
    block, on the block's grid (:func:`_declared_block`), and the blocks of one
    document declare one grid. Every expose's plan and block is checked before any
    record is read, and each expose's records are read once.

    With a *request* the profile measures what it lists and nothing else
    (:mod:`fluid_build.profiler.request`); its grid is the profile's. A request of format
    1-2-0 may also ask rank correlations of column pairs (:mod:`fluid_build.profiler.ranks`;
    *row_floor*, a count of at least 2, is then written as each one's ``rowFloor``), the
    columns of a table related in pairs, and a relationship's statistics grouped by a parent
    column or a child column grouped through the link (:mod:`fluid_build.profiler.grouped`).
    """
    if row_floor is not None and (
        isinstance(row_floor, bool) or not 2 <= row_floor <= disclosure.MAX_COUNT
    ):
        raise ProfileError(
            "profile_row_floor_invalid",
            f"a row floor is a count from 2 to {disclosure.MAX_COUNT}",
            {
                "read": "nothing: no record was read",
                "next": "give --row-floor a count of 2 or more",
            },
        )
    fact_plans: List[facts.FactPlan] = []
    if request is not None:
        from fluid_build.profiler import request as asked_for

        if bundles:
            raise ProfileError(
                "profile_request_with_bundle",
                "a request and a bundle both say what the profile answers",
                {"read": "nothing: no record was read", "next": "give one of them"},
            )
        if fractional_bits is not None and fractional_bits != request.fractional_bits:
            raise ProfileError(
                "profile_request_fractional_bits_conflict",
                f"--fractional-bits {fractional_bits} differs from the request's "
                f"encoding.fractionalBits {request.fractional_bits}",
                {
                    "read": "nothing: no record was read",
                    "next": "drop --fractional-bits: the request's grid is the profile's",
                },
            )
        fractional_bits = request.fractional_bits
        asked_for.check_tables(request, contract)
        asked_for.check_sources(request, [each.expose_id for each in exposes])
        fact_plans = _plan_facts(contract, request)
    if fractional_bits is not None:
        document.check_fractional_bits(fractional_bits)
    if not exposes:
        raise ProfileError(
            "profile_expose_required", "no expose to profile", {"next": "name one with --expose"}
        )
    _refuse_repeated(exposes)
    plans = [_plan_with_columns(contract, each.expose_id) for each in exposes]
    asked: Dict[str, RequestTable] = {}
    if request is not None:
        for each in request.tables:
            asked[each.expose_id] = each
        plans = [asked_for.restrict_plan(plan, asked[plan.expose_id]) for plan in plans]
    blocks = bundles or {}
    declared: List[Optional[DeclaredBlock]] = []
    grids: Dict[str, int] = {}
    for plan in plans:
        found, grid = _declared_block(
            blocks.get(plan.expose_id), contract, plan.expose_id, plan, backend, fractional_bits
        )
        declared.append(found)
        if found is not None:
            grids[plan.expose_id] = grid
    fractional_bits = _one_grid(grids, blocks, fractional_bits)
    uncovered: List[Tuple[Tuple[str, str], ...]] = [() for _ in plans]
    matched: Set[int] = set()
    wanted = None
    if request is not None:
        wanted, matched = asked_for.relationship_filter(request)
    relationships, relationships_left_out = _declared_relationships(contract, plans, blocks, wanted)
    held = _relationship_keys(relationships)
    unfiltered: Dict[str, Optional[DeclaredBlock]] = {}
    for position, plan in enumerate(plans):
        if plan.expose_id not in blocks:
            declared[position], uncovered[position] = _default_block(
                contract, plan, backend, fractional_bits, held.get(plan.expose_id, frozenset())
            )
            if request is not None:
                unfiltered[plan.expose_id] = declared[position]
                declared[position] = asked_for.filter_block(
                    declared[position], asked[plan.expose_id]
                )
    edges = {
        plan.expose_id: _edge_counted(found) for plan, found in zip(plans, declared, strict=True)
    }
    relationships, relationships_left_out = _keys_not_counted_at_edges(
        relationships, relationships_left_out, edges
    )
    if request is not None:
        relationships = [_with_asks(each, request) for each in relationships]
    keys = _key_columns(relationships)
    reads = [
        _dates_read(
            plan,
            sources.read_columns(
                each.source,
                _read_names(plan, fact_plans),
                table=each.table,
                cwd=cwd,
            ),
        )
        for each, plan in zip(exposes, plans, strict=True)
    ]
    spans, no_span = _date_spans(
        contract, plans, reads, blocks, held, _grouping_columns(request, relationships),
        asked, backend, rule,
    )  # fmt: skip
    for position, plan in enumerate(plans):
        if plan.expose_id not in spans:
            continue
        declared[position], uncovered[position] = _default_block(
            contract,
            plan,
            backend,
            fractional_bits,
            held.get(plan.expose_id, frozenset()),
            spans[plan.expose_id],
        )
        if request is not None:
            unfiltered[plan.expose_id] = declared[position]
            declared[position] = asked_for.filter_block(declared[position], asked[plan.expose_id])
    by_expose = {plan.expose_id: (plan, read) for plan, read in zip(plans, reads, strict=True)}
    answered = _Groupings()
    if request is not None:
        answered = _answer_groupings(
            request,
            plans,
            reads,
            declared,
            unfiltered,
            relationships,
            held,
            backend,
            fractional_bits,
        )
        declared = list(answered.declared)
    tallies: Tuple["Counter[str]", "Counter[str]"] = (Counter(), Counter())
    tables_doc: Dict[str, Dict[str, Any]] = {}
    results: List[TableResult] = []
    for plan, read, found, missing in zip(plans, reads, declared, uncovered, strict=True):
        table_doc, result = _profile_table(
            plan,
            read,
            found,
            backend,
            rule,
            tallies,
            fractional_bits,
            keys.get(plan.expose_id, frozenset()),
            asked.get(plan.expose_id),
            answered.grouped_by.get(plan.expose_id, frozenset()),
        )
        tables_doc[plan.expose_id] = table_doc
        # A request lists what it asks, so what the defaults leave out is told per listed item.
        spanless = tuple(no_span.get(plan.expose_id, ()))
        results.append(
            replace(result, uncovered=(), left_out=(), no_span=spanless)
            if request is not None
            else replace(result, uncovered=missing, no_span=spanless)
        )
    counted = [
        _children_per_parent(
            each,
            by_expose,
            backend,
            rule,
            tallies,
            fractional_bits,
            answered.relationship_groupings.get(position, ()),
        )
        for position, each in enumerate(relationships)
        if each.counted
    ]
    unanswered: Tuple[Tuple[str, str], ...] = ()
    if request is not None:
        unanswered = tuple(
            asked_for.not_measured(
                request,
                tables=tables_doc,
                defaults=unfiltered,
                uncovered={
                    plan.expose_id: dict(missing)
                    for plan, missing in zip(plans, uncovered, strict=True)
                },
                keys=keys,
                matched=matched,
                metric_names=frozenset(_METRICS),
            )
        )
    fact_results: Tuple[FactResult, ...] = ()
    invariants: List[Dict[str, Any]] = []
    if request is not None:
        by_table = {plan.expose_id: read for plan, read in zip(plans, reads, strict=True)}
        invariants, fact_results, unmeasured = _fact_entries(
            contract, fact_plans, by_table, rule, tallies[0]
        )
        unanswered = (*unanswered, *unmeasured)
        unanswered = (*unanswered, *answered.not_measured)
        for plan, read in zip(plans, reads, strict=True):
            table = asked[plan.expose_id]
            pairs, missing_pairs = ranks.column_pairs(
                table,
                {column.name: column.domain for column in plan.columns},
                read,
                backend,
                rule,
                tallies[0],
                fractional_bits,
                row_floor=row_floor,
                held=held.get(plan.expose_id, frozenset()),
            )
            if pairs:
                tables_doc[plan.expose_id]["columnPairs"] = pairs
            unanswered = (*unanswered, *missing_pairs)
    if producer is None:
        from fluid_build import __version__

        producer = document.producer_uri(__version__)
    built = document.build_document(
        producer=producer,
        rule=rule,
        tables=tables_doc,
        fractional_bits=fractional_bits,
        relationships=[entry for entry, _ in counted],
        invariants=invariants,
    )
    canonical = document.canonical_bytes(built)
    backend.check_document(canonical.decode("utf-8"))
    tally, cell_tally = tallies
    published = tally.pop(disclosure.FREE, 0)
    return ProfileResult(
        document=built,
        canonical=canonical,
        digest=document.digest_of(canonical),
        tables=tuple(results),
        published=published,
        withheld=dict(sorted(tally.items())),
        withheld_cells=dict(sorted(cell_tally.items())),
        fractional_bits=fractional_bits,
        relationships=tuple(result for _, result in counted),
        relationships_left_out=tuple(relationships_left_out),
        not_measured=unanswered,
        facts=fact_results,
    )


def _with_asks(relationship: Relationship, request: Request) -> Relationship:
    """*relationship* with what the request asks of it (:func:`request.relationship_asks`)."""
    from fluid_build.profiler import request as asked_for

    counted, group_by, linked = asked_for.relationship_asks(request, relationship)
    return replace(relationship, counted=counted, group_by=group_by, linked=linked)


@dataclass
class _Groupings:
    """The grouped statistics a request asks for, formed before any table is measured.

    ``declared`` are the tables' blocks with the two-way statistics of ``cliqueColumns`` and
    the child histograms grouped through a link added, ``relationship_groupings`` each counted
    relationship's parent groupings (by its position), ``grouped_by`` the columns of each
    table a grouping uses, and ``not_measured`` each grouping the profile does not hold, with
    why.
    """

    declared: Sequence[Optional[DeclaredBlock]] = ()
    relationship_groupings: Dict[int, List[Tuple[str, Grouping]]] = field(default_factory=dict)
    grouped_by: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    not_measured: List[Tuple[str, str]] = field(default_factory=list)


def _answer_groupings(
    request: Request,
    plans: Sequence[ExposePlan],
    reads: Sequence[sources.SourceColumns],
    declared: Sequence[Optional[DeclaredBlock]],
    structures: Mapping[str, Optional[DeclaredBlock]],
    relationships: Sequence[Relationship],
    held: Mapping[str, FrozenSet[str]],
    backend: StatisticsBackend,
    fractional_bits: int,
) -> _Groupings:
    """Form the groupings a request asks for, from the one read of each table.

    *structures* are the tables' default blocks, which hold each column's public levels and
    bins. Everything here is chosen from them and from the contract; the records only say
    which group each row is in. A grouping a column's structure does not allow is named in
    ``not_measured`` with the reason.
    """
    by_expose = {plan.expose_id: (plan, read) for plan, read in zip(plans, reads, strict=True)}
    domains = {
        plan.expose_id: {column.name: column.domain for column in plan.columns} for plan in plans
    }
    added: Dict[str, List[DeclaredStatistic]] = {}
    grouped_by: Dict[str, Set[str]] = {}
    result = _Groupings()
    for plan, read in zip(plans, reads, strict=True):
        table = request.table(plan.expose_id)
        if table is None or not table.clique_columns:
            continue
        found, missing = grouped.pair_statistics(
            table,
            domains[plan.expose_id],
            read,
            structures.get(plan.expose_id),
            backend=backend,
            fractional_bits=fractional_bits,
            held=held.get(plan.expose_id, frozenset()),
        )
        added.setdefault(plan.expose_id, []).extend(found)
        result.not_measured.extend(missing)
    for position, each in enumerate(relationships):
        spelled = f"{each.child}({', '.join(each.child_columns)}) to {each.parent}"
        if each.counted and each.group_by:
            chosen, missing = grouped.parent_groupings(
                each.group_by,
                parent=each.parent,
                domains=domains[each.parent],
                read=by_expose[each.parent][1],
                structures=structures.get(each.parent),
                backend=backend,
                fractional_bits=fractional_bits,
            )
            result.relationship_groupings[position] = chosen
            grouped_by.setdefault(each.parent, set()).update(name for name, _ in chosen)
            result.not_measured.extend(
                (f"children per parent of {spelled} {what}", why) for what, why in missing
            )
        for column, parent_column in each.linked:
            made = _linked_statistic(
                each,
                column,
                parent_column,
                by_expose,
                domains,
                structures,
                backend,
                fractional_bits,
            )
            what = f"{column} of {each.child} grouped by {each.parent}.{parent_column}"
            if isinstance(made, str):
                result.not_measured.append((what, made))
                continue
            added.setdefault(each.child, []).append(made)
            grouped_by.setdefault(each.parent, set()).add(parent_column)
    result.declared = [
        (
            replace(found, statistics=(*found.statistics, *added.get(plan.expose_id, ())))
            if found is not None
            else None
        )
        for plan, found in zip(plans, declared, strict=True)
    ]
    result.grouped_by = {expose: frozenset(names) for expose, names in grouped_by.items()}
    return result


def _linked_statistic(
    relationship: Relationship,
    column: str,
    parent_column: str,
    by_expose: Mapping[str, Tuple[ExposePlan, sources.SourceColumns]],
    domains: Mapping[str, Mapping[str, str]],
    structures: Mapping[str, Optional[DeclaredBlock]],
    backend: StatisticsBackend,
    fractional_bits: int,
) -> Union[DeclaredStatistic, str]:
    """The histogram of a child column grouped by a parent column through *relationship*.

    The groups are the parent column's levels, which the contract declares: a parent column
    whose values are not public levels is not a grouping, and the reason is returned instead.
    A child row is in the group of the parent row its key reaches, and in none when it reaches
    none (:func:`grouped.link_assignment`).
    """
    child, parent = relationship.child, relationship.parent
    base = grouped.histogram_of(structures.get(child), column)
    by = grouped.histogram_of(structures.get(parent), parent_column)
    if column not in domains[child]:
        return f"{column}: it is a column fluid profile does not read"
    if base is None:
        return (
            f"{column}: it has no public levels or bins: it is neither an enum(...) or a "
            "boolean column nor a number whose validationRules declare both ends of a range, nor "
            "a date or a timestamp"
        )
    if parent_column not in domains[parent]:
        return f"{parent_column}: it is a column fluid profile does not read"
    if by is None or by.kind != "levelHistogram":
        return (
            f"{parent_column}: a group is keyed by the parent column's value, and only the "
            "levels an enum(...) or a boolean column declares are public"
        )
    if not set(relationship.child_columns) <= set(domains[child]) or not set(
        relationship.parent_columns
    ) <= set(domains[parent]):
        return "a key column of the relationship is not a column the profile holds"
    parent_read = by_expose[parent][1]
    parent_groups = grouped.assignment(
        by,
        domains[parent][parent_column],
        parent_read.values[parent_column],
        name=parent_column,
        backend=backend,
        fractional_bits=fractional_bits,
    )
    assigned = grouped.link_assignment(
        _keys(by_expose[child], relationship.child_columns, backend),
        _keys(by_expose[parent], relationship.parent_columns, backend),
        parent_groups,
    )
    return grouped.child_groupings(
        base,
        by,
        parent=parent,
        parent_column=parent_column,
        parent_columns=relationship.parent_columns,
        child_columns=relationship.child_columns,
        assigned=assigned,
    )


def _plan_facts(contract: Mapping[str, Any], request: Request) -> List[facts.FactPlan]:
    """What each requested fact needs, decided from the contract before any record is read.

    A fact whose table or rule the contract does not hold refuses the profile; one that
    cannot be measured on a table's records comes back with its reason.
    """
    planned: List[facts.FactPlan] = []
    for each in request.facts:
        try:
            planned.append(facts.plan_fact(contract, each.name, each.table, each.rule))
        except facts.FactRefused as exc:
            raise _fact_refusal(exc) from None
    return planned


def _fact_refusal(exc: "facts.FactRefused") -> ProfileError:
    return ProfileError(
        "profile_request_" + exc.event.replace("-", "_"),
        str(exc),
        (
            {
                "written": "nothing",
                "next": "write the request again with `fluxsim init` for this contract",
            }
            if exc.event.startswith(("fact-table", "fact-rule", "fact-name"))
            else {
                "written": "nothing",
                "next": "the records and the contract disagree: fix either, then profile",
            }
        ),
    )


def _read_names(plan: ExposePlan, fact_plans: Sequence["facts.FactPlan"]) -> List[str]:
    """The columns of a table to read: those profiled, then those only a fact's rule reads.

    A column read for a fact alone is read and never profiled: reading is not publishing.
    """
    names = [column.name for column in plan.columns]
    for each in fact_plans:
        if each.table == plan.expose_id:
            names.extend(name for name in each.columns if name not in names)
    return names


#: The levels of a fact's histogram: the rows that do not satisfy it, then those that do.
_FACT_LEVELS = ["false", "true"]


def _fact_entries(
    contract: Mapping[str, Any],
    fact_plans: Sequence["facts.FactPlan"],
    reads: Mapping[str, sources.SourceColumns],
    rule: disclosure.DisclosureRule,
    tally: "Counter[str]",
) -> Tuple[List[Dict[str, Any]], Tuple[FactResult, ...], List[Tuple[str, str]]]:
    """The ``invariants`` entries of the facts measured, how each fared, and those not measured.

    A fact's two counts, the covered rows that do not and do satisfy its rule, are two
    cells of one histogram and pass the output checks as cells do: a cell of fewer than the
    minimum or a zero where zeros are disclosive is withheld, and either cell withheld
    withholds the whole group, since the other cell and the table's published row count would
    give it back. Published counts are rounded as other counts are.

    The facts of one table are then checked together, on the exact counts of every
    combination of their states (:func:`disclosure.suppress_differencing`): a fact whose
    counts, with the other facts' and the table's row count, would give back a small or a
    disclosive zero cell is withheld too, under ``D``. A withheld fact whose rule has a
    ``when`` writes the band of a statistic of unknown size, ``min`` 0 and no ``max``: the
    band of the rows it covers would give their number back, for another fact's ``when``
    to difference against.
    """
    unmeasured: List[Tuple[str, str]] = []
    measured: List[facts.FactPlan] = []
    states: Dict[str, bytes] = {}
    for each in fact_plans:
        if each.reason is not None:
            unmeasured.append((f"fact {each.name}", each.reason))
            continue
        read = reads[each.table]
        try:
            states[each.name] = facts.fact_states(
                contract, each, {name: read.values[name] for name in each.columns}
            )
        except facts.FactRefused as exc:
            raise _fact_refusal(exc) from None
        measured.append(each)
    cells = {
        each.name: [states[each.name].count(facts.FAILS), states[each.name].count(facts.HOLDS)]
        for each in measured
    }
    refused: Dict[str, Optional[disclosure.Verdict]] = {}
    for each in measured:
        verdicts = [_checked_cell(count, rule) for count in cells[each.name]]
        refused[each.name] = next((verdict for verdict in verdicts if not verdict.published), None)
    covers_all: Dict[str, bool] = {}
    for table in dict.fromkeys(each.table for each in measured):
        on_table = [each for each in measured if each.table == table]
        together = facts.states_together(contract, on_table)
        for each in on_table:
            covers_all[each.name] = not together({each.name: facts.NOT_COVERED})
        for name in _differenced(on_table, states, refused, rule, together):
            refused[name] = disclosure.Verdict(
                disclosure.SECONDARY,
                "with the other facts of its table it would give back a small cell",
            )
    entries: List[Dict[str, Any]] = []
    results: List[FactResult] = []
    for each in measured:
        counts = cells[each.name]
        total = sum(counts)
        verdict = refused[each.name]
        group: Dict[str, Any]
        band = disclosure.sample_size(total, rule)
        if verdict is None:
            tally[disclosure.FREE] += 1
            group = {
                "group": "",
                "status": disclosure.FREE,
                "k": disclosure.round_count(total, rule),
                "levelCounts": [disclosure.round_count(count, rule) for count in counts],
            }
            results.append(FactResult(each.name, True))
        else:
            tally[verdict.status] += 1
            group = {"group": "", "status": disclosure.WITHHELD}
            results.append(FactResult(each.name, False, verdict.status))
            if not covers_all[each.name]:
                band = {"min": 0}
        entries.append(
            {
                "name": each.name,
                "statistics": [
                    {
                        "definition": "levelHistogram/1",
                        "sampleSize": band,
                        "disclosure": rule.name,
                        "levels": list(_FACT_LEVELS),
                        "groups": [group],
                    }
                ],
            }
        )
    return entries, tuple(results), unmeasured


def _differenced(
    plans: Sequence["facts.FactPlan"],
    states: Mapping[str, bytes],
    refused: Mapping[str, Optional[disclosure.Verdict]],
    rule: disclosure.DisclosureRule,
    together: "facts.StatesTogether",
) -> List[str]:
    """The facts of one table that passed their own checks and are withheld against differencing.

    One pass over the rows counts every combination of the facts' states, which is the
    grouped count ``GROUP BY`` the facts' states would give, whatever the number of facts.
    """
    names = [each.name for each in plans]
    passed = [at for at, name in enumerate(names) if refused[name] is None]
    if not passed:
        return []
    combinations: "Counter[Tuple[int, ...]]" = Counter(
        zip(*(states[name] for name in names), strict=True)
    )
    position = {name: at for at, name in enumerate(names)}

    def factors(order: Sequence[int]) -> disclosure.Factors:
        return [
            (tuple(position[name] for name in members), allowed)
            for members, allowed in together.factors([names[at] for at in order])
        ]

    withheld = disclosure.suppress_differencing(combinations, passed, rule, factors, facts.STATES)
    return [names[at] for at in sorted(withheld)]


def _checked_cell(count: int, rule: disclosure.DisclosureRule) -> disclosure.Verdict:
    """One cell of a fact's counts: its size, then the rounding."""
    verdict = disclosure.check_count(count, rule)
    return disclosure.check_rounding(count, rule) if verdict.published else verdict


#: A date as a CSV, a JSON slice or a text column writes one: exactly ``YYYY-MM-DD``.
_DATE_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _dates_read(plan: ExposePlan, read: sources.SourceColumns) -> sources.SourceColumns:
    """*read*, with each ``YYYY-MM-DD`` text of a column the contract types ``date`` as that day.

    A CSV holds every value as text, and the registry reads a timestamp only from
    RFC 3339 with an offset (FLX-5022), so a ``date`` column read from a CSV was refused
    where Parquet's, which arrives as a date, was read. The text is read strictly, as
    the contract's type says: exactly ``YYYY-MM-DD`` and a real calendar day becomes that
    day's midnight in UTC, as a date from a Parquet file or a database does
    (:func:`sources.midnight_utc`). Any other value goes on as it came, for the reader to
    read or refuse: DuckDB's own date reader is not used, because it reads ``infinity``
    and ``epoch`` as dates and drops a time of day.
    """
    days = [column.name for column in plan.columns if column.date]
    if not days:
        return read
    values = dict(read.values)
    for name in days:
        values[name] = [_as_day(value) for value in read.values[name]]
    return replace(read, values=values)


def _as_day(value: Any) -> Any:
    if isinstance(value, str) and _DATE_TEXT.match(value):
        try:
            return sources.midnight_utc(date.fromisoformat(value))
        except ValueError:  # not a calendar day: the reader refuses it as written
            return value
    return value


def _declared_relationships(
    contract: Mapping[str, Any],
    plans: Sequence[ExposePlan],
    blocks: Mapping[str, BundleBlock],
    wanted: Optional[Callable[[str, str, Tuple[str, ...], Tuple[str, ...]], bool]] = None,
) -> Tuple[List[Relationship], List[Tuple[str, str, str]]]:
    """The ``semantics.relationships`` this profile counts children per parent for.

    With *wanted* (a request's test) only the declared relationships it accepts are considered.

    A relationship is counted when the profile reads both its exposes, without a bundle
    (a bundle's profile answers its block alone), and both keys are columns it reads in
    one domain each, pair by pair. One whose target declares a validity window is not:
    a parent there has a row per version. Every other is returned with why, never
    guessed at. Both come in the contract's order, whatever order the call names the
    exposes in, so the document's bytes do not depend on it.
    """
    from fluid_build.data_rules._model import declares_validity_window

    exposes = {
        str(item.get("exposeId")): item
        for item in iter_exposes(dict(contract))
        if item.get("exposeId")
    }
    planned = {plan.expose_id: plan for plan in plans}
    counted: List[Relationship] = []
    left_out: List[Tuple[str, str, str]] = []
    for plan in (planned[expose_id] for expose_id in exposes if expose_id in planned):
        semantics = exposes[plan.expose_id].get("semantics")
        declared = semantics.get("relationships") if isinstance(semantics, Mapping) else None
        for raw in declared if isinstance(declared, list) else ():
            if not isinstance(raw, Mapping):
                continue
            name, target = str(raw.get("name") or "?"), raw.get("to")
            child_columns = _column_names(raw.get("fromColumns"))
            parent_columns = _column_names(raw.get("toColumns"))
            if wanted is not None and not wanted(
                plan.expose_id, str(target), child_columns, parent_columns
            ):
                continue
            reason: Optional[str] = None
            if not isinstance(target, str) or target not in planned:
                reason = "its target is not profiled in this call: profile both exposes together"
            elif plan.expose_id in blocks or target in blocks:
                reason = "a profile that answers a bundle answers its fidelity block alone"
            elif declares_validity_window(exposes[target]):
                reason = "its target declares a validity window, so a parent has a row per version"
            elif not child_columns or len(child_columns) != len(parent_columns):
                reason = "its fromColumns and toColumns are not two lists of one length"
            else:
                reason = _keys_unread(plan, planned[target], child_columns, parent_columns)
            if reason is not None:
                left_out.append((plan.expose_id, name, reason))
            else:
                counted.append(
                    Relationship(plan.expose_id, name, str(target), child_columns, parent_columns)
                )
    return counted, left_out


def _column_names(value: Any) -> Tuple[str, ...]:
    """The column names a relationship lists; none unless *value* is a list."""
    return tuple(str(each) for each in value) if isinstance(value, list) else ()


def _keys_unread(
    child: ExposePlan,
    parent: ExposePlan,
    child_columns: Sequence[str],
    parent_columns: Sequence[str],
) -> Optional[str]:
    """Why two keys cannot be matched as read, or ``None`` when every pair can be."""
    child_domains = {column.name: column.domain for column in child.columns}
    parent_domains = {column.name: column.domain for column in parent.columns}
    for mine, theirs in zip(child_columns, parent_columns, strict=True):
        if mine not in child_domains or theirs not in parent_domains:
            return f"key column {mine!r} or {theirs!r} is not one fluid profile reads"
        if child_domains[mine] != parent_domains[theirs]:
            return f"key columns {mine!r} and {theirs!r} are read in different domains"
    return None


def _key_columns(relationships: Sequence[Relationship]) -> Dict[str, FrozenSet[str]]:
    """The foreign-key columns of each child: their ``distinctCount`` is withheld.

    A foreign key's distinct values are the parents that have a child, so beside the
    parent's row count it would give the parents with none, a cell of the histogram.
    """
    found: Dict[str, Set[str]] = {}
    for each in relationships:
        found.setdefault(each.child, set()).update(each.child_columns)
    return {child: frozenset(columns) for child, columns in found.items()}


def _relationship_keys(relationships: Sequence[Relationship]) -> Dict[str, FrozenSet[str]]:
    """The key columns of every counted relationship, both sides, by expose.

    They take no default statistic: a key's own histogram or quantile counts each key's
    rows, and the child key's rows are each parent's children, so beside children per
    parent it would give a parent's count back. Their children per parent describe them.
    """
    found: Dict[str, Set[str]] = {}
    for each in relationships:
        found.setdefault(each.child, set()).update(each.child_columns)
        found.setdefault(each.parent, set()).update(each.parent_columns)
    return {expose: frozenset(columns) for expose, columns in found.items()}


def _edge_counted(declared: Optional[DeclaredBlock]) -> FrozenSet[str]:
    """The columns a statistic counts records at the edges of, or groups by."""
    if declared is None:
        return frozenset()
    found = set()
    for statistic in declared.statistics:
        if statistic.kind in _EDGE_KINDS:
            found.add(statistic.column)
        if statistic.group_by is not None:
            found.add(statistic.group_by)
    return frozenset(found)


def _keys_not_counted_at_edges(
    relationships: Sequence[Relationship],
    left_out: Sequence[Tuple[str, str, str]],
    edges: Mapping[str, FrozenSet[str]],
) -> Tuple[List[Relationship], List[Tuple[str, str, str]]]:
    """*relationships* less any whose key a statistic still counts at its edges.

    The default blocks leave every key out, so none should be; one that is would give
    each parent's children beside the histogram, and is named and left out instead.
    """
    kept: List[Relationship] = []
    out = list(left_out)
    for each in relationships:
        if set(each.child_columns) & edges.get(each.child, frozenset()) or set(
            each.parent_columns
        ) & edges.get(each.parent, frozenset()):
            out.append((each.child, each.name, "a key column is counted at its edges"))
        else:
            kept.append(each)
    return kept, out


def _children_per_parent(
    relationship: Relationship,
    tables: Mapping[str, Tuple[ExposePlan, sources.SourceColumns]],
    backend: StatisticsBackend,
    rule: disclosure.DisclosureRule,
    tallies: Tuple["Counter[str]", "Counter[str]"],
    fractional_bits: int,
    groupings: Sequence[Tuple[str, Grouping]] = (),
) -> Tuple[Dict[str, Any], RelationshipResult]:
    """The ``relationships[]`` entry of one relationship: a histogram with the parent as unit.

    Each parent row counts once, by the child rows whose key equals its own, compared as
    the registry reads both (a parent whose key is null, like a child's null key, matches
    none). The counts go through the output check for counts at edges in a call of their
    own, as ``check_edge_counts`` documents for a relationship's count of children, and
    are spelled as a column's histogram is.

    A request's *groupings* (request format 1-2-0, PROFILE-SPEC format 1-4-0) are the parent
    columns the same histogram is asked grouped by: each parent counts in the group of its
    value, and a parent whose value is null in none. Every statistic of the relationship goes
    through the one call, so the output check sees the partition the groups share, and the
    audit of withheld cells runs group by group, on the parents and children of each.
    """
    parents = _keys(tables[relationship.parent], relationship.parent_columns, backend)
    children = Counter(
        key
        for key in _keys(tables[relationship.child], relationship.child_columns, backend)
        if None not in key
    )
    counts = [0 if None in key else children.get(key, 0) for key in parents]
    encode = backend.encoder(fractional_bits)
    statistic = DeclaredStatistic(
        identifier="marginalHistogram/1",
        kind="marginalHistogram",
        column=f"{relationship.child}.{relationship.name}",
        group_by=None,
        shape={"bins": [str(edge) for edge in CHILDREN_EDGES]},
        columns=(),
        read_as={},
        edges=tuple(encode(Fraction(edge)) for edge in CHILDREN_EDGES),
    )
    formed: List[Tuple[DeclaredStatistic, MeasuredStatistic]] = [
        (
            statistic,
            MeasuredStatistic(
                groups=(backend.count_histogram(counts, CHILDREN_EDGES, fractional_bits),)
            ),
        )
    ]
    members: List[Dict[str, List[int]]] = [{}]
    for name, grouping in groupings:
        formed.append(
            _grouped_children(statistic, name, grouping, counts, backend, fractional_bits)
        )
        members.append(_members(grouping))
    try:
        decisions = backend.check_edge_counts(
            [_edge_statistic(each, measured, rule) for each, measured in formed],
            total=_column_total(*formed[0]),
            minimum_cell=rule.minimum_cell,
            zeros_disclosive=rule.zeros_disclosive,
            round_count=lambda count: disclosure.round_count(count, rule),
        )
    except ValueError as exc:
        raise ProfileError(
            "profile_edge_counts_refused",
            f"the children per parent of {statistic.column} cannot describe one set of "
            f"parents: {exc}",
            {"written": "nothing", "next": "report this: the output check refused its input"},
        ) from None
    childless_at = [position for position, key in enumerate(parents) if None in key]
    entries: List[Dict[str, Any]] = []
    for (each, measured), decision, held in zip(formed, decisions, members, strict=True):
        if each.grouping is None:
            decision = _audited_children(
                decision, measured.groups[0], sum(counts), len(childless_at), rule
            )
        else:
            for group in measured.groups:
                at = held[group.key]
                decision = _audited_children(
                    decision,
                    group,
                    sum(counts[position] for position in at),
                    sum(1 for position in at if None in parents[position]),
                    rule,
                )
        entries.append(
            _declared_entry(
                each, measured, rule, tallies, fractional_bits, decision, records=len(parents)
            )
        )
    (group,) = entries[0]["groups"]
    published = (
        sum(1 for count in group["binCounts"] if count is not None)
        if group["status"] == disclosure.FREE
        else 0
    )
    document_entry = {
        "parent": {"table": relationship.parent, "columns": list(relationship.parent_columns)},
        "child": {"table": relationship.child, "columns": list(relationship.child_columns)},
        "statistics": entries,
    }
    return document_entry, RelationshipResult(
        relationship, published=published, cells=len(CHILDREN_EDGES) - 1
    )


def _members(grouping: Grouping) -> Dict[str, List[int]]:
    """The positions of the units in each group of *grouping*."""
    held: Dict[str, List[int]] = {key: [] for key in grouping.keys}
    for position, key in enumerate(grouping.assigned):
        if key is not None:
            held[key].append(position)
    return held


def _grouped_children(
    base: DeclaredStatistic,
    name: str,
    grouping: Grouping,
    counts: Sequence[int],
    backend: StatisticsBackend,
    fractional_bits: int,
) -> Tuple[DeclaredStatistic, MeasuredStatistic]:
    """Children per parent grouped by the parent column *name*, one group for each public key.

    The parents whose value is null are measured as one more group, never written.
    """
    held = _members(grouping)
    groups = tuple(
        backend.count_histogram(
            [counts[at] for at in held[key]], CHILDREN_EDGES, fractional_bits, key
        )
        for key in grouping.keys
    )
    outside = [counts[position] for position, key in enumerate(grouping.assigned) if key is None]
    left_out = (
        backend.count_histogram(outside, CHILDREN_EDGES, fractional_bits) if outside else None
    )
    return replace(base, group_by=name, grouping=grouping), MeasuredStatistic(groups, left_out)


#: Why the audit of children per parent withholds a cell the output check let through.
_CHILDREN_PINNED = "the parents' and the children's counts would give a withheld cell back"


def _audited_children(
    decision: EdgeDecision,
    group: MeasuredGroup,
    children: int,
    childless: int,
    rule: disclosure.DisclosureRule,
) -> EdgeDecision:
    """*decision*, with more cells withheld until no withheld cell can be worked out.

    ``check_edge_counts`` sees the parents' cells and their total. A reader can know more:
    the other tables over the same parents (an enum's level histogram, another
    relationship's cells) bound their number, and the children's own counts bound the
    children they hold, each child row having at most one parent. So the audit takes the
    strongest reader, who knows the parents' number, their *children* and the *childless*
    ones whose key is null exactly (:func:`disclosure.pinned_children_cells`). While it
    can pin a withheld cell, the published cell of the highest lower edge is withheld
    too, down to every cell, which withholds the histogram whole. It reads counts the
    profiler formed, never a record's value, and writes only which cells are withheld.
    """
    if decision.group_status[group.key] != disclosure.FREE:
        return decision
    lower = CHILDREN_EDGES[:-1]
    statuses = list(decision.cell_status[group.key])
    reasons = list(decision.cell_reason[group.key])
    while any(status == disclosure.FREE for status in statuses):
        published = [status == disclosure.FREE for status in statuses]
        if not disclosure.pinned_children_cells(
            lower, group.cells, published, children, rule, childless
        ):
            break
        heaviest = max(
            (index for index, shown in enumerate(published) if shown),
            key=lambda index: lower[index],
        )
        statuses[heaviest] = disclosure.WITHHELD
        reasons[heaviest] = _CHILDREN_PINNED
    return replace(
        decision,
        cell_status={**decision.cell_status, group.key: tuple(statuses)},
        cell_reason={**decision.cell_reason, group.key: tuple(reasons)},
    )


def _keys(
    table: Tuple[ExposePlan, sources.SourceColumns],
    names: Sequence[str],
    backend: StatisticsBackend,
) -> List[Tuple[Any, ...]]:
    """Each row's key over *names*, every part read as the registry reads its column."""
    plan, read = table
    domains = {column.name: column.domain for column in plan.columns}
    columns = [
        [
            backend.read_value(domains[name], value, column=name, row_index=index)
            for index, value in enumerate(read.values[name])
        ]
        for name in names
    ]
    return list(zip(*columns, strict=True)) if columns else []


def _default_block(
    contract: Mapping[str, Any],
    plan: ExposePlan,
    backend: StatisticsBackend,
    fractional_bits: int,
    keys: FrozenSet[str] = frozenset(),
    spans: Optional[Mapping[str, Sequence[str]]] = None,
) -> Tuple[Optional[DeclaredBlock], Tuple[Tuple[str, str], ...]]:
    """The block the profiler declares for *plan* from the contract alone, read by the registry.

    ``(None, ...)`` when the contract declares nothing to answer. The block is the
    profiler's own, so a grammar refusal of it is a defect, and says so. The *keys* of
    counted relationships take no statistic in it (:func:`_relationship_keys`). *spans* are
    the bins of the date spans the records gave (:func:`_date_spans`), which the block holds
    only once they have been read.
    """
    expose = next(
        item for item in iter_exposes(dict(contract)) if item.get("exposeId") == plan.expose_id
    )
    columns = [column for column in plan.columns if column.name not in keys]
    held = tuple(
        (column.name, "a key of a counted relationship, which its children per parent describe")
        for column in plan.columns
        if column.name in keys
    )
    planned = defaults.default_plan(expose, columns, backend.enum_levels, fractional_bits, spans)
    if planned.block is None:
        return None, planned.uncovered + held
    extended = defaults.with_timestamp_histograms(planned)
    try:
        declared = backend.read_block(extended, contract, plan.expose_id)
    except BlockRefused as exc:
        raise ProfileError(
            "profile_default_block_refused",
            f"the statistics fluid profile declares for exposes[{plan.expose_id}] are refused "
            f"by flux-stats' fidelity grammar: {exc}",
            {
                "read": "nothing: no record was read",
                "next": "report this with your forge-cli and flux-stats versions",
            },
        ) from None
    _check_declared_columns(declared, plan)
    # The histograms of the timestamp columns are the public bins of a grouping, not statistics
    # the profile writes.
    stamps = frozenset(planned.timestamps) - frozenset(planned.spanned)
    own = [
        each
        for each in declared.statistics
        if not (each.kind == "marginalHistogram" and each.column in stamps)
    ]
    block = replace(
        declared,
        statistics=tuple(own),
        structure=tuple(each for each in declared.statistics if each not in own),
        every_declared_group=True,
        declared_ranges=planned.ranges,
    )
    return block, planned.uncovered + held


def _grouping_columns(
    request: Optional[Request], relationships: Sequence[Relationship]
) -> Dict[str, Set[str]]:
    """The columns of each table another statistic of a request groups by or groups.

    A pair statistic of ``cliqueColumns`` groups each of its two columns by the other, a
    relationship's ``groupBy`` groups its parent's column, and a ``linked`` column is grouped
    by a parent column. Their groups are counted on the column's ladder bins.
    """
    found: Dict[str, Set[str]] = {}
    if request is None:
        return found
    for table in request.tables:
        if table.clique_columns:
            found.setdefault(table.expose_id, set()).update(table.clique_columns)
    for each in relationships:
        found.setdefault(each.parent, set()).update(each.group_by)
        for column, parent_column in each.linked:
            found.setdefault(each.child, set()).add(column)
            found.setdefault(each.parent, set()).add(parent_column)
    return found


_SPAN_GROUPED = "a statistic of the profile groups by it or is grouped by it"
_LADDER_STAYS = "; its quartiles stay on the type's ladder"


def _date_spans(
    contract: Mapping[str, Any],
    plans: Sequence[ExposePlan],
    reads: Sequence[sources.SourceColumns],
    blocks: Mapping[str, BundleBlock],
    keys: Mapping[str, FrozenSet[str]],
    grouping: Mapping[str, Set[str]],
    asked: Mapping[str, RequestTable],
    backend: StatisticsBackend,
    rule: disclosure.DisclosureRule,
) -> Tuple[Dict[str, Dict[str, Tuple[str, ...]]], Dict[str, List[Tuple[str, str]]]]:
    """The date spans of each table's records, and why a column has none.

    A date or timestamp column with no declared range gets a histogram on the calendar
    months or years its records span (:func:`defaults.date_span`) in place of the ladder's
    quartiles. The span depends on the records, so it is decided from the one read of each
    table, before anything is measured. A column another statistic groups by or groups keeps
    the ladder: its groups on ladder bins beside month cells would difference. A key column of
    a counted relationship holds no default statistic at all. With a request, only a column
    whose ask lists ``marginalHistogram/1`` is considered: a quantile alone stays on the ladder.
    """
    spans: Dict[str, Dict[str, Tuple[str, ...]]] = {}
    no_span: Dict[str, List[Tuple[str, str]]] = {}
    for plan, read in zip(plans, reads, strict=True):
        if plan.expose_id in blocks:
            continue
        expose = next(
            item for item in iter_exposes(dict(contract)) if item.get("exposeId") == plan.expose_id
        )
        held = keys.get(plan.expose_id, frozenset())
        columns = [column for column in plan.columns if column.name not in held]
        table = asked.get(plan.expose_id)
        for name in defaults.ladder_timestamps(expose, columns):
            # The quartiles stay on the ladder; a request that did not ask them has none to name.
            tail = _LADDER_STAYS
            if table is not None:
                ask = table.column(name)
                if ask is None or "marginalHistogram/1" not in ask.statistics:
                    continue  # a quantile alone stays on the type's ladder, as without a span
                tail = _LADDER_STAYS if "quantile/2" in ask.statistics else ""
            if name in grouping.get(plan.expose_id, ()):
                no_span.setdefault(plan.expose_id, []).append((name, _SPAN_GROUPED + tail))
                continue
            try:
                milliseconds = [
                    backend.read_value("timestamp", value, column=name, row_index=index)
                    for index, value in enumerate(read.values[name])
                    if value is not None
                ]
            except ReadRefused:  # measuring the column refuses it, naming the row
                continue
            edges, reason = defaults.date_span(milliseconds, rule.minimum_cell)
            if edges is not None:
                spans.setdefault(plan.expose_id, {})[name] = edges
            elif reason is not None:
                no_span.setdefault(plan.expose_id, []).append((name, reason + tail))
    return spans, no_span


def _refuse_repeated(exposes: Sequence[ExposeSource]) -> None:
    """Refuse an expose named twice: one document holds each table once."""
    seen = set()
    for each in exposes:
        if each.expose_id in seen:
            raise ProfileError(
                "profile_expose_repeated",
                f"exposes[{each.expose_id}] is named more than once",
                {"read": "nothing: no record was read", "next": "name each expose once"},
            )
        seen.add(each.expose_id)


def _plan_with_columns(contract: Mapping[str, Any], expose_id: str) -> ExposePlan:
    """:func:`plan_expose`, refused when it leaves no column to profile."""
    plan = plan_expose(contract, expose_id)
    if not plan.columns:
        raise ProfileError(
            "profile_no_column_to_profile",
            f"no column of exposes[{expose_id}] has a type fluid profile reads",
            {"not profiled": "; ".join(f"{name}: {reason}" for name, reason in plan.skipped)},
        )
    return plan


def _one_grid(
    grids: Mapping[str, int], blocks: Mapping[str, BundleBlock], requested: Optional[int]
) -> int:
    """The one grid of the document: the blocks' (they must agree), else *requested* or 16.

    :func:`_declared_block` has already refused a *requested* grid a block disagrees with.
    """
    if not grids:
        return DEFAULT_FRACTIONAL_BITS if requested is None else requested
    first, *others = grids.items()
    for expose_id, grid in others:
        if grid != first[1]:
            raise ProfileError(
                "profile_fractional_bits_conflict",
                f"the fidelity blocks of exposes[{first[0]}] ({blocks[first[0]].simulation}) "
                f"and exposes[{expose_id}] ({blocks[expose_id].simulation}) declare "
                f"encoding.fractionalBits {first[1]} and {grid}",
                {
                    "read": "nothing: no record was read",
                    "next": "one profile document has one grid: declare the same in both blocks",
                },
            )
    return first[1]


def _profile_table(
    plan: ExposePlan,
    read: sources.SourceColumns,
    declared: Optional[DeclaredBlock],
    backend: StatisticsBackend,
    rule: disclosure.DisclosureRule,
    tallies: Tuple["Counter[str]", "Counter[str]"],
    fractional_bits: int,
    keys: FrozenSet[str] = frozenset(),
    asked: Optional[RequestTable] = None,
    grouped_by: FrozenSet[str] = frozenset(),
) -> Tuple[Dict[str, Any], TableResult]:
    """One table of the document, from one read of its records, and its summary.

    *keys* are the table's foreign-key columns whose children per parent are counted, and
    *grouped_by* its columns a relationship's statistics are grouped by: the sizes of their
    groups are published, so each is withheld what a level or a binned number is.
    """
    tally, cell_tally = tallies
    domains = {column.name: column.domain for column in plan.columns}
    # A foreign key's own values are each parent's children, key by key: its sum, extremes
    # and quantiles describe them as a binned number's do its bins, so it withholds them too.
    binned = (
        _binned_numbers(declared, plan)
        | keys
        | {name for name in grouped_by if domains.get(name) == NUMBER}
    )
    replaced = _ungrouped_means(declared) | binned
    counted = (
        _counted_levels(declared)
        | keys
        | {name for name in grouped_by if domains.get(name) != NUMBER}
    )
    columns_doc = {
        column.name: _column(
            column,
            read.values[column.name],
            read.row_count,
            backend,
            rule,
            tally,
            fractional_bits,
            column.name in replaced,
            (frozenset({"distinctCount"}) if column.name in counted else frozenset())
            | (_BINNED_WITHHELD if column.name in binned else frozenset()),
            None if asked is None else asked.column(column.name),
        )
        for column in plan.columns
    }
    left_out: List[Tuple[str, str]] = list(declared.left_out) if declared is not None else []
    if declared is not None:
        left_out.extend(
            _declare_statistics(
                declared, read, columns_doc, backend, rule, (tally, cell_tally), fractional_bits
            )
        )
    table_doc: Dict[str, Any] = {"columns": columns_doc}
    row_count = (
        _row_count(read.row_count, rule, tally) if asked is None or asked.row_count else None
    )
    if row_count is not None:
        table_doc["rowCount"] = row_count
    all_published, none_published, some_published = _statistic_summaries(columns_doc)
    return table_doc, TableResult(
        plan=plan,
        statistics=all_published,
        withheld_statistics=none_published,
        partly_withheld_statistics=some_published,
        left_out=tuple(left_out),
    )


def _declared_block(
    bundle: Optional[BundleBlock],
    contract: Mapping[str, Any],
    expose_id: str,
    plan: ExposePlan,
    backend: StatisticsBackend,
    fractional_bits: Optional[int],
) -> Tuple[Optional[DeclaredBlock], int]:
    """The fidelity block *bundle* holds for the expose, and the grid the profile is formed on.

    Without a bundle there is no block, and the grid is *fractional_bits*, 16 unless
    given. With one, the registry's grammar reads the block and the grid is the
    block's: a *fractional_bits* that differs is refused
    (``profile_fractional_bits_conflict``), and so is a block the grammar refuses or
    one that reads a column this profile cannot, before any record is read.
    """
    if bundle is None:
        return None, DEFAULT_FRACTIONAL_BITS if fractional_bits is None else fractional_bits
    try:
        declared = backend.read_block(bundle.raw, contract, expose_id)
    except BlockRefused as exc:
        raise ProfileError(
            "profile_bundle_block_refused",
            f"the fidelity block in {bundle.simulation} is refused: {exc}",
            {
                "read": "nothing: no record was read",
                "next": "fix the block as FLUX's fidelity grammar spells it, then profile",
            },
        ) from None
    if fractional_bits is not None and fractional_bits != declared.fractional_bits:
        raise ProfileError(
            "profile_fractional_bits_conflict",
            f"--fractional-bits {fractional_bits} differs from the {bundle.simulation} "
            f"fidelity block's encoding.fractionalBits {declared.fractional_bits}",
            {
                "read": "nothing: no record was read",
                "next": "drop --fractional-bits, or declare the same grid in the block",
            },
        )
    _check_declared_columns(declared, plan)
    return declared, declared.fractional_bits


#: The metrics withheld beside a histogram the contract's own block declares on a number
#: column's declared range (``_binned_numbers``), whatever their values.
_BINNED_WITHHELD = frozenset({"sum", "min", "max", "quantiles", "distinctCount"})


def _binned_numbers(declared: Optional[DeclaredBlock], plan: ExposePlan) -> FrozenSet[str]:
    """The number columns the contract's own block gives a histogram over a declared range.

    The bins of a declared range are narrow, often one value each, so an exact ``sum`` is
    the bins' counts weighed by their values, and a published ``min``, ``max``,
    ``quantiles`` value or ``distinctCount`` places records in them: beside the cells the
    check publishes, each gives a withheld one back. So those metrics are withheld under
    ``D`` and no ``mean/1`` (the sum on the grid) is formed. ``tests/profiler/
    test_disclosure_oracle.py`` found it, and a quantile on a type's ladder, whose bins are
    wide, keeps them all.
    """
    if declared is None or not declared.every_declared_group:
        return frozenset()
    numbers = {column.name for column in plan.columns if column.domain == NUMBER}
    found = {
        each.column
        for each in declared.statistics
        if each.column in numbers and each.kind == "marginalHistogram"
    }
    # A number the statistics are grouped by the bins of has its bins' counts published too.
    found.update(
        each.group_by
        for each in declared.statistics
        if each.grouping is not None
        and each.grouping.form == "groupByBins"
        and each.group_by is not None
        and each.group_by in numbers
    )
    return frozenset(found)


def _counted_levels(declared: Optional[DeclaredBlock]) -> FrozenSet[str]:
    """The columns whose levels a level histogram counts, or a grouped statistic groups by.

    Their ``distinctCount`` is withheld: the number of levels that hold a record says how
    many of the histogram's withheld cells, or of its groups, hold none, and beside the
    cells the check lets through it can pin a cell of one record or of none (found by
    ``tests/profiler/test_disclosure_oracle.py``).
    """
    if declared is None:
        return frozenset()
    found = set()
    for statistic in declared.statistics:
        if statistic.kind == "levelHistogram":
            found.add(statistic.column)
        if statistic.group_by is not None:
            found.add(statistic.group_by)
    return frozenset(found)


def _ungrouped_means(declared: Optional[DeclaredBlock]) -> FrozenSet[str]:
    """The columns whose ungrouped mean the block declares: their fixed ``mean/1`` is the block's."""
    if declared is None:
        return frozenset()
    return frozenset(
        each.column for each in declared.statistics if each.kind == "mean" and each.group_by is None
    )


def _check_declared_columns(declared: DeclaredBlock, plan: ExposePlan) -> None:
    """Refuse a declared column this profile cannot read as the declaration reads it.

    A column the contract types with no profile domain, or in a domain other than
    the one the declaration reads it in (``support`` and ``levelHistogram`` read
    a category, a ``mean``, ``marginalHistogram`` and ``quantile`` a number or a
    timestamp, as the block's ``range`` says), would be formed over values the
    check reads another way.
    """
    domains = {column.name: column.domain for column in plan.columns}
    skipped = dict(plan.skipped)
    for statistic in declared.statistics:
        for name in statistic.columns:
            if name not in domains:
                reason = skipped.get(name, "the contract does not declare it")
                raise ProfileError(
                    "profile_bundle_column_not_profiled",
                    f"the fidelity block reads column {name!r} ({statistic.description}), "
                    f"which fluid profile cannot read: {reason}",
                    {"next": "declare the statistic on a column of a type fluid profile reads"},
                )
            if domains[name] != statistic.read_as[name]:
                raise ProfileError(
                    "profile_bundle_column_domain",
                    f"{statistic.description} reads column {name!r} as a "
                    f"{statistic.read_as[name]}, and the contract types it as a {domains[name]}",
                    {"next": "make the block and the contract agree on the column"},
                )


def _declare_statistics(
    declared: DeclaredBlock,
    read: sources.SourceColumns,
    columns_doc: Dict[str, Dict[str, Any]],
    backend: StatisticsBackend,
    rule: disclosure.DisclosureRule,
    tallies: Tuple["Counter[str]", "Counter[str]"],
    fractional_bits: int,
) -> List[Tuple[str, str]]:
    """Form every declared statistic and add it to its column; return those left out.

    The rows of each statistic are built from the one read of the table. A
    statistic no record forms a group of (a grouped one over an empty table) has
    nothing to write, and is returned with the reason. Every statistic is measured
    before any is checked, since what a histogram or a quantile may publish depends on
    the column's other statistics (:func:`_edge_decisions`). *tallies*
    count the values and the histogram cells the checks withhold, by the check.
    """
    _check_declared_ranges(declared, read, backend)
    rows: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
    left_out: List[Tuple[str, str]] = []
    formed: List[Tuple[DeclaredStatistic, MeasuredStatistic]] = []
    for statistic in declared.statistics:
        if statistic.columns not in rows:
            names = statistic.columns
            rows[names] = [
                dict(zip(names, record, strict=True))
                for record in zip(*(read.values[name] for name in names), strict=True)
            ]
        try:
            if statistic.grouping is not None:
                measured = backend.measure_keyed(
                    declared,
                    statistic,
                    rows[statistic.columns],
                    statistic.grouping.assigned,
                    statistic.grouping.keys,
                )
            else:
                measured = backend.measure(declared, statistic, rows[statistic.columns])
        except MeasureRefused as exc:
            if declared.every_declared_group:  # the block the contract's declarations give
                raise ProfileError(
                    "profile_record_outside_declared_domain",
                    f"{statistic.description}: {exc}",
                    {"written": "nothing", "next": _OUTSIDE_DECLARED_DOMAIN},
                ) from None
            raise ProfileError(
                "profile_bundle_record_refused",
                f"{statistic.description}: {exc}",
                {
                    "written": "nothing",
                    "next": "the data and the fidelity block disagree: fix either, then profile",
                },
            ) from None
        if not measured.groups:
            left_out.append((statistic.description, "no record forms a group of it"))
            continue
        formed.append((statistic, measured))
    decided = _edge_decisions(formed, backend, rule)
    for position, (statistic, measured) in enumerate(formed):
        records = None
        if statistic.group_by is not None:
            records = sum(1 for value in read.values[statistic.column] if value is not None)
        entry = _declared_entry(
            statistic,
            measured,
            rule,
            tallies,
            fractional_bits,
            decided.get(position),
            records=records,
        )
        columns_doc[statistic.column].setdefault("statistics", []).append(entry)
    return left_out


#: What to do about a record outside what the contract declares for its column.
_OUTSIDE_DECLARED_DOMAIN = (
    "the records and the contract disagree: fix the records, or the contract's column "
    "type, enum levels or validationRules range, then profile"
)


def _check_declared_ranges(
    declared: DeclaredBlock, read: sources.SourceColumns, backend: StatisticsBackend
) -> None:
    """Refuse a value outside the range its column's validationRules declare, exactly.

    A histogram's bins over the range are rounded outward to the grid, which can be
    coarser than the column's own scale, so the registry, which checks a value against
    the bins, would count one just past a bound in an end bin. The value is never shown.
    """
    for name, declared_range in declared.declared_ranges.items():
        for index, value in enumerate(read.values[name]):
            exact = backend.read_value("number", value, column=name, row_index=index)
            if exact is not None and not declared_range.holds(exact):
                raise ProfileError(
                    "profile_record_outside_declared_domain",
                    f"column {name!r} row {index}: value outside the range its "
                    "validationRules declare",
                    {"written": "nothing", "next": _OUTSIDE_DECLARED_DOMAIN},
                )


#: The declared statistics that count records between edges of their column (FLUX ADR
#: 0012), in the order a column's call to the output check lists them.
_EDGE_KINDS = ("marginalHistogram", "levelHistogram", "quantile")


def _code(reason: str) -> str:
    """The ``CL_CONF_STATUS`` code the summary tallies a withheld edge count under.

    ``A`` for a small group or cell and for one the rounding withholds, ``D`` for one
    withheld so that another cannot be recovered: off the partition, by differencing,
    or beside rows no group holds.
    """
    return disclosure.SMALL_COUNT if reason in _TALLIED_AS_SMALL else disclosure.SECONDARY


_TALLIED_AS_SMALL = frozenset({"small", WITHHELD_ROUNDING})


def _edge_decisions(
    formed: Sequence[Tuple[DeclaredStatistic, MeasuredStatistic]],
    backend: StatisticsBackend,
    rule: disclosure.DisclosureRule,
) -> Dict[int, EdgeDecision]:
    """What each histogram and quantile may publish, by its position in *formed*.

    The statistics that count at the edges of one column are checked in one call, in
    declaration order (FLUX ADR 0012): a statistic checked on its own is judged alone, so
    a quantile would pass beside a histogram and no count would be checked against
    another. Where a grouped one has rows no group holds that cannot be read as the block
    reads them, the check cannot see what its groups give back, and every group of every
    such statistic of the column is withheld instead.
    """
    columns: Dict[str, List[int]] = {}
    for position, (statistic, _) in enumerate(formed):
        if statistic.kind in _EDGE_KINDS:
            columns.setdefault(statistic.column, []).append(position)
    decided: Dict[int, EdgeDecision] = {}
    for column, positions in columns.items():
        if any(formed[each][1].left_out_unread for each in positions):
            for each in positions:
                decided[each] = _withheld_whole(formed[each][1], "rows no group holds are unread")
            continue
        statistics = [_edge_statistic(*formed[each], rule) for each in positions]
        try:
            decisions = backend.check_edge_counts(
                statistics,
                total=_column_total(*formed[positions[0]]),
                minimum_cell=rule.minimum_cell,
                zeros_disclosive=rule.zeros_disclosive,
                round_count=lambda count: disclosure.round_count(count, rule),
            )
        except ValueError as exc:
            raise ProfileError(
                "profile_edge_counts_refused",
                f"the counts of column {column!r} at its edges cannot describe one set of "
                f"records: {exc}",
                {"written": "nothing", "next": "report this: the output check refused its input"},
            ) from None
        decided.update(zip(positions, decisions, strict=True))
    return decided


def _column_total(statistic: DeclaredStatistic, measured: MeasuredStatistic) -> int:
    """The records a column's statistics count: its non-null values.

    An ungrouped statistic counts them in its one group, and a grouped one in its groups
    and the rows a null ``groupBy`` key leaves out.
    """
    held = sum(group.k for group in measured.groups)
    if statistic.group_by is not None and measured.left_out is not None:
        held += measured.left_out.k
    return held


def _edge_statistic(
    statistic: DeclaredStatistic, measured: MeasuredStatistic, rule: disclosure.DisclosureRule
) -> EdgeStatistic:
    """One histogram or quantile as the output check reads it, unrounded.

    What the profiler's own rounding check already withholds (a positive count that
    rounds to zero) is passed as withheld; the check decides every other withholding.
    """
    group_status = {
        group.key: WITHHELD_ROUNDING
        for group in measured.groups
        if not disclosure.check_rounding(group.k, rule).published
    }
    cell_status: Dict[str, Tuple[str, ...]] = {}
    if statistic.kind != "quantile":
        for group in measured.groups:
            statuses = tuple(
                (
                    disclosure.FREE
                    if disclosure.check_rounding(count, rule).published
                    else WITHHELD_ROUNDING
                )
                for count in group.cells
            )
            if any(status != disclosure.FREE for status in statuses):
                cell_status[group.key] = statuses
    return EdgeStatistic(
        identifier=statistic.identifier,
        edges=statistic.edges,
        levels=tuple(statistic.shape.get("levels", ())),
        groups=tuple(group.statistic for group in measured.groups),
        group_by=statistic.group_by,
        level=Fraction(statistic.shape["level"]) if statistic.kind == "quantile" else None,
        left_out=measured.left_out.statistic if measured.left_out is not None else None,
        group_status=group_status,
        cell_status=cell_status,
    )


def _withheld_whole(measured: MeasuredStatistic, reason: str) -> EdgeDecision:
    """Every group withheld, for a reason the output check was not asked about."""
    keys = [group.key for group in measured.groups]
    return EdgeDecision(
        group_status={key: disclosure.WITHHELD for key in keys},
        cell_status={},
        group_reason={key: reason for key in keys},
        cell_reason={},
        rounded={},
    )


def _declared_entry(
    statistic: DeclaredStatistic,
    measured: MeasuredStatistic,
    rule: disclosure.DisclosureRule,
    tallies: Tuple["Counter[str]", "Counter[str]"],
    fractional_bits: int,
    decision: Optional[EdgeDecision],
    records: Optional[int] = None,
) -> Dict[str, Any]:
    """One declared statistic as the document writes it: every group checked, none guessed at.

    A histogram's or a quantile's groups come from the *decision* of the output check on
    its column, and a mean's and a support's from their own checks. *tallies* count the
    groups and the histogram cells that are withheld, by the check. The ``sampleSize``
    band is of *records* where given, else of the records the groups hold. A grouped
    statistic is given its column's non-null records, as its column's ungrouped figures
    count them: its groups leave out each record whose ``groupBy`` key is null, so their
    band, beside the column's own counts, would give the number of those back.
    """
    groups = measured.groups
    if decision is not None:
        written = _edge_checked_groups(statistic, measured, rule, tallies, decision)
    else:
        written = _own_checked_groups(statistic, measured, rule, tallies[0], fractional_bits)
    held = sum(group.k for group in groups) if records is None else records
    entry: Dict[str, Any] = {
        "definition": statistic.identifier,
        "sampleSize": disclosure.sample_size(held, rule),
        "disclosure": rule.name,
        **statistic.shape,
        "groups": written,
    }
    if statistic.grouping is not None:
        entry.update(statistic.grouping.document)
    elif statistic.group_by is not None:
        entry["groupBy"] = statistic.group_by
    return entry


def _edge_checked_groups(
    statistic: DeclaredStatistic,
    measured: MeasuredStatistic,
    rule: disclosure.DisclosureRule,
    tallies: Tuple["Counter[str]", "Counter[str]"],
    decision: EdgeDecision,
) -> List[Dict[str, Any]]:
    """A histogram's or a quantile's groups, as the output check on its column decided them."""
    tally, cell_tally = tallies
    written: List[Dict[str, Any]] = []
    for group in measured.groups:
        if decision.group_status[group.key] == disclosure.FREE and not _every_cell_withheld(
            statistic, decision, group.key
        ):
            tally[disclosure.FREE] += 1
            published = _published_edge_group(statistic, group, decision, rule, cell_tally)
            written.append({**published, "status": disclosure.FREE})
        else:
            tally[_withheld_code(statistic, decision, group.key)] += 1
            written.append({"group": group.key, "status": disclosure.WITHHELD})
    return written


def _every_cell_withheld(statistic: DeclaredStatistic, decision: EdgeDecision, key: str) -> bool:
    """Whether a histogram group the check lets through holds no published cell.

    Such a group says nothing but its size, so it is withheld whole, as a group whose
    every cell is withheld always was. A quantile's group has no cells.
    """
    if statistic.kind == "quantile":
        return False
    return all(status != disclosure.FREE for status in decision.cell_status[key])


def _withheld_code(statistic: DeclaredStatistic, decision: EdgeDecision, key: str) -> str:
    """The code a withheld edge group is tallied under: its own reason, else its cells'.

    A group withheld whole because every cell is withheld counts as small (``A``) where
    one of its cells was withheld for its own size, and as secondary (``D``) otherwise.
    """
    if decision.group_status[key] != disclosure.FREE:
        return _code(decision.group_reason[key])
    codes = {
        _code(reason)
        for reason, status in zip(decision.cell_reason[key], decision.cell_status[key], strict=True)
        if status != disclosure.FREE
    }
    return disclosure.SMALL_COUNT if disclosure.SMALL_COUNT in codes else disclosure.SECONDARY


def _own_checked_groups(
    statistic: DeclaredStatistic,
    measured: MeasuredStatistic,
    rule: disclosure.DisclosureRule,
    tally: "Counter[str]",
    fractional_bits: int,
) -> List[Dict[str, Any]]:
    """A mean's or a support's groups, each by its own check, siblings withheld with a small one."""
    groups = measured.groups
    checked = [_check_group(statistic, group, rule, fractional_bits) for group in groups]
    verdicts = [verdict for verdict, _ in checked]
    if statistic.group_by is not None:
        verdicts = _suppressed(statistic, verdicts, measured, rule, fractional_bits)
    written: List[Dict[str, Any]] = []
    for group, verdict, (_, published) in zip(groups, verdicts, checked, strict=True):
        tally[verdict.status] += 1
        if verdict.published:
            written.append({**published, "status": disclosure.FREE})
        else:
            written.append({"group": group.key, "status": verdict.status})
    return written


def _published_edge_group(
    statistic: DeclaredStatistic,
    group: MeasuredGroup,
    decision: EdgeDecision,
    rule: disclosure.DisclosureRule,
    cell_tally: "Counter[str]",
) -> Dict[str, Any]:
    """A published group of a histogram or a quantile, its counts rounded.

    A quantile's group is spelled by the registry with its counts already rounded. A
    histogram's cells are rounded here, and a cell the check withholds is ``null`` with
    status ``C`` in ``cellStatus``, which is written only when some cell is withheld.
    """
    if statistic.kind == "quantile":
        return dict(decision.rounded[group.key])
    published = dict(group.document)
    published["k"] = disclosure.round_count(group.k, rule)
    statuses = decision.cell_status[group.key]
    reasons = decision.cell_reason[group.key]
    field_name = "binCounts" if statistic.kind == "marginalHistogram" else "levelCounts"
    published[field_name] = [
        disclosure.round_count(count, rule) if status == disclosure.FREE else None
        for count, status in zip(group.cells, statuses, strict=True)
    ]
    if any(status != disclosure.FREE for status in statuses):
        published["cellStatus"] = list(statuses)
        cell_tally.update(
            _code(reason)
            for reason, status in zip(reasons, statuses, strict=True)
            if status != disclosure.FREE
        )
    return published


def _suppressed(
    statistic: DeclaredStatistic,
    verdicts: List[disclosure.Verdict],
    measured: MeasuredStatistic,
    rule: disclosure.DisclosureRule,
    fractional_bits: int,
) -> List[disclosure.Verdict]:
    """A grouped mean's or support's verdicts after secondary suppression.

    The rows a ``groupBy`` null leaves out of every group are still in the column's
    ungrouped figures, so the published groups would give their total back: they
    count as one more group, checked and never written. When they fail, or cannot
    be read as the block reads them, every group that passed is withheld with ``D``.
    """
    hidden: List[disclosure.Verdict] = []
    if measured.left_out_unread:
        hidden.append(disclosure.Verdict(disclosure.SMALL_COUNT, "rows no group holds"))
    elif measured.left_out is not None:
        hidden.append(_check_group(statistic, measured.left_out, rule, fractional_bits)[0])
    return list(disclosure.suppress_secondary([*verdicts, *hidden]))[: len(verdicts)]


def _check_group(
    statistic: DeclaredStatistic,
    group: MeasuredGroup,
    rule: disclosure.DisclosureRule,
    fractional_bits: int,
) -> Tuple[disclosure.Verdict, Dict[str, Any]]:
    """A mean's or a support's group: its verdict, and its document as published when it allows.

    Every group first passes as a count of records (``k``), which is published
    rounded; then what its statistic adds is checked as that statistic's rule says.
    """
    kind = statistic.kind
    published = dict(group.document)
    published["k"] = disclosure.round_count(group.k, rule)
    if kind == "mean":
        return disclosure.check_mean(group.values, group.encoded, fractional_bits, rule), published
    verdict = disclosure.check_group_count(group.k, rule)
    if not verdict.published:
        return verdict, published
    if kind == "support":
        return disclosure.check_support(group.member_holders, rule), published
    raise AssertionError(f"no disclosure rule for declared statistic {statistic.identifier!r}")


def _row_count(rows: int, rule: disclosure.DisclosureRule, tally: "Counter[str]") -> Optional[int]:
    """The table's ``rowCount`` as published, or ``None`` when the rule withholds it."""
    verdict = disclosure.check_count(rows, rule)
    if verdict.published:
        verdict = disclosure.check_rounding(rows, rule)
    tally[verdict.status] += 1
    return disclosure.round_count(rows, rule) if verdict.published else None


def _grouped_by(statistic: Mapping[str, Any]) -> Optional[str]:
    """What a statistic is grouped by, as the report names it: a column, its bins, or a link."""
    if "groupByBins" in statistic:
        return f"{statistic['groupByBins']['column']} bins"
    if "groupByLink" in statistic:
        link = statistic["groupByLink"]
        return f"{link['parent']['table']}.{link['column']} through its link"
    group: Optional[str] = statistic.get("groupBy")
    return group


def _shape(statistic: Mapping[str, Any]) -> Optional[str]:
    """What tells two statistics of one column and definition apart in the report."""
    parts = []
    if "level" in statistic:
        parts.append(f"at level {statistic['level']}")
    if "bins" in statistic:
        bins = statistic["bins"]
        # A ladder has scores of edges: their number says which statistic it is.
        shown = ", ".join(bins) if len(bins) <= 12 else f"{len(bins) - 1} bins"
        parts.append(f"over bins {shown}" if len(bins) <= 12 else f"over {shown}")
    return " ".join(parts) or None


def _statistic_summaries(
    columns: Mapping[str, Mapping[str, Any]],
) -> Tuple[
    Tuple[NamedStatistic, ...], Tuple[NamedStatistic, ...], Tuple[PartlyNamedStatistic, ...]
]:
    """The fidelity statistics by how many of their groups were published.

    Each is named by its column, definition, groupBy and shape: first those whose every
    group was published, then those with no published group, then those with some, which
    also carry the number of groups published and the number of groups.
    """
    counted: List[PartlyNamedStatistic] = [
        (
            name,
            statistic["definition"],
            _grouped_by(statistic),
            _shape(statistic),
            sum(1 for group in statistic["groups"] if group["status"] == disclosure.FREE),
            len(statistic["groups"]),
        )
        for name, column in columns.items()
        for statistic in column.get("statistics", ())
    ]
    return (
        tuple(each[:4] for each in counted if each[4] == each[5]),
        tuple(each[:4] for each in counted if each[4] == 0),
        tuple(each for each in counted if 0 < each[4] < each[5]),
    )
