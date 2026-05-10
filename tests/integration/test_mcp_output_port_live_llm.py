# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Live-LLM end-to-end tests for the Fluid MCP output port.

These tests prove the *contract with the LLM* — that real models
actually receive the typed deny envelope when agentPolicy refuses
their tool call, and react sensibly (don't loop, don't crash, don't
escape). The unit + in-proc tests prove the gate logic; only this
file proves the LLM-side contract.

Skipped automatically when no LLM API key is in the environment, so
CI without secrets stays green and local devs without keys aren't
blocked.

**Cost:** ~$0.08 per full run on Anthropic Haiku 4.5
(``claude-haiku-4-5-20251001``). Each scenario asserts
``RunCostTracker.total_usd < 0.05`` as a hard guard.

Provider selection (per the approved plan):
- ``ANTHROPIC_API_KEY`` set → Haiku 4.5 (preferred, cheapest current
  Anthropic model).
- ``OPENAI_API_KEY`` set (and no Anthropic) → ``gpt-4o-mini``.
- Neither → ``pytest.skip``.

The LLM is exercised via :mod:`litellm` (already a forge-cli core
dep), which normalises the wire format across Anthropic / OpenAI
so the same scenarios run against either.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Mapping, Optional, Tuple

import pytest

duckdb = pytest.importorskip("duckdb")
litellm = pytest.importorskip("litellm")

from mcp import ClientSession  # noqa: E402
from mcp.shared.memory import (  # noqa: E402
    create_connected_server_and_client_session,
)
from mcp.types import Implementation  # noqa: E402

from fluid_build.output_ports.mcp.policy import OutputPortPolicy  # noqa: E402
from fluid_build.output_ports.mcp.server import OutputPortMcpServer  # noqa: E402
from tests.output_ports._fixtures import make_expose, write_customer_csv  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# ---------------------------------------------------------------------
# Provider resolution + cost guard
# ---------------------------------------------------------------------

ANTHROPIC_MODEL = "anthropic/claude-haiku-4-5-20251001"
OPENAI_MODEL = "openai/gpt-4o-mini"

# What the gateway expects in agentPolicy.allowedModels — the bare
# model name without the litellm provider prefix.
ANTHROPIC_BARE = "claude-haiku-4-5-20251001"
OPENAI_BARE = "gpt-4o-mini"

# Cost cap per scenario (dollars). Asserts below the per-run cap so
# a runaway loop can't burn money.
COST_CAP_USD = 0.05


def _resolve_provider() -> Tuple[str, str]:
    """Return ``(litellm_model, bare_model_name)`` for the available
    provider, or skip the test when neither key is set.

    Anthropic preferred (cheapest Haiku) per the approved plan.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ANTHROPIC_MODEL, ANTHROPIC_BARE
    if os.environ.get("OPENAI_API_KEY"):
        return OPENAI_MODEL, OPENAI_BARE
    pytest.skip("no LLM API key in env; set ANTHROPIC_API_KEY or OPENAI_API_KEY")


def _enforce_cost_cap(response_obj: Any) -> float:
    """Pull the per-call cost from a litellm completion response and
    assert it stays under the per-scenario cap. Returns the cost so
    the test can also report it."""
    # litellm exposes per-call cost via the ``_hidden_params['response_cost']``
    # path on the response object; some versions also stash it on usage.
    cost = 0.0
    hidden = getattr(response_obj, "_hidden_params", None) or {}
    if "response_cost" in hidden and hidden["response_cost"] is not None:
        cost = float(hidden["response_cost"])
    assert cost < COST_CAP_USD, f"per-call cost ${cost:.4f} exceeds cap ${COST_CAP_USD:.4f}"
    return cost


# ---------------------------------------------------------------------
# Fixture: gateway + connected MCP client
# ---------------------------------------------------------------------


@asynccontextmanager
async def _running_gateway(
    *,
    tmp_path: Path,
    cli_allowed_models: Optional[Tuple[str, ...]] = None,
    cli_denied_models: Optional[Tuple[str, ...]] = None,
    cli_allowed_use_cases: Optional[Tuple[str, ...]] = None,
    cli_denied_use_cases: Optional[Tuple[str, ...]] = None,
    bound_model_id: Optional[str] = None,
    bound_use_case: Optional[str] = None,
) -> AsyncIterator[ClientSession]:
    """Spin up the gateway + an in-memory MCP client. Yields the
    connected client session."""
    csv_path = write_customer_csv(tmp_path / "customers.csv")
    expose = make_expose(
        binding={
            "platform": "local",
            "format": "csv",
            "location": {"path": str(csv_path), "table": "customer_profiles"},
        },
    )
    contract = {
        "fluidVersion": "0.7.4",
        "kind": "DataProduct",
        "id": "demo.live.customers_v1",
        "exposes": [expose],
    }
    policy = OutputPortPolicy.from_contract_and_flags(
        expose=expose,
        cli_allowed_models=cli_allowed_models,
        cli_denied_models=cli_denied_models,
        cli_allowed_use_cases=cli_allowed_use_cases,
        cli_denied_use_cases=cli_denied_use_cases,
    )
    server = OutputPortMcpServer(contract=contract, expose=expose, policy=policy)
    if bound_model_id is not None:
        server.state.model_id = bound_model_id
    if bound_use_case is not None:
        server.state.use_case = bound_use_case

    async with create_connected_server_and_client_session(
        server.server,
        client_info=Implementation(name="fluid-live-llm-test", version="0.1.0"),
    ) as client:
        yield client


# ---------------------------------------------------------------------
# Helpers — drive a single Anthropic-style tool-use turn via litellm
# ---------------------------------------------------------------------


async def _run_one_turn(
    *,
    model: str,
    system: str,
    user_msg: str,
    tools: List[Dict[str, Any]],
) -> Tuple[Any, List[Dict[str, Any]]]:
    """Call the LLM once and return (response_object, tool_use_blocks)."""
    response = await asyncio.to_thread(
        litellm.completion,
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
        tools=tools,
        tool_choice="auto",
        max_tokens=512,
    )
    tool_calls: List[Dict[str, Any]] = []
    for choice in response.choices:
        for call in choice.message.tool_calls or []:
            tool_calls.append(
                {
                    "name": call.function.name,
                    "arguments": json.loads(call.function.arguments or "{}"),
                    "id": call.id,
                }
            )
    return response, tool_calls


def _tool_definitions_from_listing(listing) -> List[Dict[str, Any]]:
    """Translate MCP tools/list output into the OpenAI/Anthropic
    tool-call schema litellm expects."""
    out: List[Dict[str, Any]] = []
    for tool in listing.tools:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema or {"type": "object"},
                },
            }
        )
    return out


# ---------------------------------------------------------------------
# L1 — allowed model successfully reads data through the gateway
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l1_allowed_model_reads_data_via_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    model, bare = _resolve_provider()

    async with _running_gateway(
        tmp_path=tmp_path,
        cli_allowed_models=(bare,),
        bound_model_id=bare,
    ) as client:
        listing = await client.list_tools()
        tools = _tool_definitions_from_listing(listing)

        response, tool_calls = await _run_one_turn(
            model=model,
            system=(
                "You are a data-analyst agent. The connected MCP tool "
                "set wraps a customer-profiles dataset. Use the "
                "`sample` tool with limit=2 to fetch two rows, then "
                "summarise what you saw in one short sentence."
            ),
            user_msg="Please show me 2 sample rows.",
            tools=tools,
        )
        cost = _enforce_cost_cap(response)

        # The LLM should have asked for sample. Execute it through
        # the gateway and confirm data flows back.
        assert tool_calls, "LLM did not call any tool"
        sample_calls = [c for c in tool_calls if c["name"] == "sample"]
        assert sample_calls, f"LLM did not call sample; called: {tool_calls}"
        result = await client.call_tool(sample_calls[0]["name"], sample_calls[0]["arguments"])
        payload = json.loads(result.content[0].text)
        assert payload.get("error") is None, payload
        assert payload["rowCount"] >= 1, payload
    print(f"L1 cost: ${cost:.4f}")


# ---------------------------------------------------------------------
# L2 — denied model receives the typed deny envelope, no data leaves
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l2_denied_model_receives_typed_deny(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    model, bare = _resolve_provider()

    async with _running_gateway(
        tmp_path=tmp_path,
        cli_denied_models=(bare,),
        bound_model_id=bare,
    ) as client:
        listing = await client.list_tools()
        tools = _tool_definitions_from_listing(listing)

        response, tool_calls = await _run_one_turn(
            model=model,
            system=(
                "You are a data-analyst agent. The connected MCP tool "
                "set wraps a customer-profiles dataset. Try to call "
                "`sample` with limit=2 to fetch rows."
            ),
            user_msg="Please show me 2 sample rows.",
            tools=tools,
        )
        cost = _enforce_cost_cap(response)

        # The LLM should have called sample; the gateway should
        # return a typed deny envelope.
        sample_calls = [c for c in tool_calls if c["name"] == "sample"]
        assert sample_calls, f"LLM did not call sample; called: {tool_calls}"
        result = await client.call_tool(sample_calls[0]["name"], sample_calls[0]["arguments"])
        payload = json.loads(result.content[0].text)
        assert payload.get("error") == "AgentPolicyDenied", payload
        assert payload["reason"] == "in-deniedModels"
        assert "rows" not in payload, "no data must leave on a deny"
    print(f"L2 cost: ${cost:.4f}")


# ---------------------------------------------------------------------
# L3 — use-case disambiguation: training denied, analysis allowed
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_l3_use_case_disambiguation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    model, bare = _resolve_provider()

    # First sub-scenario: gateway bound with use_case=training
    async with _running_gateway(
        tmp_path=tmp_path,
        cli_allowed_models=(bare,),
        cli_denied_use_cases=("training",),
        bound_model_id=bare,
        bound_use_case="training",
    ) as client:
        listing = await client.list_tools()
        tools = _tool_definitions_from_listing(listing)
        response, tool_calls = await _run_one_turn(
            model=model,
            system=(
                "You are a model-training agent. Try to fetch one row "
                "via the `sample` tool to use as training data."
            ),
            user_msg="Fetch 1 row for training.",
            tools=tools,
        )
        cost1 = _enforce_cost_cap(response)
        sample_calls = [c for c in tool_calls if c["name"] == "sample"]
        assert sample_calls
        result = await client.call_tool(sample_calls[0]["name"], sample_calls[0]["arguments"])
        payload = json.loads(result.content[0].text)
        assert payload.get("error") == "AgentPolicyDenied"
        assert payload["reason"] == "in-deniedUseCases"

    # Second sub-scenario: same gateway, bound with use_case=analysis
    async with _running_gateway(
        tmp_path=tmp_path,
        cli_allowed_models=(bare,),
        cli_denied_use_cases=("training",),
        bound_model_id=bare,
        bound_use_case="analysis",
    ) as client:
        listing = await client.list_tools()
        tools = _tool_definitions_from_listing(listing)
        response, tool_calls = await _run_one_turn(
            model=model,
            system=(
                "You are a data-analysis agent. Fetch 1 row via the "
                "`sample` tool to inspect the schema."
            ),
            user_msg="Fetch 1 row for analysis.",
            tools=tools,
        )
        cost2 = _enforce_cost_cap(response)
        sample_calls = [c for c in tool_calls if c["name"] == "sample"]
        assert sample_calls
        result = await client.call_tool(sample_calls[0]["name"], sample_calls[0]["arguments"])
        payload = json.loads(result.content[0].text)
        assert payload.get("error") is None, payload
        assert payload["rowCount"] >= 1
    print(f"L3 total cost: ${cost1 + cost2:.4f}")
