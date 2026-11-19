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
not profiled, and neither is a boolean, which flux-engine's reader refuses as a
category value.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from fluid_build.policy._common import iter_exposes
from fluid_build.profiler import disclosure, document, sources
from fluid_build.profiler._stats import (
    WITHHELD_ROUNDING,
    BlockRefused,
    DeclaredBlock,
    DeclaredStatistic,
    EdgeDecision,
    EdgeStatistic,
    MeasuredGroup,
    MeasuredStatistic,
    MeasureRefused,
    MetricDefinition,
    StatisticDefinition,
    StatisticsBackend,
)
from fluid_build.profiler.bundle import BundleBlock
from fluid_build.profiler.document import DEFAULT_FRACTIONAL_BITS

__all__ = [
    "DEFAULT_QUANTILE_LEVELS",
    "ColumnPlan",
    "ExposePlan",
    "ProfileError",
    "ProfileResult",
    "column_domain",
    "plan_expose",
    "profile_expose",
    "resolve_expose_id",
]

CATEGORY, NUMBER, TIMESTAMP = "category", "number", "timestamp"

_CATEGORY_TYPES = frozenset(
    "string text varchar varchar2 nvarchar char nchar character clob enum uuid "
    "uniqueidentifier guid".split()
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
    # flux-engine's reader reads a category value as text and refuses a boolean, and
    # how flux-stats will read one is not decided: a boolean column waits for it.
    **{
        name: "FLUX's reader has no reading for a boolean yet"
        for name in "boolean bool bit".split()
    },
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
class ProfileResult:
    document: Dict[str, Any]
    canonical: bytes
    digest: str
    plan: ExposePlan
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


def column_domain(type_name: Any) -> Tuple[Optional[str], Optional[str]]:
    """The profile domain a FLUID column type is read in, or ``None`` and the reason."""
    text = re.sub(r"\s+", " ", str(type_name or "").strip().lower())
    base = re.sub(r"\s*\(.*\)$", "", text)
    if base in _CATEGORY_TYPES:
        return CATEGORY, None
    if base in _NUMBER_TYPES:
        return NUMBER, None
    if base in _TIMESTAMP_TYPES:
        return TIMESTAMP, None
    if base in _NOT_PROFILED:
        return None, _NOT_PROFILED[base]
    return None, "the type is not one fluid profile reads"


def resolve_expose_id(contract: Mapping[str, Any], requested: Optional[str]) -> str:
    """The expose to profile: *requested*, or the contract's only expose."""
    ids = [str(item["exposeId"]) for item in iter_exposes(dict(contract)) if item.get("exposeId")]
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
            columns.append(ColumnPlan(name=name, domain=domain))
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
) -> Dict[str, Any]:
    """One column of the table: its domain, then its checked metrics and statistics.

    *declares_mean* says the fidelity block declares an ungrouped ``mean`` of this
    column: that statistic replaces the fixed ``mean/1``, which is not formed.
    """
    read = [
        backend.read_value(column.domain, value, column=column.name, row_index=index)
        for index, value in enumerate(raw)
    ]
    entry: Dict[str, Any] = {
        "domain": column.domain,
        "metrics": _column_metrics(column, read, row_count, backend, rule, tally),
    }
    statistics = (
        []
        if declares_mean
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
) -> Dict[str, Any]:
    """The checked ``metrics`` object of one column, from its values as read."""
    present = [value for value in read if value is not None]
    holders = Counter(present)
    counted = disclosure.check_count(len(present), rule)
    available: Dict[str, MetricDefinition] = {
        metric.name: metric for metric in backend.metrics() if column.domain in metric.domains
    }
    metrics: Dict[str, Any] = {}
    for name in _METRICS:
        metric = available.get(name)
        if metric is None:
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

    *fractional_bits* is the grid ``mean/1`` is formed on (16 unless given),
    checked before any record is read; the fidelity block must declare the same
    (PROFILE-SPEC §7). With a *bundle*, the profile also answers its fidelity
    block, on the block's grid (:func:`_declared_block`).
    """
    if fractional_bits is not None:
        document.check_fractional_bits(fractional_bits)
    plan = plan_expose(contract, expose_id)
    if not plan.columns:
        raise ProfileError(
            "profile_no_column_to_profile",
            f"no column of exposes[{expose_id}] has a type fluid profile reads",
            {"not profiled": "; ".join(f"{name}: {reason}" for name, reason in plan.skipped)},
        )
    declared, fractional_bits = _declared_block(
        bundle, contract, expose_id, plan, backend, fractional_bits
    )
    read = sources.read_columns(
        source, [column.name for column in plan.columns], table=table, cwd=cwd
    )
    tally: "Counter[str]" = Counter()
    cell_tally: "Counter[str]" = Counter()
    replaced = _ungrouped_means(declared)
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
    row_count = _row_count(read.row_count, rule, tally)
    if row_count is not None:
        table_doc["rowCount"] = row_count
    if producer is None:
        from fluid_build import __version__

        producer = document.producer_uri(__version__)
    built = document.build_document(
        producer=producer,
        rule=rule,
        tables={expose_id: table_doc},
        fractional_bits=fractional_bits,
    )
    canonical = document.canonical_bytes(built)
    backend.check_document(canonical.decode("utf-8"))
    published = tally.pop(disclosure.FREE, 0)
    all_published, none_published, some_published = _statistic_summaries(columns_doc)
    return ProfileResult(
        document=built,
        canonical=canonical,
        digest=document.digest_of(canonical),
        plan=plan,
        published=published,
        withheld=dict(sorted(tally.items())),
        withheld_cells=dict(sorted(cell_tally.items())),
        fractional_bits=fractional_bits,
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
            measured = backend.measure(declared, statistic, rows[statistic.columns])
        except MeasureRefused as exc:
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
        entry = _declared_entry(
            statistic, measured, rule, tallies, fractional_bits, decided.get(position)
        )
        columns_doc[statistic.column].setdefault("statistics", []).append(entry)
    return left_out


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
) -> Dict[str, Any]:
    """One declared statistic as the document writes it: every group checked, none guessed at.

    A histogram's or a quantile's groups come from the *decision* of the output check on
    its column, and a mean's and a support's from their own checks. *tallies* count the
    groups and the histogram cells that are withheld, by the check.
    """
    groups = measured.groups
    if decision is not None:
        written = _edge_checked_groups(statistic, measured, rule, tallies, decision)
    else:
        written = _own_checked_groups(statistic, measured, rule, tallies[0], fractional_bits)
    entry: Dict[str, Any] = {
        "definition": statistic.identifier,
        "sampleSize": disclosure.sample_size(sum(group.k for group in groups), rule),
        "disclosure": rule.name,
        **statistic.shape,
        "groups": written,
    }
    if statistic.group_by is not None:
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
        if decision.group_status[group.key] == disclosure.FREE:
            tally[disclosure.FREE] += 1
            published = _published_edge_group(statistic, group, decision, rule, cell_tally)
            written.append({**published, "status": disclosure.FREE})
        else:
            tally[_code(decision.group_reason[group.key])] += 1
            written.append({"group": group.key, "status": disclosure.WITHHELD})
    return written


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


def _shape(statistic: Mapping[str, Any]) -> Optional[str]:
    """What tells two statistics of one column and definition apart in the report."""
    parts = []
    if "level" in statistic:
        parts.append(f"at level {statistic['level']}")
    if "bins" in statistic:
        parts.append(f"over bins {', '.join(statistic['bins'])}")
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
            statistic.get("groupBy"),
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
