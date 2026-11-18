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

A minimum, a maximum or a quantile is one record's value in most data. The
DwB/ESSnet "Guidelines for Output Checking" (class 3, maxima, minima and
percentiles) do not release one by rule of thumb, and clear one when more than the
threshold of units hold it. The profiler takes that reading: such a value is
published only when at least ``minimumCell`` records hold exactly that value
(founder decision, 2026-11-16). A quantile's two ranks are counts too, so the
records below its witness and the records above it must each be none or at least
``minimumCell`` (FLUX ADR 0011's three cells, founder decision 2026-11-18): a rank
of 3 would say that three records lie below the witness.

A withheld value carries an SDMX ``CL_CONF_STATUS`` 1.4 code and no value: ``A``
for a small count (a zero cell, a cell below the threshold, or a value too few
records hold), ``O`` and ``T`` for dominance by one or two records, ``M`` for
the p-ratio, another measure of concentration, ``C`` for a sum whose records
hold at most two values, and ``D`` for a group the sender withholds so that
another cannot be recovered (secondary suppression, :func:`suppress_secondary`).

The groups of a statistic the Simulation's fidelity block declares are checked
group by group, each as a cell of a table of records (the ESSnet/DwB output
checking guidelines' frequency-table and magnitude-table classes, which ACRO's
``safe_threshold`` and ``safe_nk`` checks implement): :func:`check_group_count`
for the group's record count, :func:`publish_cells` for a histogram's cells,
:func:`check_support` for a support's members and :func:`check_quantile` for a
quantile's witness.

What the group checks leave readable, on purpose:

* A grouped statistic lists every group the data forms, published or withheld.
  A key is always a level the bundle declares (a value outside the declared
  support is refused), so its presence says only that the level holds at least
  one record.
* When one group of a grouped mean or support is withheld, every other group of
  it is withheld too, under ``D``, so the withheld one cannot be recovered by
  differencing against the ungrouped figures. The records a ``groupBy`` null
  leaves out of every group count as one more group there, never written.
* A withheld cell or group of a histogram or a quantile is not secondarily
  suppressed. The rounded counts still bound it: it holds somewhere inside 0 to 9
  records, and never above the threshold.

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

import math
import re
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

__all__ = [
    "ACRO_DEFAULT",
    "CONCENTRATION",
    "DOMINANCE_BY_ONE",
    "DOMINANCE_BY_TWO",
    "FREE",
    "MAX_COUNT",
    "PUBLISHED",
    "SECONDARY",
    "SMALL_COUNT",
    "WITHHELD",
    "Cells",
    "DisclosureRule",
    "Exact",
    "Verdict",
    "check_count",
    "check_group_count",
    "check_magnitude",
    "check_mean",
    "check_order_statistic",
    "check_quantile",
    "check_rounding",
    "check_sum",
    "check_support",
    "publish_cells",
    "round_count",
    "sample_size",
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


@dataclass(frozen=True)
class Cells:
    """A histogram group's cells after the checks.

    ``verdict`` is :data:`PUBLISHED`, or ``A`` when every cell is withheld and the
    group goes whole. ``statuses`` holds one code per cell and ``counts`` the
    published count of each, rounded, or ``None`` where the cell is withheld
    (PROFILE-SPEC §3: a cell is null exactly when its status is not ``F``).
    """

    verdict: Verdict
    statuses: Tuple[str, ...]
    counts: Tuple[Optional[int], ...]

    @property
    def any_withheld(self) -> bool:
        return any(status != FREE for status in self.statuses)


def publish_cells(counts: Sequence[int], rule: DisclosureRule) -> Cells:
    """Check each cell of a histogram group as a count, and round the ones that pass.

    A cell is a frequency-table cell: a zero is withheld when the rule holds zeros
    disclosive, one below ``minimumCell`` is withheld, and so is a positive count
    that rounds to zero (:func:`check_count`, then :func:`check_rounding`), each
    with ``A``. A published cell is rounded to a multiple of the rounding base. A
    group whose every cell is withheld says nothing, so it is withheld whole.
    """
    statuses: List[str] = []
    shown: List[Optional[int]] = []
    for count in counts:
        verdict = check_group_count(count, rule)
        statuses.append(verdict.status)
        shown.append(round_count(count, rule) if verdict.published else None)
    if statuses and all(status != FREE for status in statuses):
        return Cells(Verdict(SMALL_COUNT, "every cell is withheld"), tuple(statuses), tuple(shown))
    return Cells(PUBLISHED, tuple(statuses), tuple(shown))


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


def check_quantile(
    k: int,
    below: int,
    at_or_below: int,
    rule: DisclosureRule,
    proves: Callable[[int, int, int], bool],
) -> Tuple[Verdict, int, int]:
    """Check a quantile group's witness and its ranks; return the verdict and the rounded ranks.

    *below* and *at_or_below* are the records strictly below the witness and at or
    below it, so ``at_or_below - below`` records hold it and ``k - at_or_below``
    lie above it. In order, the first that fires decides, always with ``A``:

    1. fewer than ``minimumCell`` records hold the witness
       (:func:`check_order_statistic`, as a minimum or a maximum is checked);
    2. the records below the witness, or above it, are fewer than ``minimumCell``
       and not none: on unrounded counts, the three cells the ranks state each
       hold none at an end of the column or at least ``minimumCell`` records
       (FLUX ADR 0011's three cells), since a rank of 3 says that three records
       lie below the witness, rounded or not;
    3. a rank is positive and rounds to zero (:func:`check_rounding`);
    4. the rounded ``k`` and ranks no longer prove the witness: *proves* is the
       registry's rank check on them, because a published group is read back with
       the rounded numbers, and a rank rounded across the witness's index would
       publish a witness the numbers contradict.

    The ranks are published rounded, as every count is. The returned ranks are
    only meaningful when the verdict is published.
    """
    for count in (k, below, at_or_below):
        _require_count(count)
    if not below <= at_or_below <= k:
        raise ValueError("a quantile's ranks lie from 0 to its k, the lower one first")
    verdict = check_order_statistic(at_or_below - below, rule)
    if verdict.published:
        verdict = _check_outer_cells(below, k - at_or_below, rule)
    if verdict.published:
        for rank in (below, at_or_below):
            verdict = check_rounding(rank, rule)
            if not verdict.published:
                break
    shown = (round_count(below, rule), round_count(at_or_below, rule))
    if verdict.published and not proves(round_count(k, rule), shown[0], shown[1]):
        verdict = Verdict(SMALL_COUNT, "the rounded ranks no longer prove the witness")
    return verdict, shown[0], shown[1]


def _check_outer_cells(below: int, above: int, rule: DisclosureRule) -> Verdict:
    """The records below a quantile's witness and above it: each none, or enough."""
    for count, side in ((below, "below"), (above, "above")):
        if 0 < count < rule.minimum_cell:
            return Verdict(
                SMALL_COUNT, f"fewer than {rule.minimum_cell} records lie {side} the value"
            )
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
    group, never written, since the column's own totals hold them too.
    """
    if all(verdict.published for verdict in verdicts) or not any(
        verdict.published for verdict in verdicts
    ):
        return tuple(verdicts)
    withheld = Verdict(SECONDARY, "another group of the statistic is withheld")
    return tuple(withheld if verdict.published else verdict for verdict in verdicts)
