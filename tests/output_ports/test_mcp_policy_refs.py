# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An expose whose policy is a pointer is read through it, or refused.

forge's loader resolves every ``$ref`` before the gateway reads a contract, a pointer
into the same document included: ``fluid mcp output-port serve`` loads the contract
with ``load_contract_with_overlay``, and a bundle was resolved by ``fluid bundle``. A
caller that passes an expose of its own, as Command Center calls
``compile_semantic_query``, can hand over ``policy: {$ref: ...}`` unresolved. Read as
written, that expose would declare no row filter and be served whole, so every read path
refuses it.
"""

from __future__ import annotations

import copy
import logging
from types import SimpleNamespace
from typing import Any, Callable, Dict

import pytest

from fluid_build._contract_loader import load_contract_with_overlay
from fluid_build.output_ports.mcp import query_compiler
from fluid_build.output_ports.mcp.query_compiler import RowFilterUnavailable

EU = "group:eu@x.example"
_POINTER = "#/x-shared/orders_policy"

_CONTRACT = """\
fluidVersion: 0.7.6
kind: DataProduct
id: demo.orders
name: orders
metadata:
  owner: {team: data}
x-shared:
  orders_policy:
    authz:
      readers: ["group:all@x.example"]
      rowFilters:
        - principal: "group:eu@x.example"
          where: "region = 'EU'"
exposes:
  - exposeId: orders
    kind: table
    binding:
      platform: local
      format: csv
      location: {path: orders.csv, table: orders}
    contract:
      schema:
        - {name: id, type: integer}
        - {name: region, type: string}
    semantics:
      measures:
        - {name: order_count, agg: count, expr: id}
    policy:
      $ref: "#/x-shared/orders_policy"
"""

_FILTER = {"principal": EU, "where": "region = 'EU'"}


def _expose(**changes: Any) -> Dict[str, Any]:
    expose: Dict[str, Any] = {
        "exposeId": "orders",
        "kind": "table",
        "contract": {"schema": [{"name": "id", "type": "integer"}, {"name": "region"}]},
        "semantics": {"measures": [{"name": "order_count", "agg": "count", "expr": "id"}]},
        "policy": {"authz": {"rowFilters": [dict(_FILTER)]}},
    }
    for path, value in changes.items():
        node = expose
        *parents, last = path.split(".")
        for part in parents:
            node = node[int(part)] if part.isdigit() else node[part]
        if last.isdigit():
            node[int(last)] = value
        else:
            node[last] = value
    return copy.deepcopy(expose)


_UNRESOLVED = [
    pytest.param(_expose(policy={"$ref": _POINTER}), id="policy"),
    pytest.param(_expose(**{"policy.authz": {"$ref": _POINTER}}), id="authz"),
    pytest.param(_expose(**{"policy.authz.rowFilters": {"$ref": _POINTER}}), id="row-filters"),
    pytest.param(_expose(**{"policy.authz.rowFilters.0": {"$ref": _POINTER}}), id="one-filter"),
    pytest.param(_expose(**{"policy.authz.readers": {"$ref": _POINTER}}), id="readers"),
    pytest.param(_expose(**{"contract.schema.1": {"$ref": _POINTER}}), id="schema-column"),
    pytest.param(_expose(**{"contract.schema": {"$ref": _POINTER}}), id="schema"),
    pytest.param(_expose(contract={"$ref": _POINTER}), id="contract"),
]


def test_the_gateways_loader_resolves_a_policy_pointer(tmp_path):
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(_CONTRACT, encoding="utf-8")
    contract = load_contract_with_overlay(str(path), None, logging.getLogger(__name__))
    expose = contract["exposes"][0]
    assert expose["policy"]["authz"]["rowFilters"] == [_FILTER]
    assert query_compiler.declares_row_filters(expose)
    predicate = query_compiler.row_filter_predicate(expose, {"principal": EU}, dialect="duckdb")
    assert predicate == "((_fluid_rows.region = 'EU'))"


@pytest.mark.parametrize("expose", _UNRESOLVED)
def test_every_row_filter_reader_refuses_an_unresolved_pointer(expose):
    calls: Dict[str, Callable[[], Any]] = {
        "declares_row_filters": lambda: query_compiler.declares_row_filters(expose),
        "row_filter_predicate": lambda: query_compiler.row_filter_predicate(
            expose, {"principal": EU}, dialect="duckdb"
        ),
        "query_sql": lambda: query_compiler.compile_free_form_sql(
            sql="SELECT id FROM orders",
            table_reference="orders",
            limit=10,
            expose=expose,
            caller_attributes={"principal": EU},
            dialect="duckdb",
        ),
        # Command Center's call: the raw expose, and no caller identity.
        "query": lambda: query_compiler.compile_semantic_query(
            expose=expose,
            measure="order_count",
            limit=10,
            table_reference="orders",
            dialect="duckdb",
        ),
    }
    for name, call in calls.items():
        with pytest.raises(RowFilterUnavailable, match=r"unresolved \$ref"):
            call()
            pytest.fail(f"{name} read the expose through an unresolved pointer")


def test_the_sample_and_query_sql_tools_refuse_an_unresolved_pointer(tmp_path):
    pytest.importorskip("duckdb")
    from fluid_build.output_ports.mcp._handlers import tool_query_sql, tool_sample
    from fluid_build.output_ports.mcp.drivers import build_driver

    csv = tmp_path / "orders.csv"
    csv.write_text("id,region\n1,EU\n2,US\n", encoding="utf-8")
    expose = _expose(policy={"$ref": _POINTER})
    expose["binding"] = {
        "platform": "local",
        "format": "csv",
        "location": {"path": str(csv), "table": "orders"},
    }
    driver = build_driver(expose=expose, contract={"exposes": [expose]})
    state = SimpleNamespace(
        expose=expose,
        caller_attributes={},
        policy=SimpleNamespace(max_sample_rows=100),
        query_timeout_seconds=None,
        get_driver=lambda: driver,
    )
    with pytest.raises(RowFilterUnavailable, match=r"unresolved \$ref"):
        tool_sample(state, {"limit": 10}, caller_attributes={"principal": EU})
    with pytest.raises(RowFilterUnavailable, match=r"unresolved \$ref"):
        tool_query_sql(state, {"sql": "SELECT id FROM orders"}, caller_attributes={})


def test_serve_refuses_an_unresolved_pointer_at_startup():
    from fluid_build.cli.mcp_output_port import _row_filter_gate_unsatisfiable

    reason = _row_filter_gate_unsatisfiable(
        _expose(policy={"$ref": _POINTER}), transport="http", http_auth=None
    )
    assert reason is not None and "unresolved $ref" in reason
