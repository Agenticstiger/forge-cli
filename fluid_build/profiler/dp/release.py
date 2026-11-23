# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A profile released under differential privacy.

:func:`plan_release` reads the contract and the request and decides everything the release
needs that does not depend on a record: the privacy unit, the columns released and the
reason for every requested item that is not, each column's public levels or bins, and the
budget. Nothing is read. :func:`encode_rows` then reads the values of the records once and
refuses what the release cannot hold, naming a column and a row and never a value.
:func:`draw` bounds what each unit contributes, chooses the cliques, counts, adds noise,
makes the tables agree and writes the document (profile format 1-2-0, PROFILE-SPEC section
3a and 5a). :func:`build_record` and :func:`check_record` are the release record's side:
what a release needs to be drawn again inside the data owner's boundary.

The noise is drawn from a seeded randomness source recorded inside the data owner's
boundary, so the release can be re-derived there. Nothing that depends on the records
leaves outside the noise: no row count, no unit count, no count of rows dropped by the
bounding, no distinct or null count appears in the document, in a message or in the record.

The budget is a total ``rho`` of zero-concentrated differential privacy. Choosing cliques
takes a tenth of it when two or more categorical columns are released, and the rest is split
equally over the count tables, each share floored to 12 decimal places so the parts never add
up to more than the total. A table's noise is a discrete Gaussian whose scale is the least
``m / 10**6`` that spends no more than its share on a table whose squared L2 sensitivity is
:func:`~fluid_build.profiler.dp.bounding.sensitivity_squared`, in exact integers.
"""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import math
import re
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal, localcontext
from fractions import Fraction
from typing import (
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

from fluid_build.policy._common import iter_exposes
from fluid_build.profiler import defaults, document, runner, sources
from fluid_build.profiler._stats import Request, StatisticsBackend
from fluid_build.profiler.defaults import BOOLEAN_LEVELS, DeclaredRange
from fluid_build.profiler.dp import bounding, consistency, selection
from fluid_build.profiler.dp.bounding import ROWS, UnitPlan
from fluid_build.profiler.dp.discrete_gaussian import sample_discrete_gaussian
from fluid_build.profiler.dp.randomness import SeededSource
from fluid_build.profiler.dp.record import ReleaseRecord
from fluid_build.profiler.runner import ProfileError

__all__ = [
    "DELTA_TEXT",
    "EncodedRows",
    "MECHANISM",
    "RULE_NAME",
    "Release",
    "ReleaseRefused",
    "ReleasePlan",
    "ReleasedColumn",
    "ReleasedTable",
    "SCHEMA_URL_1_2_0",
    "build_record",
    "check_record",
    "check_selection_available",
    "decimal_text",
    "draw",
    "encode_rows",
    "epsilon_at_delta",
    "noise_label",
    "parse_rho",
    "plan_release",
    "scale_units",
    "split_budget",
]

#: Format 1-2-0 (``flux_stats.profile.SCHEMA_URL_1_2_0``; a test keeps the two equal).
SCHEMA_URL_1_2_0 = "https://agenticstiger.github.io/flux/engine/spec/profile/1-2-0/FluxProfile.json"

MECHANISM = "discreteGaussian"
RULE_NAME = "noisy"

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
    wanted = Fraction(sensitivity * 10 ** (2 * _SCALE_PLACES)) / (2 * share)
    ceiling = -((-wanted.numerator) // wanted.denominator)
    root = math.isqrt(ceiling)
    return root if root * root >= wanted else root + 1


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

    @property
    def categorical(self) -> Tuple[ReleasedColumn, ...]:
        return tuple(each for each in self.columns if each.categorical)

    @property
    def ordered(self) -> Tuple[ReleasedColumn, ...]:
        return tuple(each for each in self.columns if not each.categorical)

    @property
    def sensitivity(self) -> int:
        return bounding.sensitivity_squared(self.unit)

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
) -> ReleasePlan:
    """Plan a release of one table's requested statistics. Reads no record.

    Every refusal that does not need a record is made here: the unit, the table, what is
    released, the cells and the budget.
    """
    from fluid_build.profiler import request as asked_for

    unit_plan = bounding.plan_unit(
        contract,
        unit,
        release_table,
        unit_key=unit_key,
        max_rows_per_unit=max_rows_per_unit,
        max_rows_per_cell=max_rows_per_cell,
    )
    table = unit_plan.release_table
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
        set(unit_plan.key_columns)
        | set(bounding._primary_key(table, expose))
        | set(_relationship_columns(expose))
    )
    candidates = [
        column
        for column in plan.columns
        if asked.column(column.name) is not None and column.name not in keys
    ]
    fractional_bits = request.fractional_bits
    planned = defaults.default_plan(expose, candidates, backend.enum_levels, fractional_bits)
    covered: Mapping[str, Mapping[str, Any]] = (
        planned.block["columns"] if planned.block is not None else {}
    )
    bins_of: Dict[str, List[str]] = {}
    for each in planned.block["properties"] if planned.block is not None else ():
        if "bins" in each and each["column"] not in bins_of:
            bins_of[each["column"]] = list(each["bins"])
    uncovered = dict(planned.uncovered)

    released: List[ReleasedColumn] = []
    why_not: Dict[str, str] = {}
    for column in candidates:
        entry = schema.get(column.name, {})
        ask = asked.column(column.name)
        assert ask is not None
        statistic = (
            "levelHistogram/1" if column.domain == runner.CATEGORY else "marginalHistogram/1"
        )
        if entry.get("required") is not True:
            why_not[column.name] = (
                "the contract does not declare it required: true, and a record with no value "
                "is refused, never counted"
            )
        elif statistic not in ask.statistics:
            why_not[column.name] = f"the request does not list {statistic} for it"
        elif column.domain == runner.CATEGORY:
            if column.name not in covered:
                why_not[column.name] = uncovered.get(
                    column.name, "its type declares no levels, as an enum(...) or a boolean does"
                )
            else:
                declared_type = entry.get("type")
                found = (
                    BOOLEAN_LEVELS
                    if defaults._is_boolean(declared_type)
                    else tuple(backend.enum_levels(str(declared_type)))
                )
                released.append(
                    ReleasedColumn(column.name, column.domain, levels=tuple(sorted(set(found))))
                )
        elif column.name in bins_of:
            texts = bins_of[column.name]
            if column.domain == runner.TIMESTAMP:
                edges = tuple(
                    Fraction(backend.read_value("timestamp", text, column=column.name, row_index=0))
                    for text in texts
                )
            else:
                edges = tuple(Fraction(text) for text in texts)
            released.append(
                ReleasedColumn(
                    column.name,
                    column.domain,
                    edges=edges,
                    edge_texts=tuple(decimal_text(edge) for edge in edges),
                    declared=planned.ranges.get(column.name),
                )
            )
        else:
            why_not[column.name] = uncovered.get(
                column.name, "it has neither a declared range nor a type that bounds it"
            )
    if not released:
        raise _refuse(
            "profile_release_nothing_to_release",
            f"no requested column of {table!r} can be released: "
            + "; ".join(f"{name}: {reason}" for name, reason in sorted(why_not.items())),
            "list levelHistogram/1 for a required enum column, or marginalHistogram/1 for a "
            "required number, date or timestamp column that is not a key",
            read=True,
        )
    for name in keys & set(readable):
        if asked.column(name) is not None:
            why_not.setdefault(name, "a key column: its values name an entity")

    # The cells: a column of two or more levels holds at least its size in any structure.
    cells = sum(each.size for each in released if each.size >= 2)
    if cells > selection.MOST_RELEASE_CELLS or any(
        each.size > selection.MOST_RELEASE_CELLS for each in released
    ):
        raise _refuse(
            "profile_release_too_many_cells",
            f"the columns released hold more cells than the {selection.MOST_RELEASE_CELLS:,} "
            "a release may measure",
            "list fewer columns in the request",
            read=True,
        )
    categorical = [each for each in released if each.categorical]
    selects = len(categorical) >= 2
    if selects and len(categorical) > selection.MOST_SELECTION_COLUMNS:
        raise _refuse(
            "profile_release_too_many_columns",
            f"{len(categorical)} categorical columns are released, and choosing cliques is "
            f"offered at most {selection.MOST_SELECTION_COLUMNS}",
            "list fewer categorical columns in the request",
            read=True,
        )
    # At most one table per released column, so no table gets a share below the least check.
    split_budget(rho, selects, len(released))

    read_names = {each.name for each in released} | set(unit_plan.key_columns)
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
        not_released=_not_released(
            request, table, frozenset(c.name for c in released), why_not, skipped
        ),
        rho=rho,
        selects=selects,
        fractional_bits=fractional_bits,
        dimensions=frozenset(defaults._dimension_columns(expose, [c.name for c in categorical])),
        has_other_tables=bool(others),
    )


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
            continue
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
    return tuple(found)


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


_OUTSIDE = "profile_record_outside_declared_domain"


def _row_error(event: str, column: str, row: int, what: str) -> ReleaseRefused:
    return ReleaseRefused(
        event,
        f"column {column!r} row {row}: {what}",
        {
            "next": "fix the records, or the contract's column type, enum levels or "
            "validationRules range, then release"
        },
    )


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
        found = exact[name]
        for index, value in enumerate(found):
            if value is None:
                raise _row_error(
                    "profile_release_null",
                    name,
                    index,
                    "no value, and the release needs one in every row",
                )
        if each.categorical:
            position = {level: place for place, level in enumerate(each.levels)}
            placed = []
            for index, value in enumerate(found):
                if value not in position:
                    raise _row_error(
                        _OUTSIDE, name, index, "a value outside the levels its type declares"
                    )
                placed.append(position[value])
            levels[name] = each.levels
            cells[name] = placed
        else:
            for index, value in enumerate(found):
                inside = each.edges[0] <= value <= each.edges[-1]
                if not inside or (each.declared is not None and not each.declared.holds(value)):
                    raise _row_error(
                        _OUTSIDE,
                        name,
                        index,
                        "a value outside the range its type or validationRules declare",
                    )
            cells[name] = backend.bin_cells(found, each.edges, plan.fractional_bits)
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
    order = [each.name for each in plan.columns]
    contents = [
        bounding.encode_values([exact[name][index] for name in order]) for index in range(rows)
    ]
    return EncodedRows(cells=cells, units=keyed, contents=contents, levels=levels)


# ---------------------------------------------------------------------------
# The tables of a release, and the release
# ---------------------------------------------------------------------------

CLIQUE, LEVELS, BINS = "clique", "levels", "bins"


@dataclass(frozen=True)
class ReleasedTable:
    """One count table of the release: what the record states of it."""

    label: str
    columns: Tuple[str, ...]
    kind: str
    sizes: Tuple[int, ...]
    #: The scale as the document spells it, and the exact integer it is ``m / 10**6`` of.
    scale: str
    scale_units: int
    #: The share of the budget, as the document spells it.
    share: str

    @property
    def cells(self) -> int:
        return math.prod(self.sizes)

    def as_record(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "columns": list(self.columns),
            "scale": self.scale,
            "share": self.share,
        }


@dataclass(frozen=True)
class Release:
    """A drawn release: the document, its digest and what a report may say about it."""

    document: Dict[str, Any]
    canonical: bytes
    digest: str
    tables: Tuple[ReleasedTable, ...]
    #: The maximal cliques released (two or more columns each).
    cliques: Tuple[Tuple[str, ...], ...]
    #: The cliques the selection chose, ``None`` when no selection ran.
    selected: Optional[Tuple[Tuple[str, ...], ...]]
    selection_rho: Fraction
    #: Each table's share, and what the tables spend together.
    share: Fraction
    #: Each table's noisy values before the tables are made to agree, then after, exact.
    noisy: Tuple[Tuple[Fraction, ...], ...] = field(default=(), repr=False)
    consistent: Tuple[Tuple[Fraction, ...], ...] = field(default=(), repr=False)

    @property
    def measurement_rho(self) -> Fraction:
        return self.share * len(self.tables)


def _layout(
    plan: ReleasePlan, cliques: Sequence[Sequence[str]]
) -> List[Tuple[str, Tuple[str, ...], Tuple[int, ...]]]:
    """The count tables in document order: ``(kind, columns, sizes)``.

    The cliques as given, then a level histogram for each categorical column in none of them,
    then a histogram for each number, date or timestamp column; the last two in column order.
    """
    names = [each.name for each in plan.categorical]
    position = {name: place for place, name in enumerate(names)}
    seen: List[FrozenSet[str]] = []
    tables: List[Tuple[str, Tuple[str, ...], Tuple[int, ...]]] = []
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
                "a clique is not two or more distinct released categorical columns in the "
                "contract's order",
            )
        seen.append(frozenset(members))
        tables.append((CLIQUE, members, tuple(plan.column(n).size for n in members)))
    covered = set().union(*seen) if seen else set()
    for column in plan.categorical:
        if column.name not in covered:
            tables.append((LEVELS, (column.name,), (column.size,)))
    for column in plan.ordered:
        tables.append((BINS, (column.name,), (column.size,)))
    return tables


def _describe(
    plan: ReleasePlan, cliques: Sequence[Sequence[str]]
) -> Tuple[Fraction, Tuple[ReleasedTable, ...]]:
    """The share of each table and the tables, checked against the cells a release may hold.

    The cells are added up, and refused when too many, before any table is allocated.
    """
    layout = _layout(plan, cliques)
    cells = sum(math.prod(sizes) for _, _, sizes in layout)
    if cells > selection.MOST_RELEASE_CELLS:
        raise _refuse(
            "profile_release_too_many_cells",
            "the tables of the release hold more cells than the "
            f"{selection.MOST_RELEASE_CELLS:,} a release may measure",
            "list fewer columns in the request",
        )
    labels = [noise_label(columns) for _, columns, _ in layout]
    if len(set(labels)) != len(labels) or any("\x00" in label for label in labels):
        raise _refuse(
            "profile_release_column_name",
            "two count tables would draw their noise from one stream, or a column name holds a "
            "zero character",
            "rename a column of the contract",
        )
    _, share = split_budget(plan.rho, plan.selects, len(layout))
    units = scale_units(share, plan.sensitivity)
    scale = decimal_text(Fraction(units, 10**_SCALE_PLACES))
    tables = tuple(
        ReleasedTable(
            label=label,
            columns=columns,
            kind=kind,
            sizes=sizes,
            scale=scale,
            scale_units=units,
            share=decimal_text(share),
        )
        for label, (kind, columns, sizes) in zip(labels, layout, strict=True)
    )
    return share, tables


def selection_share(plan: ReleasePlan) -> Fraction:
    """A tenth of the total, when two or more categorical columns are released, else nothing."""
    return plan.rho / 10 if plan.selects else Fraction(0)


def _pair_weights(plan: ReleasePlan) -> Dict[Tuple[str, str], int]:
    names = [each.name for each in plan.categorical]
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
    names = [each.name for each in plan.categorical]
    levels = {each.name: each.levels for each in plan.categorical}
    chosen_rows = [tuple(levels[name][rows.cells[name][i]] for name in names) for i in kept]
    run = select if select is not None else selection.select_cliques
    try:
        chosen = run(
            chosen_rows,
            names,
            levels,
            _pair_weights(plan),
            selection_share(plan),
            bounding.selection_distance(plan.unit),
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
) -> Release:
    """Bound, choose, count, add noise, make the tables agree and write the document.

    With *cliques* (the record's, on a re-derivation) no selection runs. *select* and
    *structure* stand in for :func:`selection.select_cliques` and
    :func:`selection.release_structure`.
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
                [each.name for each in plan.categorical],
                [each.size for each in plan.categorical],
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
                    "next": "list fewer categorical columns in the request, so the cliques "
                    "chosen hold fewer cells; releasing again spends a new loss on the records"
                },
            ) from None
        released = tuple(tuple(clique) for clique in joined)
    else:
        released = ()
    try:
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
    sigma2 = Fraction(tables[0].scale_units ** 2, 10 ** (2 * _SCALE_PLACES))

    kept_units = [rows.units[i] for i in kept]
    noisy: List[Tuple[Fraction, ...]] = []
    count_tables: List[consistency.CountTable] = []
    for table in tables:
        cell = [0] * len(kept)
        for name, size in zip(table.columns, table.sizes, strict=True):
            index = rows.cells[name]
            cell = [value * size + index[i] for value, i in zip(cell, kept, strict=True)]
        counts = bounding.capped_counts(
            kept_units, cell, table.cells, unit.max_rows_per_cell if unit.mode != ROWS else 1
        )
        source = SeededSource(seed, table.label)
        values = tuple(
            Fraction(count + sample_discrete_gaussian(sigma2, source)) for count in counts
        )
        noisy.append(values)
        count_tables.append(
            consistency.CountTable(table.label, table.columns, table.sizes, values, sigma2)
        )
    try:
        forest = consistency.junction_forest(count_tables)
        agreed = consistency.make_consistent(count_tables)
        texts = consistency.to_decimal_text(count_tables, agreed, forest)
    except ValueError as error:
        raise ReleaseRefused(
            "profile_release_not_a_forest",
            f"the tables of the release cannot be made to agree: {error}",
            {"next": "report it with your forge-cli version"},
        ) from None
    _agree(tables, texts)

    built = _document(plan, tables, texts, share, producer)
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
        consistent=agreed,
    )


def _noise_block(table: ReleasedTable) -> Dict[str, Any]:
    return {
        "mechanism": MECHANISM,
        "scale": table.scale,
        "privacyLoss": {"rho": table.share},
    }


def _document(
    plan: ReleasePlan,
    tables: Sequence[ReleasedTable],
    texts: Sequence[Sequence[str]],
    share: Fraction,
    producer: Optional[str],
) -> Dict[str, Any]:
    columns: Dict[str, Dict[str, Any]] = {
        name: {"domain": domain} for name, domain in plan.table_columns
    }
    cliques: List[Dict[str, Any]] = []
    for table, cells in zip(tables, texts, strict=True):
        if table.kind == CLIQUE:
            cliques.append(
                {
                    "definition": "cliqueMarginal/1",
                    "columns": list(table.columns),
                    "levels": [list(plan.column(name).levels) for name in table.columns],
                    "noisyCounts": list(cells),
                    "noise": _noise_block(table),
                }
            )
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
    out_tables: Dict[str, Any] = {plan.table: released}
    for name, listed in plan.other_tables:
        out_tables[name] = {"columns": {column: {"domain": domain} for column, domain in listed}}
    if producer is None:
        from fluid_build import __version__

        producer = document.producer_uri(__version__)
    return {
        "_producer": producer,
        "_schemaURL": SCHEMA_URL_1_2_0,
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
    cliques: List[Tuple[str, ...]] = []
    for each in record.tables:
        columns = each.get("columns") if isinstance(each, Mapping) else None
        if not isinstance(columns, list) or not all(isinstance(name, str) for name in columns):
            raise _mismatch("a table's columns are not names")
        if len(columns) >= 2:
            cliques.append(tuple(columns))
    _, expected = _describe(plan, cliques)
    if [dict(each) for each in record.tables] != [table.as_record() for table in expected]:
        raise _mismatch("its tables, scales or shares differ")
    return tuple(cliques)
