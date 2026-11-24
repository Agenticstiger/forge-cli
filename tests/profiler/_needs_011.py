# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The one skip marker for tests that need flux-stats 0.11 (profile format 1-4-0).

The profile extra accepts flux-stats 0.9 and later, and a 0.9 or 0.10 reader holds
neither the rank correlations, the follows reach nor request format 1-2-0. A test
that uses one of them carries :data:`needs_flux_stats_011`; every other test runs on
either release.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest


def _holds_the_new_surface() -> bool:
    try:
        noise: Any = importlib.import_module("flux_stats.noise")
        importlib.import_module("flux_stats.rank_correlation")
    except ImportError:
        return False
    return hasattr(noise, "reach_of")


needs_flux_stats_011 = pytest.mark.skipif(
    not _holds_the_new_surface(), reason="needs flux-stats 0.11 (profile format 1-4-0)"
)
