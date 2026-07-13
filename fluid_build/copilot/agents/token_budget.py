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

"""Pre-flight token budgeting for the staged agent layer.

Closes the "we only learn we're over budget when the API errors out"
gap. The legacy code path submits the prompt, eats a 400/413 from the
provider, then retries identically — wasting credits and wall-clock.
:func:`check_prompt_fits` lets every call site cheap-check against the
model's context window first, raising :class:`ContextOverflowError`
immediately so the agent loop can compact / summarize / fail fast
without paying for the failed call.

Tokenization is a pure-Python ``len(text) / 3.5`` char heuristic —
tuned to slightly *over*-estimate so we err on "fail fast" rather
than "bill the user for a doomed call". For a CLI's pre-flight
overflow check this is sufficient; we'd rather refuse a borderline
prompt than ship an exact-tokenizer Rust extension. Modern context
windows are 128K-1M tokens, so the ~10-20% heuristic error is far
below the precision required to make a different decision.

Context-window sizes follow a three-rung ladder (override → litellm
→ embedded offline fallback) mirroring
:func:`fluid_build.copilot.cost._resolve_per_million_rate` — see
:func:`get_context_window` for the lookup precedence. The embedded
:data:`DEFAULT_CONTEXT_WINDOWS` table is intentionally small and
mostly covers Ollama-served self-hosted models where litellm's
catalog lags real-world deployments. The per-call override
``capability_matrix["context_window"]`` is applied upstream in
:func:`check_prompt_fits`, ahead of the ladder.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from fluid_build.copilot.agents.errors import ContextOverflowError

LOG = logging.getLogger("fluid.copilot.agents.token_budget")

__all__ = [
    "DEFAULT_CONTEXT_WINDOWS",
    "check_prompt_fits",
    "count_tokens",
    "estimate_tokens",
    "get_context_window",
]


# ---------------------------------------------------------------------------
# Context-window catalog — OFFLINE FALLBACK ONLY
#
# Canonical context-window source is ``litellm.model_cost[model]
# ["max_input_tokens"]`` (rung 2 in :func:`get_context_window`).
# This table is a deliberately small offline fallback used when
# litellm isn't installed OR doesn't know the model. Mirrors the same
# posture as :data:`fluid_build.copilot.cost.MODEL_PRICES_USD`.
#
# What lives here, and why:
#
# * **Ollama-served local models.** litellm's Ollama catalog lags
#   real-world deployments — at probe time ``litellm.model_cost
#   ["ollama/llama3.1"]["max_input_tokens"] == 8192`` even though
#   llama 3.1 ships with a 128K design window. Keeping a curated
#   Ollama prefix table here means self-hosted users get the
#   model-design window without waiting for litellm to refresh.
#   Users running on small GPUs with ``num_ctx`` overrides can
#   shrink per-call via ``capability_matrix["context_window"]``.
#
# New cloud-provider models should NOT be added here — they belong
# upstream in litellm's ``model_prices_and_context_window.json``
# (which our ``>=1.83.7`` pin keeps fresh).
# ---------------------------------------------------------------------------

DEFAULT_CONTEXT_WINDOWS: Dict[str, int] = {
    # Ollama-served local models. See note above re: litellm Ollama
    # staleness. Sorted longest-prefix first so ``llama3.2:3b`` matches
    # ``llama3.2`` ahead of ``llama3`` in :func:`get_context_window`.
    "llama3.1": 128_000,
    "llama3.2": 128_000,
    "llama3.3": 128_000,
    "qwen3-coder": 256_000,
    "qwen3": 128_000,
    "gemma4": 128_000,
    "gemma3": 128_000,
    "mistral": 32_768,
    "mixtral": 32_768,
    "deepseek-r1": 128_000,
    # Conservative fallback for unknown models — keeps surprises bounded.
    "_default": 32_000,
}


# Reserve space for the model's response. Prompts that exceed
# ``context_window - reserved_output`` are rejected even though they'd
# technically fit, because we still need room for the completion.
DEFAULT_OUTPUT_RESERVATION = 4_096


_LITELLM_PROVIDER_PREFIXES: tuple = (
    "openai",
    "anthropic",
    "gemini",
    "vertex_ai",
    "groq",
    "bedrock",
    "ollama",
)


def _entry_max_input_tokens(entry: Any) -> Optional[int]:
    """Extract a positive ``max_input_tokens`` from a litellm entry, or None."""
    if not isinstance(entry, dict):
        return None
    raw = entry.get("max_input_tokens")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _lookup_litellm_max_input_tokens(model: str) -> Optional[int]:
    """Probe ``litellm.model_cost`` for the model's input window.

    Mirrors the key-shape ladder in
    :func:`fluid_build.copilot.cost._resolve_per_million_rate`,
    extended with longest-prefix matching so versioned model strings
    like ``claude-opus-4-7-20260101`` resolve via the bare
    ``claude-opus-4-7`` entry that litellm ships.

    Lookup order:

    1. Exact match on the bare model name.
    2. Exact match on ``<provider>/<model>`` for known provider
       prefixes — covers cases where the bare name isn't catalogued
       but the namespaced one is (e.g. ``vertex_ai/claude-3-5-sonnet``).
    3. Longest-prefix match across all litellm keys — a litellm key
       that is a prefix of ``model`` (e.g. litellm has
       ``claude-opus-4-7``; callers pass
       ``claude-opus-4-7-20260101``).

    Returns ``None`` when litellm isn't installed, doesn't recognise
    the model, or its entry reports a non-positive value (treated as
    a catalog miss).

    Field choice: ``max_input_tokens`` is the canonical context-window
    field in ``litellm/model_prices_and_context_window.json``. The
    legacy ``max_tokens`` field is the *output* cap on most modern
    entries — using it here would silently shrink the budget.
    ``get_max_tokens()`` in the litellm public API is similarly the
    output cap, NOT what we want.
    """
    try:
        import litellm  # type: ignore[import-untyped]
    except ImportError:
        return None
    cost = getattr(litellm, "model_cost", None)
    if not isinstance(cost, dict):
        return None

    # 1. Exact bare model.
    value = _entry_max_input_tokens(cost.get(model))
    if value is not None:
        return value

    # 2. Exact ``<provider>/<model>`` for each known provider prefix.
    for prefix in _LITELLM_PROVIDER_PREFIXES:
        value = _entry_max_input_tokens(cost.get(f"{prefix}/{model}"))
        if value is not None:
            return value

    # 3. Longest-prefix match against the full keyset. Skip litellm's
    # ``sample_spec`` documentation entry. Sort longest-first so the
    # most-specific catalog key wins.
    candidates = sorted(
        (k for k in cost if isinstance(k, str) and k != "sample_spec" and model.startswith(k)),
        key=len,
        reverse=True,
    )
    for key in candidates:
        value = _entry_max_input_tokens(cost.get(key))
        if value is not None:
            return value
    return None


def get_context_window(model: str) -> int:
    """Return the known context window for ``model``.

    Three-rung lookup ladder mirroring
    :func:`fluid_build.copilot.cost._resolve_per_million_rate`:

    1. **Per-call override.** Callers pass
       ``capability_matrix["context_window"]`` through
       :func:`check_prompt_fits` — that override is applied *before*
       this function is called, so the ladder below is reached only
       when there's no override.
    2. **litellm catalog.** ``litellm.model_cost[model]
       ["max_input_tokens"]`` is the canonical, actively-maintained
       source (litellm's ``>=1.83.7`` pin keeps it fresh).
       :func:`_lookup_litellm_max_input_tokens` probes the bare
       model name then each known provider prefix
       (``<provider>/<model>``).
    3. **Embedded fallback table.** :data:`DEFAULT_CONTEXT_WINDOWS`
       is a deliberately small offline-only table — mostly
       Ollama-served local models where litellm's catalog lags
       (e.g. ``ollama/llama3.1`` reports 8192 even though llama 3.1
       ships with a 128K design window). Longest-prefix matching
       lets ``claude-opus-4-7-20260101`` resolve to ``claude-opus-4-7``
       and ``llama3.2:3b`` to ``llama3.2``.

    Final fallback when none of the rungs match is the conservative
    ``DEFAULT_CONTEXT_WINDOWS["_default"]`` (32K).
    """
    # Rung 2 — litellm catalog (canonical).
    litellm_value = _lookup_litellm_max_input_tokens(model)
    if litellm_value is not None:
        return litellm_value

    # Rung 3a — embedded fallback, exact key match.
    if model in DEFAULT_CONTEXT_WINDOWS:
        return DEFAULT_CONTEXT_WINDOWS[model]

    # Rung 3b — embedded fallback, longest-prefix match.
    # ``claude-3-5-sonnet-20241022`` matches ``claude-3-5-sonnet``;
    # ``llama3.2:3b`` matches ``llama3.2`` ahead of ``llama3``.
    candidates = sorted(
        (k for k in DEFAULT_CONTEXT_WINDOWS if k != "_default"),
        key=len,
        reverse=True,
    )
    for prefix in candidates:
        if model.startswith(prefix):
            return DEFAULT_CONTEXT_WINDOWS[prefix]

    # Final conservative default.
    return DEFAULT_CONTEXT_WINDOWS["_default"]


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Char-based heuristic: ``ceil(len(text) / 3.5)``.

    Slightly over-estimates for English (4 chars/token typical) so we
    err on the side of "fail fast" rather than under-counting and
    submitting a prompt that gets server-clipped.
    """
    if not text:
        return 0
    return -(-len(text) // 7) * 2  # int-divide ceiling of len*2/7 == len/3.5


def count_tokens(text: str, *, provider: str = "", model: str = "") -> int:
    """Count tokens in ``text``.

    Uses ``litellm.token_counter`` for accurate, provider-specific
    counts (tiktoken for OpenAI, Anthropic's tokenizer for Claude,
    SentencePiece for Gemini). Falls back to a char-based heuristic
    when litellm doesn't recognise the model — same fail-fast bias
    as before so a too-big prompt still raises before the API call.

    ``FLUID_TOKEN_COUNTER=chars`` forces the legacy heuristic path
    (useful when test fixtures need stable counts).
    """
    if not text:
        return 0
    if os.environ.get("FLUID_TOKEN_COUNTER", "").strip().lower() == "chars":
        return estimate_tokens(text)
    if provider and model:
        try:
            import litellm  # core dep

            from fluid_build.llm.litellm_backend import (
                _LITELLM_PREFIX_BY_PROVIDER,
            )

            prefix = _LITELLM_PREFIX_BY_PROVIDER.get(provider.lower(), provider.lower())
            qualified = model if "/" in (model or "") else f"{prefix}/{model}"
            return int(litellm.token_counter(model=qualified, text=text))
        except Exception:  # noqa: BLE001 — heuristic fallback
            pass
    return estimate_tokens(text)


# ---------------------------------------------------------------------------
# Pre-flight check
# ---------------------------------------------------------------------------


def check_prompt_fits(
    *,
    system_prompt: str,
    user_prompt: str,
    provider: str,
    model: str,
    capability_matrix: Optional[Dict[str, Any]] = None,
    output_reservation: int = DEFAULT_OUTPUT_RESERVATION,
) -> int:
    """Raise :class:`ContextOverflowError` if the prompt won't fit.

    Returns the estimated input-token count when the prompt fits
    (useful for logging / cost projection).

    Reserves ``output_reservation`` tokens for the model's completion;
    the effective budget is ``context_window - output_reservation``.
    Override the context window per-call via
    ``capability_matrix["context_window"]`` for users on custom-tuned
    models or extended-context variants.
    """
    cm = capability_matrix or {}
    if cm.get("disable_token_preflight"):
        # Break-glass escape for users who want to risk the API call
        # rather than trust the local heuristic.
        return 0

    context_window = int(cm.get("context_window") or get_context_window(model))
    reserved = int(cm.get("output_reservation") or output_reservation)
    budget = max(0, context_window - reserved)

    sys_tokens = count_tokens(system_prompt, provider=provider, model=model)
    user_tokens = count_tokens(user_prompt, provider=provider, model=model)
    total = sys_tokens + user_tokens

    if total > budget:
        raise ContextOverflowError(
            (
                f"Prompt is {total:,} tokens but {model} on {provider} has a "
                f"{budget:,}-token usable budget "
                f"(context_window={context_window:,}, "
                f"output_reservation={reserved:,}). "
                "Compact the message history before retrying."
            ),
            provider=provider,
        )
    return total
