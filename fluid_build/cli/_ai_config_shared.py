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

"""Shared AI-config *loading* — the tier-0 leaf both sides of the cycle depend on.

Why this module exists
----------------------
``cli/ai_setup.py`` and ``cli/forge_copilot_llm_providers.py`` used to form a
two-way dependency: ``forge_copilot_llm_providers.check_llm_readiness`` reached
into ``ai_setup`` for ``_load_ai_config`` (function-level import), while
``ai_setup`` depends on the whole LLM-provider surface. That is the classic
circular-import shape — and the canonical fix (see the Python "move the shared
code to a neutral third module" pattern) is to lift the genuinely-shared piece
into a leaf that **neither** side imports the other through.

The genuinely-shared piece is exactly the config *read* path: parse
``~/.fluid/ai_config.json`` (plus the ``~/.fluid/config.yaml`` unified-config
override) into the flat ``{"provider": .., "model": .., ...}`` shape every
caller expects. That logic lives here now, path-injected so it stays pure:

* :func:`load_ai_config` — the active provider's saved preferences (or ``None``).
* :func:`load_ai_config_map` — the full multi-provider view.
* :func:`load_ai_config_for` — a specific saved provider's entry.
* :func:`list_configured_providers` — sorted names of every saved provider.
* :func:`_read_config_file` / :func:`_normalize_config` — the raw read +
  shape-normalisation primitives the write path in
  ``cli/_ai_setup_storage.py`` also reuses.

Tier-0 leaf invariant
----------------------
This module must **not** import ``cli.ai_setup``, ``cli._ai_setup_storage``, or
``cli.forge_copilot_llm_providers`` — importing any of them would re-create the
cycle this module exists to break. The invariant is guarded by an
``import-linter`` forbidden contract (``pyproject.toml``), mirroring the
``fluid_build._net`` tier-0 contract. The only non-stdlib dependency is the
lazily-imported ``copilot.unified_config`` (itself a leaf: stdlib + yaml +
pydantic, no ``cli`` back-edges).

Backward-compat
---------------
``cli/ai_setup.py`` re-exports ``_CONFIG_DIR`` / ``_CONFIG_FILE`` from here, and
``cli/_ai_setup_storage.py``'s read helpers (``_load_ai_config`` &co.) delegate
to these functions — so every historical call site and ``patch(...)`` test seam
on the old names keeps resolving unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("fluid.cli.ai_config")

# ---------------------------------------------------------------------------
# Canonical config-file location (single source of truth)
# ---------------------------------------------------------------------------
#
# ``cli/ai_setup.py`` re-exports these two names, so the ~24 tests that
# ``patch("...cli.ai_setup._CONFIG_FILE", tmp)`` keep flowing through the
# storage layer's ``_config_file()`` indirection (which reads the *ai_setup*
# attribute). The path-injected read helpers below default to these constants
# only when no explicit ``config_file`` is supplied — that is the code path the
# keyless ``check_llm_readiness`` reader takes.
_CONFIG_DIR = Path.home() / ".fluid"
_CONFIG_FILE = _CONFIG_DIR / "ai_config.json"

# ``ai_config.json`` schema version. v2 is the multi-provider map
# ``{"version": 2, "active": <name>, "providers": {<name>: {<entry>}}}``.
# The pre-v2 single-provider shape ``{"provider": .., "model": ..}`` is still
# read transparently (see :func:`_normalize_config`) so upgrades never strand an
# existing config. The write path (``_ai_setup_storage._write_config_map``)
# imports this constant from here to keep the version in one place.
_CONFIG_SCHEMA_VERSION = 2

# Top-level keys that are NOT part of a per-provider entry — used when folding
# the legacy single-provider shape into a one-entry map.
_MAP_META_KEYS = frozenset({"provider", "version", "active", "providers", "domain_history"})


def _default_config_file() -> Path:
    """Return the module's canonical config-file path.

    Read via module-attribute access so a test that patches
    ``_ai_config_shared._CONFIG_FILE`` flows through here too.
    """
    return _CONFIG_FILE


# ---------------------------------------------------------------------------
# Raw read + shape normalisation (pure, path-injected)
# ---------------------------------------------------------------------------


def _read_config_file(config_file: Path) -> Optional[dict]:
    """Read + JSON-parse *config_file*.

    Returns the raw ``dict`` (either shape) or ``None`` when the file is absent,
    unreadable, or not a JSON object.
    """
    import json

    try:
        if not config_file.exists():
            return None
        data = json.loads(config_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _normalize_config(data: Optional[dict]) -> dict:
    """Normalise either config shape to ``{"active": name|None, "providers": {name: entry}}``.

    Accepts the v2 multi-provider map *and* the legacy single-provider shape
    (``{"provider": .., "model": ..}``), folding the latter into a one-entry map
    so every reader sees the same structure. Each ``entry`` holds the non-name
    fields (``model``, ``endpoint``, ``ollama_host``, ``tiered``, and — only
    under the opt-in plaintext fallback — ``api_key``).
    """
    if not isinstance(data, dict):
        return {"active": None, "providers": {}}

    raw_providers = data.get("providers")
    if isinstance(raw_providers, dict):
        providers = {
            name: dict(entry)
            for name, entry in raw_providers.items()
            if isinstance(name, str) and isinstance(entry, dict)
        }
        active = data.get("active")
        if active not in providers:
            active = next(iter(providers), None)
        return {"active": active, "providers": providers}

    # Legacy single-provider shape.
    provider = data.get("provider")
    if not isinstance(provider, str) or not provider:
        return {"active": None, "providers": {}}
    entry = {k: v for k, v in data.items() if k not in _MAP_META_KEYS}
    return {"active": provider, "providers": {provider: entry}}


# ---------------------------------------------------------------------------
# High-level loaders (the shared surface)
# ---------------------------------------------------------------------------


def load_ai_config(config_file: Optional[Path] = None) -> Optional[dict]:
    """Load the ACTIVE provider's saved AI preferences (back-compat shape).

    Returns the flat ``{"provider": .., "model": .., ...}`` dict every existing
    caller expects, or ``None`` when nothing is configured.

    Lookup order (unchanged from the historical ``_load_ai_config``):

    1. ``~/.fluid/config.yaml`` ``llm:`` section (unified path).
    2. ``config_file`` (defaults to ``~/.fluid/ai_config.json``) — the active
       entry of the multi-provider map (or the sole entry of a legacy file).
    3. ``None`` — no config saved yet.
    """
    # 1. Unified config — new operators.
    try:
        from fluid_build.copilot.unified_config import load_unified_config

        cfg = load_unified_config()
        if cfg is not None and cfg.llm and cfg.llm.provider:
            data: dict = {"provider": cfg.llm.provider}
            if cfg.llm.model:
                data["model"] = cfg.llm.model
            if cfg.llm.tiered:
                data["tiered"] = cfg.llm.tiered
            return data
    except Exception as exc:  # pragma: no cover — defensive
        LOG.debug("Could not load unified AI config: %s", exc)

    # 2. ``ai_config.json`` — active provider from the map.
    cf = config_file if config_file is not None else _default_config_file()
    normalized = _normalize_config(_read_config_file(cf))
    active = normalized["active"]
    if not active:
        return None
    entry = normalized["providers"].get(active) or {}
    return {"provider": active, **entry}


def load_ai_config_map(config_file: Optional[Path] = None) -> Optional[dict]:
    """Return the full multi-provider view, or ``None`` when unconfigured.

    Shape: ``{"active": <name>, "providers": {<name>: {"provider": <name>,
    "model": .., ...}}}`` — each entry is flattened to carry its own
    ``provider`` name so callers can iterate uniformly.
    """
    cf = config_file if config_file is not None else _default_config_file()
    normalized = _normalize_config(_read_config_file(cf))
    providers = normalized["providers"]
    if not providers:
        return None
    return {
        "active": normalized["active"],
        "providers": {name: {"provider": name, **entry} for name, entry in providers.items()},
    }


def load_ai_config_for(provider: str, config_file: Optional[Path] = None) -> Optional[dict]:
    """Return the flat config dict for a SPECIFIC saved provider, or ``None``.

    Used by the resolver so ``fluid forge --llm-provider <name>`` can pick the
    requested provider out of the map regardless of which one is active.
    """
    if not provider:
        return None
    cf = config_file if config_file is not None else _default_config_file()
    normalized = _normalize_config(_read_config_file(cf))
    entry = normalized["providers"].get(provider)
    if entry is None:
        return None
    return {"provider": provider, **entry}


def list_configured_providers(config_file: Optional[Path] = None) -> list:
    """Return the sorted names of every provider saved in the map."""
    cf = config_file if config_file is not None else _default_config_file()
    normalized = _normalize_config(_read_config_file(cf))
    return sorted(normalized["providers"].keys())
