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

"""Provider/model capability matrix + user-facing degradation warnings.

Closes the "users switching providers silently get worse results" gap.
The legacy path runs the same agent loop on every (provider, model)
combination without checking whether the combination actually supports
the features the agents depend on:

* Tool use (the multi-turn agent loop is fundamentally tool-driven —
  Ollama / llama3 without function-calling produces an unsupervised
  text generator, not an agent).
* Structured output enforcement (Gemini structured output was disabled
  in the legacy provider path because of schema-budget issues — users
  on Gemini got JSON-mode-only responses with no schema enforcement
  and never saw a warning).
* Prompt caching (Anthropic-only today; toggling to OpenAI loses the
  ~90% input-cost discount on stable system prompts).
* Extended thinking (Opus 4.7 thinking, o-series reasoning) — none of
  the legacy provider adapters knew about thinking budgets, so users
  on a thinking-capable model got plain completion behaviour.

This module:

* Models capabilities as a small typed dataclass per (provider, model).
* Resolves capabilities via a catalog with longest-prefix model
  matching (so ``claude-3-5-sonnet-20241022`` picks up the
  ``claude-3-5-sonnet`` row).
* Compares the resolved capabilities against the requirements of the
  current run and emits a single, structured warning when something
  important is missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "CAPABILITY_CATALOG",
    "ProviderCapabilities",
    "assess_capabilities",
    "format_degradation_warnings",
    "required_capabilities_for",
]


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a (provider, model) actually supports.

    Fields are intentionally booleans (rather than enums or feature
    matrices) so the catalog stays compact and grep-able. Add new
    fields as the agent layer grows new requirements.
    """

    provider: str
    model_prefix: str

    # Hard requirements for the agentic path.
    tool_use: bool = False
    structured_output: bool = False
    streaming: bool = True

    # Cost / quality optimisations that aren't strictly required but
    # affect economics + behaviour.
    prompt_caching: bool = False
    extended_thinking: bool = False

    # Operational signals — surfaces in warnings to set user expectations.
    notes: Tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Catalog
#
# Entries are matched by (provider, model_prefix) with longest-prefix
# winning. Keep them sorted from most-specific to least-specific within
# a provider so a quick read shows the override hierarchy.
# ---------------------------------------------------------------------------


CAPABILITY_CATALOG: Tuple[ProviderCapabilities, ...] = (
    # ---- Anthropic ----
    ProviderCapabilities(
        provider="anthropic",
        model_prefix="claude-opus-4-7",
        tool_use=True,
        structured_output=True,
        streaming=True,
        prompt_caching=True,
        extended_thinking=True,
        notes=(
            "Temperature is deprecated on Opus 4.7 — providers drop it "
            "automatically.",
        ),
    ),
    ProviderCapabilities(
        provider="anthropic",
        model_prefix="claude-sonnet-4-7",
        tool_use=True,
        structured_output=True,
        streaming=True,
        prompt_caching=True,
        extended_thinking=True,
    ),
    ProviderCapabilities(
        provider="anthropic",
        model_prefix="claude-sonnet-4-6",
        tool_use=True,
        structured_output=True,
        streaming=True,
        prompt_caching=True,
    ),
    ProviderCapabilities(
        provider="anthropic",
        model_prefix="claude-sonnet-4-5",
        tool_use=True,
        structured_output=True,
        streaming=True,
        prompt_caching=True,
    ),
    ProviderCapabilities(
        provider="anthropic",
        model_prefix="claude-haiku-4-5",
        tool_use=True,
        structured_output=True,
        streaming=True,
        prompt_caching=True,
    ),
    ProviderCapabilities(
        provider="anthropic",
        model_prefix="claude-3-5-sonnet",
        tool_use=True,
        structured_output=True,
        streaming=True,
        prompt_caching=True,
    ),
    ProviderCapabilities(
        provider="anthropic",
        model_prefix="claude-3-5-haiku",
        tool_use=True,
        structured_output=True,
        streaming=True,
        prompt_caching=True,
    ),
    ProviderCapabilities(
        provider="anthropic",
        model_prefix="claude-3-opus",
        tool_use=True,
        structured_output=True,
        streaming=True,
        prompt_caching=True,
    ),
    ProviderCapabilities(
        provider="anthropic",
        model_prefix="claude-3",
        tool_use=True,
        structured_output=True,
        streaming=True,
    ),
    # ---- OpenAI ----
    ProviderCapabilities(
        provider="openai",
        model_prefix="o1",
        tool_use=False,  # o1 series doesn't support function calling
        structured_output=True,
        streaming=False,  # o1 doesn't stream
        extended_thinking=True,
        notes=(
            "o1 reasoning models do not support tool use or streaming. "
            "Multi-turn tool loops will degrade to single-shot prompts.",
        ),
    ),
    ProviderCapabilities(
        provider="openai",
        model_prefix="o4",
        tool_use=True,
        structured_output=True,
        streaming=True,
        extended_thinking=True,
    ),
    ProviderCapabilities(
        provider="openai",
        model_prefix="o3",
        tool_use=True,
        structured_output=True,
        streaming=True,
        extended_thinking=True,
    ),
    ProviderCapabilities(
        provider="openai",
        model_prefix="gpt-4.1-nano",
        tool_use=True,
        structured_output=True,
        streaming=True,
    ),
    ProviderCapabilities(
        provider="openai",
        model_prefix="gpt-4.1-mini",
        tool_use=True,
        structured_output=True,
        streaming=True,
    ),
    ProviderCapabilities(
        provider="openai",
        model_prefix="gpt-4.1",
        tool_use=True,
        structured_output=True,
        streaming=True,
    ),
    ProviderCapabilities(
        provider="openai",
        model_prefix="gpt-4o",
        tool_use=True,
        structured_output=True,
        streaming=True,
    ),
    ProviderCapabilities(
        provider="openai",
        model_prefix="gpt-4-turbo",
        tool_use=True,
        structured_output=True,
        streaming=True,
    ),
    ProviderCapabilities(
        provider="openai",
        model_prefix="gpt-4",
        tool_use=True,
        structured_output=False,  # Pre-`gpt-4o`: JSON mode only, no strict schema
        streaming=True,
        notes=(
            "gpt-4 (pre-4o) lacks strict JSON-Schema response format. "
            "Schema validation may fail on edge cases.",
        ),
    ),
    ProviderCapabilities(
        provider="openai",
        model_prefix="gpt-3.5",
        tool_use=True,
        structured_output=False,
        streaming=True,
        notes=(
            "gpt-3.5 should not be used for stage agent runs — "
            "the staged outputs require strict schema enforcement.",
        ),
    ),
    # ---- Google Gemini ----
    ProviderCapabilities(
        provider="gemini",
        model_prefix="gemini-2.5",
        tool_use=True,
        structured_output=True,
        streaming=True,
    ),
    ProviderCapabilities(
        provider="gemini",
        model_prefix="gemini-2.0",
        tool_use=True,
        structured_output=True,
        streaming=True,
    ),
    ProviderCapabilities(
        provider="gemini",
        model_prefix="gemini-1.5",
        tool_use=True,
        structured_output=True,
        streaming=True,
        notes=(
            "Gemini 1.5 responseSchema budget is small — the langchain "
            "provider serializes via the Pydantic schema and works for "
            "most cases, but very large schemas may still fail.",
        ),
    ),
    # ---- Ollama ----
    #
    # Ollama is a runtime, not a model. Capabilities depend on the
    # underlying model. We catalog the most common ones and fall back
    # to a conservative "no tool use" default for unknown models.
    ProviderCapabilities(
        provider="ollama",
        model_prefix="llama3.1",
        tool_use=True,  # llama3.1+ supports tool calling
        structured_output=False,
        streaming=True,
        notes=(
            "Tool-use accuracy on Ollama-served llama3.1 is lower than "
            "on hosted Anthropic / OpenAI / Gemini models. Expect more "
            "tool-call validation errors.",
        ),
    ),
    ProviderCapabilities(
        provider="ollama",
        model_prefix="llama3.2",
        tool_use=True,
        structured_output=False,
        streaming=True,
    ),
    ProviderCapabilities(
        provider="ollama",
        model_prefix="qwen",
        tool_use=True,
        structured_output=False,
        streaming=True,
    ),
)


# Conservative fallback when no catalog entry matches: assume the bare
# minimum (streaming only). Downstream code paths can disable feature
# requirements they don't actually need by passing a custom
# ``required`` set to :func:`format_degradation_warnings`.
_FALLBACK_CAPABILITIES = ProviderCapabilities(
    provider="_unknown",
    model_prefix="_unknown",
    tool_use=False,
    structured_output=False,
    streaming=True,
    notes=("This (provider, model) combination is not in the capability catalog.",),
)


def assess_capabilities(provider: str, model: str) -> ProviderCapabilities:
    """Resolve the catalog entry for ``(provider, model)``.

    Picks the longest matching ``model_prefix`` within the requested
    provider so more-specific entries override less-specific ones.
    Returns ``_FALLBACK_CAPABILITIES`` when no entry matches — so the
    caller still gets a typed object back instead of having to handle
    ``None``.
    """
    candidates = [c for c in CAPABILITY_CATALOG if c.provider == provider]
    candidates.sort(key=lambda c: len(c.model_prefix), reverse=True)
    for cand in candidates:
        if model.startswith(cand.model_prefix):
            return cand
    return _FALLBACK_CAPABILITIES


# ---------------------------------------------------------------------------
# Required capabilities per usage profile
# ---------------------------------------------------------------------------


_AGENT_LOOP_REQUIREMENTS: Sequence[str] = (
    "tool_use",
    "structured_output",
)
"""Hard requirements for the multi-turn agent-loop CLI path.

A run that lacks these will misbehave (no tool calls = unsupervised
text generation; no structured output = schema-validation failures
on every stage). Surface as warnings, not errors, so users on
unusual models can still opt into the run with their eyes open.
"""

_STAGED_PIPELINE_REQUIREMENTS: Sequence[str] = ("structured_output",)
"""Hard requirements for the single-shot staged pipeline.

The staged path doesn't strictly require tool use (each stage is one
LLM call), but it does need structured-output enforcement so the
Pydantic stage models validate cleanly.
"""

_USAGE_PROFILE_REQUIREMENTS: Dict[str, Sequence[str]] = {
    "agent_loop": _AGENT_LOOP_REQUIREMENTS,
    "staged_pipeline": _STAGED_PIPELINE_REQUIREMENTS,
}


def required_capabilities_for(usage_profile: str) -> Sequence[str]:
    """Return the capability field names required for ``usage_profile``."""
    return _USAGE_PROFILE_REQUIREMENTS.get(usage_profile, _STAGED_PIPELINE_REQUIREMENTS)


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


def format_degradation_warnings(
    *,
    provider: str,
    model: str,
    usage_profile: str = "staged_pipeline",
    required: Optional[Iterable[str]] = None,
) -> List[str]:
    """Return a list of human-readable warning strings for the
    (provider, model) combination.

    Empty list ⇒ everything required is supported. Otherwise a list
    of one or more bullet-point lines suitable for Rich / plain
    console output. Callers decide whether to print, log, or both.

    The catalog ``notes`` field is *always* surfaced (even when no
    requirement is missing) because a note like "tool-use accuracy is
    lower on Ollama" is information the user should see regardless of
    whether the run profile happens to need tool use.
    """
    caps = assess_capabilities(provider, model)
    req = list(required) if required is not None else list(required_capabilities_for(usage_profile))

    warnings: List[str] = []

    for field_name in req:
        if not getattr(caps, field_name, False):
            warnings.append(
                f"{provider}/{model} does not reliably support "
                f"{field_name.replace('_', ' ')} — agent runs may "
                f"produce degraded output."
            )

    # Unknown combos always get a warning so the user knows the
    # capability matrix isn't authoritative for them.
    if caps is _FALLBACK_CAPABILITIES:
        warnings.append(
            f"{provider}/{model} is not in the capability catalog. "
            "Behaviour is best-effort; consider adding an entry to "
            "fluid_build.copilot.agents.capability_catalog."
        )

    # Surface catalog notes as bullets so users see the operational
    # caveats that come with their pick.
    for note in caps.notes:
        warnings.append(f"Note for {provider}/{model}: {note}")

    return warnings


def emit_degradation_warnings(
    *,
    provider: str,
    model: str,
    usage_profile: str = "staged_pipeline",
    required: Optional[Iterable[str]] = None,
    quiet: bool = False,
) -> List[str]:
    """Compute :func:`format_degradation_warnings` and print each line
    via the standard CLI console (so it gets the usual Rich styling
    + the secret-redaction filter from
    :mod:`fluid_build.cli.console`).

    Returns the warnings list (possibly empty) for callers that also
    want to attach it to telemetry. ``quiet=True`` suppresses the
    print but still returns the list — useful in tests and in the
    ``FLUID_QUIET`` / ``FLUID_NONINTERACTIVE`` paths where the
    caller already gates console output.
    """
    warnings = format_degradation_warnings(
        provider=provider,
        model=model,
        usage_profile=usage_profile,
        required=required,
    )
    if warnings and not quiet:
        # Imported lazily to avoid a hard dep on ``rich``/console
        # plumbing for users of the catalog who never want the print.
        from fluid_build.cli import console as _console  # noqa: PLC0415

        for line in warnings:
            _console.warning(line)
    return warnings
