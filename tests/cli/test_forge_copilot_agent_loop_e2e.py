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

"""End-to-end tests for the forge copilot multi-turn agent loop.

``run_copilot_agent_loop`` (``cli/forge_copilot_agent_loop.py``) drives an
LLM through repeated tool-use rounds until it emits a final contract.
Until now its orchestration — tool-call extraction, parallel dispatch,
the JSON-repair round-trip, and final-payload emission — had no
deterministic test; agentic behaviour was only ever exercised against a
live provider.

These tests close that gap with **zero API keys and zero network**. The
loop's single LLM I/O boundary, ``_call_llm_with_tools``, is replaced
with a scripted stand-in that returns canned provider responses — the
established "drop-in fake LLM client" testing pattern (the project's
sibling pattern at the ``litellm.completion`` seam lives in
``tests/test_litellm_backend.py::_fake_litellm_module``). Everything
below that boundary runs for real: ``LiteLLMProvider.extract_tool_calls``
/ ``extract_text_from_tool_response``, the live tool registry via
``dispatch_tool_call``, ``build_tool_result_messages``, and corrective
feedback.

Canned responses use litellm's normalised OpenAI shape
(``choices[0].message.{tool_calls,content}``) — exactly what
``LiteLLMProvider`` already parses.

NOTE: ``_call_llm_with_tools`` httpx-POSTs the ``litellm://internal``
sentinel URL that ``LiteLLMProvider.build_tool_request`` returns. That
transport path is provider-specific; it is stubbed here so these tests
stay deterministic and exercise the loop's orchestration only.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

import pytest

from fluid_build.cli import forge_copilot_agent_loop as agent_loop
from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError, LlmConfig

# ---------------------------------------------------------------------------
# Scripted fake — stands in for the agent loop's LLM I/O boundary
# ---------------------------------------------------------------------------


def _tool_turn(*calls: Tuple[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Build a provider response that requests one or more tool calls.

    ``calls`` are ``(tool_name, arguments)`` pairs. The shape mirrors
    litellm's normalised OpenAI tool-call envelope.
    """
    return {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args),
                            },
                        }
                        for i, (name, args) in enumerate(calls)
                    ]
                }
            }
        ]
    }


def _final_turn(content: str) -> Dict[str, Any]:
    """Build a provider response with no tool calls — the model's answer."""
    return {"choices": [{"message": {"content": content}}]}


class _ScriptedLLM:
    """Scripted stand-in for ``forge_copilot_agent_loop._call_llm_with_tools``.

    Returns the next canned response on each call and snapshots the
    message history so a test can assert on what the loop fed back
    between rounds. Raises rather than silently looping if the loop
    makes more calls than were scripted.
    """

    def __init__(self, responses: List[Dict[str, Any]]):
        self._responses = list(responses)
        self.message_snapshots: List[List[Dict[str, Any]]] = []

    def __call__(self, provider, config, system_prompt, messages, tools):
        idx = len(self.message_snapshots)
        self.message_snapshots.append([dict(m) for m in messages])
        if idx >= len(self._responses):
            raise AssertionError(
                f"agent loop made {idx + 1} LLM calls but only "
                f"{len(self._responses)} were scripted"
            )
        return self._responses[idx]

    @property
    def call_count(self) -> int:
        return len(self.message_snapshots)


_FINAL_CONTRACT = json.dumps(
    {
        "recommended_template": "analytics",
        "recommended_provider": "local",
        "description": "agent-loop e2e fixture product",
        "domain": "analytics",
        "owner": "data-team",
        "contract": {"id": "agent_loop_e2e", "name": "Agent Loop E2E"},
        "additional_files": {},
    }
)


def _llm_config() -> LlmConfig:
    """Config with a dummy key — the LLM boundary is stubbed, so no real
    credential is ever read or transmitted."""
    return LlmConfig(
        provider="openai",
        model="gpt-4o-mini",
        endpoint="litellm://internal",
        api_key="sk-not-a-real-key",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_agent_loop_dispatches_tools_then_returns_contract(monkeypatch, tmp_path):
    """Happy path: one round of parallel tool calls, then a final contract.

    Exercises tool-call extraction, parallel dispatch through the real
    tool registry, tool-result feedback, and final JSON emission.
    """
    scripted = _ScriptedLLM(
        [
            # Round 1: two read-only tools — dispatched in parallel.
            _tool_turn(("list_templates", {}), ("discover_workspace", {})),
            # Round 2: no tool calls → the model's final answer.
            _final_turn(_FINAL_CONTRACT),
        ]
    )
    monkeypatch.setattr(agent_loop, "_call_llm_with_tools", scripted)

    perf: Dict[str, Any] = {}
    result = agent_loop.run_copilot_agent_loop(
        context={"project_goal": "test product", "domain": "analytics"},
        llm_config=_llm_config(),
        workspace_root=tmp_path,
        perf_stats=perf,
    )

    assert scripted.call_count == 2
    assert result["recommended_template"] == "analytics"
    assert result["contract"]["id"] == "agent_loop_e2e"
    # Round 2's message history must carry the tool results fed back
    # after the round-1 dispatch.
    assert any(m.get("role") == "tool" for m in scripted.message_snapshots[1])
    assert perf["agent_loop_rounds"] == 2
    assert perf["agent_loop_tool_calls"] == 2


@pytest.mark.unit
def test_agent_loop_recovers_from_invalid_final_json(monkeypatch, tmp_path):
    """A non-JSON final response triggers the repair round-trip.

    The loop must ask the model to retry rather than crash, and accept
    the corrected JSON on the next round.
    """
    scripted = _ScriptedLLM(
        [
            _tool_turn(("list_templates", {})),
            _final_turn("Sorry — here is the contract, but not as JSON."),
            _final_turn(_FINAL_CONTRACT),
        ]
    )
    monkeypatch.setattr(agent_loop, "_call_llm_with_tools", scripted)

    result = agent_loop.run_copilot_agent_loop(
        context={"project_goal": "test product"},
        llm_config=_llm_config(),
        workspace_root=tmp_path,
    )

    assert scripted.call_count == 3
    assert result["contract"]["id"] == "agent_loop_e2e"


@pytest.mark.unit
def test_agent_loop_raises_when_iterations_exhausted(monkeypatch, tmp_path):
    """A model that never stops calling tools fails with a typed error."""
    scripted = _ScriptedLLM([_tool_turn(("list_templates", {}))] * 3)
    monkeypatch.setattr(agent_loop, "_call_llm_with_tools", scripted)

    with pytest.raises(CopilotGenerationError) as excinfo:
        agent_loop.run_copilot_agent_loop(
            context={"project_goal": "test product"},
            llm_config=_llm_config(),
            workspace_root=tmp_path,
            max_iterations=3,
        )

    assert excinfo.value.event == "copilot_agent_loop_exhausted"
    assert scripted.call_count == 3
