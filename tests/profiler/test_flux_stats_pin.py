# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``[profile]`` names flux-stats once, and ``fluid profile`` names the same requirement.

flux-stats comes from PyPI through the ``[profile]`` extra, and ``[dev]`` includes that extra, so
CI installs it with the rest. The CLI cannot read ``pyproject.toml`` once installed, so ``_stats``
repeats the requirement in its install message; these tests keep the two equal.
"""

from __future__ import annotations

import pytest
from packaging.requirements import Requirement

from fluid_build.profiler import _stats
from tests.ci_guards import extras_alongside_base as g

pytestmark = pytest.mark.unit


def _profile_flux_stats() -> list:
    requirements = g.load()["optional-dependencies"]["profile"]
    return [r for r in requirements if g.requirement_name(r) == "flux-stats"]


def test_the_requirement_names_the_yaml_extra_for_the_bundle_reader():
    assert Requirement(_stats.FLUX_STATS_REQUIREMENT).extras == {"yaml"}


def test_the_profile_extra_names_flux_stats_once():
    assert len(_profile_flux_stats()) == 1


def test_the_constant_matches_the_profile_extra():
    assert _profile_flux_stats() == [_stats.FLUX_STATS_REQUIREMENT]


def test_the_requirement_is_a_range_of_released_versions():
    requirement = Requirement(_stats.FLUX_STATS_REQUIREMENT)
    assert requirement.url is None  # PyPI refuses a URL in Requires-Dist
    assert requirement.marker is None  # flux-stats supports Python 3.10, the floor here
    assert requirement.specifier.contains("0.9.0")
    # From 0.9 flux-stats is versioned by its own formats and API: a later 0.x minor keeps
    # what is imported, so the range runs to the next major.
    assert requirement.specifier.contains("0.10.0")
    assert requirement.specifier.contains("0.99.0")
    assert not requirement.specifier.contains("0.0.1")  # the placeholder holds no code
    assert not requirement.specifier.contains("0.8.9")  # no request reader, booleans or noise
    assert not requirement.specifier.contains("1.0.0")  # a major release may change the API


def test_the_install_command_installs_the_extra_that_holds_flux_stats():
    assert _stats.FLUX_STATS_INSTALL_COMMAND == "pip install 'data-product-forge[profile]'"
    assert _profile_flux_stats()


def test_the_unavailable_message_names_the_requirement_and_the_command():
    message = str(_stats._unavailable(ImportError("x")))
    assert _stats.FLUX_STATS_REQUIREMENT in message
    assert _stats.FLUX_STATS_INSTALL_COMMAND in message
