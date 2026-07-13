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

Catalog source of truth
-----------------------

The catalog is built from two tiers, in order:

1. **Family overlay** (``_FAMILY_OVERLAY``) — hand-curated entries
   keyed on the model *family* prefix (``claude-3-5-sonnet``,
   ``claude-3``, ``gemma``, ``phi`` …). This is the only place the
   rich, non-catalogued fields live: ``prompt_caching``,
   ``extended_thinking``, ``notes``.
2. **JSON-derived entries** — every model id under
   ``fluid_build/cli/llm_models.json::providers.<p>.models[]`` is
   reflected into the catalog with its ``capabilities`` dict (tool
   use, structured output, streaming). ``prompt_caching`` /
   ``extended_thinking`` are filled in from
   ``_PROVIDER_FIELD_DEFAULTS`` because the JSON catalog doesn't
   carry those fields *yet* — see the docstring on
   ``_PROVIDER_FIELD_DEFAULTS`` for a TODO link back to the catalog
   refresh script.

Borrows the JSON-driven "static capability lookup" pattern from
`BerriAI/litellm
<https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json>`_
and OpenRouter's `/api/v1/models` endpoint — both expose
``supports_function_calling`` / ``supports_response_schema`` /
``supports_vision`` as boolean flags on each model id. Adapted (not
copied) because the litellm registry is 1.4MB and keyed by model-id
only, whereas this module needs family-prefix matching and a small
overlay for the fields that aren't yet in our weekly refresh.

The merge means: when ``scripts/update_model_catalog.py`` adds a new
model id to ``llm_models.json``, it shows up here automatically —
no edit to this file needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CAPABILITY_CATALOG",
    "ProviderCapabilities",
    "_build_capability_catalog",
    "_reset_capability_cache",
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
# Per-provider defaults for fields the JSON catalog doesn't carry.
#
# ``llm_models.json::providers.<p>.models[].capabilities`` only carries
# the three flags the weekly refresh script populates today:
# ``tool_use``, ``structured_output``, ``streaming``.
#
# ``ProviderCapabilities`` has two more fields that affect behaviour:
# ``prompt_caching`` (Anthropic-only today) and ``extended_thinking``
# (Anthropic Opus / OpenAI o-series). Until the weekly refresh script
# learns about them, this overlay reflects "what's true for *every*
# model in the provider that the catalog itself hasn't disproved".
#
# TODO(catalog-refresh): teach ``scripts/update_model_catalog.py`` to
# emit ``prompt_caching`` and ``extended_thinking`` directly so this
# overlay can shrink to per-family overrides only.
# ---------------------------------------------------------------------------

_PROVIDER_FIELD_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "anthropic": {"prompt_caching": True, "extended_thinking": False},
    "openai": {"prompt_caching": False, "extended_thinking": False},
    "gemini": {"prompt_caching": False, "extended_thinking": False},
    "ollama": {"prompt_caching": False, "extended_thinking": False},
}


# ---------------------------------------------------------------------------
# Family overlay
#
# Hand-curated, family-prefix entries that:
#
# * Carry the rich fields ``prompt_caching`` / ``extended_thinking`` /
#   ``notes`` that the JSON catalog doesn't have.
# * Cover the family prefixes consumers depend on (``claude-3``,
#   ``gemma``, ``phi``, ``qwen`` …) even when no exact model-id with
#   that prefix is in the JSON catalog yet.
#
# Sorted most-specific → least-specific within a provider so a quick
# read shows the override hierarchy. ``assess_capabilities`` does the
# actual longest-prefix match.
# ---------------------------------------------------------------------------

_FAMILY_OVERLAY: Tuple[ProviderCapabilities, ...] = (
    # ---- Anthropic ----
    ProviderCapabilities(
        provider="anthropic",
        model_prefix="claude-opus-4-7",
        tool_use=True,
        structured_output=True,
        streaming=True,
        prompt_caching=True,
        extended_thinking=True,
        notes=("Temperature is deprecated on Opus 4.7 — providers drop it automatically.",),
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
        model_prefix="qwen3-coder",
        tool_use=True,
        structured_output=False,
        streaming=True,
        notes=(
            "qwen3-coder is tuned for code generation; tool-use latency is "
            "higher than llama3.x but accuracy on structured args is better.",
        ),
    ),
    ProviderCapabilities(
        provider="ollama",
        model_prefix="qwen3",
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
    ProviderCapabilities(
        provider="ollama",
        model_prefix="gemma4",
        tool_use=True,
        structured_output=False,
        streaming=True,
        notes=(
            "gemma4 is the project's default Ollama model. Tool-use accuracy "
            "is acceptable for the staged pipeline; the multi-turn agent loop "
            "may need more iterations to converge than on hosted providers.",
        ),
    ),
    ProviderCapabilities(
        provider="ollama",
        model_prefix="gemma3",
        tool_use=True,
        structured_output=False,
        streaming=True,
    ),
    ProviderCapabilities(
        provider="ollama",
        model_prefix="gemma2",
        tool_use=False,  # gemma2 predates tool-calling support
        structured_output=False,
        streaming=True,
        notes=(
            "gemma2 does not support tool calling. Use gemma3+ if you need "
            "the multi-turn agent loop on Ollama.",
        ),
    ),
    ProviderCapabilities(
        provider="ollama",
        model_prefix="gemma",
        tool_use=False,
        structured_output=False,
        streaming=True,
        notes=(
            "Original Gemma (1.x) does not support tool calling. Use gemma3+ for the agent loop.",
        ),
    ),
    ProviderCapabilities(
        provider="ollama",
        model_prefix="mistral",
        tool_use=True,
        structured_output=False,
        streaming=True,
    ),
    ProviderCapabilities(
        provider="ollama",
        model_prefix="mixtral",
        tool_use=True,
        structured_output=False,
        streaming=True,
    ),
    ProviderCapabilities(
        provider="ollama",
        model_prefix="deepseek",
        tool_use=True,
        structured_output=False,
        streaming=True,
    ),
    ProviderCapabilities(
        provider="ollama",
        model_prefix="phi",
        tool_use=False,  # Phi family is too small to call tools reliably
        structured_output=False,
        streaming=True,
        notes=(
            "Phi-family models are too small for reliable tool calling. "
            "Use them for completion-style prompts only.",
        ),
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


# ---------------------------------------------------------------------------
# Catalog build (overlay + JSON-derived entries)
# ---------------------------------------------------------------------------

# Module-scope cache. Tests can null it via ``_reset_capability_cache``.
_CAPABILITY_CATALOG_CACHE: Optional[Tuple[ProviderCapabilities, ...]] = None


def _reset_capability_cache() -> None:
    """Force the next ``CAPABILITY_CATALOG`` / ``assess_capabilities``
    call to rebuild from scratch.

    Tests use this after patching ``_resolve_load_model_catalog`` so
    the freshly mocked JSON catalog is reflected in the build output.
    """
    global _CAPABILITY_CATALOG_CACHE  # noqa: PLW0603
    _CAPABILITY_CATALOG_CACHE = None


def _build_capability_catalog() -> Tuple[ProviderCapabilities, ...]:
    """Build the full capability catalog by merging the family overlay
    with JSON-catalog-derived per-model entries.

    Algorithm:

    1. Start with :data:`_FAMILY_OVERLAY` — this is authoritative for
       every prefix it covers (carries ``prompt_caching`` /
       ``extended_thinking`` / ``notes``).
    2. For every model id under
       ``llm_models.json::providers.<p>.models[]``, append a derived
       :class:`ProviderCapabilities` *unless* the family overlay
       already covers the same (provider, prefix) — preventing the
       weekly refresh from clobbering hand-curated entries.

    The overlay-first resolution in :func:`assess_capabilities` means
    JSON-derived entries only get reached for model ids whose family
    isn't covered by the overlay (e.g. a future ``claude-sonnet-5-0``
    that the weekly refresh adds before anyone updates the overlay).
    The derived entries also serve as a registration manifest — when
    consumers iterate ``CAPABILITY_CATALOG`` looking for "every
    known model id", they see the JSON ids too.

    Falls back to the overlay alone when the JSON catalog can't be
    loaded (e.g. import-time failure in a stripped install).
    """
    entries: List[ProviderCapabilities] = list(_FAMILY_OVERLAY)

    catalog = _safe_load_json_catalog()
    if not catalog:
        return tuple(entries)

    # (provider, model_prefix) pairs already populated by the overlay.
    overlay_keys = {(c.provider, c.model_prefix) for c in _FAMILY_OVERLAY}

    providers = catalog.get("providers") or {}
    if not isinstance(providers, Mapping):
        return tuple(entries)

    for provider_name, provider_entry in providers.items():
        if not isinstance(provider_entry, Mapping):
            continue
        models = provider_entry.get("models") or []
        if not isinstance(models, list):
            continue
        defaults = _PROVIDER_FIELD_DEFAULTS.get(provider_name, {})
        for model in models:
            if not isinstance(model, Mapping):
                continue
            model_id = model.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            key = (provider_name, model_id)
            if key in overlay_keys:
                # Overlay already speaks for this exact (provider,
                # model_id) — don't append a duplicate.
                continue
            caps_dict = model.get("capabilities") or {}
            if not isinstance(caps_dict, Mapping):
                caps_dict = {}
            entries.append(
                ProviderCapabilities(
                    provider=provider_name,
                    model_prefix=model_id,
                    tool_use=bool(caps_dict.get("tool_use", False)),
                    structured_output=bool(caps_dict.get("structured_output", False)),
                    streaming=bool(caps_dict.get("streaming", True)),
                    prompt_caching=bool(defaults.get("prompt_caching", False)),
                    extended_thinking=bool(
                        _extended_thinking_default(provider_name, model_id, defaults)
                    ),
                )
            )

    return tuple(entries)


def _overlay_keys() -> frozenset:
    """Set of ``(provider, model_prefix)`` pairs that originate in the
    hand-curated family overlay (not the JSON catalog).

    Used by :func:`assess_capabilities` to give overlay entries
    priority over JSON-derived entries when both match — this
    preserves the "family-prefix attribution" semantics that
    downstream consumers rely on (e.g. tests pinning
    ``caps.model_prefix == 'claude-haiku-4-5'`` for
    ``'claude-haiku-4-5-20251001'``).
    """
    return frozenset((c.provider, c.model_prefix) for c in _FAMILY_OVERLAY)


def _extended_thinking_default(
    provider: str, model_id: str, provider_defaults: Mapping[str, Any]
) -> bool:
    """Pick a sensible ``extended_thinking`` default for a JSON-catalog
    entry that doesn't carry that field.

    Family rules — these mirror the family overlay so the JSON-derived
    entries for the same family stay consistent:

    * Anthropic ``claude-opus-4-*`` / ``claude-sonnet-4-7*`` → True.
    * OpenAI ``o1`` / ``o3`` / ``o4`` → True.
    * Otherwise: provider default (currently False everywhere).
    """
    if provider == "anthropic":
        if model_id.startswith("claude-opus-4-") or model_id.startswith("claude-sonnet-4-7"):
            return True
    elif provider == "openai":
        if model_id.startswith(("o1", "o3", "o4")):
            return True
    return bool(provider_defaults.get("extended_thinking", False))


def _safe_load_json_catalog() -> Optional[Dict[str, Any]]:
    """Load the JSON model catalog via the canonical resolver.

    Returns ``None`` on any failure (the build then falls back to the
    overlay alone — strictly worse than the merged catalog but never
    raises out of import).
    """
    try:
        from fluid_build.cli._llm_model_catalog import _resolve_load_model_catalog
    except Exception:  # pragma: no cover — defensive against import order
        return None
    try:
        loader = _resolve_load_model_catalog()
        data = loader()
    except Exception:  # noqa: BLE001 — defensive
        return None
    if isinstance(data, dict):
        return data
    return None


def _ensure_catalog() -> Tuple[ProviderCapabilities, ...]:
    """Return the cached catalog, building it on first call."""
    global _CAPABILITY_CATALOG_CACHE  # noqa: PLW0603
    if _CAPABILITY_CATALOG_CACHE is None:
        _CAPABILITY_CATALOG_CACHE = _build_capability_catalog()
    return _CAPABILITY_CATALOG_CACHE


class _CapabilityCatalogProxy(tuple):  # type: ignore[type-arg]
    """Tuple-shaped lazy proxy for the capability catalog.

    Backward-compat shim: every existing call site does either
    ``for entry in CAPABILITY_CATALOG`` or ``len(CAPABILITY_CATALOG)``
    or ``CAPABILITY_CATALOG[i]`` — all of which work on this subclass
    because we delegate ``__iter__`` / ``__len__`` / ``__getitem__``
    to the freshly rebuilt tuple. The class itself is a tuple, so
    ``isinstance(CAPABILITY_CATALOG, tuple)`` is True for callers that
    inspect it.

    The proxy stays empty internally — every access funnels through
    :func:`_ensure_catalog`, which honours
    :func:`_reset_capability_cache` between calls.
    """

    def __new__(cls):
        return super().__new__(cls)

    def __iter__(self):
        return iter(_ensure_catalog())

    def __len__(self) -> int:  # type: ignore[override]
        return len(_ensure_catalog())

    def __getitem__(self, index):  # type: ignore[override]
        return _ensure_catalog()[index]

    def __contains__(self, item) -> bool:  # type: ignore[override]
        return item in _ensure_catalog()

    def __repr__(self) -> str:  # pragma: no cover — debug aid only
        return f"CAPABILITY_CATALOG({len(_ensure_catalog())} entries, lazy)"

    def __eq__(self, other) -> bool:  # type: ignore[override]
        if isinstance(other, _CapabilityCatalogProxy):
            return tuple(self) == tuple(other)
        return tuple(self) == other

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(tuple(self))


# Backward-compat: ``CAPABILITY_CATALOG`` is still importable as a
# tuple-shaped object. Iteration / indexing / ``len`` work as before
# but the contents are now built lazily from the family overlay +
# JSON catalog merge.
CAPABILITY_CATALOG: Tuple[ProviderCapabilities, ...] = _CapabilityCatalogProxy()  # type: ignore[assignment]


def assess_capabilities(provider: str, model: str) -> ProviderCapabilities:
    """Resolve the catalog entry for ``(provider, model)``.

    Two-tier longest-prefix resolution:

    1. Try the **family overlay** first — longest-prefix match within
       the hand-curated entries. Preserves family-level attribution
       (so ``claude-haiku-4-5-20251001`` resolves to the
       ``claude-haiku-4-5`` family, not the JSON-derived per-id
       entry).
    2. Only when the overlay doesn't match for this ``(provider,
       model)`` pair do we fall through to JSON-derived entries —
       which exist precisely for "new model id in a family the
       overlay hasn't been updated for yet".

    Returns ``_FALLBACK_CAPABILITIES`` when neither tier matches —
    callers always get a typed object back instead of having to
    handle ``None``.
    """
    all_entries = _ensure_catalog()
    overlay_keys = _overlay_keys()

    # Tier 1: overlay only.
    overlay_candidates = [
        c
        for c in all_entries
        if c.provider == provider and (c.provider, c.model_prefix) in overlay_keys
    ]
    overlay_candidates.sort(key=lambda c: len(c.model_prefix), reverse=True)
    for cand in overlay_candidates:
        if model.startswith(cand.model_prefix):
            return cand

    # Tier 2: JSON-derived fallback (only reached when no overlay match).
    derived_candidates = [
        c
        for c in all_entries
        if c.provider == provider and (c.provider, c.model_prefix) not in overlay_keys
    ]
    derived_candidates.sort(key=lambda c: len(c.model_prefix), reverse=True)
    for cand in derived_candidates:
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
        # Points at the tier-0 ``_console`` leaf (not the ``cli.console``
        # re-export shim) so ``copilot`` carries no ``cli`` edge.
        from fluid_build import _console  # noqa: PLC0415

        for line in warnings:
            _console.warning(line)
    return warnings
