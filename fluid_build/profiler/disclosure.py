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
(founder decision, 2026-11-16).

A withheld value carries an SDMX ``CL_CONF_STATUS`` 1.4 code and no value: ``A``
for a small count (a zero cell, a cell below the threshold, or a value too few
records hold), ``O`` and ``T`` for dominance by one or two records, ``M`` for
the p-ratio, another measure of concentration, and ``C`` for a sum whose records
hold at most two values.

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

import re
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, Iterable, Sequence, Tuple

__all__ = [
    "ACRO_DEFAULT",
    "CONCENTRATION",
    "DOMINANCE_BY_ONE",
    "DOMINANCE_BY_TWO",
    "FREE",
    "MAX_COUNT",
    "PUBLISHED",
    "SMALL_COUNT",
    "WITHHELD",
    "DisclosureRule",
    "Verdict",
    "check_count",
    "check_magnitude",
    "check_mean",
    "check_order_statistic",
    "check_rounding",
    "check_sum",
    "round_count",
    "sample_size",
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

#: The largest count a profile can carry: 2^53 - 1, as RFC 8785 serialises it.
MAX_COUNT = 2**53 - 1

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


def check_magnitude(contributions: Iterable[Fraction], rule: DisclosureRule) -> Verdict:
    """Check a sum, one contribution per record: dominance (n, k), then the p-ratio.

    Each contribution counts by its absolute value (see the module docstring). A
    total of zero is published: no contribution dominates it.
    """
    sizes = sorted((abs(Fraction(value)) for value in contributions), reverse=True)
    total = sum(sizes, Fraction(0))
    if total == 0:
        return PUBLISHED
    share = rule.k * total
    if sizes[0] >= share:
        return Verdict(
            DOMINANCE_BY_ONE, f"one record makes up at least {rule.dominance_k} of the total"
        )
    if rule.dominance_n >= 2 and sum(sizes[:2], Fraction(0)) >= share:
        return Verdict(
            DOMINANCE_BY_TWO, f"two records make up at least {rule.dominance_k} of the total"
        )
    if rule.dominance_n >= 3 and sum(sizes[: rule.dominance_n], Fraction(0)) >= share:
        return Verdict(
            CONCENTRATION,
            f"{rule.dominance_n} records make up at least {rule.dominance_k} of the total",
        )
    rest = total - sum(sizes[:2], Fraction(0))
    if rest < rule.p * sizes[0]:
        return Verdict(
            CONCENTRATION,
            f"what is left after the two largest records is below {rule.p_ratio} of the largest",
        )
    return PUBLISHED


def check_sum(contributions: Sequence[Fraction], rule: DisclosureRule) -> Verdict:
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
    values = [Fraction(value) for value in contributions]
    contributors = sum(1 for value in values if value != 0)
    counted = check_count(contributors, rule)
    if not counted.published:
        if contributors == 0:
            return counted
        return Verdict(SMALL_COUNT, f"fewer than {rule.minimum_cell} records contribute to the sum")
    distinct = set(values)
    if len(distinct) <= 2 and distinct != {Fraction(0)} and distinct:
        # An all-zero sum is 0 whatever the count, so it gives no count back.
        return Verdict(WITHHELD, "the records hold at most two values, so the sum is a count")
    return check_magnitude(values, rule)


def check_mean(
    values: Sequence[Fraction],
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
    if verdict.published:
        verdict = check_sum(values, rule)
    if verdict.published:
        verdict = check_sum([Fraction(units) for units in encoded], rule)
    if verdict.published:
        scale = 1 << fractional_bits
        residues = [
            Fraction(value) * scale - units for value, units in zip(values, encoded, strict=True)
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
