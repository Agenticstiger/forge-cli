# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An extra a test lane installs that brings duckdb also brings numpy.

DuckDB's ``create_function`` needs numpy for the masking UDFs (duckdb/duckdb#17052), and
the duckdb wheel lists numpy only as optional. When ``profile`` joined ``dev`` (#925), a
``.[dev]`` lane got duckdb, every ``importorskip("duckdb")`` gate opened, and 17 masking
tests failed for the numpy they never got. ``profile`` itself stays without numpy: the
runner refuses masking without it and names the ``local`` extra.
"""

from __future__ import annotations

import pytest

from tests.ci_guards import extras_alongside_base as g

pytestmark = pytest.mark.unit

#: The extras the CI lanes install for tests (``.[dev]``, ``.[dev,local]``, emulators).
LANE_EXTRAS = ("dev", "local", "test-emulators")


@pytest.mark.parametrize("extra", LANE_EXTRAS)
def test_a_lane_extra_that_brings_duckdb_brings_numpy(extra):
    table = g.extras(g.load())
    names = {g.requirement_name(r) for r in g.closure(extra, table)}
    if "duckdb" in names:
        assert "numpy" in names, f"[{extra}] installs duckdb without numpy"
