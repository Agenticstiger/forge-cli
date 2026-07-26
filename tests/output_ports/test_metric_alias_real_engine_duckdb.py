# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Real-engine (DuckDB) test of the governed ``query`` result-column
NAMING, including the collision the naming change can create.

Sibling of ``test_column_policy_real_engine_duckdb.py`` — same reason for
existing: the interesting failure is not in the compiler's string output
but in what the ENGINE does with it and what survives the driver's
``dict(zip(columns, values))`` row keying, and neither is observable from
a compile-only assertion.

Two behaviours are pinned together because the second is what a fix for
the first can break:

1. Two metrics over one measure must be distinguishable. Every metric
   used to project ``AS <measure_name>``, so ``total_revenue`` and
   ``completed_revenue`` both came back as a column called ``revenue``.
2. A metric whose name equals a DIMENSION name must still answer. Naming
   the projection after the metric collides there, and a duplicate output
   name is not a cosmetic problem: ``ORDER BY <alias>`` becomes ambiguous
   (a hard engine error) and the two columns collapse into one key in
   every row dict.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.output_ports.mcp._handlers import tool_query  # noqa: E402
from fluid_build.output_ports.mcp.drivers import build_driver  # noqa: E402
from fluid_build.output_ports.mcp.query_compiler import QueryValidationError  # noqa: E402


def _build(tmp_path: Path):
    csv = tmp_path / "orders.csv"
    csv.write_text(
        "order_id,order_status,order_priority,amount\n"
        "1,F,high,100\n"
        "2,F,low,200\n"
        "3,O,high,400\n"
        "4,O,low,800\n"
        "5,P,high,1600\n"
    )
    expose = {
        "exposeId": "orders",
        "kind": "table",
        "contract": {
            "schema": [
                {"name": "order_id"},
                {"name": "order_status"},
                {"name": "order_priority"},
                {"name": "amount"},
            ]
        },
        "semantics": {
            "measures": [
                {"name": "revenue", "agg": "sum", "expr": "amount"},
                {"name": "order_count", "agg": "count_distinct", "expr": "order_id"},
                # Named like the ``order_priority`` dimension below.
                {"name": "order_priority", "agg": "sum", "expr": "amount"},
            ],
            "dimensions": [
                {"name": "order_status", "type": "categorical", "expr": "order_status"},
                {"name": "order_priority", "type": "categorical", "expr": "order_priority"},
            ],
            "metrics": [
                {"name": "total_revenue", "type": "simple", "measure": "revenue"},
                {
                    "name": "completed_revenue",
                    "type": "simple",
                    "measure": "revenue",
                    "filter": "order_status = 'F'",
                },
                # Deliberately named identically to the dimension.
                {"name": "order_status", "type": "simple", "measure": "revenue"},
                # Name AND measure name both collide with a dimension.
                {"name": "order_priority", "type": "simple", "measure": "order_priority"},
            ],
        },
        "binding": {
            "platform": "local",
            "format": "csv",
            "location": {"path": str(csv), "table": "orders"},
        },
    }
    driver = build_driver(expose=expose, contract={"exposes": [expose]})
    return SimpleNamespace(
        expose=expose,
        caller_attributes={},
        policy=SimpleNamespace(max_sample_rows=100),
        query_timeout_seconds=None,
        get_driver=lambda: driver,
    )


# ---------------------------------------------------------------------
# 1. Two metrics over one measure are distinguishable
# ---------------------------------------------------------------------


def test_two_metrics_over_one_measure_are_distinguishable_on_a_real_engine(tmp_path: Path):
    state = _build(tmp_path)
    total = tool_query(state, {"metric": "total_revenue", "limit": 10})
    completed = tool_query(state, {"metric": "completed_revenue", "limit": 10})
    assert total["columns"] == ["total_revenue"]
    assert completed["columns"] == ["completed_revenue"]
    assert float(total["rows"][0]["total_revenue"]) == 3100.0
    assert float(completed["rows"][0]["completed_revenue"]) == 300.0


def test_bare_measure_keeps_the_measure_name(tmp_path: Path):
    state = _build(tmp_path)
    assert tool_query(state, {"measure": "revenue", "limit": 10})["rows"] == [{"revenue": 3100.0}]
    assert tool_query(state, {"measure": "order_count", "limit": 10})["rows"] == [
        {"order_count": 5}
    ]


# ---------------------------------------------------------------------
# 2. The collision the metric aliasing creates
# ---------------------------------------------------------------------


def test_metric_named_like_a_dimension_still_answers(tmp_path: Path):
    """The regression guard. A duplicate alias makes the engine reject
    the statement outright (ambiguous ORDER BY), so this asserts the
    engine ACTUALLY RAN it and every group survived the row keying."""
    payload = tool_query(
        _build(tmp_path),
        {"metric": "order_status", "dimensions": ["order_status"], "limit": 10},
    )
    assert payload["columns"] == ["order_status", "revenue"]
    assert {row["order_status"]: float(row["revenue"]) for row in payload["rows"]} == {
        "F": 300.0,
        "O": 1200.0,
        "P": 1600.0,
    }


def test_repeated_dimension_projects_one_column_on_a_real_engine(tmp_path: Path):
    payload = tool_query(
        _build(tmp_path),
        {"metric": "total_revenue", "dimensions": ["order_status", "order_status"], "limit": 10},
    )
    assert payload["columns"] == ["order_status", "total_revenue"]
    assert {row["order_status"]: float(row["total_revenue"]) for row in payload["rows"]} == {
        "F": 300.0,
        "O": 1200.0,
        "P": 1600.0,
    }


def test_unresolvable_collision_fails_loudly_not_with_a_lost_value(tmp_path: Path):
    """Metric name and measure name both taken by requested dimensions.
    There is no distinct name left, so the request is rejected with an
    actionable message rather than answered with one of the two values
    silently missing from every row."""
    with pytest.raises(QueryValidationError, match="collides"):
        tool_query(
            _build(tmp_path),
            {"metric": "order_priority", "dimensions": ["order_priority"], "limit": 10},
        )
