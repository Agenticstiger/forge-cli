# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""flux-stats is pinned in one file, and ``fluid profile`` names the same pin.

flux-stats is not on PyPI yet, and PyPI refuses an upload whose metadata holds a URL
dependency, so ``[profile]`` cannot name it. CI installs ``requirements/flux-stats.txt``;
the CLI cannot read that file once installed, so ``_stats`` repeats its line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fluid_build.profiler import _stats

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = REPO_ROOT / "requirements" / "flux-stats.txt"


def _pin_lines() -> list:
    lines = REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def test_the_requirements_file_holds_exactly_one_pin():
    assert len(_pin_lines()) == 1


def test_the_constant_matches_the_requirements_file():
    assert _pin_lines() == [_stats.FLUX_STATS_GIT_REQUIREMENT]


def test_the_pin_is_a_full_commit_of_the_flux_stats_subdirectory():
    req = _stats.FLUX_STATS_GIT_REQUIREMENT
    sha = req.split("@")[2].split("#")[0]
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
    assert req.endswith("#subdirectory=packages/flux-stats")


def test_the_unavailable_message_gives_the_exact_install_command():
    message = str(_stats._unavailable(ImportError("x")))
    assert f'pip install "{_stats.FLUX_STATS_GIT_REQUIREMENT}"' in message
