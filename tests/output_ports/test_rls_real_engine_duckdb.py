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

"""Real-engine (DuckDB) end-to-end test of row-level security.

The compiler/handler RLS tests use a capturing driver; this one runs the full
path against a LIVE DuckDB engine over a real 2-tenant CSV, so it catches bugs
that only surface at execution. It pinned (and now guards) a pre-existing defect
the live E2E found: ``sample()`` built ``WHERE "tenant_id" = :p_0`` but never
rendered the portable ``:p_<index>`` placeholder to the driver's native form
before execute (the ``query`` path renders via CompiledQuery; ``sample`` did
not), so sample+rowFilters raised ``Parser Error: syntax error at ":"`` on every
real engine. No existing test exercised sample()+rowFilters against a real
driver, so it shipped silently.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.output_ports.mcp._handlers import (  # noqa: E402
    tool_query,
    tool_query_sql,
    tool_sample,
)
from fluid_build.output_ports.mcp.drivers import build_driver  # noqa: E402
from fluid_build.output_ports.mcp.query_compiler import QueryValidationError  # noqa: E402


def _build(tmp_path: Path):
    csv = tmp_path / "orders.csv"
    csv.write_text(
        "order_id,tenant_id,amount\n1,acme,10\n2,acme,20\n3,globex,99\n4,globex,5\n5,acme,30\n"
    )
    expose = {
        "exposeId": "orders",
        "kind": "table",
        "contract": {"schema": [{"name": "order_id"}, {"name": "tenant_id"}, {"name": "amount"}]},
        "semantics": {"measures": [{"name": "total_amount", "agg": "sum", "expr": "amount"}]},
        "binding": {
            "platform": "local",
            "format": "csv",
            "location": {"path": str(csv), "table": "orders"},
        },
        "policy": {"rowFilters": [{"column": "tenant_id", "equals": "${caller.tenant_id}"}]},
    }
    driver = build_driver(expose=expose, contract={"exposes": [expose]})

    def state(attrs):
        return SimpleNamespace(
            expose=expose,
            caller_attributes=attrs,
            policy=SimpleNamespace(max_sample_rows=100),
            query_timeout_seconds=None,
            get_driver=lambda: driver,
        )

    return state


def _scalar(rows):
    row = rows[0]
    return float(next(iter(row.values())) if isinstance(row, dict) else row[0])


def _tenants(result):
    cols = result["columns"]

    def cell(r):
        return r["tenant_id"] if isinstance(r, dict) else r[cols.index("tenant_id")]

    return sorted({cell(r) for r in result["rows"]})


def test_query_isolates_tenant_on_real_duckdb(tmp_path):
    state = _build(tmp_path)
    acme = tool_query(
        state({"tenant_id": "acme"}),
        {"measure": "total_amount"},
        caller_attributes={"tenant_id": "acme"},
    )
    globex = tool_query(
        state({"tenant_id": "globex"}),
        {"measure": "total_amount"},
        caller_attributes={"tenant_id": "globex"},
    )
    assert _scalar(acme["rows"]) == 60.0  # 10+20+30
    assert _scalar(globex["rows"]) == 104.0  # 99+5


def test_sample_isolates_tenant_on_real_duckdb(tmp_path):
    """Regression: sample+rowFilters must render :p_<index> before execute."""
    state = _build(tmp_path)
    acme = tool_sample(
        state({"tenant_id": "acme"}), {"limit": 100}, caller_attributes={"tenant_id": "acme"}
    )
    globex = tool_sample(
        state({"tenant_id": "globex"}), {"limit": 100}, caller_attributes={"tenant_id": "globex"}
    )
    assert _tenants(acme) == ["acme"] and len(acme["rows"]) == 3
    assert _tenants(globex) == ["globex"] and len(globex["rows"]) == 2


def test_free_form_query_sql_rejected_on_rowfiltered_expose(tmp_path):
    state = _build(tmp_path)
    with pytest.raises(QueryValidationError):
        tool_query_sql(
            state({"tenant_id": "acme"}),
            {"sql": "SELECT 'acme' AS tenant_id, amount FROM orders", "limit": 10},
            caller_attributes={"tenant_id": "acme"},
        )
