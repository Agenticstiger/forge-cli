# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``StatisticsBackend.bin_cells``: a value's bin, as ``marginalHistogram/1`` places it."""

from __future__ import annotations

import random
from fractions import Fraction

import pytest

from fluid_build.profiler import defaults
from fluid_build.profiler._stats import load_backend
from fluid_build.profiler.runner import ColumnPlan
from tests.profiler._fake_backend import FakeBackend

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats")

CONTRACT = {
    "exposes": [
        {
            "exposeId": "t",
            "contract": {
                "schema": [
                    {
                        "name": "amount",
                        "type": "decimal(10,2)",
                        "validationRules": [{"type": "range", "constraint": "between 0 and 1000"}],
                    },
                    {"name": "at", "type": "timestamp"},
                ]
            },
        }
    ]
}


def _tally(cells, size):
    counts = [0] * size
    for cell in cells:
        counts[cell] += 1
    return counts


@pytest.mark.parametrize("backend", [FakeBackend(), load_backend()], ids=["fake", "flux-stats"])
def test_the_bins_of_a_number_total_what_the_registry_measures(backend):
    expose = CONTRACT["exposes"][0]
    columns = [ColumnPlan("amount", "number")]
    planned = defaults.default_plan(expose, columns, backend.enum_levels, 16)
    edges = [Fraction(text) for text in planned.block["properties"][0]["bins"]]
    declared = backend.read_block(planned.block, CONTRACT, "t")
    (statistic,) = declared.statistics
    rng = random.Random(7)
    texts = [f"{rng.randint(0, 100000) / 100:.2f}" for _ in range(500)]
    # every edge, and the grid steps and cents either side of it, are values too
    for edge in edges:
        for delta in (Fraction(0), Fraction(-1, 100), Fraction(1, 100), Fraction(-1, 2**16)):
            value = edge + delta
            if 0 <= value <= 1000:
                texts.append(f"{float(value):.2f}")
    exact = [backend.read_value("number", text, column="amount", row_index=0) for text in texts]
    measured = backend.measure(declared, statistic, [{"amount": text} for text in texts])
    (group,) = measured.groups
    placed = backend.bin_cells(exact, edges, 16)
    assert _tally(placed, len(edges) - 1) == list(group.cells)


@pytest.mark.parametrize("backend", [FakeBackend(), load_backend()], ids=["fake", "flux-stats"])
def test_the_bins_of_a_timestamp_total_what_the_registry_measures(backend):
    block = {
        "encoding": {"fractionalBits": 16},
        "records": {"min": 1, "max": 1},
        "budget": {"maxChecksPerState": 1},
        "columns": {
            "at": {"range": {"min": "2020-01-01T00:00:00Z", "max": "2021-01-01T00:00:00Z"}}
        },
        "properties": [
            {
                "property": "marginalHistogram",
                "column": "at",
                "bins": [
                    "2020-01-01T00:00:00Z",
                    "2020-04-01T00:00:00Z",
                    "2020-07-01T00:00:00Z",
                    "2021-01-01T00:00:00Z",
                ],
                "tolerance": {"type": "distance", "measure": "totalVariation", "max": "1"},
            }
        ],
    }
    declared = backend.read_block(block, CONTRACT, "t")
    (statistic,) = declared.statistics
    texts = [
        "2020-01-01T00:00:00.000Z",
        "2020-03-31T23:59:59.999Z",
        "2020-04-01T00:00:00.000Z",
        "2020-06-30T23:59:59.999Z",
        "2020-07-01T00:00:00.000Z",
        "2020-12-31T23:59:59.999Z",
        "2021-01-01T00:00:00.000Z",
        "2020-05-05T05:05:05.005Z",
    ]
    exact = [backend.read_value("timestamp", text, column="at", row_index=0) for text in texts]
    edges = [
        Fraction(backend.read_value("timestamp", text, column="at", row_index=0))
        for text in block["properties"][0]["bins"]
    ]
    measured = backend.measure(declared, statistic, [{"at": text} for text in texts])
    (group,) = measured.groups
    assert _tally(backend.bin_cells(exact, edges, 16), 3) == list(group.cells)
    assert backend.bin_cells(exact, edges, 16) == [0, 0, 1, 1, 2, 2, 2, 1]
