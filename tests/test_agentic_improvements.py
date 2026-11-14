# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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


# ---------------------------------------------------------------------------
# build_structured_repair_feedback
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _self_evaluate_contract (enabled path)
# ---------------------------------------------------------------------------
