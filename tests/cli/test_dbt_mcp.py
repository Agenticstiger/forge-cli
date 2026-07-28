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

"""Pins for the dbt MCP delegated backend (FLUID_DBT_MCP).

Uses the MCP SDK's in-memory ``create_connected_server_and_client_session``
harness (real MCP protocol, zero network / subprocess) via the
``DbtMcpClient._open_session`` seam — mirroring tests/cli/test_market_mcp_catalog.
Covers: client list/call, the bridge (tool defs + dispatch), the
``forge_copilot_tools`` integration (tools surface to the LLM + route on call),
and that the whole thing is a no-op when disabled.
"""

from __future__ import annotations

import contextlib
import json
from unittest.mock import patch

import mcp.types as mcp_types
from mcp.server.lowlevel import Server
from mcp.types import Implementation

# In-memory client<->server harness + lowlevel-server factory via the SDK
# version-compat seam (the v1 helper and the @server.* decorator API were
# both removed in mcp 2.x).
from fluid_build._mcp_compat import build_lowlevel_server, open_inmemory_session
from fluid_build.cli import dbt_mcp, forge_copilot_tools
from fluid_build.cli.dbt_mcp import (
    TOOL_PREFIX,
    DbtMcpClient,
    _result_to_payload,
    dbt_mcp_tool_definitions,
    dispatch_dbt_mcp_tool,
    is_dbt_mcp_tool,
    is_enabled,
)

_ON = {"FLUID_DBT_MCP": "1"}


@contextlib.asynccontextmanager
async def _noop_lifespan(_server):
    """No-op server lifespan (compat seam passes ``lifespan`` through verbatim)."""
    yield {}


def _make_dbt_server() -> Server:
    """A minimal in-memory stand-in for dbt-labs/dbt-mcp: two tools."""

    async def _list_tools():  # noqa: D401
        return [
            mcp_types.Tool(
                name="list_metrics",
                description="List dbt Semantic Layer metrics",
                inputSchema={"type": "object", "properties": {}},
            ),
            mcp_types.Tool(
                name="run_sql",
                description="Run SQL through dbt",
                inputSchema={
                    "type": "object",
                    "properties": {"sql": {"type": "string"}},
                    "required": ["sql"],
                },
            ),
        ]

    async def _call_tool(_ctx, name, arguments):  # noqa: D401
        if name == "list_metrics":
            return [mcp_types.TextContent(type="text", text=json.dumps(["revenue", "orders"]))]
        if name == "run_sql":
            payload = {"rows": [[1], [2]], "sql": arguments.get("sql")}
            return [mcp_types.TextContent(type="text", text=json.dumps(payload))]
        raise ValueError(f"unknown tool: {name}")

    return build_lowlevel_server(
        "fake-dbt-mcp",
        version="0.0.0",
        lifespan=_noop_lifespan,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )


def _open_session_patch(server: Server):
    """Build a ``_open_session`` replacement bound to an in-memory server."""

    def _open(_self):
        @contextlib.asynccontextmanager
        async def _session():
            async with open_inmemory_session(
                server, client_info=Implementation(name="fluid-dbt-test", version="0.0.0")
            ) as session:
                yield session

        return _session()

    return _open


# ── enable / gating helpers ─────────────────────────────────────────────────
def test_is_enabled_reads_env_flag():
    assert is_enabled({"FLUID_DBT_MCP": "1"}) is True
    assert is_enabled({"FLUID_DBT_MCP": "true"}) is True
    assert is_enabled({"FLUID_DBT_MCP": "0"}) is False
    assert is_enabled({}) is False


def test_is_dbt_mcp_tool_requires_prefix_and_enabled():
    assert is_dbt_mcp_tool("dbt.run_sql", _ON) is True
    assert is_dbt_mcp_tool("run_sql", _ON) is False  # no prefix
    assert is_dbt_mcp_tool("dbt.run_sql", {}) is False  # disabled


# ── client list / call over the real (in-memory) protocol ───────────────────
def test_client_lists_tools():
    server = _make_dbt_server()
    with patch.object(DbtMcpClient, "_open_session", _open_session_patch(server)):
        names = {n for n, _d, _s in DbtMcpClient().list_tools()}
    assert names == {"list_metrics", "run_sql"}


def test_client_calls_tool_and_returns_payload():
    server = _make_dbt_server()
    with patch.object(DbtMcpClient, "_open_session", _open_session_patch(server)):
        out = DbtMcpClient().call_tool("run_sql", {"sql": "select 1"})
    parsed = json.loads(out)
    assert parsed["sql"] == "select 1" and parsed["rows"] == [[1], [2]]


# ── bridge: tool defs + dispatch ────────────────────────────────────────────
def test_tool_definitions_prefixed_and_described_when_enabled():
    server = _make_dbt_server()
    with patch.object(DbtMcpClient, "_open_session", _open_session_patch(server)):
        defs = dbt_mcp_tool_definitions(env=_ON)
    by_name = {d["name"]: d for d in defs}
    assert set(by_name) == {f"{TOOL_PREFIX}list_metrics", f"{TOOL_PREFIX}run_sql"}
    assert by_name[f"{TOOL_PREFIX}run_sql"]["description"].startswith("[dbt MCP]")
    assert by_name[f"{TOOL_PREFIX}run_sql"]["input_schema"]["required"] == ["sql"]


def test_tool_definitions_empty_when_disabled():
    # Disabled → no probe, no tools (and crucially: never raises).
    assert dbt_mcp_tool_definitions(env={}) == []


def test_tool_definitions_empty_on_discovery_failure():
    def _boom(_self):
        raise RuntimeError("server not found")

    with patch.object(DbtMcpClient, "_open_session", _boom):
        assert dbt_mcp_tool_definitions(env=_ON) == []


def test_dispatch_routes_to_server():
    server = _make_dbt_server()
    with patch.object(DbtMcpClient, "_open_session", _open_session_patch(server)):
        out = dispatch_dbt_mcp_tool("dbt.run_sql", {"sql": "select 2"}, env=_ON)
    assert json.loads(out)["sql"] == "select 2"


def test_dispatch_returns_typed_error_no_leak():
    def _boom(_self):
        dsn = "postgres://user:pw@host/db"  # pragma: allowlist secret
        raise RuntimeError(f"{dsn} unreachable")

    with patch.object(DbtMcpClient, "_open_session", _boom):
        out = dispatch_dbt_mcp_tool("dbt.run_sql", {"sql": "x"}, env=_ON)
    assert out["error"] == "RuntimeError"
    assert "postgres://" not in out["message"]  # no raw exception text leaked


# ── _result_to_payload coercion ─────────────────────────────────────────────
def test_result_payload_prefers_structured():
    class _R:
        structuredContent = {"a": 1}
        content = []

    assert _result_to_payload(_R()) == {"a": 1}


def test_result_payload_falls_back_to_text():
    class _Block:
        text = "hello"

    class _R:
        structuredContent = None
        content = [_Block()]

    assert _result_to_payload(_R()) == "hello"


# ── forge_copilot_tools integration (the real wiring) ───────────────────────
def test_get_tool_definitions_includes_dbt_tools_when_enabled(monkeypatch):
    monkeypatch.setenv("FLUID_DBT_MCP", "1")
    server = _make_dbt_server()
    with patch.object(DbtMcpClient, "_open_session", _open_session_patch(server)):
        names = {t["name"] for t in forge_copilot_tools.get_tool_definitions()}
    assert f"{TOOL_PREFIX}run_sql" in names
    assert f"{TOOL_PREFIX}list_metrics" in names


def test_get_tool_definitions_excludes_dbt_tools_when_disabled(monkeypatch):
    monkeypatch.delenv("FLUID_DBT_MCP", raising=False)
    names = {t["name"] for t in forge_copilot_tools.get_tool_definitions()}
    assert not any(n.startswith(TOOL_PREFIX) for n in names)


def test_dispatch_tool_call_routes_dbt_tool(monkeypatch):
    monkeypatch.setenv("FLUID_DBT_MCP", "1")
    server = _make_dbt_server()
    with patch.object(DbtMcpClient, "_open_session", _open_session_patch(server)):
        out = forge_copilot_tools.dispatch_tool_call("dbt.run_sql", {"sql": "select 3"})
    assert json.loads(out)["sql"] == "select 3"


def test_dispatch_tool_call_unknown_dbt_tool_when_disabled(monkeypatch):
    monkeypatch.delenv("FLUID_DBT_MCP", raising=False)
    out = forge_copilot_tools.dispatch_tool_call("dbt.run_sql", {"sql": "x"})
    assert "error" in out and "Unknown tool" in out["error"]
