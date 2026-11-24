# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Noise for the value of a rank correlation, drawn exactly on a lattice.

A rank correlation of a column pair (Kendall's ``tau`` or Spearman's ``rho``, PROFILE-SPEC
section 5) is one rational number ``x``, formed with a public row floor ``f``. Adding or removing
one record moves ``x`` by at most ``D1 = 4 / (f + 1)`` for Kendall's and ``9 / (f + 1)`` for
Spearman's (:meth:`~fluid_build.profiler._stats.StatisticsBackend.rank_sensitivity`), so a unit
that contributes ``r`` records moves it by at most ``Delta = r * D1``: one record at a time, by the
triangle inequality. Neither bound depends on the number of records, which the floor makes so.

A Gaussian drawn in floating point leaks through the least significant bits of its output and
breaks the guarantee its scale states (I. Mironov, "On significance of the least significant bits
for differential privacy", ACM CCS 2012). The release therefore draws the noise on a lattice, with
the exact discrete Gaussian sampler of this package (Canonne, Kamath and Steinke, "The discrete
Gaussian for differential privacy", NeurIPS 2020):

1. The lattice step is ``g = Delta / L`` with ``L = 2**20``, a public constant.
2. ``u = floor(x / g + 1/2)``, an integer: the value floored onto the lattice, half up. This is the
   only step that reads the data. It is exact, in rational arithmetic.
3. ``Z`` is a discrete Gaussian of parameter ``sigma_Z**2 = (s / g)**2``, where ``s`` is the scale
   the profile states.
4. The released value is ``v = g * (u + Z)``, written as a decimal string rounded to 12 places,
   which is post-processing.

**The bound.** Let ``x`` and ``x'`` be the values of two neighbouring data sets, so
``|x - x'| <= Delta`` and ``|x / g - x' / g| <= L``. Rounding half up is monotone and moves by no
more than the whole number it is given: if ``a <= b + L`` for the integer ``L`` then
``floor(a + 1/2) <= floor(b + 1/2) + L``, so ``|u - u'| <= L``. The integer ``u + Z`` is then a
discrete Gaussian release of an integer whose sensitivity is at most ``L``, and it spends
zero-concentrated differential privacy ``rho = L**2 / (2 sigma_Z**2)``: Canonne, Kamath and
Steinke (2020) show that adding a discrete Gaussian of parameter ``sigma**2`` to an integer query
of sensitivity ``Delta`` is ``Delta**2 / (2 sigma**2)``-zCDP. With ``sigma_Z = s / g``,

    rho = L**2 * g**2 / (2 * s**2) = Delta**2 / (2 * s**2),

exactly: the charge of a Gaussian of scale ``s`` on a value of sensitivity ``Delta``, which is what
the profile reader checks. The profile states the mechanism as ``gaussian``, since the reader
refuses a ``discreteGaussian`` on a rank correlation. A discrete Gaussian of parameter
``sigma_Z**2`` has a variance of at most ``sigma_Z**2``, and ``g`` times it, a variance of at most
``s**2``, so the variance the reader holds for a ``gaussian`` of scale ``s`` is an upper bound.

The scale is the least ``m / 10**6`` whose charge is no more than the share
(:func:`scale_units`); the share is written as the profile's own.

Everything here is integer and :class:`~fractions.Fraction` arithmetic, so the number drawn is the
same on every platform for one seed. No value read from a record leaves outside the noise.
"""

from __future__ import annotations

import math
from fractions import Fraction

from fluid_build.profiler.dp.discrete_gaussian import RandomSource, sample_discrete_gaussian

__all__ = [
    "LATTICE_STEPS",
    "SCALE_PLACES",
    "VALUE_PLACES",
    "lattice_point",
    "lattice_step",
    "release_value",
    "scale_units",
    "spent_rho",
]

#: ``L``: the number of lattice steps one unit can move the value by, at most.
LATTICE_STEPS = 2**20

#: The scale's step is ``10**-SCALE_PLACES``; the released value has ``VALUE_PLACES`` decimals.
SCALE_PLACES = 6
VALUE_PLACES = 12


def _check_delta(delta: Fraction) -> Fraction:
    if isinstance(delta, bool) or not isinstance(delta, (Fraction, int)) or delta <= 0:
        raise ValueError("a sensitivity above zero is needed")
    return Fraction(delta)


def lattice_step(delta: Fraction) -> Fraction:
    """``g = Delta / L``, the step of the lattice the value is floored onto."""
    return _check_delta(delta) / LATTICE_STEPS


def lattice_point(value: Fraction, delta: Fraction) -> int:
    """``u = floor(value / g + 1/2)``: the value on the lattice, rounded half up, exactly."""
    scaled = Fraction(value) / lattice_step(delta)
    return math.floor(scaled + Fraction(1, 2))


def scale_units(share: Fraction, squared_sensitivity: Fraction) -> int:
    """The least ``m`` with ``squared_sensitivity / (2 * (m / 10**6)**2) <= share``, in exact integers."""
    if share <= 0 or squared_sensitivity <= 0:
        raise ValueError("a share and a squared sensitivity above zero are needed")
    wanted = Fraction(squared_sensitivity) * 10 ** (2 * SCALE_PLACES) / (2 * Fraction(share))
    ceiling = -((-wanted.numerator) // wanted.denominator)
    root = math.isqrt(ceiling)
    return max(root if root * root >= wanted else root + 1, 1)


def spent_rho(delta: Fraction, units: int) -> Fraction:
    """What the release spends: ``L**2 / (2 sigma_Z**2)``, which is ``Delta**2 / (2 s**2)``.

    *units* is the scale ``s`` as ``m`` of ``m / 10**6``.
    """
    delta = _check_delta(delta)
    if isinstance(units, bool) or not isinstance(units, int) or units < 1:
        raise ValueError("a scale of at least one unit is needed")
    scale = Fraction(units, 10**SCALE_PLACES)
    sigma_z_squared = (scale / lattice_step(delta)) ** 2
    return Fraction(LATTICE_STEPS**2) / (2 * sigma_z_squared)


def release_value(value: Fraction, delta: Fraction, units: int, source: RandomSource) -> Fraction:
    """The released value ``g * (u + Z)``, rounded half up to 12 decimal places.

    *value* is the exact value ``x``, *delta* its sensitivity ``Delta`` to one unit, *units* the
    scale ``s`` as ``m`` of ``m / 10**6`` and *source* the randomness. The result is a terminating
    rational, ready to be written as decimal text.
    """
    step = lattice_step(delta)
    if isinstance(units, bool) or not isinstance(units, int) or units < 1:
        raise ValueError("a scale of at least one unit is needed")
    scale = Fraction(units, 10**SCALE_PLACES)
    sigma_z_squared = (scale / step) ** 2
    noisy = step * (lattice_point(value, delta) + sample_discrete_gaussian(sigma_z_squared, source))
    unit = 10**VALUE_PLACES
    return Fraction(math.floor(noisy * unit + Fraction(1, 2)), unit)
