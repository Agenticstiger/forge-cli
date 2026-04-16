# Copyright 2024-2026 Agentics Transformation Ltd
# Licensed under the Apache License, Version 2.0

"""Slice UX-I: regression tests for AI-mode baseline parity.

Slice UX-I brings ``fluid forge`` AI copilot mode up to the industry
baseline that every serious AI-CLI ships today.  There are four
sub-items, each tested in its own class here:

1. **Streaming** (``TestStreamingLlmCalls``).  Every provider
   (OpenAI, Anthropic, Gemini, Ollama) parses SSE frames and yields
   text deltas whose concatenation matches what the blocking
   ``extract_text`` would return.  ``FLUID_LLM_STREAMING=0`` reverts
   to the legacy blocking path.

2. **Prompt caching** (``TestSystemPromptCache``).
   ``build_system_prompt`` is memoized per-process keyed on a stable
   hash of the capability matrix.  The Anthropic ``system`` block
   carries ``cache_control: ephemeral`` so Anthropic's prompt cache
   can actually fire.  ``clear_capability_matrix_cache`` chains to
   ``clear_system_prompt_cache`` so stale prompts can't leak.

3. **Structured outputs** (``TestStructuredOutputs``).  Every
   provider build_request payload includes the provider-native JSON
   enforcement directive when ``FLUID_LLM_STRUCTURED_OUTPUTS=1``
   (the default).  Anthropic forces a single ``emit_forge_contract``
   tool call; ``extract_text`` unwraps the ``tool_use`` block's
   JSON input.

4. **Interview skip** (``TestInterviewSkipWhenContextSufficient``).
   ``is_context_sufficient`` returns True when the minimum slots
   (``project_goal``, ``data_sources``, ``use_case``) are populated.
   In that case, ``run_adaptive_copilot_interview`` short-circuits
   the ``request_interview_decision`` LLM round.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterator, List
from unittest.mock import MagicMock, patch

import httpx
import pytest

from fluid_build.cli.forge_copilot_interview import (
    CONTEXT_SUFFICIENT_SLOTS,
    is_context_sufficient,
)
from fluid_build.cli.forge_copilot_llm_providers import (
    AnthropicProvider,
    GeminiProvider,
    LlmConfig,
    OllamaProvider,
    OpenAIProvider,
    call_llm_streaming,
    streaming_is_enabled,
)
from fluid_build.cli.forge_copilot_response_schema import (
    FORGE_RESPONSE_SCHEMA,
    anthropic_tool_definition,
    gemini_response_schema_config,
    ollama_supports_structured_output,
    openai_response_format,
)
from fluid_build.cli.forge_copilot_runtime import (
    _call_llm_with_optional_streaming,
    _system_prompt_cache_key,
    build_system_prompt,
    clear_capability_matrix_cache,
    clear_system_prompt_cache,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _capability_matrix() -> Dict[str, Any]:
    return {
        "providers": ["local", "gcp", "aws", "snowflake"],
        "templates": {
            "starter": {"description": "Minimal starter", "technologies": ["sql"]},
            "analytics": {"description": "Analytics", "technologies": ["sql"]},
            "etl_pipeline": {"description": "ETL", "technologies": ["sql", "python"]},
        },
        "build_engines": ["sql", "python", "spark"],
        "provider_engine_compatibility": {
            "local": ["sql", "python"],
            "gcp": ["sql", "python"],
            "aws": ["sql", "python"],
            "snowflake": ["sql"],
        },
    }


def _base_config(provider: str, model: str, endpoint: str) -> LlmConfig:
    return LlmConfig(
        provider=provider,
        model=model,
        endpoint=endpoint,
        api_key="test-key" if provider != "ollama" else None,
    )


class _FakeStreamResponse:
    """Minimal ``httpx.Response``-alike for unit-testing SSE parsers.

    Provides ``iter_lines`` returning a pre-baked list of text lines
    so each provider's ``iter_stream_chunks`` can be exercised without
    touching the network.
    """

    def __init__(self, lines: List[str]):
        self._lines = lines

    def iter_lines(self) -> Iterator[str]:
        yield from self._lines

    def raise_for_status(self) -> None:  # pragma: no cover — not used
        return


# ---------------------------------------------------------------------------
# UX-I.1 — Streaming
# ---------------------------------------------------------------------------


class TestStreamingLlmCalls:
    """Every provider must parse its own SSE format and yield
    correct text deltas.  Concatenated chunks must equal what the
    blocking ``extract_text`` path would return."""

    def test_openai_stream_parses_delta_events(self):
        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "Hello "}}]}),
            "",
            "data: " + json.dumps({"choices": [{"delta": {"content": "world"}}]}),
            "",
            "data: " + json.dumps({"choices": [{"delta": {"content": "!"}}]}),
            "",
            "data: [DONE]",
        ]
        provider = OpenAIProvider()
        chunks = list(provider.iter_stream_chunks(_FakeStreamResponse(lines)))
        assert "".join(chunks) == "Hello world!"

    def test_openai_stream_ignores_malformed_json(self):
        lines = [
            "data: not-json-at-all",
            "data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}),
            "data: [DONE]",
        ]
        chunks = list(OpenAIProvider().iter_stream_chunks(_FakeStreamResponse(lines)))
        assert "".join(chunks) == "ok"

    def test_ollama_inherits_openai_parser(self):
        """Ollama uses OpenAI's chat-compat format so its parser
        must behave identically on identical input."""
        lines = [
            "data: " + json.dumps({"choices": [{"delta": {"content": "local "}}]}),
            "data: " + json.dumps({"choices": [{"delta": {"content": "only"}}]}),
            "data: [DONE]",
        ]
        chunks = list(OllamaProvider().iter_stream_chunks(_FakeStreamResponse(lines)))
        assert "".join(chunks) == "local only"

    def test_anthropic_stream_parses_text_delta(self):
        """Legacy text-block streaming (without forced tool_use)."""
        lines = [
            "data: "
            + json.dumps(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hello "},
                }
            ),
            "data: "
            + json.dumps(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "from Claude"},
                }
            ),
            "data: " + json.dumps({"type": "message_stop"}),
        ]
        chunks = list(AnthropicProvider().iter_stream_chunks(_FakeStreamResponse(lines)))
        assert "".join(chunks) == "Hello from Claude"

    def test_anthropic_stream_parses_input_json_delta(self):
        """Forced tool_use path: deltas carry JSON fragments that
        concatenate into the tool's input JSON.  This is the path
        taken when structured outputs force ``emit_forge_contract``."""
        fragments = ['{"reco', 'mmended_template"', ': "star', 'ter"}']
        lines = [
            "data: "
            + json.dumps(
                {
                    "type": "content_block_delta",
                    "delta": {"type": "input_json_delta", "partial_json": frag},
                }
            )
            for frag in fragments
        ]
        lines.append("data: " + json.dumps({"type": "message_stop"}))
        chunks = list(AnthropicProvider().iter_stream_chunks(_FakeStreamResponse(lines)))
        result = "".join(chunks)
        assert json.loads(result) == {"recommended_template": "starter"}

    def test_gemini_stream_parses_candidates(self):
        lines = [
            "data: " + json.dumps({"candidates": [{"content": {"parts": [{"text": "gemini "}]}}]}),
            "data: "
            + json.dumps({"candidates": [{"content": {"parts": [{"text": "streaming"}]}}]}),
        ]
        chunks = list(GeminiProvider().iter_stream_chunks(_FakeStreamResponse(lines)))
        assert "".join(chunks) == "gemini streaming"

    def test_gemini_streaming_url_rewrites_endpoint(self):
        cfg = _base_config(
            "gemini",
            "gemini-2.5-flash",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        )
        url, _, _ = GeminiProvider().build_streaming_request(cfg, "sys", "usr")
        assert ":streamGenerateContent" in url
        assert "alt=sse" in url

    def test_openai_build_streaming_request_sets_stream_flag(self):
        cfg = _base_config("openai", "gpt-4o-mini", "https://api.openai.com/v1/chat/completions")
        url, _, payload = OpenAIProvider().build_streaming_request(cfg, "sys", "usr")
        assert url == cfg.endpoint
        assert payload.get("stream") is True

    def test_streaming_is_enabled_honours_env_kill_switch(self, monkeypatch):
        monkeypatch.delenv("FLUID_LLM_STREAMING", raising=False)
        assert streaming_is_enabled() is True
        monkeypatch.setenv("FLUID_LLM_STREAMING", "0")
        assert streaming_is_enabled() is False
        monkeypatch.setenv("FLUID_LLM_STREAMING", "false")
        assert streaming_is_enabled() is False
        monkeypatch.setenv("FLUID_LLM_STREAMING", "1")
        assert streaming_is_enabled() is True

    def test_call_llm_streaming_empty_stream_raises(self, monkeypatch):
        """Streaming providers that return zero text chunks must
        produce a clean ``CopilotGenerationError`` so the upstream
        fallback can kick in."""
        from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError

        provider = OpenAIProvider()
        cfg = _base_config("openai", "gpt-4o-mini", "https://api.openai.com/v1/chat/completions")

        class _EmptyStream:
            def __enter__(self):
                self._resp = _FakeStreamResponse(["data: [DONE]"])
                self._resp.raise_for_status = lambda: None
                self._resp.read = lambda: None
                return self._resp

            def __exit__(self, *a):
                return False

        class _FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def stream(self, *_a, **_kw):
                return _EmptyStream()

        with patch(
            "fluid_build.cli.forge_copilot_llm_providers.httpx.Client",
            return_value=_FakeClient(),
        ):
            with pytest.raises(CopilotGenerationError):
                list(call_llm_streaming(provider, cfg, "sys", "usr"))

    def test_call_llm_with_optional_streaming_falls_back_on_failure(self):
        """If streaming errors, the helper must fall back to the
        blocking ``call_llm`` so the user's run is never lost."""
        from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError

        provider = OpenAIProvider()
        cfg = _base_config("openai", "gpt-4o-mini", "https://api.openai.com/v1/chat/completions")

        def _fail_stream(*_a, **_kw):
            raise CopilotGenerationError("copilot_llm_stream_empty", "empty")
            yield  # pragma: no cover — generator

        with patch(
            "fluid_build.cli.forge_copilot_runtime.call_llm_streaming",
            side_effect=_fail_stream,
        ):
            with patch(
                "fluid_build.cli.forge_copilot_runtime.call_llm",
                return_value='{"ok": true}',
            ) as blocking:
                result = _call_llm_with_optional_streaming(provider, cfg, "sys", "usr")

        assert result == '{"ok": true}'
        blocking.assert_called_once()

    def test_call_llm_with_optional_streaming_respects_kill_switch(self, monkeypatch):
        """When the kill-switch is off, streaming never even starts."""
        monkeypatch.setenv("FLUID_LLM_STREAMING", "0")
        provider = OpenAIProvider()
        cfg = _base_config("openai", "gpt-4o-mini", "https://api.openai.com/v1/chat/completions")
        with patch(
            "fluid_build.cli.forge_copilot_runtime.call_llm",
            return_value='{"ok": true}',
        ) as blocking:
            with patch("fluid_build.cli.forge_copilot_runtime.call_llm_streaming") as streaming:
                result = _call_llm_with_optional_streaming(provider, cfg, "sys", "usr")
        assert result == '{"ok": true}'
        blocking.assert_called_once()
        streaming.assert_not_called()


# ---------------------------------------------------------------------------
# UX-I.2 — System prompt cache + Anthropic cache_control
# ---------------------------------------------------------------------------


class TestSystemPromptCache:
    def setup_method(self):
        clear_system_prompt_cache()
        clear_capability_matrix_cache()

    def teardown_method(self):
        clear_system_prompt_cache()
        clear_capability_matrix_cache()

    def test_second_call_returns_cached_string(self):
        """Byte-identical return is what Anthropic/OpenAI prefix
        caching actually needs."""
        matrix = _capability_matrix()
        first = build_system_prompt(matrix)
        second = build_system_prompt(matrix)
        assert first is second, "system prompt must be memoized per-process"

    def test_different_matrices_produce_different_prompts(self):
        """Make sure we're not returning a stale prompt when the
        capability matrix actually changes (e.g. a new provider
        plugin lands)."""
        matrix_a = _capability_matrix()
        matrix_b = _capability_matrix()
        matrix_b["providers"] = matrix_a["providers"] + ["new-provider"]
        first = build_system_prompt(matrix_a)
        second = build_system_prompt(matrix_b)
        assert first != second

    def test_clear_cache_forces_rebuild(self):
        matrix = _capability_matrix()
        first = build_system_prompt(matrix)
        clear_system_prompt_cache()
        second = build_system_prompt(matrix)
        assert first == second  # same content
        # After a clear, the cache starts empty so we get a fresh
        # string object on the next call (not the same id).
        assert first is not second

    def test_clearing_capability_matrix_also_clears_prompt_cache(self):
        """Stale system prompts cannot survive a capability matrix
        invalidation — the chain is wired in
        ``clear_capability_matrix_cache``."""
        matrix = _capability_matrix()
        first = build_system_prompt(matrix)
        clear_capability_matrix_cache()
        second = build_system_prompt(matrix)
        assert first == second
        assert first is not second

    def test_cache_key_is_stable_for_equal_matrices(self):
        """Deep-copied matrices must produce the same cache key so
        the retry loop actually hits the cache."""
        import copy as _copy

        matrix = _capability_matrix()
        key_a = _system_prompt_cache_key(matrix)
        key_b = _system_prompt_cache_key(_copy.deepcopy(matrix))
        assert key_a == key_b
        assert key_a is not None

    def test_anthropic_system_block_carries_cache_control(self):
        cfg = _base_config(
            "anthropic",
            "claude-3-5-sonnet-latest",
            "https://api.anthropic.com/v1/messages",
        )
        _, payload = AnthropicProvider().build_request(cfg, "hello sys", "hello usr")
        system = payload["system"]
        assert isinstance(system, list)
        assert system[0]["type"] == "text"
        assert system[0]["text"] == "hello sys"
        assert system[0]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# UX-I.3 — Structured outputs
# ---------------------------------------------------------------------------


class TestStructuredOutputs:
    def test_response_schema_required_keys_match_normalizer(self):
        required = set(FORGE_RESPONSE_SCHEMA["required"])
        expected = {
            "recommended_template",
            "recommended_provider",
            "recommended_patterns",
            "architecture_suggestions",
            "best_practices",
            "technology_stack",
            "description",
            "domain",
            "owner",
            "readme_markdown",
            "contract",
            "additional_files",
        }
        assert required == expected

    def test_openai_response_format_strict_for_modern_models(self):
        rf = openai_response_format("gpt-4o-mini")
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["name"] == "ForgeContract"
        assert rf["json_schema"]["strict"] is True

    def test_openai_response_format_falls_back_for_legacy_models(self):
        rf = openai_response_format("gpt-3.5-turbo-0613")
        assert rf == {"type": "json_object"}

    def test_openai_build_request_includes_response_format(self, monkeypatch):
        monkeypatch.delenv("FLUID_LLM_STRUCTURED_OUTPUTS", raising=False)
        cfg = _base_config("openai", "gpt-4o-mini", "https://api.openai.com/v1/chat/completions")
        _, payload = OpenAIProvider().build_request(cfg, "sys", "usr")
        assert "response_format" in payload
        assert payload["response_format"]["type"] == "json_schema"

    def test_anthropic_build_request_forces_emit_tool(self, monkeypatch):
        monkeypatch.delenv("FLUID_LLM_STRUCTURED_OUTPUTS", raising=False)
        cfg = _base_config(
            "anthropic", "claude-3-5-sonnet-latest", "https://api.anthropic.com/v1/messages"
        )
        _, payload = AnthropicProvider().build_request(cfg, "sys", "usr")
        assert payload["tools"][0]["name"] == "emit_forge_contract"
        assert payload["tool_choice"] == {"type": "tool", "name": "emit_forge_contract"}

    def test_anthropic_extract_text_unwraps_tool_use_block(self):
        """When the model replies with a forced tool_use, the
        adapter must return the JSON-encoded input directly so the
        downstream parser can consume it without unwrapping."""
        response = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "emit_forge_contract",
                    "input": {"recommended_template": "starter"},
                }
            ]
        }
        text = AnthropicProvider().extract_text(response)
        assert json.loads(text) == {"recommended_template": "starter"}

    def test_anthropic_extract_text_still_handles_legacy_text_blocks(self):
        response = {"content": [{"type": "text", "text": "legacy"}]}
        assert AnthropicProvider().extract_text(response) == "legacy"

    def test_gemini_build_request_skips_response_schema_for_nested_freeform(self, monkeypatch):
        """Gemini's responseSchema was disabled for nested free-form
        objects (the contract field).  The generationConfig should
        only contain temperature, not responseMimeType."""
        monkeypatch.delenv("FLUID_LLM_STRUCTURED_OUTPUTS", raising=False)
        cfg = _base_config(
            "gemini",
            "gemini-2.5-flash",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        )
        _, payload = GeminiProvider().build_request(cfg, "sys", "usr")
        # responseSchema is intentionally disabled for Gemini because
        # it can't handle nested additionalProperties (see bugfix
        # commit c6aabb9).
        assert "responseMimeType" not in payload["generationConfig"]
        assert "responseSchema" not in payload["generationConfig"]

    def test_ollama_supports_structured_output_reads_catalog(self):
        """Ollama structured output support is now catalog-driven, not
        a hardcoded allowlist.  Unknown models return False."""
        # Ollama models list in the catalog is empty (dynamic),
        # so no model is marked as structured_output capable.
        assert ollama_supports_structured_output("some-random-model") is False

    def test_ollama_build_request_drops_response_format_for_unknown_models(self, monkeypatch):
        monkeypatch.delenv("FLUID_LLM_STRUCTURED_OUTPUTS", raising=False)
        cfg = _base_config(
            "ollama", "some-random-model", "http://localhost:11434/v1/chat/completions"
        )
        _, payload = OllamaProvider().build_request(cfg, "sys", "usr")
        # Ollama catalog has an empty models list, so no model is
        # recognized as structured-output-capable → format dropped.
        assert "response_format" not in payload

    def test_ollama_build_request_also_drops_for_known_names_without_catalog_entry(
        self, monkeypatch
    ):
        """Even well-known model names like llama3.1 don't get
        structured output unless the catalog explicitly lists them
        with the structured_output capability flag."""
        monkeypatch.delenv("FLUID_LLM_STRUCTURED_OUTPUTS", raising=False)
        cfg = _base_config("ollama", "llama3.1", "http://localhost:11434/v1/chat/completions")
        _, payload = OllamaProvider().build_request(cfg, "sys", "usr")
        # Ollama's models list is empty in the catalog (dynamic),
        # so the capability check returns False → no response_format.
        assert "response_format" not in payload

    def test_kill_switch_disables_structured_outputs(self, monkeypatch):
        """``FLUID_LLM_STRUCTURED_OUTPUTS=0`` reverts every provider
        to pre-slice-UX-I plaintext JSON mode."""
        monkeypatch.setenv("FLUID_LLM_STRUCTURED_OUTPUTS", "0")
        cfg_o = _base_config("openai", "gpt-4o-mini", "https://api.openai.com/v1/chat/completions")
        _, pay = OpenAIProvider().build_request(cfg_o, "sys", "usr")
        assert "response_format" not in pay

        cfg_a = _base_config(
            "anthropic", "claude-3-5-sonnet-latest", "https://api.anthropic.com/v1/messages"
        )
        _, pay = AnthropicProvider().build_request(cfg_a, "sys", "usr")
        assert "tools" not in pay

        cfg_g = _base_config(
            "gemini",
            "gemini-2.5-flash",
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        )
        _, pay = GeminiProvider().build_request(cfg_g, "sys", "usr")
        assert "responseSchema" not in pay["generationConfig"]


# ---------------------------------------------------------------------------
# UX-I.4 — Interview skip when context is sufficient
# ---------------------------------------------------------------------------


class TestInterviewSkipWhenContextSufficient:
    def test_is_context_sufficient_full_context(self):
        assert (
            is_context_sufficient(
                {
                    "project_goal": "Sales Analytics",
                    "data_sources": "sql/orders.sql",
                    "use_case": "analytics",
                }
            )
            is True
        )

    def test_is_context_sufficient_missing_slot(self):
        assert is_context_sufficient({"project_goal": "x", "data_sources": "y"}) is False

    def test_is_context_sufficient_empty_string_slot(self):
        assert (
            is_context_sufficient(
                {
                    "project_goal": "x",
                    "data_sources": "   ",
                    "use_case": "analytics",
                }
            )
            is False
        )

    def test_is_context_sufficient_empty_list_slot(self):
        assert (
            is_context_sufficient(
                {
                    "project_goal": "x",
                    "data_sources": [],
                    "use_case": "analytics",
                }
            )
            is False
        )

    def test_is_context_sufficient_requires_mapping_input(self):
        assert is_context_sufficient("not a dict") is False

    def test_context_sufficient_slots_are_documented(self):
        """Guardrail: the slot tuple is part of the public contract
        with the forge interview flow — adding or removing slots
        here should be a deliberate change."""
        assert CONTEXT_SUFFICIENT_SLOTS == ("project_goal", "data_sources", "use_case")

    def test_interview_loop_short_circuits_when_context_is_sufficient(self):
        """The clarification LLM round must not fire when
        ``is_context_sufficient`` returns True."""
        from fluid_build.cli.forge_copilot_interview import run_adaptive_copilot_interview

        # A MagicMock console is enough to enter the `while console`
        # branch; the interview short-circuit check must still beat
        # that.
        console = MagicMock()
        llm_config = _base_config(
            "openai", "gpt-4o-mini", "https://api.openai.com/v1/chat/completions"
        )
        discovery = MagicMock()
        discovery.existing_contracts = []
        discovery.provider_hints = []
        discovery.sample_files = []
        discovery.sql_files = []
        discovery.dbt_projects = []
        discovery.terraform_projects = []
        discovery.detected_sources = []
        discovery.readmes = []
        discovery.to_prompt_payload = MagicMock(return_value={})

        context = {
            "project_goal": "Sales Analytics",
            "data_sources": "warehouse tables",
            "use_case": "analytics",
        }

        with patch("fluid_build.cli.forge_copilot_interview._ask_bootstrap_questions"):
            with patch(
                "fluid_build.cli.forge_copilot_interview.request_interview_decision"
            ) as clarifier:
                state = run_adaptive_copilot_interview(
                    initial_context=context,
                    console=console,
                    llm_config=llm_config,
                    discovery_report=discovery,
                    capability_matrix=_capability_matrix(),
                    project_memory=None,
                )

        clarifier.assert_not_called(), (
            "is_context_sufficient must short-circuit the clarification LLM round"
        )
        assert state.ready is True

    def test_interview_loop_runs_clarifier_when_force_env_is_set(self, monkeypatch):
        """``FLUID_COPILOT_FORCE_INTERVIEW=1`` keeps the old behaviour."""
        from fluid_build.cli.forge_copilot_interview import (
            InterviewDecision,
            run_adaptive_copilot_interview,
        )

        monkeypatch.setenv("FLUID_COPILOT_FORCE_INTERVIEW", "1")

        console = MagicMock()
        llm_config = _base_config(
            "openai", "gpt-4o-mini", "https://api.openai.com/v1/chat/completions"
        )
        discovery = MagicMock()
        discovery.existing_contracts = []
        discovery.provider_hints = []
        discovery.sample_files = []
        discovery.sql_files = []
        discovery.dbt_projects = []
        discovery.terraform_projects = []
        discovery.detected_sources = []
        discovery.readmes = []
        discovery.to_prompt_payload = MagicMock(return_value={})

        context = {
            "project_goal": "Sales Analytics",
            "data_sources": "warehouse tables",
            "use_case": "analytics",
        }

        fake_decision = InterviewDecision(
            status="ready",
            reason="forced round",
            context_patch={},
            assumptions=[],
            questions=[],
        )

        with patch("fluid_build.cli.forge_copilot_interview._ask_bootstrap_questions"):
            with patch(
                "fluid_build.cli.forge_copilot_interview.request_interview_decision",
                return_value=fake_decision,
            ) as clarifier:
                run_adaptive_copilot_interview(
                    initial_context=context,
                    console=console,
                    llm_config=llm_config,
                    discovery_report=discovery,
                    capability_matrix=_capability_matrix(),
                    project_memory=None,
                )

        clarifier.assert_called_once()
