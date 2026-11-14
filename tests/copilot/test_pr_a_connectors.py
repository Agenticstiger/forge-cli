# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for PR-A connectors / config refresh.

Covers:

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

from fluid_build.copilot.agents.capability_catalog import (
    assess_capabilities,
    emit_degradation_warnings,
)
from fluid_build.copilot.agents.token_budget import get_context_window

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
        "model,minimum_window",
        [
            # Modern cloud models. We assert at-least minimums because
            # ``get_context_window`` delegates to litellm's
            # actively-maintained ``model_cost`` catalog (rung 2),
            # which sometimes carries the provider's exact figure
            # (e.g. 1_048_576 for Gemini 2.5) vs the round-number
            # marketing figure (1_000_000). Either is correct; an
            # equality pin would be brittle across litellm releases.
            ("claude-sonnet-4-6", 200_000),
            ("claude-haiku-4-5-20251001", 200_000),
            ("claude-opus-4-7-20260101", 1_000_000),
            ("gpt-4.1-mini", 1_000_000),
            ("gemini-2.5-flash", 1_000_000),
            ("gemini-2.5-pro-preview", 1_000_000),
            ("o4-mini", 200_000),
        ],
    )
    def test_token_budget_catalog_covers_modern_models(
        self, model: str, minimum_window: int
    ) -> None:
        window = get_context_window(model)
        assert window >= minimum_window, (
            f"{model} resolved to {window:,}-token window; " f"expected at least {minimum_window:,}"
        )


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
        assert (
            caps.model_prefix == expected_prefix
        ), f"expected prefix {expected_prefix!r} for {model!r}, got {caps.model_prefix!r}"
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
        assert (
            window >= minimum_window
        ), f"{model} resolved to {window:,}-token window; expected at least {minimum_window:,}"
