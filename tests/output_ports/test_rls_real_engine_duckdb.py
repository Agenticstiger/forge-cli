# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Real-engine (DuckDB) end-to-end test of row-level security.

The compiler/handler RLS tests use a capturing driver; this one runs the full
path against a LIVE DuckDB engine over a real 2-tenant CSV, so it catches bugs
that only surface at execution: each tenant principal of
``policy.authz.rowFilters`` reads its own rows through ``sample``, ``query`` and
``query_sql``, including when its statement fabricates the filter column.
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

ACME = "tenant:acme"
GLOBEX = "tenant:globex"


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
        "policy": {
            "authz": {
                "rowFilters": [
                    {"principal": ACME, "where": "tenant_id = 'acme'"},
                    {"principal": GLOBEX, "where": "tenant_id = 'globex'"},
                ]
            }
        },
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
    acme = tool_query(state({}), {"measure": "total_amount"}, caller_attributes={"principal": ACME})
    globex = tool_query(
        state({}), {"measure": "total_amount"}, caller_attributes={"principal": GLOBEX}
    )
    assert _scalar(acme["rows"]) == 60.0  # 10+20+30
    assert _scalar(globex["rows"]) == 104.0  # 99+5


def test_sample_isolates_tenant_on_real_duckdb(tmp_path):
    state = _build(tmp_path)
    acme = tool_sample(state({}), {"limit": 100}, caller_attributes={"principal": ACME})
    globex = tool_sample(state({}), {"limit": 100}, caller_attributes={"principal": GLOBEX})
    assert _tenants(acme) == ["acme"] and len(acme["rows"]) == 3
    assert _tenants(globex) == ["globex"] and len(globex["rows"]) == 2


def test_free_form_query_sql_isolates_tenant_on_real_duckdb(tmp_path):
    """A statement that fabricates the filter column (``'acme' AS tenant_id``) still
    reads only the caller's rows: the filter is applied where the table is read."""
    state = _build(tmp_path)
    globex = tool_query_sql(
        state({}),
        {"sql": "SELECT 'acme' AS tenant_id, amount FROM orders", "limit": 10},
        caller_attributes={"principal": GLOBEX},
    )
    assert sorted(float(row["amount"]) for row in globex["rows"]) == [5.0, 99.0]
    both = tool_query_sql(
        state({}),
        {"sql": "SELECT o.amount FROM orders AS o, orders AS p WHERE o.order_id = p.order_id"},
        caller_attributes={"principal": ACME},
    )
    assert sorted(float(row["amount"]) for row in both["rows"]) == [10.0, 20.0, 30.0]
