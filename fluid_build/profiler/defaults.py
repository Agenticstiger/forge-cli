# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The statistics a profile carries without ``--bundle``, declared from the contract alone.

Without a Simulation to answer, the profiler declares, from the contract and never
from the records, the statistics ``fluxsim init`` reads, and writes them as a fidelity
block (SEAM-FIDELITY-SPEC §2). The registry's own grammar parses that block exactly as
it parses a Simulation's under ``--bundle``, so there is no second parser, and every
statistic passes the same output checks: ``check_edge_counts`` for the counts at a
column's edges (FLUX ADR 0012) and the ACRO rules for the rest.

- **A column typed** ``enum(...)``: ``levelHistogram/1`` over every level the type
  declares. A level no record holds is a cell of none, which the check withholds as
  ``C``, so the levels listed never depend on the records.
- **A number whose** ``validationRules`` **declare both ends of a range** inside its
  type: ``marginalHistogram/1`` over about ten bins of one width, exact at both ends (an
  integer range ends half a unit past its top, a whole unit on a grid of whole steps, so
  each value has a bin of its own). A side at or past the type's own bound declares
  nothing the type does not, and a range of one side is not closed: ten bins over the rest
  of the type would hold every record in one of them, so such a number takes the ladder.
- **Any other number of a bounded type, a date or a timestamp**: ``quantile/2`` at the
  lower quartile, the median and the upper quartile, on a ladder fixed by the type: 0,
  ±1, ±2, ±5, ±10, ±20, ±50 ... within the type's bounds for a number, one bin per year
  from 1900 to 2100 for a date or a timestamp. No histogram is declared on a ladder: its
  end bins hold no record, so the check can tie no published cell to an end of the range,
  and it withholds a quantile beside the histogram first. Measured on ages, amounts, ids
  and counts, a histogram on the ladder published nothing, and the quantiles beside it
  nothing.
- **A grouping the contract declares**, a categorical ``semantics.dimensions`` entry on
  an ``enum`` column: each other ``enum`` column's level histogram per level of the
  grouping. Every declared level is a group, one that no record holds included, so the
  groups listed never depend on the records either; such a group withholds its siblings.
  One column's groupings hold at most :data:`MOST_GROUPED_CELLS` cells together: one past
  that is left out, and named.

A column gets a histogram or quantiles, never both on one set of bins: checked together,
the two let a reader who knows the check's choices tell worlds apart, as
``tests/profiler/test_disclosure_oracle.py`` found. For the same reason the runner
withholds a column's ``distinctCount`` beside a level histogram, and a number's ``sum``,
``min``, ``max``, ``quantiles`` and ``distinctCount`` beside a histogram over its declared
range, where it forms no ``mean/1``.

A number with neither a range of two ends nor a bounded type (a ``decimal`` without
``(p,s)``) gets none, and the owner is told. A record outside what the contract
declares, a level its type does not list or a value outside its type or the range,
refuses the profile, as one outside a Simulation's block does. The bins of a range are
rounded outward to the grid, which can be coarser than the column's own scale, so each
value is checked against the range itself first (:class:`DeclaredRange`).

The tolerances and the ``records`` and ``budget`` sections are fields the grammar
requires of any block; a profile reads none of them.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "DeclaredRange",
    "DefaultPlan",
    "MOST_GROUPED_CELLS",
    "QUANTILE_LEVELS",
    "default_plan",
    "declared_range",
    "range_bins",
    "number_ladder",
]

#: The quantile levels declared on every number, date and timestamp: the quartiles and
#: the median, as the ``quantiles`` metric is measured.
QUANTILE_LEVELS: Tuple[str, ...] = ("0.25", "0.5", "0.75")

#: About how many bins a declared range is cut into.
RANGE_BINS = 10

#: The bounds an integer column is read within: FLUX writes every integer as int64.
_INT64 = (-(2**63), 2**63 - 1)

#: The widest number a bin edge spells: 10^38, the widest SQL ``DECIMAL``. A wider type or
#: declared bound gets no statistic: its edges could pass the 1,000 digits flux-stats
#: writes out, which would refuse the whole profile for one column.
_WIDEST = Fraction(10) ** 38

#: The bounds a date or a timestamp is read within, and the years its ladder steps by.
_FIRST_INSTANT = "0001-01-01T00:00:00Z"
_LAST_INSTANT = "9999-12-31T23:59:59.999Z"
_LADDER_YEARS = range(1900, 2101)

#: The smallest sub-unit edge a decimal's ladder takes: 2^-10, and never below the grid.
_FINEST_SUB_UNIT = 10

#: The most cells one column's grouped level histograms hold together, every declared level
#: of each grouping a group. Checking them together costs about the cube of the levels, so
#: a grouping past this is left out, and named: the contract alone decides, never a record.
MOST_GROUPED_CELLS = 10_000

_DISTANCE = {"type": "distance", "measure": "totalVariation", "max": "1"}
_ABSOLUTE = {"type": "absolute", "value": "1"}


@dataclass(frozen=True)
class DeclaredRange:
    """The range a number's ``validationRules`` declare, read exactly as they state it.

    The bins of a histogram over it are rounded outward to the grid, which can be coarser
    than the column's own scale, so the runner checks every value against this range
    before the value is counted: :meth:`holds` is exact, and a strict bound (``>``,
    ``<``) is not reached.
    """

    low: Fraction
    high: Fraction
    low_strict: bool = False
    high_strict: bool = False

    def holds(self, value: Fraction) -> bool:
        """Whether *value* lies in the range, each bound as strict as its rule."""
        above = value > self.low if self.low_strict else value >= self.low
        below = value < self.high if self.high_strict else value <= self.high
        return above and below


@dataclass(frozen=True)
class DefaultPlan:
    """The block a profile answers without ``--bundle``, and the columns it cannot cover.

    ``block`` is ``None`` when the contract declares nothing to answer. ``uncovered``
    names each number, date or timestamp column with no statistic, with why. ``ranges``
    holds the declared range of each column the block gives a histogram over one.
    """

    block: Optional[Dict[str, Any]]
    uncovered: Tuple[Tuple[str, str], ...] = ()
    ranges: Mapping[str, DeclaredRange] = field(default_factory=dict)


def declared_range(column: Mapping[str, Any]) -> Tuple[Optional[Fraction], Optional[Fraction]]:
    """The exact bounds a column's ``range`` validationRules declare: ``(low, high)``.

    The grammar is the one ``fluid test`` reads (``quality_engine.parse_range``), read
    without rounding (``parse_range_exact``). Several rules hold together, so the
    tightest bound of each side is kept. A rule the grammar does not read declares
    nothing, and so does a ``validationRules`` that is not a list.
    """
    (low, _), (high, _) = _declared_sides(column)
    return low, high


_Side = Tuple[Optional[Fraction], bool]


def _declared_sides(column: Mapping[str, Any]) -> Tuple[_Side, _Side]:
    """:func:`declared_range`'s two bounds, each with whether its rule is strict."""
    from fluid_build.providers.quality_engine import parse_range_exact

    low: _Side = (None, False)
    high: _Side = (None, False)
    rules = column.get("validationRules")
    for rule in rules if isinstance(rules, list) else ():
        if not isinstance(rule, Mapping) or str(rule.get("type") or "").lower() != "range":
            continue
        for operator, value in parse_range_exact(rule.get("constraint")):
            strict = operator in (">", "<")
            if operator in (">", ">="):
                if low[0] is None or value > low[0] or (value == low[0] and strict):
                    low = (value, strict)
            elif high[0] is None or value < high[0] or (value == high[0] and strict):
                high = (value, strict)
    return low, high


def _type_bounds(declared: Any) -> Tuple[Optional[Tuple[Fraction, Fraction]], bool]:
    """The bounds a number type holds every value within, and whether its scale is above 0.

    An integer type is read as int64. ``decimal(p,s)`` holds less than ``10^(p-s)`` either
    way. A decimal without ``(p,s)`` has no bound. The decimal is read as FLUID SQL/1
    reads one (``fluid_sql._types.column_type``), so ``decimal(0012, 02)`` is ``(12, 2)``.
    """
    from fluid_build.fluid_sql._types import NUMBER, column_type

    read = column_type(declared)
    if read is None or read.type.kind != NUMBER or read.is_float or read.type.scale is None:
        return None, False
    scale = read.type.scale
    if read.precision is None:  # an integer type
        return (Fraction(_INT64[0]), Fraction(_INT64[1])), False
    bound = Fraction(10) ** (read.precision - scale)
    if bound > _WIDEST:  # wider than any edge is spelled: no bound a bin can use
        return None, False
    return (-bound, bound), scale > 0


def _scale(declared: Any) -> Optional[int]:
    """A number type's static scale as FLUID SQL/1 reads it: 0 for an integer type."""
    from fluid_build.fluid_sql._types import column_type

    read = column_type(declared)
    return None if read is None or read.is_float else read.type.scale


def number_ladder(
    low: Fraction, high: Fraction, *, sub_unit: bool, fractional_bits: int
) -> Tuple[Fraction, ...]:
    """The edges 0, ±1, ±2, ±5, ±10, ... strictly inside ``(low, high)``, and both bounds.

    With *sub_unit*, also ±1/2, ±1/4 ... down to 2^-10 or the grid's step, whichever is
    coarser, so that a column of rates or shares has bins below 1. Every edge is a grid
    point: an integer, or a power of two the grid holds.
    """
    edges = {low, high}
    if low < 0 < high:
        edges.add(Fraction(0))
    magnitude = Fraction(1)
    while magnitude < max(-low, high):
        for step in (1, 2, 5):
            for edge in (magnitude * step, -magnitude * step):
                if low < edge < high:
                    edges.add(edge)
        magnitude *= 10
    if sub_unit:
        for power in range(1, min(_FINEST_SUB_UNIT, fractional_bits) + 1):
            for edge in (Fraction(1, 2**power), -Fraction(1, 2**power)):
                if low < edge < high:
                    edges.add(edge)
    return tuple(sorted(edges))


def range_bins(
    low: Fraction, high: Fraction, fractional_bits: int, *, integral: bool = False
) -> Tuple[Fraction, ...]:
    """About :data:`RANGE_BINS` bins over the declared range ``[low, high]``, and no wider.

    The edges are the range's two ends and the multiples of one width strictly between
    them: the narrowest of 2^-k (on the grid), 1, 2, 5, 10, 20, 50 ... that cuts the range
    into at most :data:`RANGE_BINS` bins. An *integral* column (an integer type, or a
    decimal of scale 0) takes whole widths only, and its range ends half a unit past
    ``high`` (a whole unit on a grid of whole steps, which holds no half), so the last
    value is not alone with the one below it in the last bin, which is closed at the top:
    ``between 1 and 5`` gets one bin per value. The ends are rounded outward to the grid.
    The grid can be coarser than the column's own scale, so an end bin can reach past a
    bound: the runner checks each value against the range itself before it is counted
    (:class:`DeclaredRange`).
    """
    step = Fraction(1, 2**fractional_bits)
    first = math.floor(low / step) * step
    top = high + (Fraction(1, 2) if fractional_bits >= 1 else 1) if integral else high
    last = math.ceil(top / step) * step
    span = high - low
    widths: List[Fraction] = (
        [] if integral else [Fraction(1, 2**power) for power in range(fractional_bits, 0, -1)]
    )
    magnitude = 1
    while not widths or widths[-1] * RANGE_BINS < span:
        widths.extend(Fraction(magnitude * scale) for scale in (1, 2, 5))
        magnitude *= 10
    width = next(each for each in widths if each * RANGE_BINS >= span)
    inner = []
    edge = (math.floor(first / width) + 1) * width
    while edge < last:
        inner.append(edge)
        edge += width
    return (first, *inner, last)


def _instant(year: int) -> str:
    return datetime(year, 1, 1, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decimal(value: Fraction) -> str:
    """An exact edge as a decimal string: every edge here is a terminating decimal."""
    if value.denominator == 1:
        return str(value.numerator)
    places = 0
    while (value * 10**places).denominator != 1:
        places += 1
    scaled = value * 10**places
    digits = str(abs(scaled.numerator)).rjust(places + 1, "0")
    sign = "-" if scaled < 0 else ""
    return f"{sign}{digits[:-places]}.{digits[-places:]}"


def _dimension_columns(expose: Mapping[str, Any], names: Sequence[str]) -> List[str]:
    """The columns a categorical ``semantics.dimensions`` entry groups by, in order, once.

    A dimension names its column by ``expr`` or, without one, by its ``name``; an
    ``expr`` that is not a bare column name is a computed grouping, not a column's.
    """
    semantics = expose.get("semantics")
    dimensions = semantics.get("dimensions") if isinstance(semantics, Mapping) else None
    found: List[str] = []
    for dimension in dimensions if isinstance(dimensions, list) else ():
        if not isinstance(dimension, Mapping) or dimension.get("type") != "categorical":
            continue
        column = dimension.get("expr") or dimension.get("name")
        if isinstance(column, str) and column in names and column not in found:
            found.append(column)
    return found


def default_plan(
    expose: Mapping[str, Any],
    columns: Sequence[Any],
    enum_levels: Callable[[str], Sequence[str]],
    fractional_bits: int,
) -> DefaultPlan:
    """The fidelity block the contract's declarations give *expose*, on the profile's grid.

    *columns* are the expose's profiled columns (``runner.ColumnPlan``: name and domain)
    and *enum_levels* reads the levels a FLUID ``enum(...)`` type declares, as the
    registry reads them (:meth:`StatisticsBackend.enum_levels`).
    """
    contract = expose.get("contract")
    entries = contract.get("schema") if isinstance(contract, Mapping) else None
    schema = {
        entry["name"]: entry
        for entry in (entries if isinstance(entries, list) else ())
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
    }
    declared: Dict[str, Any] = {}
    ranges: Dict[str, DeclaredRange] = {}
    properties: List[Dict[str, Any]] = []
    uncovered: List[Tuple[str, str]] = []
    levels: Dict[str, int] = {}
    for column in columns:
        entry = schema.get(column.name, {})
        declared_type = entry.get("type")
        if column.domain == "category":
            try:
                found = _levels(enum_levels, declared_type)
            except ValueError as refused:  # the registry's FLX-3001: a list it cannot read
                uncovered.append((column.name, f"its enum(...) type does not read ({refused})"))
                continue
            if found:
                levels[column.name] = found
                declared[column.name] = {"support": "declared", "maxCardinality": found}
            continue
        if column.domain == "timestamp":
            declared[column.name] = {"range": {"min": _FIRST_INSTANT, "max": _LAST_INSTANT}}
            ladder = [_FIRST_INSTANT, *(_instant(year) for year in _LADDER_YEARS), _LAST_INSTANT]
            properties.extend(_quantiles(column.name, ladder))
            continue
        bounds, sub_unit = _type_bounds(declared_type)
        (low, low_strict), (high, high_strict) = _declared_sides(entry)
        if bounds is not None:
            # A side at or past the type's own bound narrows nothing: bins over the rest
            # of the type would hold every record in one of them, and publish none.
            low = low if low is not None and low > bounds[0] else None
            high = high if high is not None and high < bounds[1] else None
        if low is not None and high is not None and max(-low, high) > _WIDEST:
            uncovered.append((column.name, "its validationRules range is wider than 10^38"))
        elif low is not None and high is not None and low < high:
            integral = _scale(declared_type) == 0
            edges = [
                _decimal(edge) for edge in range_bins(low, high, fractional_bits, integral=integral)
            ]
            declared[column.name] = {"range": {"min": edges[0], "max": edges[-1]}}
            ranges[column.name] = DeclaredRange(low, high, low_strict, high_strict)
            properties.append(
                {
                    "property": "marginalHistogram",
                    "column": column.name,
                    "bins": edges,
                    "tolerance": dict(_DISTANCE),
                }
            )
        elif low is not None and high is not None:
            uncovered.append((column.name, "its validationRules range is empty or inverted"))
        elif bounds is not None:  # no range, or one of one side: the type's own ladder
            steps = number_ladder(
                bounds[0], bounds[1], sub_unit=sub_unit, fractional_bits=fractional_bits
            )
            edges = [_decimal(edge) for edge in steps]
            declared[column.name] = {"range": {"min": edges[0], "max": edges[-1]}}
            properties.extend(_quantiles(column.name, edges))
        elif low is not None or high is not None:
            uncovered.append(
                (column.name, "its validationRules range bounds one side, and its type no side")
            )
        else:
            uncovered.append(
                (column.name, "neither its type nor a validationRules range bounds it within 10^38")
            )
    for name in levels:
        properties.append(
            {"property": "levelHistogram", "column": name, "tolerance": dict(_DISTANCE)}
        )
    grouped_cells: Dict[str, int] = {}
    for by in _dimension_columns(expose, list(levels)):
        for name in levels:
            if name == by:
                continue
            cells = grouped_cells.get(name, 0) + levels[by] * levels[name]
            if cells > MOST_GROUPED_CELLS:
                uncovered.append(
                    (
                        name,
                        f"its level histogram per level of {by!r}, past the "
                        f"{MOST_GROUPED_CELLS:,} cells one column's groupings may hold",
                    )
                )
                continue
            grouped_cells[name] = cells
            properties.append(
                {
                    "property": "levelHistogram",
                    "column": name,
                    "groupBy": by,
                    "tolerance": dict(_DISTANCE),
                }
            )
    if not properties:
        return DefaultPlan(block=None, uncovered=tuple(uncovered))
    block = {
        "encoding": {"fractionalBits": fractional_bits},
        "records": {"min": 1, "max": 1},
        "budget": {"maxChecksPerState": 1},
        "columns": declared,
        "properties": properties,
    }
    return DefaultPlan(block=block, uncovered=tuple(uncovered), ranges=ranges)


def _levels(enum_levels: Callable[[str], Sequence[str]], declared_type: Any) -> int:
    """How many levels a column's ``enum(...)`` type declares; 0 for any other type."""
    text = str(declared_type or "")
    if not re.match(r"\s*enum\s*\(", text, re.IGNORECASE):
        return 0
    return len(enum_levels(text))


def _quantiles(column: str, edges: Sequence[str]) -> List[Dict[str, Any]]:
    return [
        {
            "property": "quantile",
            "column": column,
            "level": level,
            "bins": list(edges),
            "tolerance": dict(_ABSOLUTE),
        }
        for level in QUANTILE_LEVELS
    ]
