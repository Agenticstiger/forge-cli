# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The output checks ``fluid profile`` applies before anything is written."""

from __future__ import annotations

import json
from fractions import Fraction

import pytest

from fluid_build.profiler import disclosure
from fluid_build.profiler.disclosure import (
    ACRO_DEFAULT,
    DisclosureRule,
    check_count,
    check_magnitude,
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
