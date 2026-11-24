# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The noise of a rank correlation: its bound, its charge, and the draw."""

from __future__ import annotations

import itertools
import math
from fractions import Fraction

import pytest

from fluid_build.profiler import _stats
from fluid_build.profiler.dp import rank_noise
from fluid_build.profiler.dp.randomness import SeededSource

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.rank_correlation")

F = Fraction
DEFINITIONS = ("kendallTau/1", "spearmanRho/1")
SEED = bytes(range(1, 33))


@pytest.fixture(scope="module")
def backend():
    return _stats.load_backend()


def _data_sets(values, most):
    """Every multiset of at most *most* records, each a pair of values (ties included)."""
    records = list(itertools.product(values, repeat=2))
    for size in range(most + 1):
        yield from itertools.combinations_with_replacement(records, size)


@pytest.mark.parametrize("definition", DEFINITIONS)
def test_one_record_moves_the_lattice_point_by_at_most_L(backend, definition):
    """``|u - u'| <= L`` for every pair of data sets that differ by one record, n <= 5, ties."""
    checked = 0
    for floor in range(2, 7):
        delta = backend.rank_sensitivity(definition, floor)
        for records in _data_sets((0, 1, 2), 5):
            if not records:
                continue
            exact = backend.rank_value(definition, list(records), floor)
            u = rank_noise.lattice_point(exact, delta)
            for drop in set(records):
                rest = list(records)
                rest.remove(drop)
                other = backend.rank_value(definition, rest, floor)
                assert abs(exact - other) <= delta
                assert abs(u - rank_noise.lattice_point(other, delta)) <= rank_noise.LATTICE_STEPS
                checked += 1
    assert checked > 10_000


@pytest.mark.parametrize("definition", DEFINITIONS)
def test_a_unit_of_two_records_moves_it_by_at_most_two_deltas(backend, definition):
    for floor in (2, 4):
        delta = 2 * backend.rank_sensitivity(definition, floor)
        for records in _data_sets((0, 1, 2), 4):
            if len(records) < 2:
                continue
            exact = backend.rank_value(definition, list(records), floor)
            u = rank_noise.lattice_point(exact, delta)
            for pair in set(itertools.combinations(records, 2)):
                rest = list(records)
                for drop in pair:
                    rest.remove(drop)
                other = backend.rank_value(definition, rest, floor)
                assert abs(u - rank_noise.lattice_point(other, delta)) <= rank_noise.LATTICE_STEPS


def test_the_lattice_point_rounds_half_up_exactly():
    delta = F(4, 3)
    step = rank_noise.lattice_step(delta)
    assert step == delta / 2**20
    assert rank_noise.lattice_point(F(0), delta) == 0
    assert rank_noise.lattice_point(step / 2, delta) == 1
    assert rank_noise.lattice_point(-step / 2, delta) == 0
    assert rank_noise.lattice_point(step * 3 + step / 3, delta) == 3
    assert rank_noise.lattice_point(-step * 3, delta) == -3


@pytest.mark.parametrize("definition", DEFINITIONS)
@pytest.mark.parametrize("floor", [2, 99, 1000])
@pytest.mark.parametrize("share", [F(1, 100), F(9, 50), F(3, 2)])
def test_the_stated_share_is_the_charge_exactly_and_the_reader_accepts_it(
    backend, definition, floor, share
):
    from flux_stats.errors import FluxError
    from flux_stats.noise import read_accounting, read_noise

    delta = backend.rank_sensitivity(definition, floor)
    units = rank_noise.scale_units(share, delta * delta)
    scale = F(units, 10**6)
    # what the release spends: L^2 / (2 sigma_Z^2) = Delta^2 / (2 s^2), exactly
    assert rank_noise.spent_rho(delta, units) == delta * delta / (2 * scale * scale)
    assert rank_noise.spent_rho(delta, units) <= share
    # the least scale of the 10**-6 grid: one step less would spend more than the share
    assert units == 1 or rank_noise.spent_rho(delta, units - 1) > share

    accounting = read_accounting(
        {
            "measure": "zeroConcentratedDP",
            "privacyLoss": {"rho": "10"},
            "privacyUnit": {"expose": "t"},
            "publicStructure": True,
        },
        "profile.privacy",
    )

    def block(count, rho):
        return {
            "mechanism": "gaussian",
            "scale": _decimal(F(count, 10**6)),
            "privacyLoss": {"rho": _decimal(rho)},
        }

    read_noise(
        block(units, share), "where", accounting, l1=delta, l2_squared=delta * delta
    )  # as the reader reads a rank correlation: one row of the table
    if units > 1:
        with pytest.raises(FluxError):
            read_noise(
                block(units - 1, share), "where", accounting, l1=delta, l2_squared=delta * delta
            )


def _decimal(value):
    from fluid_build.profiler.dp.release import decimal_text

    return decimal_text(value)


def test_a_seeded_draw_is_deterministic_and_a_label_gives_another_stream():
    delta, units = F(12, 1001), 19981
    first = rank_noise.release_value(F(1, 3), delta, units, SeededSource(SEED, "rank/a"))
    again = rank_noise.release_value(F(1, 3), delta, units, SeededSource(SEED, "rank/a"))
    other = rank_noise.release_value(F(1, 3), delta, units, SeededSource(SEED, "rank/b"))
    assert first == again and first != other


def test_the_released_value_has_at_most_twelve_decimals():
    delta, units = F(12, 1001), 19981
    for index in range(20):
        value = rank_noise.release_value(
            F(-2, 7), delta, units, SeededSource(SEED, f"rank/{index}")
        )
        assert (value * 10**12).denominator == 1


def test_the_noise_is_centred_on_the_exact_value():
    delta, units = F(12, 1001), 19981
    exact, scale, count = F(1, 3), F(units, 10**6), 400
    values = [
        rank_noise.release_value(exact, delta, units, SeededSource(SEED, f"draw/{i}"))
        for i in range(count)
    ]
    mean = sum(values, F(0)) / count
    # a fixed seed: the mean of 400 draws is within five standard errors of the exact value
    assert abs(mean - exact) < 5 * scale / math.isqrt(count)
    # and the spread is the scale's
    variance = sum(((v - mean) ** 2 for v in values), F(0)) / count
    assert F(1, 2) < variance / (scale * scale) < F(3, 2)


def test_bad_arguments_are_refused():
    with pytest.raises(ValueError):
        rank_noise.scale_units(F(0), F(1))
    with pytest.raises(ValueError):
        rank_noise.scale_units(F(1), F(0))
    with pytest.raises(ValueError):
        rank_noise.lattice_step(F(0))
    with pytest.raises(ValueError):
        rank_noise.spent_rho(F(1), 0)
    with pytest.raises(ValueError):
        rank_noise.release_value(F(0), F(1), 0, SeededSource(SEED, "x"))
