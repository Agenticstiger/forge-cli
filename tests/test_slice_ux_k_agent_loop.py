# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Slice UX-K: regression tests for agent-loop tool use."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.forge_copilot_llm_providers import (
    AnthropicProvider,
    GeminiProvider,
    LlmConfig,
    OpenAIProvider,
    ToolCall,
)
from fluid_build.cli.forge_copilot_tools import (
    TOOL_REGISTRY,
    dispatch_tool_call,
    get_tool_definitions,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _base_config(provider: str = "openai", model: str = "gpt-4o-mini") -> LlmConfig:
    endpoints = {
        "openai": "https://api.openai.com/v1/chat/completions",
        "anthropic": "https://api.anthropic.com/v1/messages",
        "gemini": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
    }
    return LlmConfig(
        provider=provider,
        model=model,
        endpoint=endpoints.get(provider, "http://localhost"),
        api_key="test-key" if provider != "ollama" else None,
    )


def _minimal_contract() -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.2",
        "kind": "DataProduct",
        "id": "test.agent",
        "name": "Agent Test",
        "description": "Test",
        "domain": "analytics",
        "metadata": {"layer": "Bronze", "owner": {"team": "t"}},
        "builds": [
            {
                "id": "b",
                "pattern": "embedded-logic",
                "engine": "sql",
                "properties": {"sql": "SELECT 1"},
                "execution": {
                    "trigger": {"type": "manual", "iterations": 1},
                    "runtime": {"platform": "local", "resources": {"cpu": "1", "memory": "1Gi"}},
                },
            }
        ],
        "exposes": [],
    }


def _final_response() -> Dict[str, Any]:
    return {
        "recommended_template": "starter",
        "recommended_provider": "local",
        "recommended_patterns": [],
        "architecture_suggestions": [],
        "best_practices": [],
        "technology_stack": ["sql"],
        "description": "Test product",
        "domain": "analytics",
        "owner": "data-team",
        "readme_markdown": "# Test",
        "contract": _minimal_contract(),
        "additional_files": {},
    }


# ---------------------------------------------------------------------------
# TestToolRegistry
# ---------------------------------------------------------------------------


class TestToolRegistry:
    def test_tools_registered(self):
        assert len(TOOL_REGISTRY) >= 5
        expected = {
            "discover_workspace",
            "read_sample_schema",
            "list_templates",
            "propose_contract",
            "validate_contract",
            "list_schedulers",
        }
        assert set(TOOL_REGISTRY.keys()) == expected

    def test_tool_definitions_shape(self):
        defs = get_tool_definitions()
        for d in defs:
            assert "name" in d
            assert "description" in d
            assert "input_schema" in d
            assert d["input_schema"]["type"] == "object"

    def test_list_templates_returns_providers(self):
        result = dispatch_tool_call("list_templates", {})
        assert "providers" in result
        assert isinstance(result["providers"], list)

    def test_validate_contract_returns_errors_warnings(self):
        result = dispatch_tool_call(
            "validate_contract",
            {"contract": _minimal_contract()},
        )
        assert "errors" in result
        assert "warnings" in result
        assert isinstance(result["errors"], list)

    def test_unknown_tool_returns_error(self):
        result = dispatch_tool_call("nonexistent_tool", {})
        assert "error" in result

    def test_tool_failure_returns_error_not_raises(self):
        """Tools that crash internally must return an error dict,
        never raise, so the agent loop can continue."""
        with patch.dict(
            TOOL_REGISTRY,
            {
                "crash_test": {
                    "name": "crash_test",
                    "description": "test",
                    "input_schema": {},
                    "impl": lambda **_kw: (_ for _ in ()).throw(RuntimeError("boom")),
                }
            },
        ):
            result = dispatch_tool_call("crash_test", {})
        assert "error" in result
        assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# TestProviderToolUse
# ---------------------------------------------------------------------------


class TestProviderToolUse:
    """Each provider's build_tool_request / extract_tool_calls /
    build_tool_result_messages must round-trip correctly."""

    def test_openai_build_tool_request_shape(self):
        cfg = _base_config("openai")
        tools = get_tool_definitions()
        url, headers, payload = OpenAIProvider().build_tool_request(
            cfg, "system", [{"role": "user", "content": "hi"}], tools
        )
        assert url == cfg.endpoint
        assert "tools" in payload
        assert payload["tools"][0]["type"] == "function"
        assert payload["tools"][0]["function"]["name"] == tools[0]["name"]

    def test_openai_extract_tool_calls(self):
        response = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "list_templates",
                                    "arguments": "{}",
                                },
                            }
                        ]
                    }
                }
            ]
        }
        calls = OpenAIProvider().extract_tool_calls(response)
        assert len(calls) == 1
        assert calls[0]["name"] == "list_templates"
        assert calls[0]["id"] == "call_1"

    def test_openai_extract_no_tool_calls(self):
        response = {
            "choices": [{"message": {"content": "final answer"}}]
        }
        calls = OpenAIProvider().extract_tool_calls(response)
        assert calls == []

    def test_openai_build_tool_result_messages(self):
        tool_calls = [{"id": "c1", "name": "list_templates", "arguments": {}}]
        results = [{"providers": ["local"]}]
        msgs = OpenAIProvider().build_tool_result_messages(tool_calls, results)
        assert len(msgs) == 2  # assistant + tool
        assert msgs[0]["role"] == "assistant"
        assert msgs[1]["role"] == "tool"
        assert msgs[1]["tool_call_id"] == "c1"

    def test_anthropic_build_tool_request_shape(self):
        cfg = _base_config("anthropic", "claude-3-5-sonnet-latest")
        tools = get_tool_definitions()
        url, headers, payload = AnthropicProvider().build_tool_request(
            cfg, "system", [{"role": "user", "content": "hi"}], tools
        )
        assert "tools" in payload
        assert payload["tools"][0]["name"] == tools[0]["name"]
        assert "input_schema" in payload["tools"][0]

    def test_anthropic_extract_tool_calls(self):
        response = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "discover_workspace",
                    "input": {"workspace_path": "."},
                }
            ]
        }
        calls = AnthropicProvider().extract_tool_calls(response)
        assert len(calls) == 1
        assert calls[0]["name"] == "discover_workspace"

    def test_anthropic_build_tool_result_messages(self):
        tool_calls = [{"id": "tu_1", "name": "discover_workspace", "arguments": {}}]
        results = [{"files_scanned": 42}]
        msgs = AnthropicProvider().build_tool_result_messages(tool_calls, results)
        assert len(msgs) == 2  # assistant + user
        assert msgs[0]["role"] == "assistant"
        assert msgs[0]["content"][0]["type"] == "tool_use"
        assert msgs[1]["role"] == "user"
        assert msgs[1]["content"][0]["type"] == "tool_result"

    def test_gemini_build_tool_request_has_function_declarations(self):
        cfg = _base_config("gemini", "gemini-2.5-flash")
        tools = get_tool_definitions()
        url, headers, payload = GeminiProvider().build_tool_request(
            cfg, "system", [{"role": "user", "content": "hi"}], tools
        )
        assert "tools" in payload
        assert "functionDeclarations" in payload["tools"][0]

    def test_gemini_extract_tool_calls(self):
        response = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "list_templates",
                                    "args": {"use_case": "analytics"},
                                }
                            }
                        ]
                    }
                }
            ]
        }
        calls = GeminiProvider().extract_tool_calls(response)
        assert len(calls) == 1
        assert calls[0]["name"] == "list_templates"
        assert calls[0]["arguments"] == {"use_case": "analytics"}


# ---------------------------------------------------------------------------
# TestAgentLoopRunner
# ---------------------------------------------------------------------------


class TestAgentLoopRunner:
    """Test the multi-turn loop with mocked LLM responses."""

    def test_loop_terminates_with_final_text(self):
        """Simplest case: model returns final text immediately."""
        from fluid_build.cli.forge_copilot_agent_loop import run_copilot_agent_loop

        final_json = json.dumps(_final_response())
        fake_response = {
            "choices": [{"message": {"content": final_json}}]
        }

        with patch(
            "fluid_build.cli.forge_copilot_agent_loop._call_llm_with_tools",
            return_value=fake_response,
        ):
            result = run_copilot_agent_loop(
                context={"project_goal": "test", "use_case": "analytics"},
                llm_config=_base_config(),
            )
        assert result["contract"]["id"] == "test.agent"

    def test_loop_dispatches_tool_then_terminates(self):
        """Model calls a tool once, then returns the final response."""
        from fluid_build.cli.forge_copilot_agent_loop import run_copilot_agent_loop

        # Round 1: model requests list_templates
        round1_response = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {
                                    "name": "list_templates",
                                    "arguments": "{}",
                                },
                            }
                        ]
                    }
                }
            ]
        }
        # Round 2: model returns final text
        round2_response = {
            "choices": [{"message": {"content": json.dumps(_final_response())}}]
        }

        call_count = {"n": 0}

        def _fake_call(*_a, **_kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return round1_response
            return round2_response

        with patch(
            "fluid_build.cli.forge_copilot_agent_loop._call_llm_with_tools",
            side_effect=_fake_call,
        ):
            result = run_copilot_agent_loop(
                context={"project_goal": "test"},
                llm_config=_base_config(),
            )

        assert call_count["n"] == 2
        assert "contract" in result

    def test_loop_exhausts_iterations_raises(self):
        """If the model never stops calling tools, the loop must
        raise after max_iterations."""
        from fluid_build.cli.forge_copilot_agent_loop import run_copilot_agent_loop
        from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError

        # Always return a tool call, never a final response.
        infinite_tools_response = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {
                                    "name": "list_templates",
                                    "arguments": "{}",
                                },
                            }
                        ]
                    }
                }
            ]
        }

        with patch(
            "fluid_build.cli.forge_copilot_agent_loop._call_llm_with_tools",
            return_value=infinite_tools_response,
        ):
            with pytest.raises(CopilotGenerationError, match="exhausted"):
                run_copilot_agent_loop(
                    context={"project_goal": "test"},
                    llm_config=_base_config(),
                    max_iterations=3,
                )

    def test_parallel_dispatch_for_read_only_tools(self):
        """When all tool calls are read-only, they should run in
        parallel via ThreadPoolExecutor."""
        from fluid_build.cli.forge_copilot_agent_loop import _dispatch_tools

        tool_calls = [
            {"id": "c1", "name": "list_templates", "arguments": {}},
            {"id": "c2", "name": "discover_workspace", "arguments": {}},
        ]
        results = _dispatch_tools(tool_calls)
        assert len(results) == 2
        # list_templates should return providers
        assert "providers" in results[0]

    def test_sequential_dispatch_for_mixed_tools(self):
        """When tool calls include non-parallelizable tools (like
        validate_contract), dispatch must be sequential."""
        from fluid_build.cli.forge_copilot_agent_loop import _dispatch_tools

        tool_calls = [
            {"id": "c1", "name": "list_templates", "arguments": {}},
            {"id": "c2", "name": "validate_contract", "arguments": {"contract": _minimal_contract()}},
        ]
        results = _dispatch_tools(tool_calls)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# TestAgentLoopGating
# ---------------------------------------------------------------------------


class TestAgentLoopGating:
    def test_agent_loop_flag_registered_on_forge_parser(self):
        import argparse

        from fluid_build.cli.forge import register

        top = argparse.ArgumentParser()
        sub = top.add_subparsers(dest="command")
        register(sub)
        ns = top.parse_args(["forge"])
        assert getattr(ns, "agent_loop", None) is False

        ns = top.parse_args(["forge", "--agent-loop"])
        assert ns.agent_loop is True

    def test_unsupported_provider_errors_cleanly(self):
        """build_tool_request on a provider that doesn't support
        tool use must raise NotImplementedError."""
        from fluid_build.cli.forge_copilot_llm_providers import LlmProvider

        # The base class raises NotImplementedError
        class FakeProvider(LlmProvider):
            name = "fake"
            default_model = "fake-model"

            def default_endpoint(self, model, env):
                return "http://fake"

            def build_request(self, config, system_prompt, user_prompt):
                return {}, {}

            def extract_text(self, response_json):
                return ""

        with pytest.raises(NotImplementedError, match="fake"):
            FakeProvider().build_tool_request(
                _base_config(), "sys", [{"role": "user", "content": "hi"}], []
            )
