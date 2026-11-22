# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The exact discrete Gaussian and discrete Laplace samplers.

Floats appear here only to compute the exact pmf the samples are compared with.
Every random test uses a fixed seed, so none is flaky.
"""

from __future__ import annotations

import ast
import math
from collections import Counter
from fractions import Fraction
from pathlib import Path

import pytest

import fluid_build.profiler.dp as dp_package
from fluid_build.profiler.dp.discrete_gaussian import (
    sample_bernoulli_exp,
    sample_discrete_gaussian,
    sample_discrete_laplace,
)
from fluid_build.profiler.dp.randomness import SeededSource

pytestmark = pytest.mark.unit

SEED = bytes(range(32))
SAMPLES = 20_000


def _gaussian_pmf(sigma2: Fraction) -> dict[int, float]:
    s2 = float(sigma2)
    reach = int(60 * math.sqrt(s2)) + 1
    weights = {x: math.exp(-(x * x) / (2 * s2)) for x in range(-reach, reach + 1)}
    # math.fsum, not sum: from Python 3.12 sum() compensates float rounding and before it does
    # not, so with sum the exact pmf would differ in its last bits between the Pythons CI runs.
    total = math.fsum(weights.values())
    return {x: w / total for x, w in weights.items()}


def _draw(sigma2: Fraction, label: str, n: int = SAMPLES) -> list[int]:
    source = SeededSource(SEED, label)
    return [sample_discrete_gaussian(sigma2, source) for _ in range(n)]


def _total_variation(samples: list[int], pmf: dict[int, float]) -> float:
    counts = Counter(samples)
    n = len(samples)
    support = set(counts) | set(pmf)
    return 0.5 * sum(abs(counts.get(x, 0) / n - pmf.get(x, 0.0)) for x in support)


SIGMAS = [Fraction(1, 4), Fraction(4), Fraction(9, 4), Fraction(100)]


@pytest.mark.parametrize("sigma2", SIGMAS, ids=str)
def test_samples_are_integers(sigma2):
    assert all(type(x) is int for x in _draw(sigma2, "ints", 500))


@pytest.mark.parametrize("sigma2", SIGMAS, ids=str)
def test_moments_match_the_exact_ones(sigma2):
    pmf = _gaussian_pmf(sigma2)
    exact_var = math.fsum(x * x * p for x, p in pmf.items())
    # The variance is at most sigma2 (Canonne, Kamath and Steinke 2020). From a sigma2 of about 2
    # the gap shrinks like exp(-2 pi^2 sigma2), far below a double's resolution, so the bound is
    # checked up to the rounding of the computation.
    assert exact_var <= float(sigma2) * (1 + 1e-12)
    samples = _draw(sigma2, f"moments/{sigma2}")
    n = len(samples)
    mean = sum(samples) / n
    var = sum((x - mean) ** 2 for x in samples) / n
    # Five standard errors: the mean's is sd/sqrt(n); the variance's is about var*sqrt(2/n)
    # (the kurtosis of a discrete Gaussian is close to the normal 3).
    assert abs(mean) < 5 * math.sqrt(exact_var / n)
    assert abs(var - exact_var) < 5 * exact_var * math.sqrt(2 / n)


@pytest.mark.parametrize("sigma2", SIGMAS, ids=str)
def test_empirical_pmf_is_near_the_exact_pmf(sigma2):
    samples = _draw(sigma2, f"pmf/{sigma2}")
    # The expected distance for n samples over about 8 sigma of support is about
    # 0.4 * sqrt(support / n): under 0.03 here at sigma 10, so 0.06 leaves a wide margin.
    assert _total_variation(samples, _gaussian_pmf(sigma2)) < 0.06


def test_samples_are_symmetric():
    samples = _draw(Fraction(9, 4), "sym")
    counts = Counter(samples)
    n = len(samples)
    pmf = _gaussian_pmf(Fraction(9, 4))
    for x in range(1, 5):
        # Each side is within 5 standard errors of the same exact probability.
        sd = math.sqrt(pmf[x] * (1 - pmf[x]) / n)
        assert abs(counts[x] / n - counts[-x] / n) < 5 * math.sqrt(2) * sd


@pytest.mark.parametrize("x", [Fraction(0), Fraction(1, 2), Fraction(1), Fraction(3, 2)], ids=str)
def test_bernoulli_exp_frequencies(x):
    source = SeededSource(SEED, f"bern/{x}")
    n = 20_000
    hits = sum(sample_bernoulli_exp(x, source) for _ in range(n))
    p = math.exp(-float(x))
    assert abs(hits / n - p) < 5 * math.sqrt(p * (1 - p) / n) + 1e-12


@pytest.mark.parametrize("scale", [Fraction(1), Fraction(3, 2), Fraction(5)], ids=str)
def test_discrete_laplace_pmf(scale):
    t = float(scale)
    norm = (math.exp(1 / t) - 1) / (math.exp(1 / t) + 1)
    pmf = {x: math.exp(-abs(x) / t) * norm for x in range(-int(60 * t) - 1, int(60 * t) + 2)}
    source = SeededSource(SEED, f"lap/{scale}")
    samples = [sample_discrete_laplace(scale, source) for _ in range(SAMPLES)]
    assert all(type(x) is int for x in samples)
    assert _total_variation(samples, pmf) < 0.06


@pytest.mark.parametrize("bad", [4, 4.0, True, "4", None])
def test_gaussian_refuses_a_non_fraction(bad):
    with pytest.raises(TypeError):
        sample_discrete_gaussian(bad, SeededSource(SEED, "r"))


@pytest.mark.parametrize("bad", [Fraction(0), Fraction(-1, 2)])
def test_gaussian_refuses_a_non_positive_parameter(bad):
    with pytest.raises(ValueError):
        sample_discrete_gaussian(bad, SeededSource(SEED, "r"))


@pytest.mark.parametrize("bad", [2, 2.5, True])
def test_laplace_refuses_a_non_fraction(bad):
    with pytest.raises(TypeError):
        sample_discrete_laplace(bad, SeededSource(SEED, "r"))


@pytest.mark.parametrize("bad", [Fraction(0), Fraction(-3)])
def test_laplace_refuses_a_non_positive_scale(bad):
    with pytest.raises(ValueError):
        sample_discrete_laplace(bad, SeededSource(SEED, "r"))


def test_the_samplers_use_no_float_and_no_math_or_random_module():
    folder = Path(dp_package.__file__).parent
    banned = {"math", "random", "numpy"}
    for name in ("discrete_gaussian.py", "randomness.py"):
        tree = ast.parse((folder / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "float", name
            if isinstance(node, ast.Constant):
                assert not isinstance(node.value, float), name
            if isinstance(node, ast.Import):
                assert not banned & {a.name.split(".")[0] for a in node.names}, name
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned, name
            if isinstance(node, ast.Div):
                # true division of ints would give a float; Fractions only.
                assert name == "discrete_gaussian.py", name
