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

"""Unit tests for :mod:`fluid_build.copilot.agents.token_budget`.

Covers:

* context-window catalog lookups (exact + longest-prefix matching),
* tiktoken-backed counting on OpenAI models,
* fallback to the char-based heuristic when tiktoken is unavailable
  or explicitly forced via env,
* :func:`check_prompt_fits` raising :class:`ContextOverflowError`
  with the right diagnostic shape on overflow,
* the capability-matrix overrides (``context_window``,
  ``output_reservation``, ``disable_token_preflight``).
"""

from __future__ import annotations

import pytest

from fluid_build.copilot.agents.errors import ContextOverflowError
from fluid_build.copilot.agents.token_budget import (
    DEFAULT_CONTEXT_WINDOWS,
    DEFAULT_OUTPUT_RESERVATION,
    check_prompt_fits,
    count_tokens,
    estimate_tokens,
    get_context_window,
)


class TestContextWindowCatalog:
    def test_exact_match_returns_listed_window(self) -> None:
        assert get_context_window("gpt-4o") == 128_000

    def test_longest_prefix_match(self) -> None:
        # ``claude-3-5-sonnet-20241022`` should resolve to
        # ``claude-3-5-sonnet`` (200K), not ``claude-3`` (which we
        # don't even list — but ``claude-3-sonnet`` is listed and the
        # longest-prefix rule should pick the more specific entry).
        assert (
            get_context_window("claude-3-5-sonnet-20241022")
            == DEFAULT_CONTEXT_WINDOWS["claude-3-5-sonnet"]
        )

    def test_opus_4_7_resolves_to_million(self) -> None:
        assert get_context_window("claude-opus-4-7-20260101") == 1_000_000

    def test_unknown_model_falls_back_to_default(self) -> None:
        assert (
            get_context_window("totally-novel-model-v9")
            == DEFAULT_CONTEXT_WINDOWS["_default"]
        )


class TestEstimateTokens:
    def test_empty_string_zero_tokens(self) -> None:
        assert estimate_tokens("") == 0

    def test_one_word_about_one_token(self) -> None:
        # "hello" = 5 chars → ceil(5*2/7) = 2 tokens by our formula
        # (slight over-estimate is the design point).
        assert estimate_tokens("hello") == 2

    def test_long_text_scales_linearly_ish(self) -> None:
        text = "a" * 700
        # 700 chars / 3.5 = 200 tokens (exact)
        assert estimate_tokens(text) == 200


class TestCountTokens:
    def test_empty_string_zero_regardless_of_path(self) -> None:
        assert count_tokens("", provider="openai", model="gpt-4o") == 0

    def test_chars_override_forces_heuristic(self, monkeypatch) -> None:
        monkeypatch.setenv("FLUID_TOKEN_COUNTER", "chars")
        # Even if tiktoken is installed, the override forces our
        # heuristic so test results are tiktoken-version-agnostic.
        text = "a" * 700
        assert count_tokens(text, provider="openai", model="gpt-4o") == 200

    def test_tiktoken_path_when_available(self, monkeypatch) -> None:
        # tiktoken is installed in the test venv (ships with the
        # langchain extra); count for "hello world" is small but
        # nonzero. Just check it returns a sensible positive integer.
        monkeypatch.delenv("FLUID_TOKEN_COUNTER", raising=False)
        n = count_tokens("hello world", provider="openai", model="gpt-4o")
        assert isinstance(n, int)
        assert 1 <= n <= 10  # generous bound, exact value is encoder-dependent

    def test_tiktoken_force_raises_when_missing(self, monkeypatch) -> None:
        """When the user demands tiktoken explicitly, fall-through to
        the heuristic is *not* allowed — surface the import error so
        they know their env isn't set up the way they think."""
        monkeypatch.setenv("FLUID_TOKEN_COUNTER", "tiktoken")
        # Simulate tiktoken being unavailable by monkeypatching the
        # internal helper to raise.
        from fluid_build.copilot.agents import token_budget as tb

        def boom(*args, **kwargs):
            raise ImportError("tiktoken not installed")

        monkeypatch.setattr(tb, "_tiktoken_count", boom)
        with pytest.raises(ImportError):
            count_tokens("anything", provider="openai", model="gpt-4o")

    def test_auto_falls_back_to_heuristic_when_tiktoken_breaks(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("FLUID_TOKEN_COUNTER", raising=False)
        from fluid_build.copilot.agents import token_budget as tb

        def boom(*args, **kwargs):
            raise RuntimeError("tiktoken exploded")

        monkeypatch.setattr(tb, "_tiktoken_count", boom)
        # Falls back to estimate_tokens silently — no exception.
        assert count_tokens("a" * 700, provider="openai", model="gpt-4o") == 200


class TestCheckPromptFits:
    def test_fits_returns_token_count(self, monkeypatch) -> None:
        monkeypatch.setenv("FLUID_TOKEN_COUNTER", "chars")
        n = check_prompt_fits(
            system_prompt="You are helpful.",
            user_prompt="Hi.",
            provider="openai",
            model="gpt-4o",
        )
        assert isinstance(n, int)
        assert n > 0

    def test_overflow_raises_context_overflow_error(self, monkeypatch) -> None:
        """A user_prompt longer than the model's window should fail
        BEFORE we attempt the call — that's the whole point of the
        pre-flight check."""
        monkeypatch.setenv("FLUID_TOKEN_COUNTER", "chars")
        # gpt-4 has an 8192-token window. With the default 4096-token
        # output reservation, the budget is 4096. 4096 * 3.5 chars =
        # 14336 chars. Throw 50K chars at it.
        huge = "x" * 50_000
        with pytest.raises(ContextOverflowError) as excinfo:
            check_prompt_fits(
                system_prompt="",
                user_prompt=huge,
                provider="openai",
                model="gpt-4",
            )
        # The error message must name the model + budget so users
        # know what to compact toward.
        msg = str(excinfo.value)
        assert "gpt-4" in msg
        assert "tokens" in msg
        assert excinfo.value.provider == "openai"

    def test_capability_matrix_can_override_window(self, monkeypatch) -> None:
        monkeypatch.setenv("FLUID_TOKEN_COUNTER", "chars")
        # Pretend gpt-4 has a million-token window for this user
        # (e.g. they're on a custom Azure deployment with extended
        # context). 50K chars / 3.5 = ~14K tokens — fits easily.
        n = check_prompt_fits(
            system_prompt="",
            user_prompt="x" * 50_000,
            provider="openai",
            model="gpt-4",
            capability_matrix={"context_window": 1_000_000},
        )
        assert n > 0

    def test_disable_preflight_skips_check_entirely(self, monkeypatch) -> None:
        """Break-glass: when explicitly disabled, even an obviously
        too-large prompt is allowed through (the caller takes
        responsibility for any 4xx that comes back)."""
        monkeypatch.setenv("FLUID_TOKEN_COUNTER", "chars")
        n = check_prompt_fits(
            system_prompt="",
            user_prompt="x" * 1_000_000,
            provider="openai",
            model="gpt-4",
            capability_matrix={"disable_token_preflight": True},
        )
        assert n == 0  # function bails early — no count returned

    def test_output_reservation_eats_into_budget(self, monkeypatch) -> None:
        """If we reserve more output tokens, less prompt fits.
        Verifies the reservation knob actually shrinks the usable
        budget."""
        monkeypatch.setenv("FLUID_TOKEN_COUNTER", "chars")
        # gpt-4 = 8192 window. Reserve 7000 → budget = 1192 tokens
        # = ~4172 chars. 5000 chars should overflow.
        with pytest.raises(ContextOverflowError):
            check_prompt_fits(
                system_prompt="",
                user_prompt="x" * 5_000,
                provider="openai",
                model="gpt-4",
                capability_matrix={"output_reservation": 7_000},
            )
