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

"""LLM model registry — thin role -> tier shim over ``cli/llm_models.json``.

History note: this module used to carry an in-tree ``_DEFAULT_MODELS``
dict mapping ``(provider, role) -> model_id``. That dict drifted (e.g.
``"claude-haiku-4"`` while the catalog had ``"claude-haiku-4-5-20251001"``,
``"claude-opus-4"`` while the catalog had ``"claude-opus-4-7"``) because
the catalog file is refreshed weekly by
``.github/workflows/update-model-catalog.yml`` and nobody was hand-syncing
the hardcoded duplicate.

The catalog is now the **single source of truth**. This module's only
job is to translate the copilot-internal *role* vocabulary
(``default`` / ``fast`` / ``deep``) into the catalog's *tier* vocabulary
(``balanced`` / ``fast`` / ``deep``) and probe via
:func:`get_explicit_catalog_tier` (which returns ``None`` rather than
silently escalating to the flagship — the safe primitive for ladders).

Borrow-before-build receipts (mirror of the judge_agent.py precedent):

* LiteLLM's ``model_cost`` table — used for deprecation_date probing in
  :func:`_is_deprecated_in_litellm` / :func:`_suggest_replacement_via_litellm`;
  does NOT carry a curated default-per-provider map, so we cannot lean
  on it for the role -> model resolution. We do lean on it for
  deprecation-hint sibling search (kept verbatim).
* No canonical "tier" vocabulary across ai-gateway projects (Helicone /
  Portkey / LiteLLM proxy / Inworld Router); the local
  ``deep / balanced / fast`` shape is the dominant cascading-tier
  pattern, just without a shared word list.

Roles (this module's external vocabulary, kept for backward compat
with every existing caller — DO NOT rename):

* ``default`` — the everyday tool-use-capable model for forge.
* ``fast`` — small / cheap model for routing / interview / self-checks.
* ``deep`` — flagship model for hard reasoning steps.

Role -> tier mapping (the actual catalog keys):

* ``default`` -> ``balanced``  (the catalog's "default for everyday use")
* ``fast``    -> ``fast``      (haiku / nano / flash-lite class)
* ``deep``    -> ``deep``      (opus / gpt-4.1 / pro class)

Costs (USD per million tokens) and context windows live in
``copilot.cost`` and ``copilot.agents.token_budget`` respectively —
extend those tables in lockstep when adding a new model.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

# Role (this module's vocabulary) -> tier (the catalog's vocabulary).
# Public via :func:`default_model_for`; kept module-level so tests can
# assert the mapping is stable.
_ROLE_TO_TIER: Dict[str, str] = {
    "default": "balanced",
    "fast": "fast",
    "deep": "deep",
}

# Provider aliases. Existing callers pass ``"claude"`` as a synonym for
# ``"anthropic"`` (e.g. legacy LLM-config defaults from older releases).
# Normalising at the function boundary keeps the catalog lookup keyed by
# the canonical provider name without duplicating an entry in
# ``llm_models.json``.
_PROVIDER_ALIASES: Dict[str, str] = {
    "claude": "anthropic",
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
    via :func:`default_model_for`.
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

    Resolution ladder (mirror of ``judge_agent.py``'s cheap-tier ladder
    — same explicit-tier-only posture as :data:`MODEL_PRICES_USD` in
    ``cost.py``):

    1. ``$FLUID_LLM_DEFAULT_MODEL_<PROVIDER>_<ROLE>`` env override
       (uppercase provider/role, dashes → underscores). Pin a specific
       model per workspace / per CI.
    2. Catalog's *explicit* tier for the role-mapped tier
       (``role`` → ``tier`` via :data:`_ROLE_TO_TIER`).
    3. Catalog's *explicit* ``balanced`` tier — last-resort within the
       catalog so a provider missing one specific tier still resolves
       to a real model rather than silently escalating to the flagship.
    4. ``fallback`` (default ``None``) — the caller's existing
       None-handling kicks in; pass an explicit ``fallback=`` when the
       call site needs a guaranteed string.

    The ``"claude"`` alias normalises to ``"anthropic"`` so legacy
    callers keep working without a duplicate catalog entry.

    NB: ``get_catalog_tier_model`` (the non-explicit variant) silently
    escalates to the flagship when the requested tier isn't defined —
    that's the wrong contract for a "default model" lookup, because it
    means a misspelled role returns the most expensive model. We use
    :func:`get_explicit_catalog_tier` for both rungs of the ladder.
    """
    env_key = f"FLUID_LLM_DEFAULT_MODEL_{provider.upper().replace('-', '_')}_{role.upper()}"
    override = os.environ.get(env_key)
    if override:
        return override

    canonical_provider = _PROVIDER_ALIASES.get(provider.lower(), provider.lower())
    tier = _ROLE_TO_TIER.get(role.lower(), role.lower())

    # Local import keeps this module importable in contexts where the
    # CLI package hasn't been initialised yet (e.g. pure-library tests).
    from fluid_build.cli._llm_model_catalog import get_explicit_catalog_tier

    explicit = get_explicit_catalog_tier(canonical_provider, tier)
    if explicit:
        return explicit
    # Last-resort within the catalog: the provider's ``balanced`` tier.
    # NOT the flagship — that's the silent-escalation antipattern we're
    # specifically guarding against. ``balanced`` is the cheapest
    # tool-use-capable rung; if it's also missing we return ``fallback``
    # and let the caller decide.
    if tier != "balanced":
        balanced = get_explicit_catalog_tier(canonical_provider, "balanced")
        if balanced:
            return balanced
    return fallback


def deprecation_hint(model: str) -> Optional[str]:
    """Return the canonical replacement for a deprecated model id.

    Resolution order:

    1. **Explicit override** in :data:`_DEPRECATION_OVERRIDES` — for
       deprecations where the heuristic-search picks the wrong target.
    2. **litellm registry** — read ``litellm.model_cost[model].deprecation_date``
       to detect retirement, then sibling-search the same registry
       for a non-deprecated replacement (same provider, same suffix:
       ``flash``/``pro``/``mini``).
    3. **Catalog default via** :func:`default_model_for` — fallback
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
    # Fallback: provider's canonical default (catalog-driven).
    provider = _provider_prefix_for(model)
    return default_model_for(provider, "default") if provider else None


def all_supported_models() -> Dict[str, Dict[str, str]]:
    """Snapshot of the role -> model mapping for every catalog provider.

    Used by ``fluid env --models`` and by the live-LLM smoke test gated
    on ``@pytest.mark.live_llm``. Built dynamically from the catalog so
    a new provider added to ``llm_models.json`` (or to the user-override
    catalog at ``~/.fluid/llm_models.json``) shows up without any code
    change here. Empty / missing tier values are skipped so the snapshot
    doesn't carry None placeholders.
    """
    from fluid_build.cli._llm_model_catalog import _resolve_load_model_catalog

    try:
        catalog = _resolve_load_model_catalog()()
    except Exception:  # noqa: BLE001 — defensive
        return {}
    providers = set(catalog.get("tiers", {}).keys()) | set(catalog.get("providers", {}).keys())
    result: Dict[str, Dict[str, str]] = {}
    for provider in sorted(providers):
        roles: Dict[str, str] = {}
        for role in _ROLE_TO_TIER:
            model = default_model_for(provider, role)
            if model:
                roles[role] = model
        if roles:
            result[provider] = roles
    return result


__all__ = [
    "default_model_for",
    "deprecation_hint",
    "all_supported_models",
]
