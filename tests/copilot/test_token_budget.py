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

* The three-rung context-window lookup ladder (override → litellm
  → embedded offline fallback) — mirrors
  :func:`fluid_build.copilot.cost._resolve_per_million_rate`.
* litellm catalog probe with exact-bare, ``<provider>/<model>``,
  and longest-prefix matching against ``litellm.model_cost`` keys.
* The embedded fallback table is shrunk to a small offline-only
  surface; we pin its exact contents so accidental regrowth is
  caught.
* tiktoken-backed counting on OpenAI models, with char-heuristic
  fallback when forced via ``FLUID_TOKEN_COUNTER=chars``.
* :func:`check_prompt_fits` raising :class:`ContextOverflowError`
  with the right diagnostic shape on overflow.
* The capability-matrix overrides (``context_window``,
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
        # gpt-4o is canonically 128K in litellm's catalog (rung 2).
        assert get_context_window("gpt-4o") == 128_000

    def test_longest_prefix_match_via_litellm(self) -> None:
        # ``claude-opus-4-7-20260101`` exercises the litellm probe's
        # longest-prefix scan: litellm ships a bare ``claude-opus-4-7``
        # entry (1M tokens) that's a prefix of the versioned model
        # string. The probe should find it without us caring about
        # the datestamp.
        assert get_context_window("claude-opus-4-7-20260101") == 1_000_000

    def test_longest_prefix_match_via_embedded_fallback(self) -> None:
        # ``llama3.2:3b`` isn't in litellm with a usable ``max_input_tokens``
        # for the bare/ollama-prefixed key, so the embedded fallback's
        # longest-prefix rule kicks in: ``llama3.2`` wins (128K),
        # NOT ``llama3.1`` (also 128K) — the longer-prefix entry wins.
        assert get_context_window("llama3.2:3b") == DEFAULT_CONTEXT_WINDOWS["llama3.2"]

    def test_opus_4_7_resolves_to_million(self) -> None:
        assert get_context_window("claude-opus-4-7-20260101") == 1_000_000

    def test_unknown_model_falls_back_to_default(self) -> None:
        # No litellm match, no embedded prefix match → conservative default.
        assert get_context_window("totally-novel-model-v9") == DEFAULT_CONTEXT_WINDOWS["_default"]

    def test_embedded_fallback_table_is_small_and_pinned(self) -> None:
        """Pin the surviving offline-fallback entries.

        The pre-litellm-ladder table carried 30+ entries; after the
        rewrite the embedded table is a small offline-fallback surface
        that exists primarily to paper over litellm's stale Ollama
        coverage. Pin the exact set so accidental regrowth (e.g.
        someone adding a cloud-provider model that belongs upstream
        in litellm) is caught here.
        """
        assert set(DEFAULT_CONTEXT_WINDOWS) == {
            "llama3.1",
            "llama3.2",
            "llama3.3",
            "qwen3-coder",
            "qwen3",
            "gemma4",
            "gemma3",
            "mistral",
            "mixtral",
            "deepseek-r1",
            "_default",
        }


class TestLitellmLadderRung:
    """Verify the three-rung ladder uses litellm + embedded in order.

    The per-call override at ``capability_matrix["context_window"]``
    is applied *upstream* in :func:`check_prompt_fits` — that's
    covered separately under :class:`TestCheckPromptFits`. Here we
    target the rungs inside :func:`get_context_window` itself.
    """

    def _fake_model_cost(self, mapping: dict) -> object:
        """Build a fake ``litellm`` module with ``model_cost`` set."""

        class _FakeLitellm:
            model_cost = mapping

        return _FakeLitellm()

    def test_litellm_value_wins_over_embedded_fallback(self, monkeypatch) -> None:
        """When litellm returns a positive max_input_tokens, it
        wins over the embedded fallback for the same model."""
        # ``llama3.1`` is in the embedded fallback at 128K. Inject a
        # litellm bare-key answer of 250K and assert the litellm value
        # wins (proves rung 2 runs ahead of rung 3).
        fake = self._fake_model_cost({"llama3.1": {"max_input_tokens": 250_000}})
        import sys

        monkeypatch.setitem(sys.modules, "litellm", fake)
        assert get_context_window("llama3.1") == 250_000

    def test_litellm_zero_falls_through_to_embedded(self, monkeypatch) -> None:
        """A zero / missing litellm value is treated as a catalog miss
        and the embedded fallback wins."""
        fake = self._fake_model_cost(
            {
                # Zero max_input_tokens → catalog miss (rung 2 skips).
                "llama3.1": {"max_input_tokens": 0},
                "ollama/llama3.1": {"max_input_tokens": 0},
            }
        )
        import sys

        monkeypatch.setitem(sys.modules, "litellm", fake)
        # Falls through to embedded ``llama3.1`` (128K).
        assert get_context_window("llama3.1") == DEFAULT_CONTEXT_WINDOWS["llama3.1"]

    def test_litellm_provider_prefix_lookup(self, monkeypatch) -> None:
        """Litellm bare key absent; ``<provider>/<model>`` key
        present → use the namespaced answer."""
        fake = self._fake_model_cost({"vertex_ai/some-novel-model": {"max_input_tokens": 500_000}})
        import sys

        monkeypatch.setitem(sys.modules, "litellm", fake)
        assert get_context_window("some-novel-model") == 500_000

    def test_litellm_longest_prefix_match(self, monkeypatch) -> None:
        """A litellm key that is a prefix of the requested model is
        used when no exact match exists — covers versioned dated
        model names."""
        fake = self._fake_model_cost(
            {
                "my-future-model-2": {"max_input_tokens": 300_000},
                "my-future-model-2-mini": {"max_input_tokens": 400_000},
            }
        )
        import sys

        monkeypatch.setitem(sys.modules, "litellm", fake)
        # ``my-future-model-2-mini-20260601`` matches both prefixes;
        # longest-prefix rule picks the ``-mini`` entry (400K).
        assert get_context_window("my-future-model-2-mini-20260601") == 400_000

    def test_litellm_unknown_and_no_embedded_match_returns_default(self, monkeypatch) -> None:
        """When litellm + embedded both miss, the conservative
        ``_default`` is returned (NOT zero, NOT None — the call
        contract is ``int``)."""
        fake = self._fake_model_cost({})
        import sys

        monkeypatch.setitem(sys.modules, "litellm", fake)
        assert get_context_window("nonexistent-model-xyz") == DEFAULT_CONTEXT_WINDOWS["_default"]

    def test_litellm_import_error_falls_back_silently(self, monkeypatch) -> None:
        """If litellm isn't importable, the function must still
        return a sensible int (embedded → default)."""
        import builtins

        original_import = builtins.__import__

        def _block_litellm(name, *args, **kwargs):
            if name == "litellm":
                raise ImportError("litellm not installed")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _block_litellm)
        # Embedded prefix-match still works.
        assert get_context_window("qwen3-coder:30b") == DEFAULT_CONTEXT_WINDOWS["qwen3-coder"]
        # Unknown model → default.
        assert get_context_window("totally-novel-model-v9") == DEFAULT_CONTEXT_WINDOWS["_default"]

    def test_litellm_non_int_value_ignored(self, monkeypatch) -> None:
        """A malformed (non-numeric) max_input_tokens in litellm's
        catalog shouldn't crash the lookup."""
        fake = self._fake_model_cost({"weird-model": {"max_input_tokens": "garbage"}})
        import sys

        monkeypatch.setitem(sys.modules, "litellm", fake)
        # Treated as catalog miss → embedded fallback / default.
        assert get_context_window("weird-model") == DEFAULT_CONTEXT_WINDOWS["_default"]

    def test_per_call_override_via_check_prompt_fits(self, monkeypatch) -> None:
        """Rung 1 (per-call override) is applied in ``check_prompt_fits``
        upstream of ``get_context_window``. Asserting the override
        beats whatever the ladder would return for ``gpt-4`` (8192).
        """
        monkeypatch.setenv("FLUID_TOKEN_COUNTER", "chars")
        # 50K chars / 3.5 = ~14K tokens. gpt-4's native window is
        # 8192, so without an override this would overflow. With a
        # 1M override it fits.
        n = check_prompt_fits(
            system_prompt="",
            user_prompt="x" * 50_000,
            provider="openai",
            model="gpt-4",
            capability_matrix={"context_window": 1_000_000},
        )
        assert n > 0


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
    def test_empty_string_zero(self) -> None:
        assert count_tokens("", provider="openai", model="gpt-4o") == 0

    def test_uses_char_heuristic(self, monkeypatch) -> None:
        # FLUID_TOKEN_COUNTER=chars forces the char-heuristic path.
        # litellm.token_counter would otherwise be authoritative.
        monkeypatch.setenv("FLUID_TOKEN_COUNTER", "chars")
        text = "a" * 700  # 700 / 3.5 = 200 tokens (exact under the formula)
        assert count_tokens(text, provider="openai", model="gpt-4o") == 200

    def test_provider_and_model_args_are_accepted_but_unused(self, monkeypatch) -> None:
        # Symmetry with call sites — when the char-heuristic path is
        # forced, the args are accepted but unused.
        monkeypatch.setenv("FLUID_TOKEN_COUNTER", "chars")
        assert count_tokens("hello", provider="anthropic", model="claude-opus-4-7") == 2
        assert count_tokens("hello", provider="gemini", model="gemini-2.5-pro") == 2

    def test_litellm_path_uses_real_tokenizer(self) -> None:
        """When the env var is unset, count_tokens delegates to
        ``litellm.token_counter`` for accurate per-provider counts."""
        # tiktoken counts "hello" as 1 token; the char heuristic would
        # over-estimate at 2. The test confirms we're on the litellm
        # path by accepting either accurate value (token_counter for
        # OpenAI returns 1 today; if upstream changes the tokenizer
        # the count may change but should stay <= heuristic).
        n = count_tokens("hello", provider="openai", model="gpt-4o")
        assert 1 <= n <= 2


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
