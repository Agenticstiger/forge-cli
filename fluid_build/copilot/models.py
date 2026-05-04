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

"""LLM model registry — single source of truth for default models per
provider, per role, plus a deprecated-list so error messages can hint
at the current canonical name when an operator passes an old one.

Replaces the previous pattern of ``"gpt-4.1-mini"`` / ``"claude-..."``
/ ``"gemini-2.5-flash"`` literals scattered across 30+ files. Adding a
new default here propagates to every site that calls
:func:`default_model_for`.

Roles:

* ``default`` — the everyday tool-use-capable model for forge.
* ``fast`` — small / cheap model for routing / interview / self-checks.
* ``deep`` — flagship model for hard reasoning steps.

The mapping intentionally lives in ONE place. Costs (USD per million
tokens) and context windows live in ``copilot.cost`` and
``copilot.agents.token_budget`` respectively — extend those tables in
lockstep when adding a new model.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

# Provider × role → model name. Add a row here, every consumer picks it up.
_DEFAULT_MODELS: Dict[str, Dict[str, str]] = {
    "openai": {
        "default": "gpt-4.1-mini",
        "fast": "gpt-4.1-mini",
        "deep": "gpt-4.1",
    },
    "anthropic": {
        "default": "claude-sonnet-4-6",
        "fast": "claude-haiku-4",
        "deep": "claude-opus-4",
    },
    "claude": {  # alias for anthropic — keep consumers stable
        "default": "claude-sonnet-4-6",
        "fast": "claude-haiku-4",
        "deep": "claude-opus-4",
    },
    "gemini": {
        "default": "gemini-2.5-flash",
        "fast": "gemini-2.5-flash",
        "deep": "gemini-2.5-pro",
    },
    "ollama": {
        "default": "gemma4",
        "fast": "gemma4",
        "deep": "llama3.1:70b",
    },
}


# Manual override for replacement suggestions. Most deprecations are
# detected dynamically via ``litellm.model_cost[name].deprecation_date``
# (litellm tracks this for major providers). Use this map only for
# replacements where heuristic-search would pick the wrong target.
_DEPRECATION_OVERRIDES: Dict[str, str] = {}


def _is_deprecated_in_litellm(model: str) -> bool:
    """True when litellm's model registry flags ``model`` as deprecated.

    ``litellm.model_cost`` carries a ``deprecation_date`` field for
    upstream-retired models (Gemini 1.x, Claude 3.x, etc.). We treat
    any present-but-non-empty value as "deprecated" — operators
    typically only see this list AFTER the upstream cutoff.
    """
    try:
        import litellm  # type: ignore

        rec = litellm.model_cost.get(model) or litellm.model_cost.get(
            f"{_provider_prefix_for(model)}/{model}"
        )
    except Exception:  # pragma: no cover — litellm optional
        return False
    if not rec:
        return False
    return bool(rec.get("deprecation_date"))


def _provider_prefix_for(model: str) -> str:
    """Best-effort guess at the litellm provider prefix from the model id.

    ``gemini-2.0-flash`` → ``gemini``; ``gpt-4o`` → ``openai``;
    ``claude-3-5-sonnet`` → ``anthropic``. Used to look up the
    namespaced row in ``litellm.model_cost`` (e.g. ``gemini/gemini-2.0-flash``).
    """
    m = model.lower()
    if m.startswith("gpt-") or m.startswith("o1") or m.startswith("o3"):
        return "openai"
    if m.startswith("claude-"):
        return "anthropic"
    if m.startswith("gemini-"):
        return "gemini"
    return ""


def _suggest_replacement_via_litellm(model: str) -> Optional[str]:
    """Find a non-deprecated sibling model with the same provider /
    family / size class. Returns ``None`` when no clean replacement
    can be inferred — caller falls back to the per-provider default
    in :data:`_DEFAULT_MODELS`.
    """
    try:
        import litellm  # type: ignore

        provider = _provider_prefix_for(model)
        if not provider:
            return None
        prefix = model.split("-")[0]  # e.g. ``gemini``, ``gpt``, ``claude``
        candidates = [
            name
            for name, rec in litellm.model_cost.items()
            if not isinstance(rec, dict) or rec.get("litellm_provider") == provider
        ]
        # Filter to the same family + a flash/mini/pro suffix match
        suffix_marker = ""
        for marker in ("flash", "mini", "pro", "opus", "sonnet", "haiku"):
            if marker in model:
                suffix_marker = marker
                break
        # Filter out variants the user didn't ask for (e.g. ``gemini-2.0-flash``
        # → don't suggest ``gemini-flash-lite-*`` because the source had
        # no "lite"). Same for "preview" / "experimental" / "thinking"
        # — keep stable releases only.
        excluded_when_absent = ("lite", "preview", "experimental", "thinking", "exp-")
        excluded_in_source = {tag for tag in excluded_when_absent if tag in model.lower()}
        live = [
            c
            for c in candidates
            if c.startswith(prefix)
            and (not suffix_marker or suffix_marker in c)
            and not (
                isinstance(litellm.model_cost.get(c), dict)
                and litellm.model_cost[c].get("deprecation_date")
            )
            and "/" not in c  # skip namespaced duplicates
            and all(
                tag not in c.lower()
                for tag in excluded_when_absent
                if tag not in excluded_in_source
            )
        ]
        if not live:
            return None
        # Prefer the canonical default when it's in the candidate set;
        # fall back to the highest-versioned alternative.
        canonical_default = default_model_for(provider, "default")
        if canonical_default and canonical_default in live:
            return canonical_default
        return max(live)
    except Exception:  # pragma: no cover
        return None


def default_model_for(
    provider: str,
    role: str = "default",
    *,
    fallback: Optional[str] = None,
) -> Optional[str]:
    """Return the canonical model id for ``(provider, role)``.

    ``$FLUID_LLM_DEFAULT_MODEL_<PROVIDER>_<ROLE>`` (uppercase, dashes
    to underscores) overrides the registry — set it once per
    workspace / per CI to pin a specific model. Otherwise the
    in-tree default applies.

    Returns ``fallback`` (default ``None``) when the provider isn't
    in the registry.
    """
    env_key = f"FLUID_LLM_DEFAULT_MODEL_{provider.upper().replace('-', '_')}_" f"{role.upper()}"
    override = os.environ.get(env_key)
    if override:
        return override
    return _DEFAULT_MODELS.get(provider.lower(), {}).get(role.lower(), fallback)


def deprecation_hint(model: str) -> Optional[str]:
    """Return the canonical replacement for a deprecated model id.

    Resolution order:

    1. **Explicit override** in :data:`_DEPRECATION_OVERRIDES` — for
       deprecations where the heuristic-search picks the wrong target.
    2. **litellm registry** — read ``litellm.model_cost[model].deprecation_date``
       to detect retirement, then sibling-search the same registry
       for a non-deprecated replacement (same provider, same suffix:
       ``flash``/``pro``/``mini``).
    3. **Per-provider default** in :data:`_DEFAULT_MODELS` — fallback
       when the sibling search can't find a clean match.

    Returns ``None`` when the model isn't deprecated.

    Used by error renderers so a 404 from Google for
    ``gemini-2.0-flash`` surfaces as "Try ``--llm-model
    gemini-2.5-flash`` instead" rather than the bare HTTP error.
    """
    if model in _DEPRECATION_OVERRIDES:
        return _DEPRECATION_OVERRIDES[model]
    if not _is_deprecated_in_litellm(model):
        return None
    suggestion = _suggest_replacement_via_litellm(model)
    if suggestion:
        return suggestion
    # Fallback: provider's canonical default.
    provider = _provider_prefix_for(model)
    return default_model_for(provider, "default") if provider else None


def all_supported_models() -> Dict[str, Dict[str, str]]:
    """Snapshot of the registry — used by ``fluid env --models`` and
    by the live-LLM smoke test gated on ``@pytest.mark.live_llm``."""
    return {provider: dict(roles) for provider, roles in _DEFAULT_MODELS.items()}


__all__ = [
    "default_model_for",
    "deprecation_hint",
    "all_supported_models",
]
