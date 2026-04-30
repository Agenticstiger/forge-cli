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

"""Default summarizer for ``FLUID_COMPACTION_STRATEGY=summarize``.

The compaction module accepts a ``Callable[[str], str]`` summarizer
hook so callers can plug in their own LLM. Until now no default was
provided, so users opting into the ``summarize`` / ``hybrid``
strategies had to write the closure themselves. This module ships a
default that:

* Reuses the existing ``LlmProvider`` HTTP shape — no new deps,
  no new SDKs.
* Uses the cheap ``fast`` tier from the same provider the user
  picked (Haiku for Anthropic, gpt-4.1-nano for OpenAI, gemini
  flash for Gemini, llama for Ollama).
* Honors the existing ``--llm-timeout-seconds`` and credential
  resolution so users don't have to configure anything extra.

Opt in via::

    FLUID_COMPACTION_STRATEGY=summarize fluid forge ...

or via the capability matrix::

    capability_matrix["compaction_strategy"] = "summarize"

The summarizer is invoked at most once per agent-loop iteration (when
the loop hits ``_COMPACT_AFTER``), so worst-case cost is ~one Haiku
call per long agent loop.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

import httpx

from fluid_build.cli.forge_copilot_llm_providers import (
    BUILTIN_LLM_PROVIDERS,
    LlmConfig,
    get_catalog_routing_model,
)

LOG = logging.getLogger("fluid.cli.forge_copilot.summarizer")

__all__ = [
    "build_default_summarizer",
]


_SUMMARIZER_SYSTEM_PROMPT = (
    "You compress a JSON-encoded transcript of an AI agent's earlier "
    "tool calls + tool results into a tight English summary. Keep:\n"
    "  - the user's original goal\n"
    "  - which tools were called and what they returned (key facts only)\n"
    "  - any decisions the agent has already made\n"
    "Drop verbose tool output bodies, repeated context, and any "
    "examples. Write 4-12 sentences. No code blocks, no JSON. The "
    "summary will replace the transcript in the model's context, so "
    "it must stand alone."
)


def build_default_summarizer(
    config: LlmConfig,
    *,
    timeout_seconds: int = 30,
    max_summary_tokens: int = 600,
) -> Callable[[str], str]:
    """Return a summarizer closure that calls the user's provider.

    The closure takes a single ``blob`` argument (the JSON-serialised
    middle messages) and returns a plain-text summary. Wrapping
    around the existing ``LlmProvider.build_request`` means we
    inherit the same HTTP shape, headers, retry posture, and Anthropic
    prompt-cache handling — no new abstractions, no new deps.

    A *fast-tier* model is selected via
    :func:`get_catalog_routing_model` so summarization stays cheap
    even when the user picked a flagship model for the main task.
    Falls back to the user's configured ``model`` if no fast tier is
    catalogued.
    """
    fast_model = get_catalog_routing_model(config.provider, config.model) or config.model
    provider = BUILTIN_LLM_PROVIDERS.get(config.provider)
    if provider is None:
        # Fallback summarizer = no-op: returns a truncated blob so the
        # compaction layer's caller still gets a usable string. This
        # keeps the world-class branch's pluggable contract intact even
        # when the user picked an unknown provider.
        return lambda blob: blob[:2000] + (
            f"\n[truncated — {len(blob)} chars total — unknown provider for summarizer]"
            if len(blob) > 2000
            else ""
        )

    routing_config = LlmConfig(
        provider=config.provider,
        model=fast_model,
        endpoint=provider.default_endpoint(fast_model, {}),
        api_key=config.api_key,
        timeout_seconds=timeout_seconds,
    )

    def summarize(blob: str) -> str:
        # Cap the input at a generous-but-finite size so a runaway
        # blob can't burn the user's budget. Trimming from the head
        # preserves the most-recent context, which is usually most
        # relevant.
        if len(blob) > 60_000:
            blob = blob[-60_000:]
        try:
            headers, payload = provider.build_request(
                routing_config, _SUMMARIZER_SYSTEM_PROMPT, blob
            )
            payload = dict(payload)
            payload.pop("response_format", None)  # plain text, not json_schema
            payload.pop("tools", None)
            payload.pop("tool_choice", None)
            if config.provider in {"anthropic", "claude"}:
                payload["max_tokens"] = max_summary_tokens
            response = httpx.post(
                routing_config.endpoint,
                headers=headers,
                json=payload,
                timeout=routing_config.timeout_seconds,
            )
            response.raise_for_status()
            raw = provider.extract_text(response.json())
            return raw.strip()
        except Exception as exc:  # noqa: BLE001 — never break the compaction caller
            LOG.warning(
                "default_summarizer_failed: provider=%s model=%s err=%s",
                routing_config.provider,
                routing_config.model,
                exc,
            )
            # Return a structural truncation marker so the compaction
            # layer's truncate-fallback inside ``summarize_messages``
            # has something useful to keep.
            return (
                f"[summarization failed — {len(blob)} chars of prior context "
                "compressed via fallback truncation]"
            )

    return summarize


def maybe_build_default_summarizer(
    config: Optional[LlmConfig],
) -> Optional[Callable[[str], str]]:
    """Convenience helper for call sites that don't always have a config.

    Returns ``None`` when ``config`` is missing, which the compaction
    module already handles by falling back to char truncation.
    """
    if config is None:
        return None
    return build_default_summarizer(config)
