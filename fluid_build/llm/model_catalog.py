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

"""LLM model catalog query helpers — physical extraction.

Lifted from ``cli/forge_copilot_llm_providers.py`` (the host file
was 1687 LOC; the model-catalog block is ~220 LOC of pure JSON-walk
helpers with no side-effects beyond a process-local cache).

Responsibilities:

* Load the bundled ``llm_models.json`` (with optional user override
  at ``~/.fluid/llm_models.json``).
* Answer "what's the default model for OpenAI?", "what's the routing
  model for Anthropic?", "does this Gemini model support tool use?"

The catalog is the single source of truth — every other module that
needs a model hint reads through these helpers, never inlines a
constant.

``forge_copilot_llm_providers.py`` re-imports each function at module
top so existing test patches that target
``fluid_build.llm.providers.<helper>`` keep
resolving via the namespace.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

LOG = logging.getLogger("fluid.cli.llm_catalog")

# Role (the copilot's external vocabulary) -> tier (the catalog's vocabulary),
# used by :func:`default_model_for`. Kept module-level so tests can assert the
# mapping is stable.
_ROLE_TO_TIER: Dict[str, str] = {
    "default": "balanced",
    "fast": "fast",
    "deep": "deep",
}

# Provider aliases. Legacy callers pass ``"claude"`` as a synonym for
# ``"anthropic"``; normalising at the boundary keeps the catalog lookup keyed by
# the canonical provider name without a duplicate ``llm_models.json`` entry.
_PROVIDER_ALIASES: Dict[str, str] = {
    "claude": "anthropic",
}

# Process-local cache. Populated on first ``_load_model_catalog`` call.
_model_catalog_cache: Optional[Dict[str, Any]] = None


def _reset_catalog_cache() -> None:
    """Reset the catalog cache. Tests use this to force a re-load
    after patching the bundled file."""
    global _model_catalog_cache  # noqa: PLW0603
    _model_catalog_cache = None


def _resolve_load_model_catalog():
    """Resolve ``_load_model_catalog`` via the canonical host
    namespace so test patches on
    ``fluid_build.llm.providers._load_model_catalog``
    flow through to the catalog query helpers.

    The host is now :mod:`fluid_build.llm.providers`; the legacy
    ``cli.forge_copilot_llm_providers`` path is a ``sys.modules`` alias of it
    (same object), so patches applied via either import path are honoured."""
    try:
        from fluid_build.llm import providers as _host
    except Exception:  # pragma: no cover — defensive
        return _load_model_catalog
    return getattr(_host, "_load_model_catalog", _load_model_catalog)


def _get_cache_holder():
    """Return the module that owns ``_model_catalog_cache``.

    Tests reset the cache by setting
    ``forge_copilot_llm_providers._model_catalog_cache = None``. The
    cache is logically per-process; we honour the host module as the
    single source of truth so test resets work regardless of which
    name the test imports.
    """
    try:
        from fluid_build.llm import providers as _host

        if hasattr(_host, "_model_catalog_cache"):
            return _host
    except Exception:  # pragma: no cover — defensive
        pass
    import sys

    return sys.modules[__name__]


def _load_model_catalog() -> Dict[str, Any]:
    """Load the model catalog with a two-tier resolution.

    1. ``~/.fluid/llm_models.json`` (user override — checked first)
    2. ``fluid_build/cli/llm_models.json`` (bundled baseline)

    Cache lives on the host module (``forge_copilot_llm_providers``)
    so test resets via that namespace work end-to-end.
    """
    holder = _get_cache_holder()
    cached = getattr(holder, "_model_catalog_cache", None)
    if cached is not None:
        return cached

    # Tier 1: user override
    user_catalog = Path.home() / ".fluid" / "llm_models.json"
    if user_catalog.is_file():
        try:
            data = json.loads(user_catalog.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("providers"):
                holder._model_catalog_cache = data
                LOG.debug("Loaded user model catalog from %s", user_catalog)
                return data
        except Exception as exc:  # noqa: BLE001
            LOG.debug("User catalog at %s unreadable: %s", user_catalog, exc)

    # Tier 2: bundled baseline. The catalog JSON ships as ``cli`` package data
    # (it is refreshed weekly by ``.github/workflows/update-model-catalog.yml``,
    # which writes ``cli/llm_models.json``); locate it by a package-relative
    # path — a data-file reference, not a ``cli`` code import.
    bundled_path = Path(__file__).resolve().parent.parent / "cli" / "llm_models.json"
    try:
        loaded = json.loads(bundled_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Could not load model catalog %s: %s", bundled_path, exc)
        loaded = {}
    holder._model_catalog_cache = loaded
    return loaded


def _default_routing_model(provider_name: str, strong_model: str) -> Optional[str]:
    """Return the catalog's routing model for *provider_name*.

    Reads the ``routing`` field from the catalog (v2 schema). Returns
    ``None`` when no cheaper alternative is available or when the
    routing model would be the same as the strong model.
    """
    catalog = _resolve_load_model_catalog()()
    entry = catalog.get("providers", {}).get(provider_name, {})
    routing = entry.get("routing")
    if routing and routing != strong_model:
        return routing
    return None


def get_catalog_default(provider: str) -> Optional[str]:
    """Return the catalog's default model for *provider*."""
    catalog = _resolve_load_model_catalog()()
    entry = catalog.get("providers", {}).get(provider)
    if entry:
        return entry.get("default") or entry.get("flagship")
    return None


def get_catalog_routing_model(provider_name: str, strong_model: str = "") -> Optional[str]:
    """Return the catalog routing model when it differs from *strong_model*."""
    return _default_routing_model(provider_name, strong_model or "")


def get_explicit_catalog_tier(provider_name: str, tier: str) -> Optional[str]:
    """Return the catalog's ``tier`` model ONLY when explicitly defined.

    ``get_catalog_tier_model(provider, tier)`` silently falls back to
    the provider's flagship/default when ``tier`` is not configured.
    That's the wrong contract for "cheap-tier" ladders — we want a
    clear "yes this tier is set" signal, not a silent escalation to
    the most-expensive model.

    Checks both the catalog ``tiers`` section and the provider entry
    for an explicit key. Returns None when the tier isn't actually
    configured anywhere (so the caller can fall through to the next
    rung of its own ladder).

    Pinned by ``tests/copilot/agents/test_judge_cheap_tier_default.py``
    (the live-test discovery that surfaced the silent-flagship-fallback
    bug — JudgeAgent was using gemini-2.5-pro instead of gemini-2.5-flash
    because ``get_catalog_tier_model("gemini","judge")`` silently
    escalated to flagship when no "judge" tier existed).
    """
    try:
        catalog = _resolve_load_model_catalog()()
    except Exception:  # noqa: BLE001 — defensive
        return None
    if not isinstance(catalog, dict):
        return None
    tier_entry = catalog.get("tiers", {}).get(provider_name, {})
    if tier in tier_entry and tier_entry[tier]:
        return str(tier_entry[tier])
    entry = catalog.get("providers", {}).get(provider_name, {})
    if tier in entry and entry[tier]:
        return str(entry[tier])
    return None


def get_catalog_tier_model(provider_name: str, tier: str = "flagship") -> Optional[str]:
    """Return the model for a given tier.

    Catalogs with a ``tiers`` section are keyed by ``deep`` / ``balanced``
    / ``fast``. A user-supplied catalog (``~/.fluid/llm_models.json``)
    without a ``tiers`` section is keyed by ``flagship`` / ``balanced``
    / ``routing`` on the provider entry, so the tier name is mapped to
    that shape as a fallback.
    """
    catalog = _resolve_load_model_catalog()()
    tier_entry = catalog.get("tiers", {}).get(provider_name, {})
    if tier in tier_entry:
        return tier_entry.get(tier)
    entry = catalog.get("providers", {}).get(provider_name, {})
    provider_keyed_tier = {
        "deep": "flagship",
        "balanced": "balanced",
        "fast": "routing",
    }.get(tier, tier)
    return (
        entry.get(provider_keyed_tier)
        or entry.get(tier)
        or entry.get("flagship")
        or entry.get("default")
    )


def get_catalog_tier_models(provider_name: str) -> Dict[str, str]:
    """Return non-empty configured tier models for *provider_name*.

    Whitespace-only and empty-string tier values are treated as
    missing (effectively unset). The result is intentionally small
    and explicit: only ``deep``, ``balanced``, and ``fast`` are
    returned, with provider-schema fallback for older catalog shapes.
    """
    result: Dict[str, str] = {}
    for tier in ("deep", "balanced", "fast"):
        model = get_catalog_tier_model(provider_name, tier)
        if isinstance(model, str) and model.strip():
            result[tier] = model.strip()
    return result


def has_distinct_tier_models(provider_name: str) -> bool:
    """Return ``True`` when the catalog declares at least two distinct
    tier models for *provider_name*."""
    models = get_catalog_tier_models(provider_name)
    return len({m for m in models.values() if m}) >= 2


def model_supports_structured_output(provider_name: str, model: str) -> bool:
    """Return True when the catalog declares structured-output support
    for the (provider, model) pair."""
    return _model_has_capability(provider_name, model, "structured_output")


def model_supports_tool_use(provider_name: str, model: str) -> bool:
    """Return True when the catalog declares tool-use support for the
    (provider, model) pair."""
    return _model_has_capability(provider_name, model, "tool_use")


def _model_has_capability(provider_name: str, model: str, capability: str) -> bool:
    catalog = _resolve_load_model_catalog()()
    models = catalog.get("providers", {}).get(provider_name, {}).get("models") or []
    lower = (model or "").lower()
    for m in models:
        if lower == m["id"].lower() or lower in [a.lower() for a in (m.get("aliases") or [])]:
            return bool(m.get("capabilities", {}).get(capability, False))
    return False


def resolve_model_name(provider: str, user_input: str) -> str:
    """Resolve a potentially fuzzy model name to its canonical id.

    Returns *user_input* unchanged if no match is found (the API will
    decide whether it is valid).
    """
    text = (user_input or "").strip()
    if not text:
        return text
    catalog = _resolve_load_model_catalog()()
    models = catalog.get("providers", {}).get(provider, {}).get("models") or []
    lower = text.lower()
    for entry in models:
        if lower == entry["id"].lower():
            return entry["id"]
        for alias in entry.get("aliases") or []:
            if lower == alias.lower():
                return entry["id"]
    return text


def default_model_for(
    provider: str,
    role: str = "default",
    *,
    fallback: Optional[str] = None,
) -> Optional[str]:
    """Return the canonical model id for ``(provider, role)``.

    Resolution ladder (explicit-tier-only — never a silent escalation to the
    flagship):

    1. ``$FLUID_LLM_DEFAULT_MODEL_<PROVIDER>_<ROLE>`` env override (uppercase
       provider/role, dashes → underscores). Pin a model per workspace / CI.
    2. Catalog's *explicit* tier for the role-mapped tier
       (``role`` → ``tier`` via :data:`_ROLE_TO_TIER`).
    3. Catalog's *explicit* ``balanced`` tier — last-resort within the catalog so
       a provider missing one specific tier still resolves to a real model.
    4. ``fallback`` (default ``None``).

    The ``"claude"`` alias normalises to ``"anthropic"``. Uses
    :func:`get_explicit_catalog_tier` (returns ``None`` rather than escalating to
    the flagship) for both catalog rungs.

    Lives here (with the model catalog) rather than in ``copilot.models`` so the
    LLM-provider surface can resolve default models without a
    ``providers -> copilot`` back-edge; ``copilot.models`` re-exports it.
    """
    env_key = f"FLUID_LLM_DEFAULT_MODEL_{provider.upper().replace('-', '_')}_{role.upper()}"
    override = os.environ.get(env_key)
    if override:
        return override

    canonical_provider = _PROVIDER_ALIASES.get(provider.lower(), provider.lower())
    tier = _ROLE_TO_TIER.get(role.lower(), role.lower())

    explicit = get_explicit_catalog_tier(canonical_provider, tier)
    if explicit:
        return explicit
    # Last-resort within the catalog: the provider's ``balanced`` tier. NOT the
    # flagship — that's the silent-escalation antipattern we guard against.
    if tier != "balanced":
        balanced = get_explicit_catalog_tier(canonical_provider, "balanced")
        if balanced:
            return balanced
    return fallback


__all__ = [
    "_PROVIDER_ALIASES",
    "_ROLE_TO_TIER",
    "_default_routing_model",
    "_load_model_catalog",
    "_model_has_capability",
    "_reset_catalog_cache",
    "default_model_for",
    "get_catalog_default",
    "get_catalog_routing_model",
    "get_catalog_tier_model",
    "get_catalog_tier_models",
    "get_explicit_catalog_tier",
    "has_distinct_tier_models",
    "model_supports_structured_output",
    "model_supports_tool_use",
    "resolve_model_name",
]
