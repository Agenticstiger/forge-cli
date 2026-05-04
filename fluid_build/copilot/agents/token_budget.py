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

Context-window sizes are kept as a small in-process catalog rather
than calling out to provider APIs — model context windows change
infrequently and the catalog is overrideable per-call via
``capability_matrix["context_window"]``.
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
# Context-window catalog
#
# Picked to be slightly conservative — we'd rather refuse a borderline
# prompt and have the caller compact it than submit something that
# barely fits and gets clipped server-side. Override per-session via
# ``capability_matrix["context_window"]``.
# ---------------------------------------------------------------------------

DEFAULT_CONTEXT_WINDOWS: Dict[str, int] = {
    # Anthropic
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 200_000,
    "claude-opus-4-5": 200_000,
    "claude-sonnet-4-7": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    # OpenAI
    "gpt-4.1": 1_000_000,
    "gpt-4.1-mini": 1_000_000,
    "gpt-4.1-nano": 1_000_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
    # Google Gemini
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_000_000,
    "gemini-2.0-flash": 1_000_000,
    "gemini-2.5-pro": 2_000_000,
    "gemini-2.5-flash": 1_000_000,
    # Ollama-served local models. Windows are the model's design
    # window; users running on small GPUs may have lower effective
    # windows due to ``num_ctx`` env / Modelfile overrides — we pick
    # the model-design value because the dispatcher has no way to
    # know the per-server override at call time.
    "llama3.1": 128_000,
    "llama3.2": 128_000,
    "llama3.3": 128_000,
    "llama3": 8_192,
    "qwen3-coder": 256_000,
    "qwen3": 128_000,
    "qwen2.5": 128_000,
    "qwen": 32_768,
    "gemma4": 128_000,
    "gemma3": 128_000,
    "gemma2": 8_192,
    "gemma": 8_192,
    "mistral": 32_768,
    "mixtral": 32_768,
    "deepseek-r1": 128_000,
    "deepseek": 32_768,
    "phi-4": 16_384,
    "phi-3": 4_096,
    "phi": 2_048,
    # Conservative fallback for unknown models — keeps surprises bounded.
    "_default": 32_000,
}


# Reserve space for the model's response. Prompts that exceed
# ``context_window - reserved_output`` are rejected even though they'd
# technically fit, because we still need room for the completion.
DEFAULT_OUTPUT_RESERVATION = 4_096


def get_context_window(model: str) -> int:
    """Return the known context window for ``model``.

    Embedded ``DEFAULT_CONTEXT_WINDOWS`` is canonical; we prefer
    longer-prefix matches before delegating to litellm's
    ``model_cost`` catalog (whose ``max_input_tokens`` is the same
    figure but only populated for the providers it knows). litellm's
    plain ``get_max_tokens`` is the wrong API here — it returns the
    *output* limit, not the input context window. Last-resort fallback
    is the 32K conservative default.
    """
    if model in DEFAULT_CONTEXT_WINDOWS:
        return DEFAULT_CONTEXT_WINDOWS[model]
    # Longest-prefix match: ``claude-3-5-sonnet-20241022`` matches
    # ``claude-3-5-sonnet``.
    candidates = sorted(
        (k for k in DEFAULT_CONTEXT_WINDOWS if k != "_default"),
        key=len,
        reverse=True,
    )
    for prefix in candidates:
        if model.startswith(prefix):
            return DEFAULT_CONTEXT_WINDOWS[prefix]
    # Fall back to litellm's static catalog when our table doesn't
    # know the model. Use ``model_cost[*]["max_input_tokens"]`` (the
    # context window) — NOT ``get_max_tokens()`` which is the output
    # cap.
    try:
        import litellm  # core dep

        cost = getattr(litellm, "model_cost", None) or {}
        entry = cost.get(model) or cost.get(f"openai/{model}")
        max_input = entry.get("max_input_tokens") if isinstance(entry, dict) else None
        if max_input and int(max_input) > 0:
            return int(max_input)
    except Exception:  # noqa: BLE001 — fall back to default
        pass
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

            from fluid_build.cli.forge_copilot_llm_litellm import (
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
