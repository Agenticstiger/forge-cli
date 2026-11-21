# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Exact sampling of the discrete Gaussian and the discrete Laplace distributions.

The algorithm is that of Clement Canonne, Gautam Kamath and Thomas Steinke,
"The discrete Gaussian for differential privacy" (NeurIPS 2020,
arXiv:2004.00010). The code is adapted from the Apache-2.0 reference
implementation ``discretegauss.py`` of
https://github.com/IBM/discrete-gaussian-differential-privacy at commit
``cb190d2a990a78eff6e21159203bc888e095f01b``. Only its exact part is ported
(``sample_uniform`` through ``sample_dgauss``, and ``floorsqrt``), step for
step. What changed: every random draw is ``source.below(m)`` of a seeded
source instead of a ``random`` generator; each ``assert`` is an explicit check
that raises, so it holds under ``python -O``; type hints; names in words.

No floating-point number is used anywhere: the parameters are
:class:`~fractions.Fraction` values and every comparison is exact, so a sample
has exactly the distribution the paper states. The variance of the discrete
Gaussian with parameter ``sigma2`` is at most ``sigma2``.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Protocol


class RandomSource(Protocol):
    """Where the randomness comes from: a uniform integer below ``n``."""

    def below(self, n: int) -> int: ...


def _check_fraction(value: object, name: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, Fraction):
        raise TypeError(f"{name} must be a Fraction")
    return value


def _sample_bernoulli(p: Fraction, source: RandomSource) -> int:
    """Bernoulli(p) for a rational p in [0, 1]."""
    if not 0 <= p <= 1:
        raise ValueError("p must be in [0, 1]")
    m = source.below(p.denominator)
    return 1 if m < p.numerator else 0


def _sample_bernoulli_exp1(x: Fraction, source: RandomSource) -> int:
    """Bernoulli(exp(-x)) for a rational x in [0, 1]."""
    if not 0 <= x <= 1:
        raise ValueError("x must be in [0, 1]")
    k = 1
    while True:
        if _sample_bernoulli(x / k, source) == 1:
            k = k + 1
        else:
            break
    return k % 2


def sample_bernoulli_exp(x: Fraction, source: RandomSource) -> int:
    """Bernoulli(exp(-x)) for a rational ``x >= 0``."""
    x = _check_fraction(x, "x")
    if x < 0:
        raise ValueError("x must not be negative")
    # Floor(x) independent Bernoulli(exp(-1)); if all are 1, Bernoulli(exp(-(x - floor(x)))).
    while x > 1:
        if _sample_bernoulli_exp1(Fraction(1, 1), source) == 1:
            x = x - 1
        else:
            return 0
    return _sample_bernoulli_exp1(x, source)


def _sample_geometric_exp_slow(x: Fraction, source: RandomSource) -> int:
    """Geometric(1 - exp(-x)) for a rational ``x >= 0``."""
    if x < 0:
        raise ValueError("x must not be negative")
    k = 0
    while True:
        if sample_bernoulli_exp(x, source) == 1:
            k = k + 1
        else:
            return k


def _sample_geometric_exp_fast(x: Fraction, source: RandomSource) -> int:
    """Geometric(1 - exp(-x)) for a rational ``x >= 0``."""
    if x == 0:
        return 0  # degenerate case
    if x < 0:
        raise ValueError("x must not be negative")
    t = x.denominator
    while True:
        u = source.below(t)
        b = sample_bernoulli_exp(Fraction(u, t), source)
        if b == 1:
            break
    v = _sample_geometric_exp_slow(Fraction(1, 1), source)
    value = v * t + u
    return value // x.numerator


def sample_discrete_laplace(scale: Fraction, source: RandomSource) -> int:
    """An integer ``x`` with probability ``exp(-|x|/scale) (e^(1/scale) - 1) / (e^(1/scale) + 1)``.

    *scale* is a :class:`~fractions.Fraction` above zero.
    """
    scale = _check_fraction(scale, "scale")
    if scale <= 0:
        raise ValueError("scale must be above zero")
    while True:
        sign = _sample_bernoulli(Fraction(1, 2), source)
        magnitude = _sample_geometric_exp_fast(1 / scale, source)
        if sign == 1 and magnitude == 0:
            continue
        return magnitude * (1 - 2 * sign)


def _floor_sqrt(x: Fraction) -> int:
    """floor(sqrt(x)) exactly, using only comparisons of x with integers."""
    if x < 0:
        raise ValueError("x must not be negative")
    a = 0  # a * a <= x
    b = 1  # b * b > x
    while b * b <= x:
        b = 2 * b  # double to get an upper bound
    while a + 1 < b:  # binary search
        c = (a + b) // 2
        if c * c <= x:
            a = c
        else:
            b = c
    return a


def sample_discrete_gaussian(sigma2: Fraction, source: RandomSource) -> int:
    """An integer ``x`` with probability proportional to ``exp(-x^2 / (2 sigma2))``.

    *sigma2* is a :class:`~fractions.Fraction` above zero. The variance of the
    discrete Gaussian with parameter ``sigma2`` is at most ``sigma2``.
    """
    sigma2 = _check_fraction(sigma2, "sigma2")
    if sigma2 <= 0:
        raise ValueError("sigma2 must be above zero")
    t = _floor_sqrt(sigma2) + 1
    while True:
        candidate = sample_discrete_laplace(Fraction(t), source)
        bias = ((abs(candidate) - sigma2 / t) ** 2) / (2 * sigma2)
        if sample_bernoulli_exp(bias, source) == 1:
            return candidate
