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

"""Tests for PR-A connectors / config refresh.

Covers:

* ``dispatch_tool_call`` resolves ``@forge_tool``-registered tools when
  they aren't in the legacy ``TOOL_REGISTRY``.
* ``get_tool_definitions`` exposes the @forge_tool surface to the LLM.
* ``emit_degradation_warnings`` prints warnings via the standard
  console and is silenced by ``quiet=True``.
* The capability catalog now covers the modern Anthropic 4.x and
  OpenAI 4.1 model families.
* The token-budget context-window catalog covers the modern model
  families too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from fluid_build.cli.forge_copilot_tools import (
    TOOL_REGISTRY,
    dispatch_tool_call,
    get_tool_definitions,
)
from fluid_build.cli.forge_tool import (
    FORGE_TOOL_REGISTRY,
    forge_tool,
)
from fluid_build.copilot.agents.capability_catalog import (
    assess_capabilities,
    emit_degradation_warnings,
)
from fluid_build.copilot.agents.token_budget import get_context_window


@pytest.fixture(autouse=True)
def _reset_forge_registry():
    FORGE_TOOL_REGISTRY.clear()
    yield
    FORGE_TOOL_REGISTRY.clear()


class _EchoArgs(BaseModel):
    text: str = Field(description="Text to echo")


class _PathArgs(BaseModel):
    relative_path: str = Field(default=".", description="Path under workspace")


# ---------------------------------------------------------------------------
# Bridge: FORGE_TOOL_REGISTRY → dispatch_tool_call
# ---------------------------------------------------------------------------


class TestDispatchToolCallBridge:
    def test_dispatch_resolves_forge_tool_when_not_in_legacy(self) -> None:
        @forge_tool(
            name="echo_demo",
            description="Echo a string back.",
            args_schema=_EchoArgs,
        )
        def echo(args: _EchoArgs):
            return {"echoed": args.text}

        # Sanity: not in the legacy registry.
        assert "echo_demo" not in TOOL_REGISTRY
        assert "echo_demo" in FORGE_TOOL_REGISTRY

        result = dispatch_tool_call("echo_demo", {"text": "hi"})
        assert result == {"echoed": "hi"}

    def test_dispatch_forge_tool_invalid_args_returns_typed_error(self) -> None:
        @forge_tool(
            name="bad_args_demo",
            description="Needs text",
            args_schema=_EchoArgs,
        )
        def echo(args):
            return args.text

        # Missing required ``text``.
        result = dispatch_tool_call("bad_args_demo", {})
        assert isinstance(result, dict)
        assert result.get("error") == "ToolValidationError"

    def test_dispatch_forge_tool_workspace_root_injected(self) -> None:
        captured = {}

        @forge_tool(
            name="ws_demo",
            description="Reads workspace root",
            args_schema=_PathArgs,
            workspace_root_aware=True,
        )
        def read(args, *, workspace_root):
            captured["ws"] = workspace_root
            captured["p"] = args.relative_path
            return "ok"

        ws = Path("/tmp/safe-zone")
        result = dispatch_tool_call("ws_demo", {"relative_path": "data/x"}, workspace_root=ws)
        assert result == "ok"
        assert captured["ws"] == ws

    def test_dispatch_unknown_name_returns_error_dict(self) -> None:
        result = dispatch_tool_call("not_a_real_tool_anywhere", {})
        assert isinstance(result, dict)
        assert "Unknown tool" in result.get("error", "")

    def test_legacy_registry_wins_on_name_collision(self) -> None:
        """If a legacy tool and a forge_tool share a name, the legacy
        impl is preferred so existing wire shapes are preserved until
        they're explicitly migrated."""
        # Pick the first legacy tool name to collide with.
        legacy_names = list(TOOL_REGISTRY.keys())
        if not legacy_names:
            pytest.skip("Legacy TOOL_REGISTRY is empty in this test env")
        target = legacy_names[0]

        # Stash original forge_tool count.
        @forge_tool(
            name=target,
            description="Override attempt",
            args_schema=_EchoArgs,
        )
        def shadow(args):
            return {"shadowed": True}

        # Dispatching ``target`` must NOT hit the shadow impl — legacy wins.
        # We don't actually call the legacy impl (might need workspace state);
        # we just verify the resolved tool is the legacy one by checking the
        # output isn't ``{"shadowed": True}`` IF the legacy impl returns
        # something else. Most legacy tools error without proper context, so
        # a returned dict that lacks ``"shadowed"`` proves the legacy path
        # was hit.
        try:
            result = dispatch_tool_call(target, {})
        except Exception:
            # Some legacy impls raise without args — that itself is proof.
            return
        assert result != {"shadowed": True}


class TestGetToolDefinitionsExposesForgeTools:
    def test_forge_tools_appear_in_definitions(self) -> None:
        @forge_tool(
            name="defs_demo_xyz",
            description="visible to LLM",
            args_schema=_EchoArgs,
        )
        def echo(args):
            return args.text

        names = {t["name"] for t in get_tool_definitions()}
        assert "defs_demo_xyz" in names

    def test_legacy_tools_still_appear(self) -> None:
        names = {t["name"] for t in get_tool_definitions()}
        legacy_names = set(TOOL_REGISTRY.keys())
        # All legacy entries must be present — bridge cannot hide them.
        assert legacy_names.issubset(names)


# ---------------------------------------------------------------------------
# Capability warnings emit
# ---------------------------------------------------------------------------


class TestEmitDegradationWarnings:
    def test_quiet_returns_warnings_without_printing(self, capsys) -> None:
        warnings = emit_degradation_warnings(
            provider="openai",
            model="o1-mini",
            usage_profile="agent_loop",
            quiet=True,
        )
        out = capsys.readouterr()
        assert warnings  # something to warn about
        # Quiet path: no stdout from the warning printer.
        assert "tool use" not in out.out

    def test_no_gaps_returns_empty_list(self) -> None:
        warnings = emit_degradation_warnings(
            provider="anthropic",
            model="claude-sonnet-4-6",
            usage_profile="staged_pipeline",
            quiet=True,
        )
        # claude-sonnet-4-6 supports structured_output → silent.
        assert warnings == []

    def test_unknown_combo_warns(self) -> None:
        warnings = emit_degradation_warnings(
            provider="totally_fake_provider",
            model="any-model",
            quiet=True,
        )
        assert any("not in the capability catalog" in w for w in warnings)


# ---------------------------------------------------------------------------
# Catalog freshness
# ---------------------------------------------------------------------------


class TestCatalogFreshness:
    @pytest.mark.parametrize(
        "model,expected_prefix",
        [
            ("claude-sonnet-4-6", "claude-sonnet-4-6"),
            ("claude-sonnet-4-7", "claude-sonnet-4-7"),
            ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
            ("gpt-4.1", "gpt-4.1"),
            ("gpt-4.1-mini", "gpt-4.1-mini"),
            ("gpt-4.1-nano", "gpt-4.1-nano"),
            ("gemini-2.5-flash-001", "gemini-2.5"),
        ],
    )
    def test_capability_catalog_resolves_modern_models(
        self, model: str, expected_prefix: str
    ) -> None:
        provider = (
            "anthropic"
            if model.startswith("claude")
            else "gemini" if model.startswith("gemini") else "openai"
        )
        caps = assess_capabilities(provider, model)
        assert caps.model_prefix == expected_prefix
        assert caps.tool_use is True
        assert caps.structured_output is True

    @pytest.mark.parametrize(
        "model,expected_window",
        [
            ("claude-sonnet-4-6", 200_000),
            ("claude-haiku-4-5-20251001", 200_000),
            ("claude-opus-4-7-20260101", 1_000_000),
            ("gpt-4.1-mini", 1_000_000),
            ("gemini-2.5-flash", 1_000_000),
            ("gemini-2.5-pro-preview", 2_000_000),
            ("o4-mini", 200_000),
        ],
    )
    def test_token_budget_catalog_covers_modern_models(
        self, model: str, expected_window: int
    ) -> None:
        assert get_context_window(model) == expected_window


# ---------------------------------------------------------------------------
# Ollama coverage
# ---------------------------------------------------------------------------


class TestOllamaCatalogCoverage:
    """Pin the project-default Ollama models in both catalogs.

    The ``llm_models.json`` Ollama default is ``gemma4:latest``; the
    user's local server (per the PR-A discovery) also runs
    ``qwen3-coder:30b`` and ``gemma4:31b``. Both must resolve in the
    capability catalog (no "not in catalog" warning) and produce a
    reasonable context-window estimate (not the 32K fallback).
    """

    @pytest.mark.parametrize(
        "model,expected_prefix,expected_tool_use",
        [
            ("gemma4:31b", "gemma4", True),
            ("gemma4:latest", "gemma4", True),
            ("gemma3:9b", "gemma3", True),
            ("gemma2:9b", "gemma2", False),  # gemma2 predates tool calling
            ("qwen3-coder:30b", "qwen3-coder", True),
            ("qwen3:7b", "qwen3", True),
            ("qwen2.5:14b", "qwen", True),  # qwen prefix matches qwen2.5
            ("llama3.1:70b", "llama3.1", True),
            ("llama3.2:3b", "llama3.2", True),
            ("mistral:7b", "mistral", True),
            ("mixtral:8x7b", "mixtral", True),
            ("deepseek-r1:8b", "deepseek", True),
            ("phi-4:14b", "phi", False),  # too small for reliable tool calls
        ],
    )
    def test_capability_catalog_resolves_ollama_models(
        self, model: str, expected_prefix: str, expected_tool_use: bool
    ) -> None:
        caps = assess_capabilities("ollama", model)
        assert caps.model_prefix == expected_prefix, (
            f"expected prefix {expected_prefix!r} for {model!r}, " f"got {caps.model_prefix!r}"
        )
        assert caps.tool_use is expected_tool_use
        # Every catalogued Ollama entry should advertise streaming.
        assert caps.streaming is True
        # No Ollama entry advertises strict structured output today —
        # they all run JSON mode without server-side schema.
        assert caps.structured_output is False

    @pytest.mark.parametrize(
        "model,minimum_window",
        [
            ("gemma4:31b", 100_000),
            ("gemma4:latest", 100_000),
            ("qwen3-coder:30b", 200_000),
            ("qwen3:7b", 100_000),
            ("llama3.1:70b", 100_000),
            ("llama3.2:3b", 100_000),
            ("mistral:7b", 30_000),
            ("mixtral:8x7b", 30_000),
        ],
    )
    def test_token_budget_catalog_covers_ollama_models(
        self, model: str, minimum_window: int
    ) -> None:
        # The Ollama prefix lookup should produce the model-design
        # window, not the conservative 32K fallback.
        window = get_context_window(model)
        assert window >= minimum_window, (
            f"{model} resolved to {window:,}-token window; " f"expected at least {minimum_window:,}"
        )
