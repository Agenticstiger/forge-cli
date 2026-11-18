# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The output checks ``fluid profile`` applies before anything is written."""

from __future__ import annotations

import json
import random
from fractions import Fraction

import pytest

from fluid_build.profiler import disclosure
from fluid_build.profiler.disclosure import (
    ACRO_DEFAULT,
    DisclosureRule,
    check_count,
    check_magnitude,
    check_mean,
    check_order_statistic,
    check_rounding,
    check_sum,
    round_count,
    sample_size,
)
from tests.profiler._fake_backend import FIXTURES

pytestmark = pytest.mark.unit


def _rule(**changes):
    fields = dict(
        name="test",
        minimum_cell=10,
        zeros_disclosive=True,
        rounding_base=5,
        dominance_n=2,
        dominance_k="0.90",
        p_ratio="0.10",
    )
    fields.update(changes)
    return DisclosureRule(**fields)


def test_the_default_rule_is_the_one_profile_spec_names():
    example = json.loads((FIXTURES / "profile-example.json").read_text(encoding="utf-8"))
    assert {ACRO_DEFAULT.name: ACRO_DEFAULT.to_document()} == example["disclosureRules"]


def test_acro_default_carries_acros_values():
    # acro/default.yaml at v1.1.0: safe_threshold 10, zeros_are_disclosive True,
    # safe_round_base 5, safe_nk_n 2, safe_nk_k 0.90, safe_pratio_p 0.10.
    assert ACRO_DEFAULT.minimum_cell == 10
    assert ACRO_DEFAULT.zeros_disclosive is True
    assert ACRO_DEFAULT.rounding_base == 5
    assert (ACRO_DEFAULT.dominance_n, ACRO_DEFAULT.k) == (2, Fraction(9, 10))
    assert ACRO_DEFAULT.p == Fraction(1, 10)


@pytest.mark.parametrize(
    "changes",
    [
        {"name": ""},
        {"name": "é"},  # decomposed: not NFC
        {"minimum_cell": 0},
        {"minimum_cell": True},
        {"rounding_base": 0},
        {"dominance_n": 0},
        {"dominance_k": "1.5"},
        {"dominance_k": "-0.1"},
        {"p_ratio": "0.1e0"},
        {"p_ratio": 0.1},
        {"zeros_disclosive": "yes"},
    ],
)
def test_a_rule_the_format_cannot_name_is_refused(changes):
    with pytest.raises(ValueError):
        _rule(**changes)


@pytest.mark.parametrize("count", [1, 5, 9])
def test_a_count_below_the_threshold_is_withheld_as_small(count):
    verdict = check_count(count, ACRO_DEFAULT)
    assert verdict.status == "A" and not verdict.published
    assert str(count) not in verdict.reason.replace("10", "")


def test_a_zero_is_withheld_when_zeros_are_disclosive_and_published_otherwise():
    assert check_count(0, ACRO_DEFAULT).status == "A"
    assert check_count(0, _rule(zeros_disclosive=False)).published
    # A positive count below the threshold stays withheld either way.
    assert check_count(3, _rule(zeros_disclosive=False)).status == "A"


@pytest.mark.parametrize("count", [10, 11, 1000])
def test_a_count_at_or_above_the_threshold_is_published(count):
    assert check_count(count, ACRO_DEFAULT).published


@pytest.mark.parametrize("bad", [-1, 2**53, True, 1.0])
def test_a_count_must_be_a_whole_number_in_range(bad):
    with pytest.raises((TypeError, ValueError)):
        check_count(bad, ACRO_DEFAULT)


@pytest.mark.parametrize(
    "count, rounded",
    [(0, 0), (1, 0), (2, 0), (3, 5), (7, 5), (8, 10), (10, 10), (12, 10), (13, 15), (1002, 1000)],
)
def test_counts_round_to_the_nearest_multiple_of_five(count, rounded):
    assert round_count(count, ACRO_DEFAULT) == rounded


@pytest.mark.parametrize("count, rounded", [(5, 0), (15, 20), (25, 20), (35, 40)])
def test_an_even_base_sends_a_tie_to_the_even_multiple(count, rounded):
    # Python's and numpy's round settle a tie this way, and ACRO rounds through numpy.
    assert round_count(count, _rule(rounding_base=10)) == rounded


def test_a_count_withheld_as_small_gets_a_band_that_says_no_more_than_a():
    assert sample_size(3, ACRO_DEFAULT) == {"min": 0, "max": 9}
    assert sample_size(0, ACRO_DEFAULT) == {"min": 0, "max": 9}
    assert sample_size(4, _rule(zeros_disclosive=False)) == {"min": 1, "max": 9}


@pytest.mark.parametrize(
    "count, band",
    [(10, {"min": 10, "max": 12}), (12, {"min": 10, "max": 12}), (1000, {"min": 998, "max": 1002})],
)
def test_a_published_count_gets_the_band_of_counts_that_publish_the_same_value(count, band):
    assert sample_size(count, ACRO_DEFAULT) == band


def test_a_published_zero_is_exact():
    assert sample_size(0, _rule(zeros_disclosive=False)) == {"min": 0, "max": 0}


@pytest.mark.parametrize("base", [1, 2, 3, 5, 10])
@pytest.mark.parametrize("zeros", [True, False])
def test_every_band_holds_its_count_and_every_count_in_it_publishes_alike(base, zeros):
    rule = _rule(rounding_base=base, zeros_disclosive=zeros)
    for count in range(0, 80):
        band = sample_size(count, rule)
        assert band["min"] <= count <= band["max"], (count, band)
        published = check_count(count, rule).published
        for other in range(band["min"], band["max"] + 1):
            # A band says no more than the published value (or the status) does.
            assert check_count(other, rule).published == published, (count, other)
            if published:
                assert round_count(other, rule) == round_count(count, rule), (count, other)


def test_a_sum_one_record_dominates_is_withheld_as_o():
    values = [Fraction(95)] + [Fraction(1)] * 5
    assert check_magnitude(values, ACRO_DEFAULT).status == "O"


def test_a_sum_two_records_dominate_is_withheld_as_t():
    values = [Fraction(50), Fraction(45)] + [Fraction(1)] * 5
    assert check_magnitude(values, ACRO_DEFAULT).status == "T"


def test_the_dominance_boundary_is_inclusive_as_in_acro():
    # ACRO flags top_n_sum / sum >= safe_nk_k.
    assert check_magnitude([Fraction(45), Fraction(45), Fraction(10)], ACRO_DEFAULT).status == "T"
    assert check_magnitude([Fraction(44), Fraction(45), Fraction(11)], ACRO_DEFAULT).published


def test_the_p_ratio_withholds_as_m_when_dominance_does_not():
    # Under ACRO's own (2, 0.90) the p-ratio of 0.10 can never fire on its own:
    # the rest after two is then above a tenth of the total. A looser k shows it.
    rule = _rule(dominance_k="0.99")
    assert check_magnitude([Fraction(60), Fraction(35), Fraction(3)], rule).status == "M"
    assert check_magnitude([Fraction(60), Fraction(30), Fraction(10)], rule).published


def test_more_than_two_dominant_records_is_another_measure_of_concentration():
    rule = _rule(dominance_n=3)
    values = [Fraction(31), Fraction(30), Fraction(30)] + [Fraction(1)] * 9
    assert check_magnitude(values, rule).status == "M"


def test_dominance_reads_each_contribution_by_its_size():
    values = [Fraction(-95)] + [Fraction(1)] * 5
    assert check_magnitude(values, ACRO_DEFAULT).status == "O"


def test_a_spread_sum_and_an_all_zero_sum_are_published():
    assert check_magnitude([Fraction(n) for n in range(1, 21)], ACRO_DEFAULT).published
    assert check_magnitude([Fraction(0)] * 12, ACRO_DEFAULT).published


def test_an_order_statistic_is_published_only_when_enough_records_hold_it():
    assert check_order_statistic(9, ACRO_DEFAULT).status == "A"
    assert check_order_statistic(10, ACRO_DEFAULT).published


def test_no_reason_names_a_value():
    verdicts = [
        check_count(7, ACRO_DEFAULT),
        check_magnitude([Fraction(123456)] + [Fraction(1)] * 9, ACRO_DEFAULT),
        check_order_statistic(1, ACRO_DEFAULT),
    ]
    for verdict in verdicts:
        assert "123456" not in verdict.reason and "7" not in verdict.reason


def test_the_codes_are_sdmx_cl_conf_status_codes():
    codes = {
        disclosure.FREE,
        disclosure.SMALL_COUNT,
        disclosure.DOMINANCE_BY_ONE,
        disclosure.DOMINANCE_BY_TWO,
        disclosure.CONCENTRATION,
        disclosure.WITHHELD,
    }
    schema = json.loads((FIXTURES / "profile-1.schema.json").read_text(encoding="utf-8"))
    assert codes <= set(schema["$defs"]["Status"]["enum"])


def test_a_sum_few_records_contribute_to_is_a_small_cell():
    # A sum of 0s and 1s is the count of the 1s: ACRO's magnitude checks alone publish a 3.
    values = [Fraction(1)] * 3 + [Fraction(0)] * 30
    assert check_magnitude(values, ACRO_DEFAULT).published
    verdict = check_sum(values, ACRO_DEFAULT)
    assert verdict.status == "A" and "contribute" in verdict.reason


def test_a_sum_whose_records_hold_at_most_two_values_is_withheld_as_c():
    # With the values published as min and max, the sum gives each count back exactly.
    assert check_sum([Fraction(7)] * 40, ACRO_DEFAULT).status == "C"
    assert check_sum([Fraction(0)] * 5000 + [Fraction(7)] * 12, ACRO_DEFAULT).status == "C"
    assert check_sum([Fraction(1)] * 30 + [Fraction(1000)] * 20, ACRO_DEFAULT).status == "C"
    # Three values give one equation in two unknown counts.
    assert check_sum([Fraction(n % 3) for n in range(60)], ACRO_DEFAULT).published


def test_a_sum_is_then_checked_for_dominance():
    # 200 of 214: one record makes up more than 0.90, with 11 contributors, 3 values.
    values = [Fraction(200)] + [Fraction(1)] * 6 + [Fraction(2)] * 4
    assert check_sum(values, ACRO_DEFAULT).status == "O"
    assert check_sum([Fraction(n) for n in range(1, 21)], ACRO_DEFAULT).published


def test_a_sum_of_no_record_is_a_zero_cell():
    assert check_sum([], ACRO_DEFAULT).status == "A"
    assert check_sum([Fraction(0)] * 12, ACRO_DEFAULT).status == "A"
    assert check_sum([], _rule(zeros_disclosive=False)).published
    assert check_sum([Fraction(0)] * 12, _rule(zeros_disclosive=False)).published


def test_a_positive_count_that_rounds_to_zero_is_withheld():
    # Published, a 0 would read as a zero cell, which a document never carries.
    assert check_rounding(1, ACRO_DEFAULT).status == "A"
    assert check_rounding(2, ACRO_DEFAULT).status == "A"
    assert check_rounding(3, ACRO_DEFAULT).published
    assert check_rounding(0, ACRO_DEFAULT).published  # a true zero is check_count's to judge


def _on_grid(values, bits=16):
    return [(value.numerator << bits) // value.denominator for value in values]


def _mean(values, rule=ACRO_DEFAULT, bits=16):
    return check_mean(values, _on_grid(values, bits), bits, rule)


def test_a_mean_publishes_only_where_the_count_and_the_sum_would():
    values = [Fraction(n) for n in range(1, 21)]
    assert _mean(values).published
    assert _mean(values[:9]).status == "A"  # k below 10
    assert _mean([]).status == "A"  # a zero
    assert _mean([], _rule(zeros_disclosive=False)).published
    dominated = [Fraction(200)] + [Fraction(1)] * 6 + [Fraction(2)] * 4
    assert _mean(dominated).status == "O"
    assert _mean([Fraction(1), Fraction(2)] * 10).status == "C"


def test_a_mean_is_withheld_when_its_values_on_the_grid_hold_two():
    # Four values, so the sum publishes; floored to whole numbers they are 1 and 2.
    values = [Fraction(n, 10) for n in (11, 12, 13, 24)] * 5
    assert check_sum(values, ACRO_DEFAULT).published
    assert _mean(values, bits=0).status == "C"


def test_a_mean_whose_count_rounds_to_zero_is_withheld():
    # A rule whose threshold lets 2 records through still never publishes k = 0.
    assert _mean([Fraction(3), Fraction(5)], _rule(minimum_cell=1)).status == "A"


def _decimals(*texts):
    return [Fraction(text) for text in texts]


@pytest.mark.parametrize(
    ("values", "bits", "status"),
    [
        # Next to the exact sum, Sx gives the residues' total: 0.36 + 0.64 here, which
        # alone tells this column from 10.50, 10.50, 20 ... 29 (same count, same sum).
        (_decimals("10.01", "10.99") + [Fraction(n) for n in range(20, 30)], 16, "A"),
        # 25 split into 12.01 and 12.99: 12 records against 11, with the same metrics.
        (_decimals("12.01", "12.99") + [Fraction(n) for n in range(20, 31) if n != 25], 16, "A"),
        # Every value ends in .5: on a grid of whole numbers the residues total k / 2.
        ([Fraction(2 * n + 1, 2) for n in range(10, 22)], 0, "C"),
        # Prices that all end in .99 share one residue, so its total is a count.
        ([n + Fraction(99, 100) for n in range(1, 41)], 16, "C"),
    ],
)
def test_a_mean_whose_residues_would_give_a_count_or_a_value_back_is_withheld(values, bits, status):
    assert check_sum(values, ACRO_DEFAULT).published  # the sum itself is published
    assert _mean(values, bits=bits).status == status


def test_a_mean_publishes_when_its_values_are_on_the_grid_or_their_residues_pass():
    halves = [Fraction(2 * n + 1, 2) for n in range(10, 22)]
    assert _mean(halves, bits=16).published  # every half is on a 16-bit grid
    cents = [n + Fraction(n * 37 % 100, 100) for n in range(1, 41)]
    assert _mean(cents, bits=16).published  # varied residues, none dominant


def _reference_sum_check(values, rule):
    """check_sum as it read before it ran on integers: every step on fractions."""
    values = [Fraction(value) for value in values]
    contributors = sum(1 for value in values if value != 0)
    counted = check_count(contributors, rule)
    if not counted.published:
        return counted.status
    distinct = set(values)
    if len(distinct) <= 2 and distinct != {Fraction(0)} and distinct:
        return "C"
    sizes = sorted((abs(value) for value in values), reverse=True)
    total = sum(sizes, Fraction(0))
    if total == 0:
        return "F"
    share = rule.k * total
    if sizes[0] >= share:
        return "O"
    if rule.dominance_n >= 2 and sum(sizes[:2], Fraction(0)) >= share:
        return "T"
    if rule.dominance_n >= 3 and sum(sizes[: rule.dominance_n], Fraction(0)) >= share:
        return "M"
    return "M" if total - sum(sizes[:2], Fraction(0)) < rule.p * sizes[0] else "F"


@pytest.mark.parametrize("seed", range(40))
def test_the_sum_checks_decide_on_integers_as_they_did_on_fractions(seed):
    rng = random.Random(seed)
    rule = _rule(dominance_n=rng.choice([1, 2, 3]), dominance_k=rng.choice(["0.5", "0.90"]))
    size = rng.choice([0, 3, 12, 40])
    values = [
        Fraction(
            rng.choice([0, rng.randint(-50, 50), rng.randint(1, 10**6)]), rng.choice([1, 3, 4, 100])
        )
        for _ in range(size)
    ]
    assert check_sum(values, rule).status == _reference_sum_check(values, rule)
    # Whole numbers, and the same values on any common scale, decide alike.
    scale = rng.randint(1, 10**9)
    assert (
        check_sum([value * scale for value in values], rule).status
        == check_sum(values, rule).status
    )


def test_a_sum_of_decimals_or_text_is_checked_as_the_same_fractions():
    from decimal import Decimal

    fractions = [Fraction(n, 4) for n in range(1, 21)] + [Fraction(500)]
    expected = check_sum(fractions, ACRO_DEFAULT).status
    assert (
        check_sum(
            [Decimal(str(value.numerator / value.denominator)) for value in fractions], ACRO_DEFAULT
        ).status
        == expected
    )
    assert check_sum([str(value) for value in fractions], ACRO_DEFAULT).status == expected


# -- the groups of a declared statistic -----------------------------------------


@pytest.mark.parametrize(
    ("k", "status"),
    [(0, "A"), (1, "A"), (9, "A"), (10, "F"), (11, "F"), (120, "F")],
)
def test_a_groups_k_is_checked_as_a_count(k, status):
    assert disclosure.check_group_count(k, ACRO_DEFAULT).status == status


def test_a_k_that_rounds_to_zero_is_withheld_under_a_low_threshold():
    rule = _rule(minimum_cell=1)
    assert disclosure.check_group_count(2, rule).status == "A"  # 2 rounds to 0 at base 5
    assert disclosure.check_group_count(3, rule).status == "F"


def test_cells_are_checked_one_by_one_and_rounded():
    cells = disclosure.publish_cells([0, 3, 12, 40], ACRO_DEFAULT)
    assert cells.statuses == ("A", "A", "F", "F")
    assert cells.counts == (None, None, 10, 40)
    assert cells.any_withheld
    assert cells.verdict.published


def test_cells_that_all_pass_carry_no_withheld_cell():
    cells = disclosure.publish_cells([10, 25, 31], ACRO_DEFAULT)
    assert cells.statuses == ("F", "F", "F")
    assert cells.counts == (10, 25, 30)
    assert not cells.any_withheld


def test_a_group_whose_every_cell_is_withheld_is_withheld_whole():
    cells = disclosure.publish_cells([0, 4, 9], ACRO_DEFAULT)
    assert cells.verdict.status == "A"
    assert cells.counts == (None, None, None)


def test_a_zero_cell_is_published_where_the_rule_holds_zeros_safe():
    cells = disclosure.publish_cells([0, 20], _rule(zeros_disclosive=False))
    assert cells.statuses == ("F", "F") and cells.counts == (0, 20)


def test_a_positive_cell_that_rounds_to_zero_is_withheld():
    cells = disclosure.publish_cells([2, 30], _rule(minimum_cell=1))
    assert cells.statuses == ("A", "F")


@pytest.mark.parametrize(
    ("holders", "status"),
    [((10, 12), "F"), ((10, 9), "A"), ((1,), "A"), ((), "F"), ((40, 0), "A")],
)
def test_a_support_needs_every_member_held_by_enough_records(holders, status):
    assert disclosure.check_support(holders, ACRO_DEFAULT).status == status


def _proves(k, below, at_or_below, level=Fraction(1, 2)):
    index = -((-level.numerator * k) // level.denominator)
    return below < index <= at_or_below


def test_a_quantile_witness_needs_enough_holders_and_publishes_rounded_ranks():
    verdict, below, at_or_below = disclosure.check_quantile(120, 48, 72, ACRO_DEFAULT, _proves)
    assert verdict.status == "F" and (below, at_or_below) == (50, 70)
    verdict, *_ = disclosure.check_quantile(120, 48, 57, ACRO_DEFAULT, _proves)
    assert verdict.status == "A"  # nine records hold the witness
    verdict, *_ = disclosure.check_quantile(120, 48, 58, ACRO_DEFAULT, _proves)
    assert verdict.status == "F"  # ten do


def test_a_quantile_whose_rounded_ranks_no_longer_prove_the_witness_is_withheld():
    # 40 records, the median is the 20th. 17 below and 27 at or below prove it, with
    # 10 holders and 13 records above, and so do their rounded 15 and 25.
    assert _proves(40, 17, 27)
    verdict, below, at_or_below = disclosure.check_quantile(40, 17, 27, ACRO_DEFAULT, _proves)
    assert (below, at_or_below) == (15, 25) and verdict.status == "F"
    # 18 below and 28 at or below prove it too, and round to 20 and 30: 20 records
    # below no longer leaves the 20th record in the witness's place.
    assert _proves(40, 18, 28)
    verdict, below, at_or_below = disclosure.check_quantile(40, 18, 28, ACRO_DEFAULT, _proves)
    assert (below, at_or_below) == (20, 30)
    assert verdict.status == "A" and "no longer prove" in verdict.reason


@pytest.mark.parametrize(
    ("k", "below", "at_or_below", "status", "side"),
    [
        # The records below the witness, holding it and above it: each none at an end of
        # the column, or at least minimumCell (FLUX ADR 0011's three cells).
        (120, 9, 30, "A", "below"),
        (120, 1, 30, "A", "below"),
        (120, 10, 30, "F", None),
        (120, 0, 30, "F", None),  # the witness is the smallest value
        (120, 50, 111, "A", "above"),
        (120, 50, 119, "A", "above"),
        (120, 50, 110, "F", None),
        (120, 50, 120, "F", None),  # the witness is the largest value
        (10, 0, 10, "F", None),  # every record holds it
    ],
)
def test_a_quantile_needs_none_or_enough_records_below_and_above_its_witness(
    k, below, at_or_below, status, side
):
    verdict, *_ = disclosure.check_quantile(k, below, at_or_below, ACRO_DEFAULT, lambda *_: True)
    assert verdict.status == status
    if side is not None:
        assert verdict.reason == f"fewer than 10 records lie {side} the value"


def test_the_outer_cells_are_checked_before_rounding():
    # 3 records above would round to 5 and read as a cell the threshold cleared.
    verdict, *_ = disclosure.check_quantile(60, 20, 57, ACRO_DEFAULT, lambda *_: True)
    assert verdict.status == "A" and "above" in verdict.reason


@pytest.mark.parametrize(
    ("k", "below", "at_or_below"), [(120, 50, 40), (120, 50, 121), (120, -1, 30)]
)
def test_quantile_ranks_that_cannot_describe_the_group_are_refused(k, below, at_or_below):
    with pytest.raises(ValueError):
        disclosure.check_quantile(k, below, at_or_below, ACRO_DEFAULT, lambda *_: True)


def test_a_quantile_rank_that_rounds_to_zero_withholds_the_group():
    rule = _rule(minimum_cell=1)
    verdict, *_ = disclosure.check_quantile(30, 2, 20, rule, lambda *_: True)
    assert verdict.status == "A"
    verdict, *_ = disclosure.check_quantile(30, 0, 20, rule, lambda *_: True)
    assert verdict.status == "F"  # nothing below the witness is a zero rank, not a small one


def _published():
    return disclosure.PUBLISHED


def _withheld(code="A"):
    return disclosure.Verdict(code, "x")


def test_secondary_suppression_withholds_the_groups_that_passed_with_d():
    verdicts = [_published(), _withheld(), _published(), _withheld("C")]
    result = disclosure.suppress_secondary(verdicts)
    assert [v.status for v in result] == ["D", "A", "D", "C"]


def test_secondary_suppression_leaves_a_statistic_alone_when_nothing_is_withheld():
    verdicts = [_published(), _published()]
    assert disclosure.suppress_secondary(verdicts) == tuple(verdicts)


def test_secondary_suppression_leaves_a_statistic_alone_when_everything_is_withheld():
    verdicts = [_withheld(), _withheld("O")]
    assert [v.status for v in disclosure.suppress_secondary(verdicts)] == ["A", "O"]


def test_secondary_suppression_has_a_code_of_its_own():
    assert disclosure.SECONDARY == "D"
    assert disclosure.SECONDARY not in {
        disclosure.FREE,
        disclosure.SMALL_COUNT,
        disclosure.WITHHELD,
        disclosure.DOMINANCE_BY_ONE,
        disclosure.DOMINANCE_BY_TWO,
        disclosure.CONCENTRATION,
    }
