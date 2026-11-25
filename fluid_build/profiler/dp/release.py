# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A profile released under differential privacy.

:func:`plan_release` reads the contract and the request and decides everything the release
needs that does not depend on a record: the privacy unit, the columns released and the
reason for every requested item that is not, each column's public levels or bins, and the
budget. Nothing is read. :func:`encode_rows` then reads the values of the records once and
refuses what the release cannot hold, naming a column and a row and never a value.
:func:`draw` bounds what each unit contributes, chooses the cliques, counts, adds noise,
makes the tables agree and writes the document (profile format 1-2-0, 1-3-0 when it holds a
date pair, or 1-4-0 when it holds a rank correlation, a clique with a binned column or a
``cliqueSelection``; PROFILE-SPEC section 3a and 5a). :func:`build_record` and :func:`check_record` are the release record's side: what a
release needs to be drawn again inside the data owner's boundary.

The noise is drawn from a seeded randomness source recorded inside the data owner's
boundary, so the release can be re-derived there. Nothing that depends on the records
leaves outside the noise: no row count, no unit count, no count of rows dropped by the
bounding, no distinct or null count appears in the document, in a message or in the record.

The budget is a total ``rho`` of zero-concentrated differential privacy. Choosing cliques
takes a tenth of it when two or more columns may join cliques (the categorical ones, or the
columns of the request's ``cliqueColumns``, a number or timestamp column joining by its public
bins), and the rest is split equally over the parts, each share floored to 12 decimal places so
the parts never add up to more than the total. :func:`release_parts` lists the parts once, and
the document, the report and the record all read that list. A part is a count table (a clique, a
level histogram or a histogram of a binned column, or the days or the order of a date pair) or a
rank correlation of a pair of columns.

A column the contract does not declare required is released like a required one, counting its
known values only: a record with no value lands in no cell, so a unit still changes at most
``m_T`` cells by one each. Its table has a share of its own and is noised on its own: it joins no
clique, no selection and no consistency step (cliques count a record only where every column is
known, and tables that share a column must agree, so they keep the required columns). A date pair
the request asks is two such tables over the rows where both dates are known: the days from start
to end on the whole public ladder :data:`~fluid_build.profiler.dates.DAY_LADDER`, never trimmed,
and the order over ``after``, ``before`` and ``same``. No null count, no share of nulls and no row
count is written: the format has no noisy null count, and a share formed from two noisy totals
would be too noisy to use.

A count table's noise is a discrete Gaussian whose scale is the least ``m / 10**6`` that spends
no more than its share on a table whose squared L2 sensitivity is
:func:`~fluid_build.profiler.dp.bounding.sensitivity_squared`, in exact integers. A rank
correlation's value is released with the noise of :mod:`~fluid_build.profiler.dp.rank_noise`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import math
import re
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, Decimal, localcontext
from fractions import Fraction
from typing import (
    AbstractSet,
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from fluid_build.policy._common import iter_exposes
from fluid_build.profiler import dates, defaults, document, runner, sources
from fluid_build.profiler._stats import Request, StatisticsBackend
from fluid_build.profiler.defaults import BOOLEAN_LEVELS, DeclaredRange
from fluid_build.profiler.dp import bounding, consistency, rank_noise, selection
from fluid_build.profiler.dp.bounding import ROWS, UnitPlan
from fluid_build.profiler.dp.discrete_gaussian import sample_discrete_gaussian
from fluid_build.profiler.dp.randomness import SeededSource
from fluid_build.profiler.dp.record import ReleaseRecord
from fluid_build.profiler.runner import ProfileError

__all__ = [
    "DELTA_TEXT",
    "EncodedRows",
    "DEFAULT_ROW_FLOOR",
    "MECHANISM",
    "LINK",
    "NO_CELL",
    "PAIR_DAYS",
    "PAIR_ORDER",
    "PAIRS",
    "Part",
    "PublicStructures",
    "RANK",
    "RELATION",
    "RANK_DEFINITIONS",
    "RULE_NAME",
    "Release",
    "ReleaseRefused",
    "ReleasePlan",
    "ReleasedColumn",
    "ReleasedTable",
    "SCHEMA_URL_1_2_0",
    "added_units",
    "admit",
    "build_record",
    "check_record",
    "check_selection_available",
    "decimal_text",
    "draw",
    "encode_rows",
    "measure_counts",
    "epsilon_at_delta",
    "fit_prefix",
    "left_out_reason",
    "noise_label",
    "pair_label",
    "parse_rho",
    "plan_release",
    "release_parts",
    "scale_units",
    "split_budget",
]

#: Format 1-2-0 (``flux_stats.profile.SCHEMA_URL_1_2_0``; a test keeps the two equal).
SCHEMA_URL_1_2_0 = "https://agenticstiger.github.io/flux/engine/spec/profile/1-2-0/FluxProfile.json"

MECHANISM = "discreteGaussian"
#: A noisy rank correlation states ``gaussian``: the spec refuses a ``discreteGaussian`` on one.
RANK_MECHANISM = "gaussian"
RULE_NAME = "noisy"

#: The row floor of a release's rank correlations when ``--row-floor`` names none.
DEFAULT_ROW_FLOOR = 1000
RANK_DEFINITIONS = ("kendallTau/1", "spearmanRho/1")

#: Fractional digits a share is floored to, and the scale's step is ``10**-SCALE_PLACES``.
_SHARE_PLACES = 12
_SCALE_PLACES = 6
#: The digits a ``--rho`` may hold.
_MOST_RHO_DIGITS = 100
_RHO_TEXT = re.compile(r"[0-9]+(\.[0-9]+)?\Z")

#: A total rho above this draws a warning; the release still runs, at any rho above zero.
#: OpenDP's guidance: "a common rule-of-thumb is to limit epsilon to 1.0"
#: (https://docs.opendp.org/en/stable/getting-started/typical-workflow.html), and an
#: epsilon-DP release is (epsilon^2/2)-zCDP, so epsilon 1 is rho 0.5. The US Census Bureau's
#: 2020 Census products used rho 2.63 for the redistricting data
#: (https://arxiv.org/abs/2204.08986) and 4.96 and 7.70 for the two universes of the
#: demographic and housing characteristics file (https://arxiv.org/abs/2312.10863).
RHO_WARN = Fraction(1)

#: The delta of the informational conversion to (epsilon, delta).
DELTA_TEXT = "1e-6"

_NOT_NOISY = "format 1-2-0 holds no noisy form of it"
#: Why a request's workload entry is not used: a release chooses the column pairs it joins by
#: weighing them with the contract's dimensions (:func:`_pair_weights`).
WORKLOAD_UNUSED = (
    "the release weighs the column pairs it may join by the contract's dimensions, not by a "
    "workload"
)
_NOTHING_READ = "nothing: no record was read"


class ReleaseRefused(ProfileError):
    """A release that cannot be formed. ``event`` is a stable code, ``context`` its details."""

    def __init__(self, event: str, message: str, context: Optional[Dict[str, str]] = None) -> None:
        super().__init__(event, message, {"written": "nothing", **(context or {})})


def _refuse(event: str, message: str, next_step: str, read: bool = False) -> ReleaseRefused:
    context = {"next": next_step}
    if read:
        context["read"] = _NOTHING_READ
    return ReleaseRefused(event, message, context)


# ---------------------------------------------------------------------------
# Exact numbers
# ---------------------------------------------------------------------------


def parse_rho(text: str) -> Fraction:
    """``--rho``: decimal text above zero, at most 100 digits, no exponent and no sign."""
    if not isinstance(text, str) or not _RHO_TEXT.match(text):
        raise _refuse(
            "profile_release_rho_invalid",
            "--rho is a decimal number such as 0.5, written without a sign or an exponent",
            "write the total privacy loss as plain decimal text above zero",
            read=True,
        )
    if len(text.replace(".", "")) > _MOST_RHO_DIGITS:
        raise _refuse(
            "profile_release_rho_invalid",
            f"--rho holds more than {_MOST_RHO_DIGITS} digits",
            "write it with fewer digits",
            read=True,
        )
    value = Fraction(text)
    if value <= 0:
        raise _refuse(
            "profile_release_rho_invalid",
            "--rho is above zero",
            "write the total privacy loss as a number above zero",
            read=True,
        )
    return value


def rho_warning(rho: Fraction) -> Optional[str]:
    """The warning for a total privacy loss above :data:`RHO_WARN`, else None."""
    if rho <= RHO_WARN:
        return None
    return (
        f"rho {decimal_text(rho)} is above 1, a large privacy loss. OpenDP's rule of thumb "
        "keeps epsilon at most 1, which is rho 0.5 (an epsilon-DP release is "
        "(epsilon^2/2)-zCDP); the US Census Bureau's 2020 Census products used rho from "
        "about 2.6 to 7.7. The release goes ahead."
    )


def decimal_text(value: Fraction) -> str:
    """Decimal text of a terminating rational: no exponent, no trailing zero of the fraction."""
    reduced = Fraction(value)
    rest, twos, fives = reduced.denominator, 0, 0
    while rest % 2 == 0:
        rest //= 2
        twos += 1
    while rest % 5 == 0:
        rest //= 5
        fives += 1
    if rest != 1:
        raise ValueError("not a terminating decimal")
    places = max(twos, fives)
    scaled = reduced.numerator * 2 ** (places - twos) * 5 ** (places - fives)
    sign = "-" if scaled < 0 else ""
    digits = str(abs(scaled)).rjust(places + 1, "0")
    return f"{sign}{digits}" if places == 0 else f"{sign}{digits[:-places]}.{digits[-places:]}"


def _floor_places(value: Fraction, places: int) -> Fraction:
    unit = 10**places
    return Fraction(math.floor(value * unit), unit)


def scale_units(share: Fraction, sensitivity: int) -> int:
    """The least ``m`` with ``sensitivity / (2 * (m / 10**6)**2) <= share``, in exact integers."""
    if share <= 0 or sensitivity < 1:
        raise ValueError("a share and a sensitivity above zero are needed")
    return rank_noise.scale_units(share, Fraction(sensitivity))


def split_budget(total: Fraction, selects: bool, tables: int) -> Tuple[Fraction, Fraction]:
    """``(selection share, share of each table)`` of *total* over *tables* count tables.

    Choosing cliques takes a tenth when *selects*; the rest is split equally and each share is
    floored to 12 decimal places, so the parts never add up to more than the total. Refused,
    before any division by a share, when a share is zero at that precision.
    """
    spent = total / 10 if selects else Fraction(0)
    if selects:
        try:
            selection.largest_float_not_above(spent)
        except ValueError:
            raise _refuse(
                "profile_release_rho_too_small",
                f"--rho {decimal_text(total)} leaves choosing the cliques a share below the "
                "smallest positive number a float holds",
                "raise --rho, or release one categorical column only",
            ) from None
    share = _floor_places(Fraction(total - spent) / max(tables, 1), _SHARE_PLACES)
    if share <= 0:
        raise _refuse(
            "profile_release_rho_too_small",
            f"--rho {decimal_text(total)} leaves each of the count tables a share of zero at "
            f"{_SHARE_PLACES} decimal places",
            "raise --rho, or release fewer columns",
        )
    return spent, share


def epsilon_at_delta(rho: Fraction) -> Decimal:
    """``rho + 2 sqrt(rho ln(1/delta))`` at delta = 1e-6, rounded up to 3 decimal places.

    The conversion of zero-concentrated to approximate differential privacy of Bun and Steinke
    (2016), for information: the release states its loss as *rho*.
    """
    with localcontext() as context:
        context.prec = 60
        value = Decimal(rho.numerator) / Decimal(rho.denominator)
        epsilon = value + 2 * (value * Decimal(10**6).ln()).sqrt()
        return epsilon.quantize(Decimal("0.001"), rounding=ROUND_CEILING)


def noise_label(columns: Sequence[str]) -> str:
    """The label of a count table's noise stream: ``noise/`` and its columns joined by ``/``.

    It is never ``bound``, the label of the bounding priorities.
    """
    return "noise/" + "/".join(columns)


# ---------------------------------------------------------------------------
# The plan: everything that does not depend on a record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleasedColumn:
    """A column of the release table whose counts are released, and its public cells.

    A categorical column has its *levels*, sorted by code point; a number, date or timestamp
    column has its bin *edges* (exact, in milliseconds for a timestamp) and the same edges
    spelled as the document spells them.
    """

    name: str
    domain: str
    levels: Tuple[str, ...] = ()
    edges: Tuple[Fraction, ...] = ()
    edge_texts: Tuple[str, ...] = ()
    #: The exact range its validationRules declare, when its bins are cut over one.
    declared: Optional[DeclaredRange] = None
    #: The contract does not declare it required: a record may hold no value, which lands in no cell.
    nullable: bool = False

    @property
    def categorical(self) -> bool:
        return self.domain == runner.CATEGORY

    @property
    def size(self) -> int:
        return len(self.levels) if self.categorical else len(self.edges) - 1


@dataclass(frozen=True)
class ReleasePlan:
    """What a release holds and spends, decided before any record is read."""

    unit: UnitPlan
    table: str
    #: The released columns, in the contract's order.
    columns: Tuple[ReleasedColumn, ...]
    #: Every requested column of the release table that is read, with its domain, in order.
    table_columns: Tuple[Tuple[str, str], ...]
    #: The other tables of the request, each with its requested columns and their domains.
    other_tables: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...]
    #: The columns to read: the released ones and the key columns.
    read: Tuple[runner.ColumnPlan, ...]
    #: Each requested item the release does not hold, with the reason.
    not_released: Tuple[Tuple[str, str], ...]
    rho: Fraction
    #: Whether two or more categorical columns are released, so cliques are chosen.
    selects: bool
    fractional_bits: int
    #: The categorical columns a ``semantics.dimensions`` entry groups by.
    dimensions: FrozenSet[str]
    #: Columns of the request's other tables hold no record in the release.
    has_other_tables: bool = False
    #: The released columns that may join cliques, in the contract's order: the categorical ones,
    #: or with a request ``cliqueColumns`` the released columns it names (a number or timestamp
    #: column by its bins).
    eligible: Tuple[ReleasedColumn, ...] = ()
    #: The rank correlations released: ``(definition, first, second)``, in the request's order.
    rank_pairs: Tuple[Tuple[str, str, str], ...] = ()
    #: The public ``rowFloor`` of every rank correlation, when there is one.
    row_floor: Optional[int] = None
    #: The sensitivity ``Delta`` of each definition's value to one unit (rows per unit times the
    #: registry's), as ``(definition, Delta)``.
    rank_deltas: Tuple[Tuple[str, Fraction], ...] = ()
    #: How the cliques are chosen: one of :data:`selection.METHODS`.
    clique_method: str = "aim"
    #: A unit that follows its child rows changes at most this many rows of the release table
    #: (``m_T``, PROFILE-SPEC section 3a), which scales every sensitivity; ``None`` otherwise.
    reach: Optional[int] = None
    #: The share a unit that follows its child rows spends choosing this table's cliques.
    selection_rho: Optional[Fraction] = None
    #: The date pairs released, ``(start, end)``, in the request's order: both DATE columns of
    #: the table, neither a key.
    date_pairs: Tuple[Tuple[str, str], ...] = ()
    #: The columns of those pairs the contract declares required: a record with no value in one
    #: refuses the release, as in a released column.
    date_required: FrozenSet[str] = frozenset()

    @property
    def categorical(self) -> Tuple[ReleasedColumn, ...]:
        return tuple(each for each in self.columns if each.categorical)

    @property
    def ordered(self) -> Tuple[ReleasedColumn, ...]:
        return tuple(each for each in self.columns if not each.categorical)

    @property
    def sensitivity(self) -> int:
        if self.reach is not None:
            return self.reach * self.reach
        return bounding.sensitivity_squared(self.unit)

    @property
    def rows_per_unit(self) -> int:
        """The most records of the release table one unit changes."""
        if self.reach is not None:
            return self.reach
        return 1 if self.unit.mode == ROWS else self.unit.max_rows_per_unit

    @property
    def distance(self) -> int:
        """The symmetric distance between neighbouring tables, for the selection step."""
        if self.reach is not None:
            return self.reach
        return bounding.selection_distance(self.unit)

    def delta(self, definition: str) -> Fraction:
        """The sensitivity of the value of rank correlation *definition* to one unit."""
        return dict(self.rank_deltas)[definition]

    def column(self, name: str) -> ReleasedColumn:
        return next(each for each in self.columns if each.name == name)


def _schema(expose: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    block = expose.get("contract")
    entries = block.get("schema") if isinstance(block, Mapping) else None
    return {
        entry["name"]: entry
        for entry in (entries if isinstance(entries, list) else ())
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
    }


def _relationship_columns(expose: Mapping[str, Any]) -> FrozenSet[str]:
    """The ``fromColumns`` of every relationship of *expose*."""
    found = set()
    semantics = expose.get("semantics")
    entries = semantics.get("relationships") if isinstance(semantics, Mapping) else None
    for entry in entries if isinstance(entries, list) else ():
        columns = entry.get("fromColumns") if isinstance(entry, Mapping) else None
        for name in columns if isinstance(columns, list) else ():
            if isinstance(name, str):
                found.add(name)
    return frozenset(found)


@dataclass(frozen=True)
class PublicStructures:
    """The levels and bins the contract gives the columns of one table, which no record shapes."""

    planned: Any
    covered: Mapping[str, Mapping[str, Any]]
    bins_of: Mapping[str, List[str]]
    uncovered: Mapping[str, str]

    def column(
        self, column: runner.ColumnPlan, entry: Mapping[str, Any], backend: StatisticsBackend
    ) -> Union[ReleasedColumn, str]:
        """The public cells of *column*, or the reason it has none."""
        if column.domain == runner.CATEGORY:
            if column.name not in self.covered:
                return self.uncovered.get(
                    column.name, "its type declares no levels, as an enum(...) or a boolean does"
                )
            declared_type = entry.get("type")
            found = (
                BOOLEAN_LEVELS
                if defaults._is_boolean(declared_type)
                else tuple(backend.enum_levels(str(declared_type)))
            )
            return ReleasedColumn(column.name, column.domain, levels=tuple(sorted(set(found))))
        if column.name in self.bins_of:
            texts = self.bins_of[column.name]
            if column.domain == runner.TIMESTAMP:
                edges = tuple(
                    Fraction(backend.read_value("timestamp", text, column=column.name, row_index=0))
                    for text in texts
                )
            else:
                edges = tuple(Fraction(text) for text in texts)
            return ReleasedColumn(
                column.name,
                column.domain,
                edges=edges,
                edge_texts=tuple(decimal_text(edge) for edge in edges),
                declared=self.planned.ranges.get(column.name),
            )
        return self.uncovered.get(
            column.name, "it has neither a declared range nor a type that bounds it"
        )


def public_structures(
    expose: Mapping[str, Any],
    columns: Sequence[runner.ColumnPlan],
    backend: StatisticsBackend,
    fractional_bits: int,
) -> PublicStructures:
    """The public structure the contract gives *columns* of *expose*, read from the contract alone."""
    planned = defaults.default_plan(expose, columns, backend.enum_levels, fractional_bits)
    covered: Mapping[str, Mapping[str, Any]] = (
        planned.block["columns"] if planned.block is not None else {}
    )
    bins_of = defaults.public_bins(planned.block)
    return PublicStructures(planned, covered, bins_of, dict(planned.uncovered))


@dataclass(frozen=True)
class _TableColumns:
    """What the release of one table holds, decided from the contract and the request alone."""

    asked: Any
    expose: Mapping[str, Any]
    schema: Mapping[str, Mapping[str, Any]]
    expose_plan: runner.ExposePlan
    readable: Mapping[str, runner.ColumnPlan]
    skipped: Mapping[str, str]
    released: Tuple[ReleasedColumn, ...]
    why_not: Dict[str, str]
    keys: FrozenSet[str]
    eligible: Tuple[ReleasedColumn, ...]
    selects: bool
    rank_pairs: Tuple[Tuple[str, str, str], ...]
    rank_left_out: Tuple[Tuple[str, str], ...]
    #: The date pairs the table releases, the required ones among their columns, and each pair
    #: asked that it does not, and each ask to join a clique that it does not hold, with why.
    date_pairs: Tuple[Tuple[str, str], ...] = ()
    date_required: FrozenSet[str] = frozenset()
    date_left_out: Tuple[Tuple[str, str], ...] = ()
    clique_left_out: Tuple[Tuple[str, str], ...] = ()


_NOT_A_DATE = (
    "a date pair is formed over DATE columns, whose days are whole; a timestamp column's order "
    "is on the millisecond and its days are whole, which one count cannot hold together"
)


def _date_pairs(
    asked: Any,
    table: str,
    readable: Mapping[str, runner.ColumnPlan],
    skipped: Mapping[str, str],
    keys: FrozenSet[str],
    schema: Mapping[str, Mapping[str, Any]],
) -> Tuple[Tuple[Tuple[str, str], ...], FrozenSet[str], List[Tuple[str, str]]]:
    """The date pairs of *asked* the release holds, the required columns among them, and the rest.

    Decided from the contract and the request alone: a pair is released when both its columns are
    DATE columns that fluid profile reads and that are not keys. Never a value.
    """
    kept: List[Tuple[str, str]] = []
    required: set[str] = set()
    left_out: List[Tuple[str, str]] = []
    for pair in asked.date_pairs:
        what = f"date pair {pair.start} to {pair.end} of {table}"
        reason: Optional[str] = None
        for name in (pair.start, pair.end):
            column = readable.get(name)
            if column is None:
                reason = f"{name}: a column fluid profile does not read" + (
                    f" ({skipped[name]})" if name in skipped else ""
                )
            elif name in keys:
                reason = f"{name}: a key column, whose values name an entity"
            elif not (column.domain == runner.TIMESTAMP and column.date):
                reason = f"{name}: {_NOT_A_DATE}"
            if reason is not None:
                break
        if reason is not None:
            left_out.append((what, reason))
            continue
        kept.append((pair.start, pair.end))
        required.update(
            name for name in (pair.start, pair.end) if schema.get(name, {}).get("required") is True
        )
    return tuple(kept), frozenset(required), left_out


def _plan_columns(
    contract: Mapping[str, Any],
    request: Request,
    backend: StatisticsBackend,
    table: str,
    unit_keys: Sequence[str],
    *,
    nothing_ok: bool = False,
) -> _TableColumns:
    """The columns of *table* a release holds, its cliques and its rank correlations.

    *unit_keys* are the unit's key columns, which are never released. A release of one table
    refuses when no column is released; with *nothing_ok* the table holds none.
    """
    from fluid_build.profiler import request as asked_for

    asked_for.check_tables(request, contract)
    asked = request.table(table)
    if asked is None:
        raise _refuse(
            "profile_release_table_not_in_request",
            f"the request does not list the release table {table!r}",
            "release a table the request lists: "
            + ", ".join(each.expose_id for each in request.tables),
            read=True,
        )
    expose = next(each for each in iter_exposes(dict(contract)) if each.get("exposeId") == table)
    schema = _schema(expose)
    plan = runner.plan_expose(contract, table)
    readable = {column.name: column for column in plan.columns}
    skipped = dict(plan.skipped)
    keys = (
        set(unit_keys)
        | set(bounding._primary_key(table, expose))
        | set(_relationship_columns(expose))
    )
    candidates = [
        column
        for column in plan.columns
        if asked.column(column.name) is not None and column.name not in keys
    ]
    known = public_structures(expose, candidates, backend, request.fractional_bits)

    released: List[ReleasedColumn] = []
    why_not: Dict[str, str] = {}
    for column in candidates:
        entry = schema.get(column.name, {})
        ask = asked.column(column.name)
        assert ask is not None
        statistic = (
            "levelHistogram/1" if column.domain == runner.CATEGORY else "marginalHistogram/1"
        )
        if statistic not in ask.statistics:
            why_not[column.name] = f"the request does not list {statistic} for it"
        else:
            made = known.column(column, entry, backend)
            if isinstance(made, str):
                why_not[column.name] = made
            elif entry.get("required") is not True:
                released.append(replace(made, nullable=True))
            else:
                released.append(made)
    pairs, pair_required, pair_left_out = _date_pairs(
        asked, table, readable, skipped, frozenset(keys), schema
    )
    if not released and not pairs and not nothing_ok:
        raise _refuse(
            "profile_release_nothing_to_release",
            f"no requested column of {table!r} can be released: "
            + "; ".join(f"{name}: {reason}" for name, reason in sorted(why_not.items())),
            "list levelHistogram/1 for an enum column, or marginalHistogram/1 for a number, date "
            "or timestamp column that is not a key, or a date pair of two DATE columns",
            read=True,
        )
    for name in keys & set(readable):
        if asked.column(name) is not None:
            why_not.setdefault(name, "a key column: its values name an entity")

    # The cells: a column of two or more levels holds at least its size in any structure. A column
    # that may hold a null is an added part: it is fitted into the cap once the cliques are known
    # (:func:`admit`), so it does not count here.
    required = [each for each in released if not each.nullable]
    cells = sum(each.size for each in required if each.size >= 2)
    if cells > selection.MOST_RELEASE_CELLS or any(
        each.size > selection.MOST_RELEASE_CELLS for each in required
    ):
        raise _refuse(
            "profile_release_too_many_cells",
            f"the columns released hold more cells than the {selection.MOST_RELEASE_CELLS:,} "
            "a release may measure",
            "list fewer columns in the request",
            read=True,
        )
    joining = set(asked.clique_columns)
    eligible = tuple(
        each
        for each in released
        if not each.nullable and (each.name in joining if joining else each.categorical)
    )
    clique_left_out = tuple(
        (
            f"clique column {table}.{each.name}",
            "the contract does not declare it required, a clique counts a record only where "
            "every column has a value, and a table that shares a column with another must agree "
            "with it",
        )
        for each in released
        if each.nullable and each.name in joining
    )
    selects = len(eligible) >= 2
    if selects and len(eligible) > selection.MOST_SELECTION_COLUMNS:
        raise _refuse(
            "profile_release_too_many_columns",
            f"{len(eligible)} {_joiners(eligible)} are released, and choosing cliques is "
            f"offered at most {selection.MOST_SELECTION_COLUMNS}",
            f"list fewer {_joiners(eligible)} in the request",
            read=True,
        )
    for name in sorted(joining - {each.name for each in released}):
        why_not.setdefault(name, "it is not released, so it joins no clique")
    rank_pairs, rank_left_out = _rank_pairs(asked, released, why_not, skipped)
    return _TableColumns(
        asked=asked,
        expose=expose,
        schema=schema,
        expose_plan=plan,
        readable=readable,
        skipped=skipped,
        released=tuple(released),
        why_not=why_not,
        keys=frozenset(keys),
        eligible=eligible,
        selects=selects,
        rank_pairs=rank_pairs,
        rank_left_out=tuple(rank_left_out),
        date_pairs=pairs,
        date_required=pair_required,
        date_left_out=tuple(pair_left_out),
        clique_left_out=clique_left_out,
    )


def plan_release(
    contract: Mapping[str, Any],
    request: Request,
    backend: StatisticsBackend,
    *,
    unit: str,
    rho: Fraction,
    release_table: Optional[str] = None,
    unit_key: Optional[Sequence[str]] = None,
    max_rows_per_unit: Optional[int] = None,
    max_rows_per_cell: Optional[int] = None,
    row_floor: Optional[int] = None,
    clique_method: str = "aim",
) -> ReleasePlan:
    """Plan a release of one table's requested statistics. Reads no record.

    Every refusal that does not need a record is made here: the unit, the table, what is
    released, the cells and the budget.
    """
    if clique_method not in selection.METHODS:
        raise _refuse(
            "profile_release_clique_method",
            f"--clique-method is one of {', '.join(selection.METHODS)}",
            "name one of them, or leave it out for aim",
            read=True,
        )
    unit_plan = bounding.plan_unit(
        contract,
        unit,
        release_table,
        unit_key=unit_key,
        max_rows_per_unit=max_rows_per_unit,
        max_rows_per_cell=max_rows_per_cell,
    )
    table = unit_plan.release_table
    found = _plan_columns(contract, request, backend, table, unit_plan.key_columns)
    asked, skipped, released = found.asked, found.skipped, list(found.released)
    why_not, eligible, selects = found.why_not, found.eligible, found.selects
    rank_pairs, rank_left_out = found.rank_pairs, list(found.rank_left_out)
    plan, readable, expose = found.expose_plan, found.readable, found.expose
    categorical = [each for each in released if each.categorical]
    fractional_bits = request.fractional_bits
    if row_floor is not None and not rank_pairs:
        raise _refuse(
            "profile_release_row_floor_unused",
            "--row-floor is the floor of a rank correlation, and no rank correlation of "
            f"{table!r} is released",
            "leave --row-floor out, or ask for a rank correlation of two released number or "
            "timestamp columns",
            read=True,
        )
    floor = None if not rank_pairs else (DEFAULT_ROW_FLOOR if row_floor is None else row_floor)
    # Adding, removing or replacing one row of a unit can change which kept row of that unit wins,
    # so a keyed or related unit moves the value as two rows do at the least (see
    # bounding.selection_distance).
    rows_per_unit = bounding.selection_distance(unit_plan)
    deltas = tuple(
        (definition, rows_per_unit * backend.rank_sensitivity(definition, floor or 2))
        for definition in RANK_DEFINITIONS
        if any(each[0] == definition for each in rank_pairs)
    )
    # At most one part per released column and rank pair, so no part gets a share below the least
    # check.
    split_budget(rho, selects, len(released) + len(rank_pairs) + 2 * len(found.date_pairs))

    read_names = (
        {each.name for each in released}
        | set(unit_plan.key_columns)
        | set(paired_columns(found.date_pairs))
    )
    missing_keys = [name for name in unit_plan.key_columns if name not in readable]
    if missing_keys:
        raise _refuse(
            "profile_release_key_not_read",
            f"the key column {missing_keys[0]!r} of {table!r} has a type fluid profile does not "
            "read" + (f" ({skipped[missing_keys[0]]})" if missing_keys[0] in skipped else ""),
            "key the privacy unit by columns of a type fluid profile reads",
            read=True,
        )
    read = tuple(column for column in plan.columns if column.name in read_names)
    in_request = {each.name for each in asked.columns}
    table_columns = tuple(
        (column.name, column.domain) for column in plan.columns if column.name in in_request
    )
    others: List[Tuple[str, Tuple[Tuple[str, str], ...]]] = []
    for each in request.tables:
        if each.expose_id == table:
            continue
        other = {column.name for column in each.columns}
        domains = tuple(
            (column.name, column.domain)
            for column in runner.plan_expose(contract, each.expose_id).columns
            if column.name in other
        )
        others.append((each.expose_id, domains))
    return ReleasePlan(
        unit=unit_plan,
        table=table,
        columns=tuple(released),
        table_columns=table_columns,
        other_tables=tuple(each for each in others if each[1]),
        read=read,
        not_released=(
            *_not_released(request, table, frozenset(c.name for c in released), why_not, skipped),
            *rank_left_out,
            *found.date_left_out,
            *found.clique_left_out,
        ),
        rho=rho,
        selects=selects,
        fractional_bits=fractional_bits,
        dimensions=frozenset(defaults._dimension_columns(expose, [c.name for c in categorical])),
        has_other_tables=bool(others),
        eligible=eligible,
        rank_pairs=rank_pairs,
        row_floor=floor,
        rank_deltas=deltas,
        clique_method=clique_method,
        date_pairs=found.date_pairs,
        date_required=found.date_required,
    )


def _joiners(eligible: Sequence[ReleasedColumn]) -> str:
    """What to call the columns that may join cliques in a message."""
    return "categorical columns" if all(each.categorical for each in eligible) else "columns"


def _rank_pairs(
    asked: Any,
    released: Sequence[ReleasedColumn],
    why_not: Mapping[str, str],
    skipped: Mapping[str, str],
) -> Tuple[Tuple[Tuple[str, str, str], ...], List[Tuple[str, str]]]:
    """The rank correlations the release holds, and each pair asked that it does not, with why.

    A pair is released when both its columns are released number or timestamp columns: their
    bins are public and a record holds a value in each: a column that may hold a null is left
    out, since a rank correlation is formed over every record.
    """
    ordered = {each.name for each in released if not each.categorical and not each.nullable}
    nullable = {each.name for each in released if each.nullable}
    kept: List[Tuple[str, str, str]] = []
    left_out: List[Tuple[str, str]] = []
    for ask in asked.rank_correlations:
        for position, first in enumerate(ask.columns):
            for second in ask.columns[position + 1 :]:
                missing = [name for name in (first, second) if name not in ordered]
                if not missing:
                    if (ask.definition, first, second) not in kept:
                        kept.append((ask.definition, first, second))
                    continue
                reason = _rank_left_out_reason(missing[0], nullable, why_not, skipped)
                left_out.append(
                    (f"{ask.definition} of {asked.expose_id}.{first} and {second}", reason)
                )
    return tuple(kept), left_out


def _rank_left_out_reason(
    name: str,
    nullable: AbstractSet[str],
    why_not: Mapping[str, str],
    skipped: Mapping[str, str],
) -> str:
    """Why a rank correlation over the column *name* is not released."""
    reason = why_not.get(name) or skipped.get(name)
    if reason is not None:
        return reason
    if name in nullable:
        return (
            "the contract does not declare it required, and a rank correlation is "
            "formed over every record"
        )
    return "the release holds the rank correlation of released number or timestamp columns only"


def _table_not_released(
    each: Any,
    table: str,
    released: FrozenSet[str],
    why_not: Mapping[str, str],
    skipped: Mapping[str, str],
) -> List[Tuple[str, str]]:
    """Each requested item of the listed table *each* the release does not hold, with why."""
    found: List[Tuple[str, str]] = []
    if each.row_count:
        found.append((f"rowCount of {table}", _NOT_NOISY))
    for column in each.columns:
        where = f"{table}.{column.name}"
        if column.name in skipped:
            found.append((f"column {where}", f"not profiled: {skipped[column.name]}"))
            continue
        if column.metrics:
            found.append((f"{', '.join(column.metrics)} of {where}", _NOT_NOISY))
        for identifier in column.statistics:
            if column.name in released and identifier in (
                "levelHistogram/1",
                "marginalHistogram/1",
            ):
                continue
            if identifier in ("levelHistogram/1", "marginalHistogram/1"):
                reason = why_not.get(column.name, _NOT_NOISY)
            else:
                reason = _NOT_NOISY
            found.append((f"{identifier} of {where}", reason))
    return found


def _not_released(
    request: Request,
    table: str,
    released: FrozenSet[str],
    why_not: Mapping[str, str],
    skipped: Mapping[str, str],
) -> Tuple[Tuple[str, str], ...]:
    """Each requested item the release does not hold, with why. Never a value."""
    found: List[Tuple[str, str]] = []
    for each in request.tables:
        if each.expose_id != table:
            found.append(
                (
                    f"table {each.expose_id}",
                    f"the release holds the statistics of one table, {table}; this table's "
                    "listed columns appear with their domains only",
                )
            )
            found.extend(
                other_table_pairs(each, f"the release holds the statistics of one table, {table}")
            )
            continue
        found.extend(_table_not_released(each, table, released, why_not, skipped))
    for link in request.relationships:
        found.append(
            (
                f"children per parent of {link.child.table}({', '.join(link.child.columns)}) to "
                f"{link.parent.table}({', '.join(link.parent.columns)})",
                _NOT_NOISY,
            )
        )
    for fact in request.facts:
        found.append((f"fact {fact.name}", _NOT_NOISY))
    from fluid_build.profiler import request as asked_for

    found.extend(asked_for.unformed(request, workload=WORKLOAD_UNUSED, private=True))
    return tuple(found)


def other_table_pairs(each: Any, reason: str) -> List[Tuple[str, str]]:
    """The date pairs of a listed table the release does not hold, each with *reason*."""
    return [
        (f"date pair {pair.start} to {pair.end} of {each.expose_id}", reason)
        for pair in each.date_pairs
    ]


def check_selection_available(plan: ReleasePlan) -> None:
    """Refuse, before a record is read, a release that must choose cliques without OpenDP.

    Only the real selection needs the ``[privacy]`` extra: a selection put in its place, as the
    tests do, does not.
    """
    if not plan.selects or selection.select_cliques.__module__ != selection.__name__:
        return
    for module, requirement in (
        ("opendp", selection.OPENDP_REQUIREMENT),
        ("polars", "polars"),
        ("mbi", selection.MBI_REQUIREMENT),
    ):
        if importlib.util.find_spec(module) is None:
            raise _refuse(
                "profile_release_selection_unavailable",
                "choosing the cliques of a release under differential privacy needs "
                f"{selection.OPENDP_REQUIREMENT} and {selection.MBI_REQUIREMENT}, and "
                f"{requirement} is not installed",
                selection.PRIVACY_INSTALL_COMMAND,
                read=True,
            )


# ---------------------------------------------------------------------------
# Reading the records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EncodedRows:
    """The records as the release counts them: a cell index per column, and each row's unit.

    Nothing here leaves the process.
    """

    #: Per released column, the level or bin each row falls in.
    cells: Mapping[str, Sequence[int]]
    #: Each row's unit key: ``bounding.NULL_KEY`` for a row with a null in a key column (the
    #: null-key pool, one shared unit), ``None`` for a row that is a unit of its own (own rows).
    units: Sequence[Optional[bytes]]
    #: Each row's content, for the priority that decides which rows of a unit are kept.
    contents: Sequence[bytes]
    #: The levels of each categorical column, to hand to the selection.
    levels: Mapping[str, Sequence[str]] = field(default_factory=dict)
    #: Per column of a rank correlation, each row's value floored onto the grid.
    grid: Mapping[str, Sequence[int]] = field(default_factory=dict)
    #: Per date pair, the bin of the days from start to end each row falls in, :data:`NO_CELL`
    #: where either date is null.
    pairs: Mapping[Tuple[str, str], Sequence[int]] = field(default_factory=dict)


_OUTSIDE = "profile_record_outside_declared_domain"
#: The cell of a row with no value in a column that may hold none: it is counted in no cell.
NO_CELL = -1
_MS_PER_DAY = 86_400_000
_INNER_DAYS = dates.DAY_LADDER[1:-1]
#: The level of ``order`` (an index into :data:`dates.ORDER_LEVELS`) of each bin of the day ladder.
_ORDER_OF_BIN = tuple(
    dates.ORDER_LEVELS.index("before" if left < 0 else "same" if left == 0 else "after")
    for left in dates.DAY_LADDER[:-1]
)


def _row_error(
    event: str, column: str, row: int, what: str, table: Optional[str] = None
) -> ReleaseRefused:
    where = (
        f"column {column!r} row {row}"
        if table is None
        else (f"table {table!r} column {column!r} row {row}")
    )
    return ReleaseRefused(
        event,
        f"{where}: {what}",
        {
            "next": "fix the records, or the contract's column type, enum levels or "
            "validationRules range, then release"
        },
    )


@dataclass(frozen=True)
class Placement:
    """Where the values of one column sit, for the row a refusal names.

    *table* is named in a refusal when the release holds more than one table. *rows* are the rows
    of the table the values are, when they are not all of them (a column that may hold a null is
    placed over the rows that hold a value).
    """

    table: Optional[str] = None
    rows: Optional[Sequence[int]] = None

    def row(self, index: int) -> int:
        """The row of the table that the *index*-th value is."""
        return index if self.rows is None else self.rows[index]


def place_values(
    name: str,
    column: ReleasedColumn,
    found: Sequence[Any],
    backend: StatisticsBackend,
    fractional_bits: int,
    where: Placement = Placement(),
) -> List[int]:
    """The level or bin each value of *column* falls in, no value being ``None``.

    A value outside the levels or the range the column declares refuses, naming the column and
    the row and never the value.
    """
    if column.categorical:
        position = {level: place for place, level in enumerate(column.levels)}
        placed = []
        for index, value in enumerate(found):
            if value not in position:
                raise _row_error(
                    _OUTSIDE,
                    name,
                    where.row(index),
                    "a value outside the levels its type declares",
                    where.table,
                )
            placed.append(position[value])
        return placed
    for index, value in enumerate(found):
        inside = column.edges[0] <= value <= column.edges[-1]
        if not inside or (column.declared is not None and not column.declared.holds(value)):
            raise _row_error(
                _OUTSIDE,
                name,
                where.row(index),
                "a value outside the range its type or validationRules declare",
                where.table,
            )
    return backend.bin_cells(found, column.edges, fractional_bits)


def place_known(
    name: str,
    column: ReleasedColumn,
    found: Sequence[Any],
    backend: StatisticsBackend,
    fractional_bits: int,
    *,
    table: Optional[str] = None,
) -> List[int]:
    """The cell of each value of a column that may hold a null: :data:`NO_CELL` where it holds none.

    A column the contract declares required refuses a null (``profile_release_null``), naming the
    column and the row. Only the values that exist are placed, and a value outside the levels or
    bins refuses as in :func:`place_values`.
    """
    known = [index for index, value in enumerate(found) if value is not None]
    if len(known) != len(found) and not column.nullable:
        index = next(i for i, value in enumerate(found) if value is None)
        raise _row_error(
            "profile_release_null",
            name,
            index,
            "no value, and the release needs one in every row",
            table,
        )
    placed = place_values(
        name,
        column,
        [found[index] for index in known],
        backend,
        fractional_bits,
        Placement(table, known),
    )
    cells = [NO_CELL] * len(found)
    for index, cell in zip(known, placed, strict=True):
        cells[index] = cell
    return cells


def paired_columns(pairs: Sequence[Tuple[str, str]]) -> Tuple[str, ...]:
    """The columns of the date pairs, each once, in the order they first appear."""
    return tuple(dict.fromkeys(name for pair in pairs for name in pair))


def day_bin(days: int) -> int:
    """The bin of :data:`dates.DAY_LADDER` that *days* falls in; a day beyond an end is in that end."""
    return bisect_right(_INNER_DAYS, days)


def pair_cells(
    plan: "ReleasePlan",
    exact: Mapping[str, Sequence[Any]],
    *,
    table: Optional[str] = None,
) -> Dict[Tuple[str, str], List[int]]:
    """The bin of the days from start to end of each row of each date pair of *plan*.

    *exact* are the values read for each column of the pairs, in the timestamp domain
    (milliseconds). A row where either date is null has :data:`NO_CELL`. A null in a column the
    contract declares required refuses (``profile_release_null``), and so does a value that is no
    whole day (``profile_record_outside_declared_domain``): the days of a pair are whole, and a
    pair is never left out because of what the records hold. A refusal names the column and the
    row, never a value.
    """
    whole: Dict[str, List[Optional[int]]] = {}
    for name in paired_columns(plan.date_pairs):
        found: List[Optional[int]] = []
        for index, value in enumerate(exact[name]):
            if value is None:
                if name in plan.date_required:
                    raise _row_error(
                        "profile_release_null",
                        name,
                        index,
                        "no value, and the release needs one in every row",
                        table,
                    )
                found.append(None)
            elif value % _MS_PER_DAY:
                raise _row_error(_OUTSIDE, name, index, "a value that is not a whole day", table)
            else:
                found.append(int(value // _MS_PER_DAY))
        whole[name] = found
    cells: Dict[Tuple[str, str], List[int]] = {}
    for start, end in plan.date_pairs:
        cells[(start, end)] = [
            NO_CELL if first is None or last is None else day_bin(last - first)
            for first, last in zip(whole[start], whole[end], strict=True)
        ]
    return cells


def encode_rows(
    plan: ReleasePlan, read: sources.SourceColumns, backend: StatisticsBackend
) -> EncodedRows:
    """Read each value once and place it in its cell; refuse what the release cannot hold.

    A refusal names a column and a row, and never a value or how many. Every value goes
    through ``backend.read_value`` in its column's domain, key columns included, so a key is
    always of one type.
    """
    plan_read = runner.ExposePlan(plan.table, plan.read)
    values = runner._dates_read(plan_read, read).values
    released = {each.name: each for each in plan.columns}
    rows = max((len(column) for column in values.values()), default=0)
    exact: Dict[str, List[Any]] = {}
    for column in plan.read:
        exact[column.name] = [
            backend.read_value(column.domain, raw, column=column.name, row_index=index)
            for index, raw in enumerate(values[column.name])
        ]
    cells: Dict[str, List[int]] = {}
    levels: Dict[str, Sequence[str]] = {}
    for name, each in released.items():
        if each.categorical:
            levels[name] = each.levels
        cells[name] = place_known(name, each, exact[name], backend, plan.fractional_bits)
    pairs = pair_cells(plan, exact)
    unit = plan.unit
    keyed: List[Optional[bytes]] = [None] * rows
    if unit.mode != ROWS or unit.key_columns:
        key_values = [exact[name] for name in unit.key_columns]
        for index in range(rows):
            parts = [column[index] for column in key_values]
            keyed[index] = (
                None if any(part is None for part in parts) else bounding.encode_values(parts)
            )
    if unit.mode != ROWS:
        # no key value: the row joins the null-key pool, one unit bounded like any other
        keyed = [bounding.NULL_KEY if key is None else key for key in keyed]
    if unit.mode == ROWS and unit.key_columns:
        for index, key in enumerate(keyed):
            if key is None:
                raise ReleaseRefused(
                    "profile_release_null_key",
                    f"column(s) {', '.join(map(repr, unit.key_columns))} row {index}: a null in "
                    f"the primary key of {unit.release_table!r}, which names each row",
                    {"next": "give every row a key, or release a table that has no primary key"},
                )
        if bounding.repeated_keys(keyed)[0]:
            raise ReleaseRefused(
                "profile_release_repeated_key",
                f"a primary key value of {unit.release_table!r} "
                f"({', '.join(map(repr, unit.key_columns))}) is held by more than one row, so a "
                "row is not one entity",
                {
                    "next": "release a child table of the unit, or key the unit with "
                    "--unit-key, which bounds each entity's rows"
                },
            )
        keyed = [None] * rows
    grid: Dict[str, List[int]] = {}
    if plan.rank_pairs:
        encode = backend.encoder(plan.fractional_bits)
        for name in dict.fromkeys(n for _, a, b in plan.rank_pairs for n in (a, b)):
            grid[name] = [encode(Fraction(value)) for value in exact[name]]
    order = list(
        dict.fromkeys(
            [*(each.name for each in plan.columns), *(n for p in plan.date_pairs for n in p)]
        )
    )
    contents = [
        bounding.encode_values([exact[name][index] for name in order]) for index in range(rows)
    ]
    return EncodedRows(
        cells=cells, units=keyed, contents=contents, levels=levels, grid=grid, pairs=pairs
    )


# ---------------------------------------------------------------------------
# The tables of a release, and the release
# ---------------------------------------------------------------------------

CLIQUE, LEVELS, BINS, RANK = "clique", "levels", "bins", "rank"
#: The two parts of a date pair: the days from start to end on the public ladder, and the order
#: (``after``, ``before``, ``same``), each a count table over the rows where both dates are known.
PAIR_DAYS, PAIR_ORDER = "pairDays", "pairOrder"
PAIRS = (PAIR_DAYS, PAIR_ORDER)
#: The two kinds a unit that follows its child rows adds: a relationship's children-per-parent
#: histogram and a statistic grouped through a link.
RELATION, LINK = "relationship", "link"


@dataclass(frozen=True)
class Part:
    """One noisy statistic of the release, before its scale is chosen.

    ``kind`` is :data:`CLIQUE`, :data:`LEVELS`, :data:`BINS`, :data:`PAIR_DAYS`,
    :data:`PAIR_ORDER` or :data:`RANK`; ``sizes`` are the cells of each column of a count table
    (the cells of a date pair's days or order; none for a rank correlation, whose ``definition``
    names it and whose ``columns`` are its pair). ``label`` names its noise stream.
    """

    kind: str
    label: str
    columns: Tuple[str, ...]
    sizes: Tuple[int, ...] = ()
    definition: str = ""

    @property
    def cells(self) -> int:
        """The cells of a count table; a rank correlation holds none."""
        return 0 if self.kind == RANK else math.prod(self.sizes)


@dataclass(frozen=True)
class ReleasedTable:
    """One noisy part of the release (a count table or a rank correlation): what the record states."""

    label: str
    columns: Tuple[str, ...]
    kind: str
    sizes: Tuple[int, ...]
    #: The scale as the document spells it, and the exact integer it is ``m / 10**6`` of.
    scale: str
    scale_units: int
    #: The share of the budget, as the document spells it.
    share: str
    #: A rank correlation's definition, its row floor and its sensitivity ``Delta`` to one unit.
    definition: str = ""
    row_floor: int = 0
    delta: Fraction = Fraction(0)
    #: A unit that follows its child rows: the table whose rows the part counts, and the squared
    #: L2 sensitivity of a count table, a relationship histogram or a link statistic to one unit.
    table: str = ""
    sensitivity: int = 0

    @property
    def cells(self) -> int:
        return 0 if self.kind == RANK else math.prod(self.sizes)

    def as_record(self) -> Dict[str, Any]:
        found: Dict[str, Any] = {
            "label": self.label,
            "columns": list(self.columns),
            "scale": self.scale,
            "share": self.share,
        }
        if self.table:
            found["table"] = self.table
        if self.kind == RANK:
            found.update({"kind": RANK, "definition": self.definition, "rowFloor": self.row_floor})
        elif self.kind in (RELATION, LINK, *PAIRS):
            found["kind"] = self.kind
        return found


@dataclass(frozen=True)
class Release:
    """A drawn release: the document, its digest and what a report may say about it."""

    document: Dict[str, Any]
    canonical: bytes
    digest: str
    #: Every part, in document order: the count tables, then the rank correlations.
    tables: Tuple[ReleasedTable, ...]
    #: The maximal cliques released (two or more columns each).
    cliques: Tuple[Tuple[str, ...], ...]
    #: The cliques the selection chose, ``None`` when no selection ran.
    selected: Optional[Tuple[Tuple[str, ...], ...]]
    selection_rho: Fraction
    #: Each part's share.
    share: Fraction
    #: Each count table's noisy values before the tables are made to agree, then after, exact.
    noisy: Tuple[Tuple[Fraction, ...], ...] = field(default=(), repr=False)
    consistent: Tuple[Tuple[Fraction, ...], ...] = field(default=(), repr=False)
    #: The plan of what was released: the plan without the added parts that did not fit.
    plan: Optional["ReleasePlan"] = field(default=None, repr=False)
    #: Each added part left out, ``(label, reason)``, in the order they were to be fitted in.
    left_out: Tuple[Tuple[str, str], ...] = ()

    @property
    def measurement_rho(self) -> Fraction:
        return self.share * len(self.tables)

    @property
    def count_tables(self) -> Tuple[ReleasedTable, ...]:
        return tuple(each for each in self.tables if each.kind != RANK)

    @property
    def rank_tables(self) -> Tuple[ReleasedTable, ...]:
        return tuple(each for each in self.tables if each.kind == RANK)


def _eligible_names(plan: ReleasePlan) -> List[str]:
    return [each.name for each in plan.eligible]


def release_parts(plan: ReleasePlan, cliques: Sequence[Sequence[str]]) -> Tuple[Part, ...]:
    """Every noisy statistic of the release, once, in document order. The one source of the parts.

    The document, the report, the budget and the record all read this list. The parts are the
    cliques as given, then a level histogram for each categorical column in none of them, then
    a histogram for each number, date or timestamp column in none of them (the last two in column
    order), then each rank correlation in the request's order, then the days and the order of each
    date pair in the request's order. Each is one equal share.
    """
    names = _eligible_names(plan)
    position = {name: place for place, name in enumerate(names)}
    seen: List[FrozenSet[str]] = []
    parts: List[Part] = []
    for clique in cliques:
        members = tuple(clique)
        ordered = tuple(sorted(members, key=lambda n: position.get(n, -1)))
        if (
            len(members) < 2
            or len(set(members)) != len(members)
            or members != ordered
            or any(name not in position for name in members)
            or frozenset(members) in seen
        ):
            raise ReleaseRefused(
                "profile_release_record_mismatch",
                "a clique is not two or more distinct columns that may join cliques, in the "
                "contract's order",
            )
        seen.append(frozenset(members))
        parts.append(
            Part(
                CLIQUE,
                noise_label(members),
                members,
                tuple(plan.column(n).size for n in members),
            )
        )
    covered = set().union(*seen) if seen else set()
    for column in plan.categorical:
        if column.name not in covered:
            parts.append(Part(LEVELS, noise_label((column.name,)), (column.name,), (column.size,)))
    for column in plan.ordered:
        if column.name not in covered:
            parts.append(Part(BINS, noise_label((column.name,)), (column.name,), (column.size,)))
    for definition, first, second in plan.rank_pairs:
        parts.append(
            Part(RANK, f"rank/{definition}/{first}/{second}", (first, second), (), definition)
        )
    for start, end in plan.date_pairs:
        parts.append(
            Part(
                PAIR_DAYS,
                pair_label(PAIR_DAYS, start, end),
                (start, end),
                (len(dates.DAY_LADDER) - 1,),
            )
        )
        parts.append(
            Part(
                PAIR_ORDER,
                pair_label(PAIR_ORDER, start, end),
                (start, end),
                (len(dates.ORDER_LEVELS),),
            )
        )
    return tuple(parts)


def pair_label(kind: str, start: str, end: str) -> str:
    """The label of the noise stream of a date pair's days (:data:`PAIR_DAYS`) or order."""
    return f"pair/{'days' if kind == PAIR_DAYS else 'order'}/{start}/{end}"


def added_units(
    plan: ReleasePlan, parts: Sequence[Part]
) -> Tuple[List[Tuple[Part, ...]], List[Tuple[Part, ...]]]:
    """The added parts of *parts* as units: the date pairs, then the nullable columns.

    A unit is released whole or not at all: a pair's days and order together, a column's histogram.
    The pairs are in the request's order, the columns in the plan's order.
    """
    by_label = {part.label: part for part in parts}
    pairs: List[Tuple[Part, ...]] = [
        (by_label[pair_label(PAIR_DAYS, start, end)], by_label[pair_label(PAIR_ORDER, start, end)])
        for start, end in plan.date_pairs
    ]
    columns: List[Tuple[Part, ...]] = [
        (by_label[noise_label((each.name,))],) for each in plan.columns if each.nullable
    ]
    return pairs, columns


def left_out_reason() -> str:
    """Why an added part that does not fit under the cap is not released. Never a count."""
    return (
        f"the release's parts would hold more cells than the {selection.MOST_RELEASE_CELLS:,} a "
        "release may measure"
    )


def fit_prefix(fixed: int, units: Sequence[int]) -> int:
    """How many of the added *units* (their cells, in priority order) fit beside *fixed* cells.

    A prefix: the first unit that does not fit ends it, so what is released never depends on which
    later unit happens to be small. When *fixed* alone is over the cap none fits.
    """
    total = fixed
    for place, cells in enumerate(units):
        if total + cells > selection.MOST_RELEASE_CELLS:
            return place
        total += cells
    return len(units)


def admit(
    plan: ReleasePlan, cliques: Sequence[Sequence[str]]
) -> Tuple[ReleasePlan, Tuple[Tuple[str, str], ...]]:
    """The plan a release holds, once its added parts are fitted into the cap, and what is left out.

    The parts the release always held (the cliques and the histograms of required columns, the
    rank correlations) keep their place and are never left out. The added parts (every date pair,
    then every column that may hold a null) are admitted in that order while the release's cells
    stay within ``selection.MOST_RELEASE_CELLS``: the first that does not fit is left out and so is
    every one after it. Each left-out part is named with its label and the reason, never a count.
    The decision reads the cliques, which are released under the selection's share, and nothing
    else: it is post-processing and costs no privacy. The plan comes back as it was when every
    added part fits.
    """
    parts = release_parts(plan, cliques)
    pairs, columns = added_units(plan, parts)
    units = [*pairs, *columns]
    if not units:
        return plan, ()
    added = {part.label for unit in units for part in unit}
    fixed = sum(part.cells for part in parts if part.label not in added)
    fits = fit_prefix(fixed, [sum(part.cells for part in unit) for unit in units])
    if fits == len(units):
        return plan, ()
    gone = {part.label for unit in units[fits:] for part in unit}
    reason = left_out_reason()
    left = tuple((part.label, reason) for unit in units[fits:] for part in unit)
    kept = replace(
        plan,
        columns=tuple(each for each in plan.columns if noise_label((each.name,)) not in gone),
        date_pairs=tuple(
            pair for pair in plan.date_pairs if pair_label(PAIR_DAYS, *pair) not in gone
        ),
    )
    return kept, left


def agrees(plan: ReleasePlan, table: "ReleasedTable") -> bool:
    """Whether *table* is made to agree with the others: a clique, or the histogram of a required column.

    A histogram of a column that may hold a null counts that column's known values, which are not
    the records the other tables count, and the days and the order of a date pair count the rows
    where both dates are known; none is made to agree with another table, and each is noised on
    its own.
    """
    if table.kind in PAIRS:
        return False
    if table.kind in (LEVELS, BINS):
        return not plan.column(table.columns[0]).nullable
    return True


def _describe(
    plan: ReleasePlan, cliques: Sequence[Sequence[str]]
) -> Tuple[Fraction, Tuple[ReleasedTable, ...]]:
    """The share of each part and the parts, checked against the cells a release may hold.

    The added parts are fitted into the cap first (:func:`admit`). The cells are then added up,
    and refused when the parts the release always held are too many, before any table is allocated.
    """
    plan, _ = admit(plan, cliques)
    parts = release_parts(plan, cliques)
    cells = sum(part.cells for part in parts)
    if cells > selection.MOST_RELEASE_CELLS:
        raise _refuse(
            "profile_release_too_many_cells",
            "the tables of the release hold more cells than the "
            f"{selection.MOST_RELEASE_CELLS:,} a release may measure",
            "list fewer columns in the request",
        )
    labels = [part.label for part in parts]
    if len(set(labels)) != len(labels) or any("\x00" in label for label in labels):
        raise _refuse(
            "profile_release_column_name",
            "two parts of the release would draw their noise from one stream, or a column name "
            "holds a zero character",
            "rename a column of the contract",
        )
    _, share = split_budget(plan.rho, plan.selects, len(parts))
    count_units = scale_units(share, plan.sensitivity)
    tables: List[ReleasedTable] = []
    for part in parts:
        if part.kind == RANK:
            delta = plan.delta(part.definition)
            units = rank_noise.scale_units(share, delta * delta)
            assert plan.row_floor is not None
            extra: Dict[str, Any] = {
                "definition": part.definition,
                "row_floor": plan.row_floor,
                "delta": delta,
            }
        else:
            units, extra = count_units, {}
        tables.append(
            ReleasedTable(
                label=part.label,
                columns=part.columns,
                kind=part.kind,
                sizes=part.sizes,
                scale=decimal_text(Fraction(units, 10**_SCALE_PLACES)),
                scale_units=units,
                share=decimal_text(share),
                **extra,
            )
        )
    return share, tuple(tables)


def selection_share(plan: ReleasePlan) -> Fraction:
    """A tenth of the total, when two or more columns may join cliques, else nothing."""
    if plan.selection_rho is not None:
        return plan.selection_rho if plan.selects else Fraction(0)
    return plan.rho / 10 if plan.selects else Fraction(0)


def _pair_weights(plan: ReleasePlan) -> Dict[Tuple[str, str], int]:
    names = _eligible_names(plan)
    return {
        (a, b): 2 if a in plan.dimensions or b in plan.dimensions else 1
        for a, b in itertools.combinations(names, 2)
    }


def _selected(
    plan: ReleasePlan,
    rows: EncodedRows,
    kept: Sequence[int],
    select: Optional[Callable[..., Sequence[Sequence[str]]]],
) -> Tuple[Tuple[str, ...], ...]:
    """Choose the cliques over the eligible columns; a binned column is given by its bin's index."""
    names = _eligible_names(plan)
    levels = {
        each.name: each.levels if each.categorical else tuple(str(i) for i in range(each.size))
        for each in plan.eligible
    }
    chosen_rows = [tuple(levels[name][rows.cells[name][i]] for name in names) for i in kept]
    run = select if select is not None else selection.select_cliques
    try:
        chosen = run(
            chosen_rows,
            names,
            levels,
            _pair_weights(plan),
            selection_share(plan),
            plan.distance,
            method=plan.clique_method,
        )
    except selection.SelectionUnavailable:
        raise
    except Exception as error:  # OpenDP's own messages are not ours to vouch for
        raise ReleaseRefused(
            "profile_release_selection_failed",
            f"choosing the cliques failed ({type(error).__name__})",
            {"next": "report it with your forge-cli and OpenDP versions"},
        ) from None
    return tuple(tuple(clique) for clique in chosen)


def _agree(tables: Sequence[ReleasedTable], texts: Sequence[Sequence[str]]) -> None:
    """Refuse, as a defect, written values on which two tables that share columns differ."""
    values = [[Fraction(text) for text in each] for each in texts]
    totals = {sum(each, Fraction(0)) for each in values}
    margins: Dict[Tuple[int, Tuple[str, ...]], Dict[Tuple[int, ...], Fraction]] = {}

    def margin(index: int, names: Tuple[str, ...]) -> Dict[Tuple[int, ...], Fraction]:
        if (index, names) not in margins:
            table = tables[index]
            places = [table.columns.index(name) for name in names]
            summed: Dict[Tuple[int, ...], Fraction] = {}
            cells = itertools.product(*(range(size) for size in table.sizes))
            for cell, value in zip(cells, values[index], strict=True):
                key = tuple(cell[p] for p in places)
                summed[key] = summed.get(key, Fraction(0)) + value
            margins[(index, names)] = summed
        return margins[(index, names)]

    defect = len(totals) != 1
    for a, b in itertools.combinations(range(len(tables)), 2):
        shared = tuple(sorted(set(tables[a].columns) & set(tables[b].columns)))
        if shared and margin(a, shared) != margin(b, shared):
            defect = True
    if defect:
        raise ReleaseRefused(
            "profile_release_inconsistent",
            "defect: two tables of the release that share columns do not agree once written "
            "as decimal text",
            {"next": "report it with your forge-cli version"},
        )


def table_cells(table: ReleasedTable, encoded: EncodedRows, kept: Sequence[int]) -> List[int]:
    """The cell of each kept row in the count table *table*; :data:`NO_CELL` where it lands in none.

    A row lands in no cell of a table when a column of it holds no value (a column that may hold a
    null) or, for a date pair, when either date is null. A unit therefore still changes at most
    the cells it changed before, each by at most one.
    """
    if table.kind in PAIRS:
        days = encoded.pairs[(table.columns[0], table.columns[1])]
        if table.kind == PAIR_DAYS:
            return [days[i] for i in kept]
        return [NO_CELL if days[i] == NO_CELL else _ORDER_OF_BIN[days[i]] for i in kept]
    cell = [0] * len(kept)
    for name, size in zip(table.columns, table.sizes, strict=True):
        index = encoded.cells[name]
        cell = [
            NO_CELL if value == NO_CELL or index[i] == NO_CELL else value * size + index[i]
            for value, i in zip(cell, kept, strict=True)
        ]
    return cell


def measure_counts(
    plan: ReleasePlan,
    rows: EncodedRows,
    kept: Sequence[int],
    tables: Sequence[ReleasedTable],
) -> Dict[str, List[int]]:
    """The exact cells of every count table of the release, by label: what one unit moves by at most
    the sensitivity each part is charged. Rank correlations are not counts and are left out.

    *kept* are the rows left after the bounding. A row with no cell in a table is not counted in
    it, and a unit counts at most ``max_rows_per_cell`` rows in one cell.
    """
    unit = plan.unit
    cap = unit.max_rows_per_cell if unit.mode != ROWS else 1
    found: Dict[str, List[int]] = {}
    for table in tables:
        if table.kind == RANK:
            continue
        cells = table_cells(table, rows, kept)
        units = [rows.units[i] for i, cell in zip(kept, cells, strict=True) if cell != NO_CELL]
        found[table.label] = bounding.capped_counts(
            units, [cell for cell in cells if cell != NO_CELL], table.cells, cap
        )
    return found


def _add_noise(
    plan: ReleasePlan,
    counted: Sequence[ReleasedTable],
    exact: Mapping[str, Sequence[int]],
    seed: bytes,
) -> Tuple[List[Tuple[Fraction, ...]], List[consistency.CountTable]]:
    """The noisy cells of each counted table, and the tables that are made to agree."""
    noisy: List[Tuple[Fraction, ...]] = []
    together: List[consistency.CountTable] = []
    for table in counted:
        sigma2 = Fraction(table.scale_units**2, 10 ** (2 * _SCALE_PLACES))
        source = SeededSource(seed, table.label)
        values = tuple(
            Fraction(count + sample_discrete_gaussian(sigma2, source))
            for count in exact[table.label]
        )
        noisy.append(values)
        if agrees(plan, table):
            together.append(
                consistency.CountTable(table.label, table.columns, table.sizes, values, sigma2)
            )
    return noisy, together


def _agreed_values(
    plan: ReleasePlan,
    counted: Sequence[ReleasedTable],
    noisy: Sequence[Tuple[Fraction, ...]],
    together: Sequence[consistency.CountTable],
) -> Tuple[Dict[str, Tuple[str, ...]], Dict[str, Tuple[Fraction, ...]]]:
    """The text and the value of each counted table's cells, the agreeing ones made to agree.

    A table that does not agree (a column that may hold a null) is released as it was noised.
    """
    values_of: Dict[str, Tuple[str, ...]] = {}
    agreed_of: Dict[str, Tuple[Fraction, ...]] = {}
    if together:
        try:
            forest = consistency.junction_forest(together)
            agreed = consistency.make_consistent(together)
            texts = consistency.to_decimal_text(together, agreed, forest)
        except ValueError as error:
            raise ReleaseRefused(
                "profile_release_not_a_forest",
                f"the tables of the release cannot be made to agree: {error}",
                {"next": "report it with your forge-cli version"},
            ) from None
        agreeing = [each for each in counted if agrees(plan, each)]
        _agree(agreeing, texts)
        values_of.update({table.label: cells for table, cells in zip(agreeing, texts, strict=True)})
        agreed_of.update({table.label: each for table, each in zip(agreeing, agreed, strict=True)})
    for table, values in zip(counted, noisy, strict=True):
        if table.label not in values_of:
            values_of[table.label] = tuple(decimal_text(value) for value in values)
            agreed_of[table.label] = values
    return values_of, agreed_of


def draw(
    plan: ReleasePlan,
    rows: EncodedRows,
    seed: bytes,
    backend: StatisticsBackend,
    *,
    cliques: Optional[Sequence[Sequence[str]]] = None,
    select: Optional[Callable[..., Sequence[Sequence[str]]]] = None,
    structure: Optional[Callable[..., Sequence[Sequence[str]]]] = None,
    producer: Optional[str] = None,
    selection_version: Optional[str] = None,
) -> Release:
    """Bound, choose, count, add noise, make the tables agree and write the document.

    With *cliques* (the record's, on a re-derivation) no selection runs. *select* and
    *structure* stand in for :func:`selection.select_cliques` and
    :func:`selection.release_structure`. *selection_version* is the OpenDP version the
    ``cliqueSelection`` names (the record's, on a re-derivation; else the one installed).
    """
    unit = plan.unit
    count = len(rows.contents)
    if unit.mode == ROWS:
        kept: Sequence[int] = range(count)
    else:
        kept = bounding.bound_rows(rows.units, rows.contents, seed, unit.max_rows_per_unit)
    chosen: Optional[Tuple[Tuple[str, ...], ...]] = None
    if cliques is not None:
        released = tuple(tuple(clique) for clique in cliques)
    elif plan.selects:
        chosen = _selected(plan, rows, kept, select)
        wide = structure if structure is not None else selection.release_structure
        try:
            joined = wide(
                _eligible_names(plan),
                [each.size for each in plan.eligible],
                chosen,
            )
        except ValueError:
            raise ReleaseRefused(
                "profile_release_structure_too_large",
                "the cliques chosen hold more cells than a release may measure "
                f"({selection.MOST_RELEASE_CELLS:,}). Choosing them spent the selection share of "
                f"this release's budget, rho {decimal_text(selection_share(plan))}, on these "
                "records, and nothing was written",
                {
                    "next": f"list fewer {_joiners(plan.eligible)} in the request, so the "
                    "cliques chosen hold fewer cells; releasing again spends a new loss on "
                    "the records"
                },
            ) from None
        released = tuple(tuple(clique) for clique in joined)
    else:
        released = ()
    try:
        plan, left_out = admit(plan, released)
        share, tables = _describe(plan, released)
    except ReleaseRefused as refused:
        if chosen is None or refused.event != "profile_release_too_many_cells":
            raise
        # The cells are known only once the cliques are; the selection has run by now.
        raise ReleaseRefused(
            refused.event,
            f"{refused}. Choosing the cliques spent the selection share of this release's "
            f"budget, rho {decimal_text(selection_share(plan))}, on these records, and nothing "
            "was written",
            dict(refused.context),
        ) from None
    counted = [each for each in tables if each.kind != RANK]
    exact = measure_counts(plan, rows, kept, tables)
    noisy, together = _add_noise(plan, counted, exact, seed)
    values_of, agreed_of = _agreed_values(plan, counted, noisy, together)
    for table in tables:
        if table.kind == RANK:
            first, second = table.columns
            pairs = [(rows.grid[first][i], rows.grid[second][i]) for i in kept]
            value = backend.rank_value(table.definition, pairs, table.row_floor)
            noised = rank_noise.release_value(
                value, table.delta, table.scale_units, SeededSource(seed, table.label)
            )
            values_of[table.label] = (decimal_text(noised),)

    if selection_version is None and cliques is None and plan.selects:
        selection_version = _version("opendp")
    built = _document(plan, tables, values_of, producer, selection_version)
    canonical = document.canonical_bytes(built)
    backend.check_document(canonical.decode("utf-8"))
    return Release(
        document=built,
        canonical=canonical,
        digest=document.digest_of(canonical),
        tables=tables,
        cliques=tuple(table.columns for table in tables if table.kind == CLIQUE),
        selected=chosen,
        selection_rho=selection_share(plan),
        share=share,
        noisy=tuple(noisy),
        consistent=tuple(agreed_of[each.label] for each in counted),
        plan=plan,
        left_out=left_out,
    )


def _noise_block(table: ReleasedTable) -> Dict[str, Any]:
    return {
        "mechanism": RANK_MECHANISM if table.kind == RANK else MECHANISM,
        "scale": table.scale,
        "privacyLoss": {"rho": table.share},
    }


def _opendp_uri(version: Optional[str]) -> str:
    base = "https://pypi.org/project/opendp"
    return f"{base}/{version}/" if version else f"{base}/"


def _clique_entry(plan: ReleasePlan, table: ReleasedTable, cells: Sequence[str]) -> Dict[str, Any]:
    """The document's entry for one clique: by levels when its columns are all categorical."""
    members = [plan.column(name) for name in table.columns]
    if all(each.categorical for each in members):
        return {
            "definition": "cliqueMarginal/1",
            "columns": list(table.columns),
            "levels": [list(each.levels) for each in members],
            "noisyCounts": list(cells),
            "noise": _noise_block(table),
        }
    return {
        "definition": "cliqueMarginal/2",
        "columns": list(table.columns),
        "domains": [
            ({"levels": list(each.levels)} if each.categorical else {"bins": list(each.edge_texts)})
            for each in members
        ],
        "noisyCounts": list(cells),
        "noise": _noise_block(table),
    }


def _pair_statistic(table: ReleasedTable, cells: Sequence[str]) -> Dict[str, Any]:
    """The statistic of a date pair's days or order."""
    statistic: Dict[str, Any] = {
        "sampleSize": {"min": 0},
        "disclosure": RULE_NAME,
        "groups": [{"group": "", "status": "F", "noisyCounts": list(cells)}],
        "noise": _noise_block(table),
    }
    if table.kind == PAIR_DAYS:
        statistic["definition"] = "marginalHistogram/1"
        statistic["bins"] = [str(edge) for edge in dates.DAY_LADDER]
    else:
        statistic["definition"] = "levelHistogram/1"
        statistic["levels"] = list(dates.ORDER_LEVELS)
    return statistic


def _column_pairs_block(pairs: Mapping[Tuple[str, str], List[Dict[str, Any]]]) -> List[Any]:
    """The ``columnPairs`` of a table: each pair's rank correlations, in the order of the spec."""
    return [
        {
            "columns": [first, second],
            "statistics": sorted(
                statistics, key=lambda each: RANK_DEFINITIONS.index(each["definition"])
            ),
        }
        for (first, second), statistics in pairs.items()
    ]


def _date_pairs_block(
    plan: ReleasePlan, dated: Mapping[Tuple[str, str], Mapping[str, Dict[str, Any]]]
) -> List[Any]:
    """The ``datePairs`` of a table, in the order the plan lists them, for those it holds."""
    return [
        {
            "start": start,
            "end": end,
            "statistics": [dated[(start, end)][PAIR_DAYS]],
            "order": dated[(start, end)][PAIR_ORDER],
        }
        for start, end in plan.date_pairs
        if (start, end) in dated
    ]


def _table_block(
    plan: ReleasePlan,
    tables: Sequence[ReleasedTable],
    values_of: Mapping[str, Sequence[str]],
    selection_version: Optional[str],
    *,
    states_selection: bool = False,
) -> Dict[str, Any]:
    """The document's block for the release table of *plan*, from its parts and their values.

    A table that chose cliques states its ``cliqueSelection`` when it holds a form of format
    1-4-0, names MST, or *states_selection* (a document of format 1-4-0 for another reason).
    """
    columns: Dict[str, Dict[str, Any]] = {
        name: {"domain": domain} for name, domain in plan.table_columns
    }
    cliques: List[Dict[str, Any]] = []
    pairs: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    dated: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
    binned = False
    for table in tables:
        cells = values_of[table.label]
        if table.kind == CLIQUE:
            clique = _clique_entry(plan, table, cells)
            binned = binned or clique["definition"] == "cliqueMarginal/2"
            cliques.append(clique)
            continue
        if table.kind == RANK:
            first, second = table.columns
            pairs.setdefault((first, second), []).append(
                {
                    "definition": table.definition,
                    "rowFloor": table.row_floor,
                    "sampleSize": {"min": 0},
                    "disclosure": RULE_NAME,
                    "groups": [{"group": "", "status": "F", "noisyValue": cells[0]}],
                    "noise": _noise_block(table),
                }
            )
            continue
        if table.kind in PAIRS:
            statistic = _pair_statistic(table, cells)
            dated.setdefault((table.columns[0], table.columns[1]), {})[table.kind] = statistic
            continue
        column = plan.column(table.columns[0])
        entry: Dict[str, Any] = {
            "definition": "levelHistogram/1" if table.kind == LEVELS else "marginalHistogram/1",
            "sampleSize": {"min": 0},
            "disclosure": RULE_NAME,
            "groups": [{"group": "", "status": "F", "noisyCounts": list(cells)}],
            "noise": _noise_block(table),
        }
        if table.kind == LEVELS:
            entry["levels"] = list(column.levels)
        else:
            entry["bins"] = list(column.edge_texts)
        columns[column.name].setdefault("statistics", []).append(entry)
    released: Dict[str, Any] = {"columns": columns}
    if cliques:
        released["cliques"] = cliques
    if pairs:
        released["columnPairs"] = _column_pairs_block(pairs)
    if dated:
        released["datePairs"] = _date_pairs_block(plan, dated)
    # A release of format 1-2-0 states no selection, and the loss of choosing the cliques is
    # slack below its total. One that holds a 1-4-0 form, or names MST, states it.
    if plan.selects and (pairs or binned or plan.clique_method != "aim" or states_selection):
        released["cliqueSelection"] = {
            "method": plan.clique_method,
            "producer": _opendp_uri(selection_version),
            "selection": {"privacyLoss": {"rho": decimal_text(selection_share(plan))}},
            "published": "measured",
        }
    return released


def _document(
    plan: ReleasePlan,
    tables: Sequence[ReleasedTable],
    values_of: Mapping[str, Sequence[str]],
    producer: Optional[str],
    selection_version: Optional[str],
) -> Dict[str, Any]:
    released = _table_block(plan, tables, values_of, selection_version)
    out_tables: Dict[str, Any] = {plan.table: released}
    for name, listed in plan.other_tables:
        out_tables[name] = {"columns": {column: {"domain": domain} for column, domain in listed}}
    if producer is None:
        from fluid_build import __version__

        producer = document.producer_uri(__version__)
    needed = document.schema_url_for({plan.table: released})
    schema_url = (
        needed
        if needed in (document.SCHEMA_URL_1_3_0, document.SCHEMA_URL_1_4_0)
        else SCHEMA_URL_1_2_0
    )
    return {
        "_producer": producer,
        "_schemaURL": schema_url,
        "encoding": {"fractionalBits": plan.fractional_bits, "rounding": "floor"},
        "disclosureRules": {RULE_NAME: {}},
        "privacy": {
            "measure": "zeroConcentratedDP",
            "privacyLoss": {"rho": decimal_text(plan.rho)},
            "privacyUnit": {"expose": plan.table},
            "publicStructure": True,
        },
        "tables": out_tables,
    }


# ---------------------------------------------------------------------------
# The release record
# ---------------------------------------------------------------------------


def _version(distribution: str) -> Optional[str]:
    from importlib import metadata

    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def _unit_block(plan: ReleasePlan) -> Dict[str, Any]:
    unit = plan.unit
    return {
        "expose": unit.unit_expose,
        "releaseTable": unit.release_table,
        "mode": unit.mode,
        "keyColumns": list(unit.key_columns),
        "relationship": unit.relationship,
        "c": unit.max_rows_per_unit,
        "k": unit.max_rows_per_cell,
    }


def _options_block(plan: ReleasePlan) -> Dict[str, Any]:
    """The options that shape the parts, as the record states them; a default is left out."""
    found: Dict[str, Any] = {}
    if plan.selects and plan.clique_method != "aim":
        found["cliqueMethod"] = plan.clique_method
    if plan.row_floor is not None:
        found["rowFloor"] = plan.row_floor
    return found


def build_record(
    plan: ReleasePlan,
    release: Release,
    seed: bytes,
    *,
    request_bytes: bytes,
    contract_digest: Optional[str],
) -> ReleaseRecord:
    """The record that lets the release be drawn again inside the data owner's boundary."""
    selection_block: Optional[Dict[str, Any]] = None
    if release.selected is not None:
        selection_block = {
            "rho": decimal_text(release.selection_rho),
            "cliques": [list(clique) for clique in release.selected],
        }
    return ReleaseRecord(
        seed=seed,
        profile_digest=release.digest,
        request_digest="sha256:" + hashlib.sha256(request_bytes).hexdigest(),
        contract_digest=contract_digest,
        unit=_unit_block(plan),
        privacy_loss={"rho": decimal_text(plan.rho)},
        selection=selection_block,
        tables=tuple(table.as_record() for table in release.tables),
        options=_options_block(plan),
        versions={
            "forge-cli": _version("data-product-forge"),
            "flux-stats": _version("flux-stats"),
            "opendp": _version("opendp") if release.selected is not None else None,
        },
    )


def _mismatch(what: str) -> ReleaseRefused:
    return ReleaseRefused(
        "profile_release_record_mismatch",
        f"the release record does not match the request and the contract: {what}",
        {"next": "name the record this release wrote, with the request it was made from"},
    )


def check_record(plan: ReleasePlan, record: ReleaseRecord) -> Tuple[Tuple[str, ...], ...]:
    """The cliques of a record, once the record is shown to state what the plan computes.

    Nothing in the record is trusted: the unit, the selection share and every table's label,
    columns, scale and share are computed again from the plan, the record's rho and its
    cliques, and each must equal what the record states.
    """
    if dict(record.unit) != _unit_block(plan):
        raise _mismatch("its privacy unit differs")
    if dict(record.privacy_loss) != {"rho": decimal_text(plan.rho)}:
        raise _mismatch("its privacy loss differs")
    states = record.selection
    if plan.selects:
        if (
            not isinstance(states, Mapping)
            or states.get("rho") != decimal_text(selection_share(plan))
            or not isinstance(states.get("cliques"), list)
        ):
            raise _mismatch("its selection differs")
    elif states is not None:
        raise _mismatch("it states a selection, and this release chooses no cliques")
    if dict(record.options) != _options_block(plan):
        raise _mismatch("its options differ")
    cliques: List[Tuple[str, ...]] = []
    for each in record.tables:
        columns = each.get("columns") if isinstance(each, Mapping) else None
        if not isinstance(columns, list) or not all(isinstance(name, str) for name in columns):
            raise _mismatch("a table's columns are not names")
        if each.get("kind") in (RANK, *PAIRS):
            continue
        if len(columns) >= 2:
            cliques.append(tuple(columns))
    _, expected = _describe(plan, cliques)
    if [dict(each) for each in record.tables] != [table.as_record() for table in expected]:
        raise _mismatch("its tables, scales or shares differ")
    return tuple(cliques)
