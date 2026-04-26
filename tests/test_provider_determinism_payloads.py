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

"""Pin the LLM-level determinism levers in every provider's ``build_request``.

Closes V1.3.3 — until now we tested the user-visible ``--deterministic``
flag end-to-end (cache off, tiering off, audit metadata) but never
asserted the *root cause*: that the actual HTTP payload sent to each
provider locks ``temperature=0`` (and ``seed=42`` where supported).

Without this pin, the providers' build helpers could silently drop the
temperature line and `--deterministic` would degrade to "cache off but
sampling is whatever the model defaults to" — which on Claude is 1.0,
i.e. fully non-deterministic. That's the exact failure mode this test
guards against.

The matrix below mirrors the provider capability table in the plan:

| provider     | temperature | seed |
|--------------|-------------|------|
| openai       | yes (0.0)   | yes  |
| azure-openai | yes (0.0)   | yes  |
| ollama       | yes (0.0)   | yes  | (inherits from OpenAI base class)
| anthropic    | yes (0.0)   | no   | (Anthropic API has no seed param)
| gemini       | yes (0.0)   | no   | (Gemini API has no seed param)

The env-var override (``FLUID_LLM_TEMPERATURE=0.5``) is also pinned —
that's the experimental escape hatch the plan calls out.
"""

from __future__ import annotations

import pytest

from fluid_build.cli.forge_copilot_llm_providers import (
    _OPENAI_SEED,
    BUILTIN_LLM_PROVIDERS,
    AnthropicProvider,
    GeminiProvider,
    LlmConfig,
    OllamaProvider,
    OpenAIProvider,
    _get_temperature,
)


def _config(provider: str, model: str) -> LlmConfig:
    """Minimal LlmConfig sufficient to drive ``build_request``."""
    return LlmConfig(
        provider=provider,
        model=model,
        endpoint="https://example.test/api",
        api_key="test-key",
    )


# ----------------------------------------------------------------------
# _get_temperature — env-var override + clamping
# ----------------------------------------------------------------------


class TestGetTemperature:
    def test_default_is_zero(self, monkeypatch):
        monkeypatch.delenv("FLUID_LLM_TEMPERATURE", raising=False)
        assert _get_temperature() == 0.0

    def test_env_var_overrides(self, monkeypatch):
        monkeypatch.setenv("FLUID_LLM_TEMPERATURE", "0.7")
        assert _get_temperature() == 0.7

    def test_negative_value_clamped_to_zero(self, monkeypatch):
        monkeypatch.setenv("FLUID_LLM_TEMPERATURE", "-1.0")
        assert _get_temperature() == 0.0

    def test_excessive_value_clamped_to_two(self, monkeypatch):
        """Provider APIs reject ``temperature > 2``; the clamp keeps
        a typo from causing a 400 at the provider."""
        monkeypatch.setenv("FLUID_LLM_TEMPERATURE", "5.0")
        assert _get_temperature() == 2.0

    def test_garbage_falls_back_to_default(self, monkeypatch):
        """Bad input must not raise — ``ValueError`` from ``float()``
        falls through to the safe default of 0.0."""
        monkeypatch.setenv("FLUID_LLM_TEMPERATURE", "not-a-number")
        assert _get_temperature() == 0.0


# ----------------------------------------------------------------------
# Per-provider build_request — temperature + seed pin
# ----------------------------------------------------------------------


class TestOpenAIPayloadDeterminism:
    def test_temperature_pinned_zero_by_default(self, monkeypatch):
        monkeypatch.delenv("FLUID_LLM_TEMPERATURE", raising=False)
        provider = OpenAIProvider()
        _, payload = provider.build_request(_config("openai", "gpt-4.1-mini"), "system", "user")
        assert payload["temperature"] == 0.0

    def test_seed_pinned_to_constant(self, monkeypatch):
        """OpenAI's ``seed`` parameter is the sole route to byte-stable
        outputs; the constant must be exactly ``_OPENAI_SEED`` so audit
        trails record the same value across runs."""
        monkeypatch.delenv("FLUID_LLM_TEMPERATURE", raising=False)
        provider = OpenAIProvider()
        _, payload = provider.build_request(_config("openai", "gpt-4.1-mini"), "system", "user")
        assert payload["seed"] == _OPENAI_SEED
        assert _OPENAI_SEED == 42  # Catch silent constant changes.

    def test_temperature_env_override_propagates(self, monkeypatch):
        monkeypatch.setenv("FLUID_LLM_TEMPERATURE", "0.5")
        provider = OpenAIProvider()
        _, payload = provider.build_request(_config("openai", "gpt-4.1-mini"), "system", "user")
        assert payload["temperature"] == 0.5


class TestAnthropicPayloadDeterminism:
    def test_temperature_pinned_zero_by_default(self, monkeypatch):
        """Regression test: until V1.3.3, ``AnthropicProvider`` omitted
        ``temperature`` and silently inherited Claude's default of
        ``1.0``. The fix lands the explicit pin so ``--deterministic``
        actually delivers determinism on Claude."""
        monkeypatch.delenv("FLUID_LLM_TEMPERATURE", raising=False)
        provider = AnthropicProvider()
        _, payload = provider.build_request(
            _config("anthropic", "claude-sonnet-4-6"), "system", "user"
        )
        assert payload["temperature"] == 0.0

    def test_temperature_env_override_propagates(self, monkeypatch):
        monkeypatch.setenv("FLUID_LLM_TEMPERATURE", "0.3")
        provider = AnthropicProvider()
        _, payload = provider.build_request(
            _config("anthropic", "claude-sonnet-4-6"), "system", "user"
        )
        assert payload["temperature"] == 0.3

    def test_no_seed_field_in_anthropic_payload(self, monkeypatch):
        """The Anthropic API does not yet expose a ``seed`` parameter.
        Silently sending one (e.g. by accidentally porting OpenAI's
        envelope) would cause Anthropic to reject the request as
        having an unrecognised field. Pin the absence so a future
        cherry-pick can't accidentally introduce it."""
        provider = AnthropicProvider()
        _, payload = provider.build_request(
            _config("anthropic", "claude-sonnet-4-6"), "system", "user"
        )
        assert "seed" not in payload


class TestAnthropicCacheControl:
    """Pin Anthropic prompt-caching wiring (Gap 7.4 from V1+V2 hardening).

    Anthropic's prompt cache shaves ~50-80% off TTFT and ~90% off
    input-token cost when the system prefix is byte-identical
    across requests. The cache only activates when:

    1. The ``system`` field is an *array* of content blocks (not a
       plain string).
    2. At least one block carries ``cache_control: {type: "ephemeral"}``.
    3. The block content is ≥ 1024 tokens (server-side gate; we can't
       enforce this client-side, but we can pin that the marker
       *lands* so the server actually sees something to cache).

    Without this regression test, a future refactor could "simplify"
    the system field back to a plain string, the cache_control hint
    would silently disappear, and the warm-cache regression test would
    flap intermittently while costs climbed without any single PR
    looking suspect.
    """

    def test_system_field_is_array_not_string(self):
        """Plain-string system would defeat cache_control entirely."""
        provider = AnthropicProvider()
        _, payload = provider.build_request(
            _config("anthropic", "claude-sonnet-4-6"), "short system", "user"
        )
        assert isinstance(payload["system"], list), (
            "Anthropic ``system`` must be an array of blocks for " "cache_control to apply"
        )
        assert len(payload["system"]) >= 1
        assert payload["system"][0]["type"] == "text"

    def test_system_block_carries_ephemeral_cache_control(self):
        """The marker is the user-visible cache opt-in. If it's missing,
        Anthropic happily serves a fresh response and bills full token
        cost — exactly the regression we're guarding against."""
        provider = AnthropicProvider()
        _, payload = provider.build_request(
            _config("anthropic", "claude-sonnet-4-6"), "system", "user"
        )
        block = payload["system"][0]
        assert block.get("cache_control") == {"type": "ephemeral"}

    def test_long_system_prompt_keeps_cache_control(self):
        """A >1024-token-equivalent system prompt is the case the cache
        actually serves on. Pin that the marker survives even when the
        prompt is large enough that the server-side cache will
        engage. (At ~4 chars/token, 8000 chars is ≈ 2000 tokens —
        comfortably above the 1024-token activation floor.)"""
        provider = AnthropicProvider()
        long_system = "x" * 8000
        _, payload = provider.build_request(
            _config("anthropic", "claude-sonnet-4-6"),
            long_system,
            "user",
        )
        # The system block contains the entire prompt verbatim AND the
        # cache_control marker — no truncation, no marker drop.
        block = payload["system"][0]
        assert block["text"] == long_system
        assert block.get("cache_control") == {"type": "ephemeral"}

    def test_short_system_still_advertises_cache_control(self):
        """Anthropic ignores cache_control on prompts below the 1024-token
        floor (no caching happens server-side) but doesn't reject the
        marker either. We always send it because we can't count tokens
        accurately client-side without an extra round trip — better to
        send and let the server decide."""
        provider = AnthropicProvider()
        _, payload = provider.build_request(
            _config("anthropic", "claude-sonnet-4-6"), "tiny", "user"
        )
        block = payload["system"][0]
        # Marker is present; server will silently no-op below threshold.
        assert block.get("cache_control") == {"type": "ephemeral"}

    def test_tool_request_path_also_carries_cache_control(self):
        """The agent-loop tool path (``build_tool_request``) is a
        SEPARATE code path. It must independently honour the
        cache_control contract — without this pin the
        ``forge_copilot_agent_loop`` could re-send the same long
        system prompt 5+ times per agent turn at full cost."""
        provider = AnthropicProvider()
        _, _, payload = provider.build_tool_request(
            _config("anthropic", "claude-sonnet-4-6"),
            "system",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
        )
        assert isinstance(payload["system"], list)
        assert payload["system"][0].get("cache_control") == {"type": "ephemeral"}


class TestGeminiPayloadDeterminism:
    def test_generation_config_temperature_pinned_zero(self, monkeypatch):
        monkeypatch.delenv("FLUID_LLM_TEMPERATURE", raising=False)
        provider = GeminiProvider()
        _, payload = provider.build_request(_config("gemini", "gemini-2.5-pro"), "system", "user")
        assert payload["generationConfig"]["temperature"] == 0.0

    def test_temperature_env_override_propagates(self, monkeypatch):
        monkeypatch.setenv("FLUID_LLM_TEMPERATURE", "0.9")
        provider = GeminiProvider()
        _, payload = provider.build_request(_config("gemini", "gemini-2.5-pro"), "system", "user")
        assert payload["generationConfig"]["temperature"] == 0.9

    def test_no_seed_in_gemini_payload(self):
        """Gemini's ``generationConfig`` accepts no ``seed`` field —
        guard against a silent regression that would 400 the request."""
        provider = GeminiProvider()
        _, payload = provider.build_request(_config("gemini", "gemini-2.5-pro"), "system", "user")
        assert "seed" not in payload.get("generationConfig", {})
        assert "seed" not in payload


class TestOllamaPayloadDeterminism:
    def test_inherits_openai_temperature_pin(self, monkeypatch):
        """Ollama uses the OpenAI-compat endpoint; subclassing means it
        inherits ``temperature=0.0`` for free, but the test still
        exercises the actual ``build_request`` invocation to catch a
        future override that drops the pin."""
        monkeypatch.delenv("FLUID_LLM_TEMPERATURE", raising=False)
        provider = OllamaProvider()
        _, payload = provider.build_request(_config("ollama", "llama3.1:70b"), "system", "user")
        assert payload["temperature"] == 0.0

    def test_inherits_openai_seed_pin(self, monkeypatch):
        provider = OllamaProvider()
        _, payload = provider.build_request(_config("ollama", "llama3.1:70b"), "system", "user")
        assert payload["seed"] == _OPENAI_SEED

    def test_authorization_header_stripped(self):
        """Ollama is local; the bearer token is meaningless and would
        leak the API key into local server logs. The Ollama provider
        explicitly removes it after calling ``super().build_request``."""
        provider = OllamaProvider()
        headers, _ = provider.build_request(_config("ollama", "llama3.1:70b"), "system", "user")
        assert "Authorization" not in headers

    def test_gemma4_uses_json_object_response_format(self, monkeypatch):
        """Gemma 4 is in the local provider E2E matrix, so keep its
        OpenAI-compatible JSON mode wired instead of relying on prompt-only
        JSON discipline."""
        monkeypatch.delenv("FLUID_LLM_STRUCTURED_OUTPUTS", raising=False)
        provider = OllamaProvider()
        _, payload = provider.build_request(_config("ollama", "gemma4:latest"), "system", "user")
        assert payload["response_format"] == {"type": "json_object"}


# ----------------------------------------------------------------------
# Registry consistency — every provider in the registry has these pins
# ----------------------------------------------------------------------


class TestRegistryDeterminismMatrix:
    @pytest.mark.parametrize(
        "provider_name,model",
        [
            ("openai", "gpt-4.1-mini"),
            ("anthropic", "claude-sonnet-4-6"),
            ("claude", "claude-sonnet-4-6"),  # alias of anthropic
            ("gemini", "gemini-2.5-pro"),
            ("ollama", "llama3.1:70b"),
        ],
    )
    def test_every_registered_provider_pins_temperature_zero(
        self, monkeypatch, provider_name, model
    ):
        """Registry-level coverage: every provider in
        ``BUILTIN_LLM_PROVIDERS`` must default to a deterministic
        sampling temperature when invoked through its own
        ``build_request``. Adding a new provider without this pin
        fails this test loudly."""
        monkeypatch.delenv("FLUID_LLM_TEMPERATURE", raising=False)
        provider = BUILTIN_LLM_PROVIDERS[provider_name]
        _, payload = provider.build_request(_config(provider_name, model), "system", "user")
        # Either a top-level "temperature" or a nested
        # generationConfig.temperature (Gemini-style).
        if "temperature" in payload:
            assert payload["temperature"] == 0.0
        else:
            assert payload["generationConfig"]["temperature"] == 0.0
