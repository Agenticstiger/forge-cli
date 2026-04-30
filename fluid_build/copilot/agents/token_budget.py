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
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    # Google Gemini
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_000_000,
    "gemini-2.0-flash": 1_000_000,
    "gemini-2.5-pro": 2_000_000,
    # Conservative fallback for unknown models — keeps surprises bounded.
    "_default": 32_000,
}


# Reserve space for the model's response. Prompts that exceed
# ``context_window - reserved_output`` are rejected even though they'd
# technically fit, because we still need room for the completion.
DEFAULT_OUTPUT_RESERVATION = 4_096


def get_context_window(model: str) -> int:
    """Return the known context window for ``model``.

    Looks up by exact match first, then by longest matching prefix.
    Falls back to ``DEFAULT_CONTEXT_WINDOWS["_default"]`` (32K) — small
    enough to refuse runaway prompts on unknown models, large enough
    that legitimate stage prompts fit.
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
    """Count tokens in ``text`` using the char-based heuristic.

    Pure-Python, no external tokenizers. Provider/model arguments are
    accepted for symmetry with the call sites but currently unused —
    every supported provider uses a different tokenizer and shipping
    each one (Rust-extension tiktoken for OpenAI, custom for Anthropic,
    SentencePiece for Gemini) would bloat the CLI. The heuristic
    over-estimates by ~10-20% which matches the desired fail-fast bias.

    ``FLUID_TOKEN_COUNTER=chars`` is the only supported value today and
    is honored implicitly (it's already the only path).
    """
    if not text:
        return 0
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
