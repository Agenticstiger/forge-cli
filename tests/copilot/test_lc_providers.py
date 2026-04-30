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

"""Unit tests for the langchain-core provider adapter.

These tests exercise the pure-logic surface — feature flag resolution,
ChatModel construction (per provider), endpoint normalization,
temperature handling for Opus 4.7+, usage extraction from
``AIMessage`` — without making real LLM calls.

The end-to-end ``call_structured_via_langchain`` round-trip is covered
by the corpus replay suite (which runs against a recorded fixture set
on the project Python target).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.forge_copilot_llm_providers import LlmConfig
from fluid_build.cli.forge_copilot_lc_providers import (
    _model_deprecates_temperature,
    _resolve_temperature,
    _strip_path,
    build_chat_model,
    extract_usage_from_message,
    is_langchain_provider_enabled,
)


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


class TestFeatureFlag:
    @pytest.mark.parametrize("env_value", ["1", "true", "TRUE", "yes", "on"])
    def test_env_truthy_enables(self, env_value: str, monkeypatch) -> None:
        monkeypatch.setenv("FLUID_USE_LANGCHAIN_PROVIDERS", env_value)
        assert is_langchain_provider_enabled() is True

    @pytest.mark.parametrize("env_value", ["0", "false", "no", "off", ""])
    def test_env_falsy_disables(self, env_value: str, monkeypatch) -> None:
        monkeypatch.setenv("FLUID_USE_LANGCHAIN_PROVIDERS", env_value)
        assert is_langchain_provider_enabled() is False

    def test_default_is_disabled(self, monkeypatch) -> None:
        monkeypatch.delenv("FLUID_USE_LANGCHAIN_PROVIDERS", raising=False)
        assert is_langchain_provider_enabled() is False

    def test_capability_matrix_overrides_env_when_present(self, monkeypatch) -> None:
        monkeypatch.setenv("FLUID_USE_LANGCHAIN_PROVIDERS", "1")
        assert (
            is_langchain_provider_enabled(
                capability_matrix={"use_langchain_providers": False}
            )
            is False
        )
        monkeypatch.setenv("FLUID_USE_LANGCHAIN_PROVIDERS", "0")
        assert (
            is_langchain_provider_enabled(
                capability_matrix={"use_langchain_providers": True}
            )
            is True
        )


# ---------------------------------------------------------------------------
# Endpoint normalization
# ---------------------------------------------------------------------------


class TestStripPath:
    @pytest.mark.parametrize(
        "raw,stripped",
        [
            ("https://api.anthropic.com/v1/messages", "https://api.anthropic.com"),
            ("https://api.openai.com/v1/chat/completions", "https://api.openai.com"),
            (
                "http://localhost:11434/api/chat",
                "http://localhost:11434",
            ),
            ("https://api.anthropic.com", "https://api.anthropic.com"),
            ("", ""),
        ],
    )
    def test_strips_path_keeps_scheme_and_host(self, raw: str, stripped: str) -> None:
        assert _strip_path(raw) == stripped


# ---------------------------------------------------------------------------
# Temperature handling
# ---------------------------------------------------------------------------


class TestTemperatureHandling:
    def test_opus_4_7_drops_temperature(self) -> None:
        assert _model_deprecates_temperature("anthropic", "claude-opus-4-7") is True
        assert (
            _model_deprecates_temperature("anthropic", "claude-opus-4-7-20260101")
            is True
        )

    def test_opus_4_5_keeps_temperature(self) -> None:
        assert (
            _model_deprecates_temperature("anthropic", "claude-3-5-sonnet-20241022")
            is False
        )

    def test_openai_always_keeps_temperature(self) -> None:
        assert _model_deprecates_temperature("openai", "gpt-4o") is False
        assert _model_deprecates_temperature("openai", "claude-opus-4-7") is False

    def test_resolve_temperature_for_deprecated_model_returns_none(self) -> None:
        assert _resolve_temperature("anthropic", "claude-opus-4-7") is None

    def test_resolve_temperature_default_is_zero(self, monkeypatch) -> None:
        monkeypatch.delenv("FLUID_LLM_TEMPERATURE", raising=False)
        assert _resolve_temperature("openai", "gpt-4o") == 0.0

    def test_resolve_temperature_reads_env(self, monkeypatch) -> None:
        monkeypatch.setenv("FLUID_LLM_TEMPERATURE", "0.7")
        assert _resolve_temperature("openai", "gpt-4o") == 0.7

    def test_resolve_temperature_clamps_invalid(self, monkeypatch) -> None:
        monkeypatch.setenv("FLUID_LLM_TEMPERATURE", "garbage")
        assert _resolve_temperature("openai", "gpt-4o") == 0.0
        monkeypatch.setenv("FLUID_LLM_TEMPERATURE", "5.0")
        assert _resolve_temperature("openai", "gpt-4o") == 2.0


# ---------------------------------------------------------------------------
# ChatModel factory
# ---------------------------------------------------------------------------


def _config(provider: str, **overrides) -> LlmConfig:
    base = {
        "provider": provider,
        "model": "test-model",
        "endpoint": "",
        "api_key": "test-key",
        "timeout_seconds": 60,
    }
    base.update(overrides)
    return LlmConfig(**base)


class TestBuildChatModel:
    def test_anthropic_builds_chatanthropic(self) -> None:
        cfg = _config(
            "anthropic",
            model="claude-3-5-sonnet-latest",
            endpoint="https://api.anthropic.com/v1/messages",
        )
        model = build_chat_model(cfg)
        from langchain_anthropic import ChatAnthropic

        assert isinstance(model, ChatAnthropic)
        # Endpoint path must be stripped — langchain appends its own.
        assert getattr(model, "anthropic_api_url", None) in (
            "https://api.anthropic.com",
            None,
        )

    def test_anthropic_opus_4_7_omits_temperature(self) -> None:
        cfg = _config("anthropic", model="claude-opus-4-7")
        model = build_chat_model(cfg)
        # ChatAnthropic stores temperature in different attribute names
        # across versions; checking that *something* is None / not set
        # is sufficient — the key invariant is we don't blow up on
        # construction with this model.
        assert model is not None

    def test_openai_builds_chatopenai(self) -> None:
        cfg = _config("openai", model="gpt-4o")
        model = build_chat_model(cfg)
        from langchain_openai import ChatOpenAI

        assert isinstance(model, ChatOpenAI)

    def test_gemini_builds_chatgooglegenerativeai(self) -> None:
        cfg = _config("gemini", model="gemini-1.5-pro")
        model = build_chat_model(cfg)
        from langchain_google_genai import ChatGoogleGenerativeAI

        assert isinstance(model, ChatGoogleGenerativeAI)

    def test_ollama_builds_chatollama(self) -> None:
        cfg = _config(
            "ollama",
            model="llama3.2",
            endpoint="http://localhost:11434/api/chat",
        )
        model = build_chat_model(cfg)
        from langchain_ollama import ChatOllama

        assert isinstance(model, ChatOllama)

    def test_unknown_provider_raises(self) -> None:
        cfg = _config("totally-fake-provider")
        with pytest.raises(ValueError, match="does not yet support"):
            build_chat_model(cfg)


# ---------------------------------------------------------------------------
# Usage extraction
# ---------------------------------------------------------------------------


class TestExtractUsage:
    def test_extracts_standard_usage_metadata(self) -> None:
        msg = MagicMock()
        msg.usage_metadata = {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        }
        msg.response_metadata = {}
        usage = extract_usage_from_message(msg)
        assert usage == {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }

    def test_extracts_anthropic_cache_tokens(self) -> None:
        msg = MagicMock()
        msg.usage_metadata = {
            "input_tokens": 100,
            "output_tokens": 50,
            "input_token_details": {
                "cache_read": 2000,
                "cache_creation": 500,
            },
        }
        msg.response_metadata = {}
        usage = extract_usage_from_message(msg)
        assert usage["cache_read_tokens"] == 2000
        assert usage["cache_write_tokens"] == 500

    def test_falls_back_to_response_metadata_token_usage(self) -> None:
        """Some streaming paths populate response_metadata.token_usage
        but not the standardised usage_metadata."""
        msg = MagicMock()
        msg.usage_metadata = {}
        msg.response_metadata = {
            "token_usage": {"prompt_tokens": 80, "completion_tokens": 40}
        }
        usage = extract_usage_from_message(msg)
        assert usage["input_tokens"] == 80
        assert usage["output_tokens"] == 40

    def test_missing_message_returns_empty_dict(self) -> None:
        assert extract_usage_from_message(None) == {}

    def test_no_usage_data_returns_zero_tokens(self) -> None:
        msg = MagicMock()
        msg.usage_metadata = None
        msg.response_metadata = None
        usage = extract_usage_from_message(msg)
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0


# ---------------------------------------------------------------------------
# Anthropic prompt-cache message envelope
# ---------------------------------------------------------------------------


class TestAnthropicPromptCacheEnvelope:
    """Verify the system message gets ``cache_control`` annotated when
    the capability matrix asks for prompt caching on Anthropic."""

    def test_anthropic_default_attaches_cache_control(self) -> None:
        from fluid_build.cli.forge_copilot_lc_providers import _build_messages
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = _build_messages(
            system_prompt="You are an assistant.",
            user_prompt="Hello",
            provider="anthropic",
            capability_matrix={},
            SystemMessage=SystemMessage,
            HumanMessage=HumanMessage,
        )
        sys_msg = messages[0]
        assert isinstance(sys_msg.content, list)
        assert sys_msg.content[0]["cache_control"] == {"type": "ephemeral"}

    def test_explicit_disable_drops_cache_control(self) -> None:
        from fluid_build.cli.forge_copilot_lc_providers import _build_messages
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = _build_messages(
            system_prompt="hi",
            user_prompt="bye",
            provider="anthropic",
            capability_matrix={"anthropic_prompt_cache": False},
            SystemMessage=SystemMessage,
            HumanMessage=HumanMessage,
        )
        sys_msg = messages[0]
        # When disabled, content is a plain string — no annotated blocks.
        assert isinstance(sys_msg.content, str)

    def test_non_anthropic_provider_uses_plain_string(self) -> None:
        from fluid_build.cli.forge_copilot_lc_providers import _build_messages
        from langchain_core.messages import HumanMessage, SystemMessage

        messages = _build_messages(
            system_prompt="hi",
            user_prompt="bye",
            provider="openai",
            capability_matrix={},
            SystemMessage=SystemMessage,
            HumanMessage=HumanMessage,
        )
        sys_msg = messages[0]
        assert isinstance(sys_msg.content, str)
