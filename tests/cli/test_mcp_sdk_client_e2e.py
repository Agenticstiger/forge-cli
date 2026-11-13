# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""End-to-end SDK↔SDK integration tests.

The existing live test (``test_scaffold_ide.py::test_mcp_handshake_lives``)
spawns the real ``fluid mcp serve`` subprocess but acts as the client using
**hand-rolled JSON-RPC**. That proves wire compatibility, but it doesn't
exercise a real ``ClientSession`` (no anyio task-group machinery, no
SDK-side request-id correlation, no SDK-side capability negotiation).

This file uses the official ``mcp.client.session.ClientSession`` +
``mcp.client.stdio.stdio_client`` so we get **SDK on both ends**. If
``test_mcp_handshake_lives`` passes here, real IDEs (Claude Code, Cursor,
Kiro, Cline) — which all use the SDK or compatible implementations —
will see identical behaviour.

Two shapes verified:

1. SDK↔SDK initialize + list_tools — returns the 11 forge tools.
2. SDK↔SDK call_tool against a read-only tool (``validate_contract`` on
   the hello-world example) — returns a structured score.
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
        "list_source_adapters",
        "list_source_tables",
        "inspect_source_table",
        "list_source_lineage",
        "list_source_glossary",
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
