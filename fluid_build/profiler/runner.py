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
4. :mod:`fluid_build.profiler.disclosure` decides what each metric and statistic
   may publish.
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
from fluid_build.profiler._stats import MetricDefinition, StatisticDefinition, StatisticsBackend
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
#: is formed ungrouped, over a number column only: there it says no more than the
#: column's ``count`` and ``sum`` (:func:`disclosure.check_mean`), while over a
#: timestamp it would publish a sum that no metric publishes. The rest need a
#: declared shape (bin edges, levels, a quantile level or a ``groupBy``) that
#: only the Simulation's fidelity block holds.
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


@dataclass(frozen=True)
class ProfileResult:
    document: Dict[str, Any]
    canonical: bytes
    digest: str
    plan: ExposePlan
    published: int
    #: Withheld values per ``CL_CONF_STATUS`` code.
    withheld: Mapping[str, int] = field(default_factory=dict)
    #: The grid the document declares and its on-grid statistics were formed on.
    fractional_bits: int = DEFAULT_FRACTIONAL_BITS
    #: Each published fidelity statistic, as ``(column, definition)``.
    statistics: Tuple[Tuple[str, str], ...] = ()


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
) -> Dict[str, Any]:
    """One column of the table: its domain, then its checked metrics and statistics."""
    read = [
        backend.read_value(column.domain, value, column=column.name, row_index=index)
        for index, value in enumerate(raw)
    ]
    entry: Dict[str, Any] = {
        "domain": column.domain,
        "metrics": _column_metrics(column, read, row_count, backend, rule, tally),
    }
    statistics = _column_statistics(column, read, backend, rule, tally, fractional_bits)
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
        verdict = _check_statistic(identifier, present, encoded, rule)
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
    rule: disclosure.DisclosureRule,
) -> disclosure.Verdict:
    """Check one ungrouped statistic of a column's non-null values."""
    if identifier == "mean/1":
        return disclosure.check_mean(present, encoded, rule)
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
    fractional_bits: int = DEFAULT_FRACTIONAL_BITS,
) -> ProfileResult:
    """Profile *expose_id* of *contract* from the records *source* names.

    *fractional_bits* is the grid ``mean/1`` is formed on, checked before any record
    is read; the fidelity block must declare the same (PROFILE-SPEC §7).
    """
    document.check_fractional_bits(fractional_bits)
    plan = plan_expose(contract, expose_id)
    if not plan.columns:
        raise ProfileError(
            "profile_no_column_to_profile",
            f"no column of exposes[{expose_id}] has a type fluid profile reads",
            {"not profiled": "; ".join(f"{name}: {reason}" for name, reason in plan.skipped)},
        )
    read = sources.read_columns(
        source, [column.name for column in plan.columns], table=table, cwd=cwd
    )
    tally: "Counter[str]" = Counter()
    columns_doc = {
        column.name: _column(
            column, read.values[column.name], read.row_count, backend, rule, tally, fractional_bits
        )
        for column in plan.columns
    }
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
    return ProfileResult(
        document=built,
        canonical=canonical,
        digest=document.digest_of(canonical),
        plan=plan,
        published=published,
        withheld=dict(sorted(tally.items())),
        fractional_bits=fractional_bits,
        statistics=_published_statistics(columns_doc),
    )


def _row_count(rows: int, rule: disclosure.DisclosureRule, tally: "Counter[str]") -> Optional[int]:
    """The table's ``rowCount`` as published, or ``None`` when the rule withholds it."""
    verdict = disclosure.check_count(rows, rule)
    if verdict.published:
        verdict = disclosure.check_rounding(rows, rule)
    tally[verdict.status] += 1
    return disclosure.round_count(rows, rule) if verdict.published else None


def _published_statistics(columns: Mapping[str, Mapping[str, Any]]) -> Tuple[Tuple[str, str], ...]:
    """Each fidelity statistic whose every group was published, as ``(column, definition)``."""
    return tuple(
        (name, statistic["definition"])
        for name, column in columns.items()
        for statistic in column.get("statistics", ())
        if all(group["status"] == disclosure.FREE for group in statistic["groups"])
    )
