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

Three tokenization paths, in falling order of accuracy:

1. **tiktoken** (when ``[langchain]`` extra is installed) — exact for
   OpenAI / Azure-OpenAI ``gpt-4*`` / ``gpt-3.5*`` / o-series models.
   Approximate-but-close for everything else (we use the
   ``cl100k_base`` encoding as a generic upper-bound when the model
   isn't in tiktoken's catalog).
2. **char/3.5 heuristic** — when tiktoken isn't installed. Tuned to
   slightly *over*-estimate for English-heavy inputs so we err on
   the side of "fail fast" rather than "bill the user for a doomed
   call".
3. **provider-specific override** via ``FLUID_TOKEN_COUNTER`` env
   var (``"chars"`` / ``"tiktoken"`` / ``"none"``).

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
    """Count tokens in ``text`` for the given ``provider`` / ``model``.

    Uses tiktoken when available (exact for OpenAI; approximate for
    others via ``cl100k_base``) and falls back to the char-based
    heuristic. Honors ``FLUID_TOKEN_COUNTER`` overrides:

    * ``chars`` — force the char-based heuristic
    * ``tiktoken`` — force tiktoken (raises if not installed)
    * ``none`` / unset — auto (tiktoken if available, char otherwise)
    """
    if not text:
        return 0

    counter = os.environ.get("FLUID_TOKEN_COUNTER", "").strip().lower()
    if counter == "chars":
        return estimate_tokens(text)

    try:
        return _tiktoken_count(text, model=model, provider=provider)
    except Exception as exc:  # noqa: BLE001
        if counter == "tiktoken":
            # Caller asked for tiktoken explicitly — surface the error
            # rather than silently falling back to the char heuristic.
            raise
        LOG.debug("tiktoken count failed (%s); falling back to char heuristic", exc)
        return estimate_tokens(text)


def _tiktoken_count(text: str, *, model: str, provider: str) -> int:
    """Inner helper — kept separate so ``count_tokens`` can fall back
    cleanly if tiktoken isn't installed.

    For OpenAI models, picks the model-matched encoding. For other
    providers, uses ``cl100k_base`` (the OpenAI 2023+ encoding) as a
    reasonable upper-bound — most modern tokenizers produce similar
    counts on natural-language text.
    """
    import tiktoken  # noqa: PLC0415 — lazy: only imported when actually counting

    encoding = None
    if provider in {"openai", "azure-openai"} and model:
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = None
    if encoding is None:
        encoding = tiktoken.get_encoding("cl100k_base")
    return len(encoding.encode(text))


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
