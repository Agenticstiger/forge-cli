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

"""Tests for the agentic quality improvements.

Covers:
- _compact_message_history (message compaction in agent loop)
- build_structured_repair_feedback (structured error mapping)
- extract_usage per LLM provider (token tracking)
- _self_evaluate_contract (self-evaluation path)
- _truncate_contract_for_eval (schema truncation)
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# _compact_message_history
# ---------------------------------------------------------------------------


class TestCompactMessageHistory:
    """Message history compaction preserves head/tail and truncates middle."""

    def _import(self):
        from fluid_build.cli.forge_copilot_agent_loop import _compact_message_history

        return _compact_message_history

    def test_short_history_passes_through(self):
        compact = self._import()
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = compact(messages)
        assert result == messages

    def test_long_strings_are_truncated(self):
        compact = self._import()
        messages = [
            {"role": "user", "content": "initial context"},
            {"role": "assistant", "content": "A" * 2000},
            {"role": "user", "content": "B" * 2000},
            {"role": "assistant", "content": "C" * 2000},
            # Last 4 messages — kept intact
            {"role": "user", "content": "recent1"},
            {"role": "assistant", "content": "recent2"},
            {"role": "user", "content": "recent3"},
            {"role": "assistant", "content": "recent4"},
        ]
        result = compact(messages)
        # Head preserved
        assert result[0]["content"] == "initial context"
        # Middle truncated
        assert "[truncated" in result[1]["content"]
        assert len(result[1]["content"]) < 600
        # Tail preserved
        assert result[-1]["content"] == "recent4"
        assert result[-4]["content"] == "recent1"

    def test_first_message_always_preserved(self):
        compact = self._import()
        long_context = "X" * 3000
        messages = [
            {"role": "user", "content": long_context},
        ] + [{"role": "assistant", "content": f"msg{i}"} for i in range(6)]
        result = compact(messages)
        # First message is head — never truncated even if long
        assert result[0]["content"] == long_context

    def test_anthropic_content_blocks_handled(self):
        compact = self._import()
        messages = [
            {"role": "user", "content": "context"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Z" * 2000},
                    {"type": "tool_use", "id": "t1", "name": "discover_workspace", "input": {}},
                ],
            },
            {"role": "user", "content": "tool result"},
            {"role": "assistant", "content": "next"},
            {"role": "user", "content": "r1"},
            {"role": "assistant", "content": "r2"},
            {"role": "user", "content": "r3"},
            {"role": "assistant", "content": "r4"},
        ]
        result = compact(messages)
        # The anthropic content blocks in the middle should be truncated
        middle_content = result[1]["content"]
        assert isinstance(middle_content, list)
        text_block = middle_content[0]
        assert "[truncated" in text_block["text"]
        # tool_use block should be untouched
        assert middle_content[1]["type"] == "tool_use"

    def test_exactly_tail_plus_one_messages(self):
        compact = self._import()
        # 5 messages = 1 head + 4 tail, nothing to compact
        messages = [{"role": "user", "content": f"m{i}"} for i in range(5)]
        result = compact(messages)
        assert result == messages


# ---------------------------------------------------------------------------
# build_structured_repair_feedback
# ---------------------------------------------------------------------------


class TestBuildStructuredRepairFeedback:
    """Structured repair feedback maps errors to categories and hints."""

    def _import(self):
        from fluid_build.cli.forge_copilot_contract_helpers import (
            build_structured_repair_feedback,
        )

        return build_structured_repair_feedback

    def test_known_pattern_categorized(self):
        build = self._import()
        result = build(["exposes[0].binding.platform is a required property"])
        assert len(result) == 1
        assert result[0]["category"] == "missing_field"
        assert "platform" in result[0]["fix_hint"].lower()
        assert result[0]["error"] == "exposes[0].binding.platform is a required property"

    def test_engine_incompatible_pattern(self):
        build = self._import()
        result = build(["engine 'dataform' not supported for provider 'local'"])
        assert result[0]["category"] == "engine_incompatible"

    def test_semantics_pattern(self):
        build = self._import()
        result = build(["exposes[0] missing semantics block"])
        assert result[0]["category"] == "semantics_incomplete"

    def test_unknown_error_gets_default_category(self):
        build = self._import()
        result = build(["something completely unexpected happened"])
        assert result[0]["category"] == "schema_violation"
        assert result[0]["fix_hint"] == ""
        assert result[0]["error"] == "something completely unexpected happened"

    def test_example_attached_when_defined(self):
        build = self._import()
        result = build(["binding.platform is missing"])
        assert "example" in result[0]
        assert "platform" in result[0]["example"]

    def test_multiple_errors(self):
        build = self._import()
        result = build(
            [
                "binding.platform is missing",
                "must include sql in properties",
                "totally unknown error",
            ]
        )
        assert len(result) == 3
        assert result[0]["category"] == "missing_field"
        assert result[1]["category"] == "missing_field"
        assert result[2]["category"] == "schema_violation"

    def test_empty_errors_list(self):
        build = self._import()
        assert build([]) == []


# ---------------------------------------------------------------------------
# extract_usage per provider
# ---------------------------------------------------------------------------


class TestExtractUsage:
    """Token usage extraction for each LLM provider."""

    def test_openai_usage(self):
        from fluid_build.cli.forge_copilot_llm_providers import OpenAIProvider

        provider = OpenAIProvider()
        usage = provider.extract_usage(
            {
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            }
        )
        # Subset assertion — Wave 1 added cache_creation_input_tokens /
        # cache_read_input_tokens to the canonical extract_usage shape.
        # The core counts remain stable.
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50
        assert usage["total_tokens"] == 150

    def test_openai_missing_usage(self):
        from fluid_build.cli.forge_copilot_llm_providers import OpenAIProvider

        provider = OpenAIProvider()
        usage = provider.extract_usage({"choices": [{"message": {"content": "hi"}}]})
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0
        assert usage["total_tokens"] == 0

    def test_openai_prompt_cache_usage(self):
        from fluid_build.cli.forge_copilot_llm_providers import OpenAIProvider

        provider = OpenAIProvider()
        metrics = provider.extract_prompt_cache(
            {
                "usage": {
                    "prompt_tokens": 100,
                    "prompt_token_details": {"cached_tokens": 40},
                }
            }
        )
        assert metrics == {"read_tokens": 40, "total_tokens": 100, "hit_rate": 0.4}

    def test_openai_prompt_cache_usage_accepts_plural_details_key(self):
        from fluid_build.cli.forge_copilot_llm_providers import OpenAIProvider

        provider = OpenAIProvider()
        metrics = provider.extract_prompt_cache(
            {
                "usage": {
                    "prompt_tokens": 200,
                    "prompt_tokens_details": {"cached_tokens": 50},
                }
            }
        )
        assert metrics == {"read_tokens": 50, "total_tokens": 200, "hit_rate": 0.25}

    # Anthropic / Gemini native-shape extract_usage tests deleted —
    # litellm normalises every provider's response to the OpenAI shape
    # before our extract_* methods see it. Wire-shape pinning per
    # provider lives in litellm's own test suite; our LiteLLMProvider's
    # OpenAI-shape extraction is covered in test_litellm_backend.py.

    def test_ollama_inherits_openai(self):
        from fluid_build.cli.forge_copilot_llm_providers import OllamaProvider

        provider = OllamaProvider()
        usage = provider.extract_usage(
            {
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 25, "total_tokens": 75},
            }
        )
        # Subset assertion (see test_openai_usage rationale).
        assert usage["input_tokens"] == 50
        assert usage["output_tokens"] == 25
        assert usage["total_tokens"] == 75

    def test_base_provider_returns_zeros(self):
        from fluid_build.cli.forge_copilot_llm_providers import LlmProvider

        # LlmProvider is abstract, but extract_usage has a concrete default
        assert LlmProvider.extract_usage(None, {}) == {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        assert LlmProvider.extract_prompt_cache(None, {}) == {
            "read_tokens": 0,
            "total_tokens": 0,
            "hit_rate": 0.0,
        }


# ---------------------------------------------------------------------------
# _truncate_contract_for_eval
# ---------------------------------------------------------------------------


class TestTruncateContractForEval:
    """Schema truncation keeps eval prompts small."""

    def test_small_schema_unchanged(self):
        from fluid_build.cli.forge_copilot_prompts import _truncate_contract_for_eval

        contract = {
            "exposes": [
                {
                    "exposeId": "out",
                    "contract": {
                        "schema": [
                            {"name": "id", "type": "integer"},
                            {"name": "name", "type": "string"},
                        ]
                    },
                }
            ],
        }
        result = _truncate_contract_for_eval(contract)
        assert len(result["exposes"][0]["contract"]["schema"]) == 2

    def test_large_schema_truncated(self):
        from fluid_build.cli.forge_copilot_prompts import _truncate_contract_for_eval

        columns = [{"name": f"col_{i}", "type": "string"} for i in range(20)]
        contract = {
            "exposes": [
                {
                    "exposeId": "out",
                    "contract": {"schema": columns},
                }
            ],
        }
        result = _truncate_contract_for_eval(contract)
        schema = result["exposes"][0]["contract"]["schema"]
        assert len(schema) == 3
        assert result["exposes"][0]["contract"]["_truncated_columns"] == 20

    def test_no_exposes(self):
        from fluid_build.cli.forge_copilot_prompts import _truncate_contract_for_eval

        contract = {"id": "test", "name": "Test"}
        result = _truncate_contract_for_eval(contract)
        assert result == contract

    def test_original_contract_not_mutated(self):
        from fluid_build.cli.forge_copilot_prompts import _truncate_contract_for_eval

        columns = [{"name": f"col_{i}", "type": "string"} for i in range(10)]
        contract = {
            "exposes": [
                {
                    "exposeId": "out",
                    "contract": {"schema": list(columns)},
                }
            ],
        }
        _truncate_contract_for_eval(contract)
        assert len(contract["exposes"][0]["contract"]["schema"]) == 10


# ---------------------------------------------------------------------------
# _self_evaluate_contract (enabled path)
# ---------------------------------------------------------------------------


class TestSelfEvaluateContract:
    """Self-evaluation calls routing model and returns score."""

    def test_returns_score_when_enabled(self, monkeypatch):
        monkeypatch.setenv("FLUID_COPILOT_SELF_EVAL", "1")

        from fluid_build.cli.forge_copilot_llm_providers import LlmConfig
        from fluid_build.cli.forge_copilot_runtime import _self_evaluate_contract

        config = LlmConfig(
            provider="openai",
            model="gpt-4o-mini",
            endpoint="https://api.openai.com/v1/chat/completions",
            api_key="test-key",
        )
        eval_response = json.dumps({"score": 8, "issues": [], "suggestions": ["looks good"]})

        with patch("fluid_build.cli.forge_copilot_runtime.call_llm", return_value=eval_response):
            result = _self_evaluate_contract(
                config,
                {"project_goal": "test", "use_case": "analytics"},
                {"id": "test", "exposes": []},
            )

        assert result is not None
        assert result["score"] == 8

    def test_returns_none_when_disabled(self, monkeypatch):
        monkeypatch.setenv("FLUID_COPILOT_SELF_EVAL", "0")

        from fluid_build.cli.forge_copilot_llm_providers import LlmConfig
        from fluid_build.cli.forge_copilot_runtime import _self_evaluate_contract

        config = LlmConfig(
            provider="openai",
            model="gpt-4o-mini",
            endpoint="https://api.openai.com/v1/chat/completions",
            api_key="test-key",
        )
        result = _self_evaluate_contract(config, {}, {})
        assert result is None

    def test_returns_none_on_llm_failure(self, monkeypatch):
        monkeypatch.setenv("FLUID_COPILOT_SELF_EVAL", "1")

        from fluid_build.cli.forge_copilot_llm_providers import LlmConfig
        from fluid_build.cli.forge_copilot_runtime import _self_evaluate_contract

        config = LlmConfig(
            provider="openai",
            model="gpt-4o-mini",
            endpoint="https://api.openai.com/v1/chat/completions",
            api_key="test-key",
        )

        with patch(
            "fluid_build.cli.forge_copilot_runtime.call_llm",
            side_effect=RuntimeError("network error"),
        ):
            result = _self_evaluate_contract(config, {}, {})

        assert result is None  # fail-open

    def test_returns_none_on_malformed_response(self, monkeypatch):
        monkeypatch.setenv("FLUID_COPILOT_SELF_EVAL", "1")

        from fluid_build.cli.forge_copilot_llm_providers import LlmConfig
        from fluid_build.cli.forge_copilot_runtime import _self_evaluate_contract

        config = LlmConfig(
            provider="openai",
            model="gpt-4o-mini",
            endpoint="https://api.openai.com/v1/chat/completions",
            api_key="test-key",
        )

        with patch(
            "fluid_build.cli.forge_copilot_runtime.call_llm",
            return_value="not json at all",
        ):
            result = _self_evaluate_contract(config, {}, {})

        assert result is None  # fail-open on parse error
