# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What may leave: output checks applied to every aggregate before it is written.

The defaults are ACRO's (https://github.com/AI-SDC/ACRO, MIT, ``acro/default.yaml``
at v1.1.0), as flux-engine ADR 0003 (amended 2026-11-16) and PROFILE-SPEC §2 name
them:

* a cell of fewer than ``minimumCell`` (10) records is withheld (``safe_threshold``);
* a zero cell is withheld when zeros are disclosive (``zeros_are_disclosive``);
* every published count is rounded to a multiple of ``roundingBase`` (5,
  ``safe_round_base``);
* a magnitude (a sum) is withheld when its ``n`` largest contributions make up at
  least ``k`` of it (``safe_nk_n`` 2, ``safe_nk_k`` 0.90), or when what is left
  after the two largest is below ``p`` of the largest (``safe_pratio_p`` 0.10);
* no noise is added.

A sum is a cell of a magnitude table, so it also needs ``minimumCell`` records
that contribute to it (a non-zero value), and it is withheld when its records hold
at most two values, where it would count them: ACRO's all-values-the-same check
withholds the one-value case (:func:`check_sum`).

A mean is checked as a magnitude table's cell too, which is how the output-checking
guidelines class means (with totals: at least the threshold of units, and no
dominant contributor) and how ACRO checks a mean cell. Its ``k`` is a count of
records and is rounded like one, and its ``Sx``, the records' sum on the grid,
publishes only where the column's ``sum`` would. Next to that exact sum, ``Sx``
also gives the total of what the values lose below their grid points, so that
total is checked as a sum as well (:func:`check_mean`).

A minimum, a maximum or a column's ``quantiles`` metric is one record's value in
most data. The DwB/ESSnet "Guidelines for Output Checking" (class 3, maxima,
minima and percentiles) do not release one by rule of thumb, and clear one when
more than the threshold of units hold it. The profiler takes that reading: such a
value is published only when at least ``minimumCell`` records hold exactly that
value (founder decision, 2026-11-16). A published minimum or maximum says that no
record lies beyond it, and no check withholds it for that (FLUX ADR 0012, amended
2026-11-18). This leaves the checks inconsistent at the ends of the range: where
zeros are disclosive, a quantile whose witness is the column's smallest value is
withheld because no record lies below the witness, though a published minimum
already shows that, and one whose witness is the largest value is withheld because
no record lies above it, though a published maximum already shows that.

A DATE or timestamp column's two ends are top and bottom coded where the exact value
fails that check, as SDC tools do (sdcMicro ``topBotCoding``; ARX's date hierarchies,
day to month to year, as a global recoding): :func:`round_extreme` takes the UTC
calendar buckets that hold the smallest (or largest) value, from the finest (the day,
for a timestamp; for a DATE the exact value is the day), the month, the year, and
publishes the first that holds at least ``minimumCell`` records, as the bucket's
first value (``min``) or last value at the column's resolution (``max``: its last day
for a DATE, its last millisecond for a timestamp), status ``F``. The value is clipped
to the range the contract declares, which is public. No bucket up to the year holding
that many records leaves the end ``A``. This says nothing a date span's outer edge
does not (:func:`fluid_build.profiler.defaults.date_span` publishes an edge only where
each end bin holds at least ``minimumCell`` records): a published end says only that
its bucket holds at least ``minimumCell`` records and that no record lies beyond the
bucket, so a bucket of that many records places no record in it. The unit is chosen
from bucket counts that are each at least ``minimumCell`` or not used, so a unit that
was passed over says only that its bucket was too small, as a withheld cell does.

A verdict names the check that fired with an SDMX ``CL_CONF_STATUS`` 1.4 code:
``A`` for a small count (a zero cell, a cell below the threshold, or a value too
few records hold), ``O`` and ``T`` for dominance by one or two records, ``M`` for
the p-ratio, another measure of concentration, ``C`` for a sum whose records
hold at most two values, and ``D`` for a group the sender withholds so that
another cannot be recovered (secondary suppression, :func:`suppress_secondary`).
A withheld value is written with that code and no value, except in a count table
or a quantile (:data:`ONE_CODE`, PROFILE-SPEC §3), whose withheld groups and cells
are all written ``C``, whatever withheld them: a code that named the check would
tell a reader a range, ``A`` that a cell holds fewer than ``minimumCell`` records,
and a total the reader knows can turn the range into the count. ``C`` says nothing
of a cell's size, though where a date statistic would otherwise have shown a zero it
says the cell is not empty (below). The profiler's
summary still counts them by the check, as ACRO's outcome table keeps each
suppressed cell's reasons apart from the table it releases.

The groups of a mean or a support the Simulation's fidelity block declares are
checked group by group, each as a cell of a table of records (the ESSnet/DwB output
checking guidelines' frequency-table and magnitude-table classes, which ACRO's
``safe_threshold`` and ``safe_nk`` checks implement): :func:`check_group_count`
for the group's record count, :func:`check_support` for a support's members.

The counts a histogram, a level histogram or a quantile publishes at the edges of
its column are not checked here. FLUX ADR 0012 sets one rule for all of them, which
flux-stats applies (``flux_stats.edge_counts.check_edge_counts``, called through
:mod:`fluid_build.profiler._stats`): one declared partition, three cells for a
quantile, a group's size, no small cell a reader can form by adding or subtracting
published counts, and only then the rounding. What it leaves readable is its own to
say. The profiler passes it every such statistic of a column in one call, with this
module's :func:`round_count`.

What the group checks leave readable, on purpose:

* A grouped statistic lists every group the data forms, published or withheld.
  A key is always a level the bundle declares (a value outside the declared
  support is refused), so its presence says only that the level holds at least
  one record.
* When one group of a grouped mean or support is withheld, every other group of
  it is withheld too, under ``D``, so the withheld one cannot be recovered by
  differencing against the ungrouped figures. The records a ``groupBy`` null
  leaves out of every group count as one more group there, never written.

What the date statistics leave readable, on purpose:

* Where a profile chooses between a published zero and a withheld cell, the choice
  reads the records. A level of a date pair's ``order`` that a contract rule empties
  is published as ``0`` only when no record lies there, ``structural: ["childless"]``
  is stated only when no parent lacks a child, and a date span or a pair's day
  histogram takes its grain, or is left out, by whether its end bins hold
  ``minimumCell`` records. So a withheld level a rule empties, a missing
  ``childless`` or a coarser grain tells a reader that at least one record lies
  there: never how many beyond what ``A`` would say, and never a value.
* A relationship's day histogram counts child rows, so the children of one parent
  can fill an end bin, and a published outer edge then bounds the days from that
  parent's date to its children's to that bin: an interval, as a threshold on rows
  bounds one record's value in any histogram.

Where this differs from ACRO 1.1.0, on purpose:

* ACRO's threshold mask is ``count < safe_threshold``, so it also catches a zero
  cell whatever ``zeros_are_disclosive`` says. Here the threshold applies to a
  count of one or more records, and ``zerosDisclosive`` alone governs a zero, so a
  rule that declares zeros safe publishes them. Under the defaults both withhold
  every zero.
* ACRO answers "review" (a human decides) for a magnitude with a negative
  contribution, because dominance is defined for non-negative values. No human
  reviews this output, so the checks read each contribution's absolute value:
  what an intruder learns about the largest contributor is its size.
* ACRO rounds only when asked (its ``round`` mitigation). The ADR makes rounding
  a default, and here it applies to every count a document publishes, a distinct
  count included: an exact distinct count of an identifier column would give the
  exact row count back.

These are output-checking conventions only. No privacy or anonymity guarantee is
made or implied; ACRO's tools assist human output checkers and do not replace
them.

Everything here is integer and rational arithmetic with no I/O. A verdict's
reason names the rule that fired, never a value.
"""

from __future__ import annotations

import calendar
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date as _date
from fractions import Fraction
from typing import (
    AbstractSet,
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

__all__ = [
    "ACRO_DEFAULT",
    "CONCENTRATION",
    "ONE_CODE",
    "DOMINANCE_BY_ONE",
    "DOMINANCE_BY_TWO",
    "FREE",
    "MAX_COUNT",
    "PUBLISHED",
    "SECONDARY",
    "SMALL_COUNT",
    "WITHHELD",
    "DisclosureRule",
    "Exact",
    "Verdict",
    "check_count",
    "check_group_count",
    "check_magnitude",
    "check_mean",
    "check_order_statistic",
    "check_rounding",
    "check_sum",
    "check_support",
    "pinned_children_cells",
    "round_extreme",
    "round_count",
    "sample_size",
    "suppress_differencing",
    "suppress_secondary",
]

# SDMX ``CL_CONF_STATUS`` 1.4 codes, with PROFILE-SPEC §3's paraphrase of each
# label (the SDMX registry holds the official labels).

#: Free for publication.
FREE = "F"
#: Primary suppression: small counts.
SMALL_COUNT = "A"
#: Primary suppression: dominance by one unit.
DOMINANCE_BY_ONE = "O"
#: Primary suppression: dominance by two units.
DOMINANCE_BY_TWO = "T"
#: Primary suppression: other measures of concentration.
CONCENTRATION = "M"
#: Withheld statistical information.
WITHHELD = "C"

#: Secondary suppression set by the sender: a group that passed every check on its own
#: and is withheld so that a withheld sibling cannot be recovered from it.
SECONDARY = "D"

#: The declared statistics whose every withheld group and cell is written ``C``, whatever
#: withheld it (PROFILE-SPEC §3; the runner writes them from the decisions of flux-stats'
#: ``check_edge_counts``): the count tables, a histogram over bins and one over levels,
#: and a quantile.
ONE_CODE = frozenset({"marginalHistogram", "levelHistogram", "quantile"})


#: The largest count a profile can carry: 2^53 - 1, as RFC 8785 serialises it.
MAX_COUNT = 2**53 - 1

#: An exact number: a contribution to a sum is a fraction, or a whole number.
Exact = Union[int, Fraction]

#: PROFILE-SPEC's ``UnitInterval``: a decimal string from 0 to 1, with no sign.
_UNIT_INTERVAL = re.compile(r"(0(\.[0-9]+)?|1(\.0+)?)\Z")


@dataclass(frozen=True)
class DisclosureRule:
    """One named set of output-checking parameters, as a profile names it (§2).

    The two ratios are kept as the decimal strings the rule is written with
    (``"0.90"``, as ACRO's own file writes it), so a profile names the rule as
    configured; :attr:`k` and :attr:`p` are their exact values.
    """

    name: str
    minimum_cell: int
    zeros_disclosive: bool
    rounding_base: int
    dominance_n: int
    dominance_k: str
    p_ratio: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("a disclosure rule's name is a non-empty string")
        if unicodedata.normalize("NFC", self.name) != self.name:
            raise ValueError("a disclosure rule's name is in Unicode Normalization Form C")
        if not isinstance(self.zeros_disclosive, bool):
            raise ValueError("zeros_disclosive is true or false")
        for label, number in (
            ("minimum_cell", self.minimum_cell),
            ("rounding_base", self.rounding_base),
            ("dominance_n", self.dominance_n),
        ):
            if isinstance(number, bool) or not isinstance(number, int):
                raise ValueError(f"{label} is a whole number")
            if not 1 <= number <= MAX_COUNT:
                raise ValueError(f"{label} lies from 1 to 2^53 - 1")
        for label, text in (("dominance_k", self.dominance_k), ("p_ratio", self.p_ratio)):
            if not isinstance(text, str) or not _UNIT_INTERVAL.match(text):
                raise ValueError(f"{label} is a decimal string from 0 to 1, with no sign")

    @property
    def k(self) -> Fraction:
        """The dominance share, exactly."""
        return Fraction(self.dominance_k)

    @property
    def p(self) -> Fraction:
        """The p-ratio, exactly."""
        return Fraction(self.p_ratio)

    def to_document(self) -> Dict[str, Any]:
        """The rule as ``disclosureRules.<name>`` spells it: every field was applied."""
        return {
            "minimumCell": self.minimum_cell,
            "zerosDisclosive": self.zeros_disclosive,
            "roundingBase": self.rounding_base,
            "dominance": {"n": self.dominance_n, "k": self.dominance_k},
            "pRatio": self.p_ratio,
        }


#: ACRO 1.1.0's defaults, under the name PROFILE-SPEC's example gives them.
ACRO_DEFAULT = DisclosureRule(
    name="acro-default",
    minimum_cell=10,
    zeros_disclosive=True,
    rounding_base=5,
    dominance_n=2,
    dominance_k="0.90",
    p_ratio="0.10",
)


@dataclass(frozen=True)
class Verdict:
    """What a check decided: :data:`FREE`, or the code a value is withheld under."""

    status: str
    reason: str

    @property
    def published(self) -> bool:
        return self.status == FREE


#: The verdict of a value every check passed.
PUBLISHED = Verdict(FREE, "passed every check")


def _require_count(count: Any) -> int:
    if isinstance(count, bool) or not isinstance(count, int):
        raise TypeError("a count is a whole number")
    if not 0 <= count <= MAX_COUNT:
        raise ValueError("a count lies from 0 to 2^53 - 1")
    return count


def check_count(count: int, rule: DisclosureRule) -> Verdict:
    """Check a count of records: a cell, a column's non-null values, a table's rows."""
    _require_count(count)
    if count == 0:
        if rule.zeros_disclosive:
            return Verdict(SMALL_COUNT, "a zero cell, and the rule holds zeros disclosive")
        return PUBLISHED
    if count < rule.minimum_cell:
        return Verdict(SMALL_COUNT, f"fewer than {rule.minimum_cell} records")
    return PUBLISHED


def round_count(count: int, rule: DisclosureRule) -> int:
    """*count* rounded to the nearest multiple of the rule's base.

    A tie goes to the even multiple, as Python's and numpy's ``round`` settle one
    (ACRO's ``round_table`` rounds through numpy). An odd base, 5 among them, has
    no tie for a whole count.
    """
    _require_count(count)
    base = rule.rounding_base
    quotient, remainder = divmod(count, base)
    if 2 * remainder > base or (2 * remainder == base and quotient % 2 == 1):
        quotient += 1
    rounded = quotient * base
    return rounded if rounded <= MAX_COUNT else rounded - base


def check_rounding(count: int, rule: DisclosureRule) -> Verdict:
    """Withhold a positive count that rounds to zero: published, it would read as a zero.

    A distinct count of 1 or 2 rounds to 0 under base 5, and a rule with a threshold
    below half its base lets a record count do the same. A published 0 claims a zero
    cell, which the threshold and the zero rule exist to keep out of a document.
    """
    if count > 0 and round_count(count, rule) == 0:
        return Verdict(SMALL_COUNT, "a positive count that rounds to zero")
    return PUBLISHED


def _rounding_interval(rounded: int, base: int) -> Tuple[int, int]:
    """The counts that :func:`round_count` takes to *rounded*, as a closed interval."""
    half = base // 2
    if base % 2 == 1 or (rounded // base) % 2 == 0:
        return max(rounded - half, 0), rounded + half
    # An even base sends each tie to the even multiple, which this is not.
    return max(rounded - half + 1, 0), rounded + half - 1


def sample_size(count: int, rule: DisclosureRule) -> Dict[str, int]:
    """The band the number of records behind a statistic lies in (PROFILE-SPEC §3).

    *count* is that number. When the rule withholds it as small, the band is the
    one every withheld small count shares, so it says no more than the status
    ``A`` does. Otherwise the band holds every count that rounds to the same
    published value and that the rule would also publish, so it says no more
    than the rounded count does.
    """
    if not check_count(count, rule).published:
        lowest = 0 if rule.zeros_disclosive else 1
        return {"min": lowest, "max": rule.minimum_cell - 1}
    rounded = round_count(count, rule)
    low, high = _rounding_interval(rounded, rule.rounding_base)
    first_published = max(low, rule.minimum_cell)
    zero_published = low == 0 and not rule.zeros_disclosive
    lowest = 0 if zero_published else first_published
    highest = high if first_published <= high else 0
    return {"min": lowest, "max": min(highest, MAX_COUNT)}


def _on_common_scale(values: Sequence[Exact]) -> Tuple[int, List[int]]:
    """*values* times their least common denominator, as integers, and that multiplier.

    Every check on a sum below reads counts of values, which values are equal, and
    ratios of sums, and none of them changes when every contribution is multiplied by
    one positive number. So they run on integers, which Python adds, sorts and
    compares much faster than fractions, and decide exactly as they would on the
    fractions.
    """
    exact = [value if isinstance(value, (int, Fraction)) else Fraction(value) for value in values]
    common = math.lcm(*(value.denominator for value in exact))
    return common, [value.numerator * (common // value.denominator) for value in exact]


def check_magnitude(contributions: Iterable[Exact], rule: DisclosureRule) -> Verdict:
    """Check a sum, one contribution per record: dominance (n, k), then the p-ratio.

    Each contribution counts by its absolute value (see the module docstring). A
    total of zero is published: no contribution dominates it.
    """
    _, scaled = _on_common_scale(list(contributions))
    sizes = sorted((abs(value) for value in scaled), reverse=True)
    total = sum(sizes)
    if total == 0:
        return PUBLISHED
    share = rule.k * total
    if sizes[0] >= share:
        return Verdict(
            DOMINANCE_BY_ONE, f"one record makes up at least {rule.dominance_k} of the total"
        )
    if rule.dominance_n >= 2 and sum(sizes[:2]) >= share:
        return Verdict(
            DOMINANCE_BY_TWO, f"two records make up at least {rule.dominance_k} of the total"
        )
    if rule.dominance_n >= 3 and sum(sizes[: rule.dominance_n]) >= share:
        return Verdict(
            CONCENTRATION,
            f"{rule.dominance_n} records make up at least {rule.dominance_k} of the total",
        )
    rest = total - sum(sizes[:2])
    if rest < rule.p * sizes[0]:
        return Verdict(
            CONCENTRATION,
            f"what is left after the two largest records is below {rule.p_ratio} of the largest",
        )
    return PUBLISHED


def check_sum(contributions: Sequence[Exact], rule: DisclosureRule) -> Verdict:
    """Check a sum the way a magnitude table's cell is checked, one contribution per record.

    In order, the first that fires decides:

    1. fewer than ``minimumCell`` records with a non-zero value is a small cell (``A``):
       a sum of 0s and 1s is the count of the 1s, and ACRO's dominance and p-ratio alone
       publish a 3 (as its own defaults do);
    2. records that hold at most two values withhold it (``C``): with those values
       published as the ``min`` and the ``max``, the sum is a weighted count of each and
       gives both counts back exactly, which rounding hides everywhere else (a sum of 0s
       and 1s is the count of the 1s). ACRO's ``AllSameValuesCheck`` flags the
       one-value case;
    3. :func:`check_magnitude`, dominance then the p-ratio.
    """
    _, values = _on_common_scale(contributions)
    contributors = sum(1 for value in values if value != 0)
    counted = check_count(contributors, rule)
    if not counted.published:
        if contributors == 0:
            return counted
        return Verdict(SMALL_COUNT, f"fewer than {rule.minimum_cell} records contribute to the sum")
    distinct = set(values)
    if len(distinct) <= 2 and distinct != {0} and distinct:
        # An all-zero sum is 0 whatever the count, so it gives no count back.
        return Verdict(WITHHELD, "the records hold at most two values, so the sum is a count")
    return check_magnitude(values, rule)


def check_mean(
    values: Sequence[Exact],
    encoded: Sequence[int],
    fractional_bits: int,
    rule: DisclosureRule,
) -> Verdict:
    """Check an ungrouped ``mean/1``: ``k`` records, and ``Sx``, their values' sum on the grid.

    *values* are the column's non-null values and *encoded* the same values on
    the grid of ``2**-fractional_bits`` steps, each rounded toward -infinity. In
    order, the first that fires decides:

    1. ``k`` as a count of records (:func:`check_count`, then
       :func:`check_rounding`), the checks ``count`` passes; it is published
       rounded, as ``count`` is;
    2. *values* as the ``sum`` metric checks them (:func:`check_sum`): ``Sx``
       gives the sum back to within ``k`` grid steps, so a sum the rule withholds
       is not published as ``Sx`` either;
    3. *encoded*, the contributions ``Sx`` itself adds up (:func:`check_sum`):
       values less than a grid step apart encode alike, so they can hold fewer
       distinct values than *values* do;
    4. the residues, what each value loses below its grid point
       (:func:`check_sum`), when any is not zero. Next to the exact ``sum``,
       ``Sx`` publishes their total, ``sum * 2**fractional_bits - Sx``, so it is
       checked as the sum it is: one record's residue, or a residue that every
       record shares (prices that all end in .99), would otherwise give a value
       or the exact count back. Values on the grid (whole numbers, halves) leave
       no residue, and then the mean says no more than ``count`` and ``sum``.
    """
    count = len(values)
    verdict = check_count(count, rule)
    if verdict.published:
        verdict = check_rounding(count, rule)
    if not verdict.published:
        return verdict
    # The values times their common denominator, so each check runs on integers.
    common, scaled = _on_common_scale(values)
    verdict = check_sum(scaled, rule)
    if verdict.published:
        verdict = check_sum(encoded, rule)
    if verdict.published:
        # What each value loses below its grid point, times the same common denominator.
        residues = [
            (whole << fractional_bits) - units * common
            for whole, units in zip(scaled, encoded, strict=True)
        ]
        if any(residues):
            verdict = check_sum(residues, rule)
    return verdict


def check_order_statistic(holders: int, rule: DisclosureRule) -> Verdict:
    """Check a minimum, a maximum or a quantile value that *holders* records hold."""
    _require_count(holders)
    if holders >= rule.minimum_cell:
        return PUBLISHED
    return Verdict(SMALL_COUNT, f"fewer than {rule.minimum_cell} records hold the value")


_MS_PER_DAY = 86_400_000
#: ``date.toordinal()`` of 1970-01-01: the proleptic Gregorian day number of the epoch.
_EPOCH_ORDINAL = 719_163


def round_extreme(
    end: str,
    holders: Mapping[int, int],
    rule: DisclosureRule,
    *,
    date: bool,
    floor: Optional[int] = None,
    ceiling: Optional[int] = None,
) -> Optional[Tuple[int, str]]:
    """Round the ``"min"`` or ``"max"`` end of a date or timestamp column to a calendar bucket.

    *holders* is the number of records holding each value, in whole milliseconds since the
    epoch, of the column's non-null values; *date* says the column is a DATE (its values are
    midnights, and its smallest unit is the day). The buckets are UTC calendar days (a
    timestamp only), months and years holding the smallest value for ``"min"`` and the
    largest for ``"max"``. The first, finest bucket holding at least ``minimumCell`` records
    gives ``(value, unit)``: the bucket's first millisecond for a minimum, its last day (a
    DATE) or last millisecond (a timestamp) for a maximum, clipped to *floor* and *ceiling*,
    the range the contract declares, and only when the true extreme lies inside that range.
    ``None`` when no bucket up to the year holds that many records, or when the bucket lies
    outside the years 1 to 9999. Whole-number arithmetic on milliseconds throughout.
    """
    if not holders:
        return None
    pivot = min(holders) if end == "min" else max(holders)
    if (floor is not None and pivot < floor) or (ceiling is not None and pivot > ceiling):
        # a record lies outside the declared range: clipping would put the end on the wrong
        # side of it, so the end stays withheld
        return None
    day = pivot // _MS_PER_DAY
    try:
        buckets = _calendar_buckets(day, date)
    except (ValueError, OverflowError):
        return None
    for unit, first, after in buckets:
        low, high = first * _MS_PER_DAY, after * _MS_PER_DAY
        if sum(count for value, count in holders.items() if low <= value < high) >= (
            rule.minimum_cell
        ):
            if end == "min":
                shown = low if floor is None else max(low, floor)
            else:
                shown = high - (_MS_PER_DAY if date else 1)
                shown = shown if ceiling is None else min(shown, ceiling)
            return shown, unit
    return None


def _calendar_buckets(day: int, date: bool) -> List[Tuple[str, int, int]]:
    """The ``(unit, first day, day after)`` buckets holding epoch day *day*, finest first.

    A DATE column has no day bucket: its exact value is the day, and was checked already.
    """
    found: List[Tuple[str, int, int]] = []
    if not date:
        found.append(("day", day, day + 1))
    moment = _date.fromordinal(day + _EPOCH_ORDINAL)
    month_first = _date(moment.year, moment.month, 1).toordinal() - _EPOCH_ORDINAL
    length = calendar.monthrange(moment.year, moment.month)[1]
    found.append(("month", month_first, month_first + length))
    year_first = _date(moment.year, 1, 1).toordinal() - _EPOCH_ORDINAL
    found.append(("year", year_first, year_first + (366 if calendar.isleap(moment.year) else 365)))
    return found


def check_group_count(k: int, rule: DisclosureRule) -> Verdict:
    """Check the ``k`` of a group of a declared statistic: a count of records.

    :func:`check_count`, then :func:`check_rounding`, the two checks a column's
    ``count`` passes. A group that passes is published with ``k`` rounded
    (:func:`round_count`), so what it says about the records is no more than
    ``count`` says.
    """
    verdict = check_count(k, rule)
    if verdict.published:
        verdict = check_rounding(k, rule)
    return verdict


def check_support(holders: Sequence[int], rule: DisclosureRule) -> Verdict:
    """Check a support: every member must be held by at least ``minimumCell`` records.

    *holders* is the number of records holding each member. A support lists its
    members, so a member one record holds would name that record's value. Each
    member is checked as a minimum or a maximum is (:func:`check_order_statistic`,
    the DwB/ESSnet guidelines' rule for extremes), and one member too few records
    hold withholds the whole group with ``A``: a document cannot say which member
    was left out.
    """
    for count in holders:
        verdict = check_order_statistic(count, rule)
        if not verdict.published:
            return verdict
    return PUBLISHED


def suppress_secondary(verdicts: Sequence[Verdict]) -> Tuple[Verdict, ...]:
    """Withhold every published group of a statistic when any of its groups is withheld.

    Secondary suppression, as the output-checking guidelines call it: a statistic
    published for every group has a total in the column's own published metrics
    (``count``, ``sum``), so one withheld group is given back by subtracting the
    published groups from that total. When any group fails its checks, every
    group that passed is withheld too with ``D`` (set by the sender); the groups
    that failed keep their own code. A statistic whose groups all pass, or all
    fail, is unchanged. It applies to a grouped ``mean/1`` and a grouped
    ``support/1``, whose groups partition a total the profile publishes. It does
    not apply to an ungrouped statistic, which has one group, or to a histogram
    or a quantile, whose own cells and ranks are checked as counts. The caller
    adds the records a ``groupBy`` null leaves out of every group as one more
    group, never written, since the column's own totals hold them too. The
    facts a request lists are checked together by :func:`suppress_differencing`.
    """
    if all(verdict.published for verdict in verdicts) or not any(
        verdict.published for verdict in verdicts
    ):
        return tuple(verdicts)
    withheld = Verdict(SECONDARY, "another group of the statistic is withheld")
    return tuple(withheld if verdict.published else verdict for verdict in verdicts)


#: A combination of the states of a table's facts, one state per fact.
Combination = Tuple[int, ...]


#: The facts of a combination in factors: each factor's fact positions, with every
#: combination of their states that the facts' texts allow (one state per position, in
#: that order). The combinations the texts allow are every choice of one per factor.
Factors = Sequence[Tuple[Tuple[int, ...], AbstractSet[Combination]]]


def suppress_differencing(
    cells: Mapping[Combination, int],
    passed: Iterable[int],
    rule: DisclosureRule,
    factors: Callable[[Sequence[int]], Factors],
    states: Sequence[int],
    fixed: AbstractSet[int] = frozenset(),
) -> FrozenSet[int]:
    """The facts of one table to withhold so that together they give back no small cell.

    A fact publishes how many of a table's rows its rule covers, and how many of those
    satisfy it and how many do not; the table publishes its row count. Facts that overlap
    give more together than each alone: ``A`` and ``A OR id = 'x'`` differ by the one row
    ``x`` when it lies outside ``A``, and ``A``, ``B`` and ``A OR B`` give
    ``|A AND B| = |A| + |B| - |A OR B|`` however large each pair's overlap is, so checking
    facts two at a time is not enough. The check reads every combination of the facts'
    states at once. *cells* maps each combination that at least one record holds (one of
    *states* per fact, in the facts' order) to the exact number of records holding it.
    The counts are read before any rounding: 102 and 103 records round to 100 and 105,
    and a reader differences those back to one record.

    A combination of the published facts that holds from 1 to ``minimumCell - 1`` records
    is a small cell. Where zeros are disclosive, every empty combination that the facts'
    texts allow is a zero cell too, since its emptiness is a containment the records show
    (no record of the one state has the other), and an empty combination the texts rule
    out on every row is not (``A`` TRUE beside ``A OR id = 'x'`` not TRUE is no row's, so
    the texts show that containment already). *factors* gives, for the positions of the
    facts still published, the factors of :data:`Factors`: facts whose texts are linked,
    with the combinations of states the texts let them take together. The combinations
    allowed are every choice of one per factor, and the check looks at each that one
    factor's change takes to a held combination: if none of those is empty, the held
    combinations are every allowed one, so it finds an empty one whenever any is left,
    however many changes of single facts away from a held one it lies.

    A fact touches such a cell when changing that fact's state alone takes the cell to a
    combination some record holds, so that withholding the fact merges the two. Every
    fact that touches a small cell is withheld, the combinations of the facts still
    published are formed again, and so on until no small cell is left; then every fact
    that touches a zero cell is withheld, and the small cells are looked for again, until
    neither is left. Small cells go first because the empty combinations around a small
    cell go when it merges: read in the same round, they would withhold a fact that only
    the small cell set apart. Each round only withholds more, never brings a fact back,
    and depends on the facts and the records alone, not on the order the facts are listed
    in. A small or a zero cell that no one fact's change merges withholds every fact it
    differs in from its nearest held combinations. *passed* holds the positions of the facts that
    passed their own checks; the result is those of them to withhold, which the summary
    counts under ``D`` and the document writes ``C``, as it writes every withheld group of
    a level histogram.

    The check covers the facts of one table among themselves and against the table's row
    count, not a fact's overlap with a histogram's cells, a column's counts or a
    relationship's counts, unless those are given as *fixed* parts (the consumer queries hold
    a column's published cells that way). Two exposes are two tables to it, even when the
    data owner reads both from the same records, so facts asked of each are not checked
    together. Noise is what the ONS credits with protecting against differencing, in its
    comparison of post-tabular methods (2024), and the release under
    differential privacy (``fluid profile --privacy-unit``) is the profiler's way to
    publish counts with noise. The pattern is the coordinated secondary suppression of
    linked tables that the Handbook on Statistical Disclosure Control (2nd edition, section
    4.3.1) sets out, adapted: tables are linked by the cells they share, and here every
    combination of the facts' states is held to the threshold, which errs toward
    withholding, where an audit such as sdcTable's ``attack()`` solves for the bounds a
    reader can put on each suppressed cell.

    *fixed* holds the positions of counts published already, such as a column's published
    cells, which the check holds in every combination and never withholds. A cell only they
    touch withholds the parts it differs in from the nearest held combination with the same
    fixed states; when no such combination is held, merging every other part leaves the cell
    as it is, so it is the published counts' own and withholds nothing here.
    """
    pinned = frozenset(fixed)
    candidates = frozenset(passed) - pinned
    shown = set(candidates)
    width = len(next(iter(cells), ()))
    while shown:
        order = sorted(shown | pinned)
        formed: Mapping[Combination, int]
        if len(order) == width:
            # Every fact is published: the combinations are the ones given, not formed again.
            formed = {combination: count for combination, count in cells.items() if count > 0}
        else:
            projected: Counter[Combination] = Counter()
            for combination, count in cells.items():
                if count > 0:
                    projected[tuple(combination[at] for at in order)] += count
            formed = projected
        touched = _touched(formed, order, rule, factors, states, pinned)
        if not touched:
            break
        shown -= touched
    return candidates - shown


def _touched(
    formed: Mapping[Combination, int],
    order: Sequence[int],
    rule: DisclosureRule,
    factors: Callable[[Sequence[int]], Factors],
    states: Sequence[int],
    fixed: AbstractSet[int] = frozenset(),
) -> Set[int]:
    """The facts (positions) that touch a small cell of *formed*, else those touching a zero cell.

    A position in *fixed* is never returned (:func:`suppress_differencing`).
    """
    width = len(order)
    kept = frozenset(at for at, position in enumerate(order) if position in fixed)
    most = width - len(kept)

    def changed(cell: Combination, at: int) -> List[Combination]:
        return [cell[:at] + (state,) + cell[at + 1 :] for state in states if state != cell[at]]

    def touching(cell: Combination) -> Set[int]:
        return {
            at
            for at in range(width)
            if at not in kept and any(other in formed for other in changed(cell, at))
        }

    touched: Set[int] = set()
    for cell, count in formed.items():
        if len(touched) == most:
            break
        if count < rule.minimum_cell:
            touched |= touching(cell) or _differing_from_nearest(cell, formed, kept)
    if touched or not rule.zeros_disclosive:
        return {order[at] for at in touched}
    local = {position: at for at, position in enumerate(order)}
    looked_at: Set[Combination] = set()
    for positions, allowed in factors(order):
        inside = tuple(local[position] for position in positions)
        apart = set(inside)
        seen_beside: Dict[Combination, Set[Combination]] = {}
        for cell in formed:
            rest = tuple(state for at, state in enumerate(cell) if at not in apart)
            seen_beside.setdefault(rest, set()).add(tuple(cell[at] for at in inside))
        for rest, seen in seen_beside.items():
            for missing in allowed:
                if missing in seen:
                    continue
                zero = _put_together(rest, inside, missing, width)
                if zero in looked_at:
                    continue
                looked_at.add(zero)
                touched |= touching(zero) or _differing_from_nearest(zero, formed, kept)
                if len(touched) == most:
                    return {order[at] for at in touched}
    return {order[at] for at in touched}


def _put_together(
    rest: Combination, inside: Tuple[int, ...], states: Combination, width: int
) -> Combination:
    """The combination with *states* at the positions *inside* and *rest*, in order, elsewhere."""
    placed = dict(zip(inside, states, strict=True))
    others = iter(rest)
    return tuple(placed[at] if at in placed else next(others) for at in range(width))


def _differing_from_nearest(
    cell: Combination, formed: Iterable[Combination], kept: AbstractSet[int] = frozenset()
) -> Set[int]:
    """Where *cell* differs from the held combinations nearest it; every fact if none is held.

    With *kept* (the local positions of fixed counts), only the held combinations with the same
    states there are looked at, and none held returns nothing.
    """
    nearest: Optional[int] = None
    found: Set[int] = set()
    for other in formed:
        if other == cell or any(cell[at] != other[at] for at in kept):
            continue
        differ = {
            at for at, (mine, theirs) in enumerate(zip(cell, other, strict=True)) if mine != theirs
        }
        if nearest is None or len(differ) < nearest:
            nearest, found = len(differ), differ
        elif len(differ) == nearest:
            found |= differ
    if kept:
        return found
    return found or set(range(len(cell)))


def pinned_children_cells(
    lower_edges: Sequence[int],
    cells: Sequence[int],
    published: Sequence[bool],
    children: int,
    rule: DisclosureRule,
    childless: int = 0,
) -> FrozenSet[int]:
    """The withheld cells of a children-per-parent table a reader may work out exactly.

    The table counts parents by their number of children, on bins whose lower edges are
    *lower_edges* (the first 0; each bin ends below the next, the last has no top). The
    reader is the strongest a profile can meet: it knows the exact number of parents,
    the exact number of children they hold together (*children*), how many parents hold
    none because their key is null (*childless*), each published cell's band, and the
    output check's own rule, so that where a cell is published the withheld cells
    together are not a small count, nor a zero where zeros are disclosive. Any table it
    can hold true has nonnegative cells with the parents' sum, ``lower * c <= children
    <= upper * c`` (a parent holds between its bin's edges), at least *childless* parents
    in the first cell, and each published cell in its band.

    A withheld cell is safe when two such tables differ in it. The true table (*cells*)
    is one; the other is searched for among the tables one or two parents' moves away.
    A cell with no such pair is returned, so the search errs toward withholding: a
    returned cell may be one no reader could pin after all.
    """
    count = len(cells)
    tops: List[Optional[int]] = [edge - 1 for edge in lower_edges[1:]] + [None]
    bands = [
        sample_size(value, rule) if shown else None
        for value, shown in zip(cells, published, strict=True)
    ]
    any_published = any(published)

    def holds(table: Sequence[int]) -> bool:
        if min(table) < 0 or table[0] < childless:
            return False
        for value, band in zip(table, bands, strict=True):
            if band is not None and not band["min"] <= value <= band["max"]:
                return False
        if any_published:
            rest = sum(value for value, shown in zip(table, published, strict=True) if not shown)
            if not check_count(rest, rule).published:
                return False
        if sum(edge * value for edge, value in zip(lower_edges, table, strict=True)) > children:
            return False
        if tops[-1] is None and table[-1] > 0:
            return True
        return sum((top or 0) * value for top, value in zip(tops, table, strict=True)) >= children

    withheld = [index for index, shown in enumerate(published) if not shown]
    if not holds(cells):
        return frozenset(withheld)  # the reader's model misses these records: no claim
    moves = [
        (source, target) for source in range(count) for target in range(count) if source != target
    ]

    def repairs(table: Sequence[int], source: int, target: int) -> Set[int]:
        """One parent, or as many moved *source* to *target* as the children's bounds need."""
        many = {1}
        low = sum(edge * value for edge, value in zip(lower_edges, table, strict=True))
        if low > children and lower_edges[source] > lower_edges[target]:
            gap = lower_edges[source] - lower_edges[target]
            many.add(-(-(low - children) // gap))
        unbounded = tops[-1] is None and table[-1] > 0
        if not unbounded:
            high = sum((top or 0) * value for top, value in zip(tops, table, strict=True))
            if high < children and tops[target] is None:
                many.add(1)
            elif high < children and (tops[target] or 0) > (tops[source] or 0):
                gap = (tops[target] or 0) - (tops[source] or 0)
                many.add(-(-(children - high) // gap))
        return {each for each in many if 1 <= each <= table[source]}

    pinned: Set[int] = set()
    for index in withheld:
        if not _two_tables(cells, index, moves, holds, repairs):
            pinned.add(index)
    return frozenset(pinned)


def _two_tables(
    cells: Sequence[int],
    index: int,
    moves: Sequence[Tuple[int, int]],
    holds: Callable[[Sequence[int]], bool],
    repairs: Callable[[Sequence[int], int, int], Set[int]],
) -> bool:
    """Whether a table two moves from *cells* holds and differs at *index*.

    The first move takes one parent into or out of *index*; the second moves one parent,
    or as many as the children's bounds then need (*repairs*), between any two cells.
    """
    for source, target in moves:
        if index not in (source, target):
            continue
        first = list(cells)
        first[source] -= 1
        first[target] += 1
        if first[index] != cells[index] and holds(first):
            return True
        for again_source, again_target in moves:
            for many in repairs(first, again_source, again_target):
                second = list(first)
                second[again_source] -= many
                second[again_target] += many
                if second[index] != cells[index] and holds(second):
                    return True
    return False
