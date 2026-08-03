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

"""End-to-end SDK↔SDK integration tests.

The existing live tests (``test_mcp_forge_run_live.py``,
``test_scaffold_ide.py::test_mcp_handshake_lives``) spawn the real
``fluid mcp serve`` subprocess but act as the client using **hand-rolled
JSON-RPC**. That proves wire compatibility, but it doesn't exercise the
sampling callback round-trip the same way a real ``ClientSession`` does
(no anyio task-group machinery, no SDK-side request-id correlation, no
SDK-side capability negotiation).

This file uses the official ``mcp.client.session.ClientSession`` +
``mcp.client.stdio.stdio_client`` so we get **SDK on both ends**. If
``test_mcp_handshake_lives`` passes here, real IDEs (Claude Code, Cursor,
Kiro, Cline) — which all use the SDK or compatible implementations —
will see identical behaviour.

Three shapes verified:

1. SDK↔SDK initialize + list_tools — returns the 14 forge tools.
2. SDK↔SDK call_tool against a read-only tool (``validate_contract`` on
   the hello-world example) — returns a structured score.
3. SDK↔SDK call_tool against ``forge_run mode='blank'`` — produces a
   real contract.fluid.yaml on disk.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from fluid_build._mcp_compat import attr as _mcp_attr


def _server_params(cwd: Path) -> StdioServerParameters:
    """Spawn the same way an IDE would: ``python -m fluid_build.cli mcp serve``.

    Using ``sys.executable`` so the test inherits the venv we're running in
    (no PATH dependency).
    """
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "fluid_build.cli", "mcp", "serve"],
        env={**os.environ},
        cwd=str(cwd),
    )


# ---------------------------------------------------------------------------
# 1. SDK↔SDK handshake + tools/list
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_sdk_client_lists_all_14_forge_tools(tmp_path: Path):
    """Both ends use the MCP SDK. After ``ClientSession.initialize()`` the
    server advertises all 14 forge tools in ``list_tools``.
    """

    async def _run() -> list[str]:
        async with stdio_client(_server_params(tmp_path)) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                listing = await client.list_tools()
                return sorted(t.name for t in listing.tools)

    names = asyncio.run(_run())
    expected = {
        "read_logical_model",
        "update_entity",
        "add_relationship",
        "regenerate_physical",
        "validate_contract",
        "diff_models",
        "search_semantic_memory",
        "list_source_adapters",
        "list_source_tables",
        "inspect_source_table",
        "list_source_lineage",
        "list_source_glossary",
        "forge_from_source",
        "forge_run",
    }
    missing = expected - set(names)
    assert not missing, f"missing tools: {missing}; got {names}"


# ---------------------------------------------------------------------------
# 2. SDK↔SDK call_tool against a read-only tool (validate_contract)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_sdk_client_calls_validate_contract(tmp_path: Path):
    """Use the real SDK to call ``validate_contract`` against the bundled
    hello-world example. The result has a ``score`` field and a list of
    issues — same structure the SDK delivers to a real IDE.
    """
    repo_root = Path(__file__).resolve().parents[2]
    contract_path = repo_root / "examples" / "01-hello-world" / "contract.fluid.yaml"
    assert contract_path.is_file(), f"missing fixture: {contract_path}"

    async def _run() -> dict:
        async with stdio_client(_server_params(repo_root)) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                result = await client.call_tool(
                    "validate_contract",
                    arguments={"contract_path": str(contract_path)},
                )
                # Tool returned a single TextContent with JSON-serialised body.
                assert not _mcp_attr(result, "is_error", "isError", False), result
                text_block = result.content[0]
                return json.loads(text_block.text)

    body = asyncio.run(_run())
    assert "score" in body, body
    assert isinstance(body["score"], int)
    assert "issues" in body
    # passes_schema is bool; we don't assert which value because the example
    # contract may legitimately have warnings (e.g. missing semantics block).
    assert "passes_schema" in body


# ---------------------------------------------------------------------------
# 3. SDK↔SDK call_tool against forge_run mode='blank' — produces a contract
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_sdk_client_runs_forge_run_blank(tmp_path: Path):
    """The deepest path: SDK → forge_run tool → in-process forge.run() →
    contract.fluid.yaml on disk. Exercises the threading bridge
    (``asyncio.to_thread``) used by the sampling path.

    REGRESSION GATE: ``forge --agent`` writes JSON-Lines events to stdout;
    inside ``forge_run`` that stdout MUST be captured and surfaced as the
    ``events`` field on the result, NOT leaked onto the MCP wire (which
    would crash the SDK's strict JSON-RPC parser with a
    ``JSONRPCMessage`` validation error).
    """
    target = tmp_path / "product"

    async def _run() -> dict:
        async with stdio_client(_server_params(tmp_path)) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                result = await client.call_tool(
                    "forge_run",
                    arguments={
                        "mode": "blank",
                        "target_dir": str(target),
                        "data_product_type": "SDP",
                    },
                )
                assert not _mcp_attr(result, "is_error", "isError", False), result
                return json.loads(result.content[0].text)

    payload = asyncio.run(_run())
    assert payload["mode"] == "blank"
    assert payload["exit_code"] == 0
    assert payload["contract_exists"] is True
    assert (target / "contract.fluid.yaml").is_file()

    # Regression: forge's JSONL events must be surfaced as structured data
    # in the tool result, never leaked onto the MCP wire. If this list is
    # empty for blank mode, forge stopped emitting events; if the stdio
    # collision returns, the SDK call would error before we get here.
    events = payload.get("events", [])
    event_names = [e["event"] for e in events]
    assert "forge.start" in event_names, event_names
    assert "forge.done" in event_names, event_names
    assert "forge.contract_written" in event_names, event_names
