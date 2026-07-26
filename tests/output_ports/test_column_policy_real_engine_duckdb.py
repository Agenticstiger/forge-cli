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

"""Real-engine (DuckDB) end-to-end test of COLUMN-level policy on the
governed ``query`` path.

Sibling of ``test_rls_real_engine_duckdb.py``, and it exists for the same
reason: the defects it pins were invisible to every compiler-level test
because they lived in the WIRING between the driver's policy sets and the
compiler.

``EngineDriver.project()`` enforces both column-level layers by matching
the OUTPUT column name — it drops ``columnRestrictions`` columns and
redacts ``sensitivity: pii`` values. The semantic layer aliases the
projection, so the name never matched:

* a measure ``{name: avg_balance, agg: avg, expr: account_balance}``
  served statistics over a DENIED column, and an equality filter on that
  column was a working inference oracle over its values;
* a dimension ``{name: seg_alias, expr: market_segment}`` served RAW PII
  while the identically-sourced ``{name: market_segment}`` was redacted.

Both are now enforced at compile time / by expression, and both are
exercised here through a live engine so the guard can't rot into a
mock-only assertion.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.output_ports.mcp._handlers import tool_query, tool_sample  # noqa: E402
from fluid_build.output_ports.mcp.drivers import build_driver  # noqa: E402
from fluid_build.output_ports.mcp.drivers.base import EngineDriver  # noqa: E402
from fluid_build.output_ports.mcp.query_compiler import QueryValidationError  # noqa: E402


def _build(tmp_path: Path, *, max_sample_rows: int = 100):
    csv = tmp_path / "customers.csv"
    csv.write_text(
        "customer_id,email,segment,account_balance,amount\n"
        "1,a@example.com,retail,100,10\n"
        "2,b@example.com,retail,200,20\n"
        "3,c@example.com,wholesale,300,30\n"
        "4,d@example.com,wholesale,400,40\n"
        "5,e@example.com,direct,500,50\n"
    )
    expose = {
        "exposeId": "customers",
        "kind": "table",
        "contract": {
            "schema": [
                {"name": "customer_id"},
                {"name": "email", "sensitivity": "pii"},
                {"name": "segment"},
                {"name": "account_balance"},
                {"name": "amount"},
            ]
        },
        "semantics": {
            "measures": [
                {"name": "total_amount", "agg": "sum", "expr": "amount"},
                {"name": "avg_balance", "agg": "avg", "expr": "account_balance"},
                {"name": "max_email", "agg": "max", "expr": "email"},
                {"name": "email_count", "agg": "count_distinct", "expr": "email"},
            ],
            "dimensions": [
                {"name": "segment", "type": "categorical", "expr": "segment"},
                # Same PII column, different projection name — the bypass.
                {"name": "contact_alias", "type": "categorical", "expr": "email"},
                {"name": "balance_alias", "type": "categorical", "expr": "account_balance"},
            ],
            "metrics": [
                {"name": "revenue", "type": "simple", "measure": "total_amount"},
                {"name": "mean_balance", "type": "simple", "measure": "avg_balance"},
            ],
        },
        "binding": {
            "platform": "local",
            "format": "csv",
            "location": {"path": str(csv), "table": "customers"},
        },
        "policy": {
            "authz": {
                "columnRestrictions": [
                    {"principal": "*", "columns": ["account_balance"], "access": "deny"}
                ]
            }
        },
    }
    driver = build_driver(expose=expose, contract={"exposes": [expose]})
    return SimpleNamespace(
        expose=expose,
        caller_attributes={},
        policy=SimpleNamespace(max_sample_rows=max_sample_rows),
        query_timeout_seconds=None,
        get_driver=lambda: driver,
    )


# ---------------------------------------------------------------------
# columnRestrictions: denied on EVERY route into the governed query
# ---------------------------------------------------------------------


def test_sample_still_drops_the_denied_column(tmp_path: Path):
    """Baseline — the layer that already worked must keep working."""
    payload = tool_sample(_build(tmp_path), {"limit": 5})
    assert "account_balance" not in payload["columns"]
    assert "segment" in payload["columns"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"measure": "avg_balance", "limit": 10},
        {"metric": "mean_balance", "limit": 10},
        {"metric": "revenue", "dimensions": ["balance_alias"], "limit": 10},
        {"metric": "revenue", "filters": {"account_balance": 500}, "limit": 10},
    ],
    ids=["measure", "metric", "dimension", "filter-oracle"],
)
def test_denied_column_is_rejected_on_every_query_route(tmp_path: Path, arguments):
    state = _build(tmp_path)
    with pytest.raises(QueryValidationError, match="account_balance"):
        tool_query(state, arguments)


def test_governed_query_over_permitted_columns_still_works(tmp_path: Path):
    payload = tool_query(_build(tmp_path), {"metric": "revenue", "dimensions": ["segment"]})
    assert payload["columns"] == ["segment", "revenue"]
    assert {row["segment"]: float(row["revenue"]) for row in payload["rows"]} == {
        "retail": 30.0,
        "wholesale": 70.0,
        "direct": 50.0,
    }


# ---------------------------------------------------------------------
# PII: redaction follows the EXPRESSION, not the output name
# ---------------------------------------------------------------------


def test_pii_dimension_is_redacted_under_a_different_alias(tmp_path: Path):
    payload = tool_query(_build(tmp_path), {"metric": "revenue", "dimensions": ["contact_alias"]})
    values = {row["contact_alias"] for row in payload["rows"]}
    assert values == {EngineDriver.PII_TOKEN}
    assert not any("@example.com" in str(value) for value in values)


def test_value_revealing_aggregate_over_pii_is_redacted(tmp_path: Path):
    """MAX(email) hands back a real address; COUNT(DISTINCT email) does
    not — aggregate analysis over a PII column stays available."""
    revealing = tool_query(_build(tmp_path), {"measure": "max_email", "limit": 10})
    assert revealing["rows"] == [{"max_email": EngineDriver.PII_TOKEN}]
    summarising = tool_query(_build(tmp_path), {"measure": "email_count", "limit": 10})
    assert summarising["rows"] == [{"email_count": 5}]


# ---------------------------------------------------------------------
# truncated: a clipped GROUP BY must say so
# ---------------------------------------------------------------------


def test_clipped_group_by_reports_truncated(tmp_path: Path):
    state = _build(tmp_path, max_sample_rows=2)
    payload = tool_query(state, {"metric": "revenue", "dimensions": ["segment"]})
    # 3 segments exist; the server cap clipped it to 2.
    assert payload["rowCount"] == 2
    assert payload["truncated"] is True
    # …and the 2 it kept are the deterministic top-2 by the metric, not
    # whatever the engine happened to emit first.
    assert [row["segment"] for row in payload["rows"]] == ["wholesale", "direct"]


def test_complete_group_by_is_not_reported_truncated(tmp_path: Path):
    payload = tool_query(_build(tmp_path), {"metric": "revenue", "dimensions": ["segment"]})
    assert payload["rowCount"] == 3
    assert payload["truncated"] is False


def test_ungrouped_aggregate_is_never_truncated(tmp_path: Path):
    """One row is the complete answer however small the LIMIT is."""
    payload = tool_query(_build(tmp_path, max_sample_rows=1), {"metric": "revenue"})
    assert payload["rowCount"] == 1
    assert payload["truncated"] is False
    assert float(payload["rows"][0]["revenue"]) == 150.0
