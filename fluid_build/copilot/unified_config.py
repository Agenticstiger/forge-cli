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

"""Unified ``~/.fluid/config.yaml`` config primitive (Mediocre #5).

Before this module: operator config was split across four files
plus env-vars plus OS keyring entries plus CLI flags:

* ``~/.fluid/ai_config.json`` — LLM provider config
* ``~/.fluid/sources.yaml`` — catalog source registry
* ``~/.fluid/prices.json`` — price-table override
* ``~/.fluid/copilot-memory.json`` — legacy memory snapshot
* OS keyring — sensitive credentials
* env-vars — provider hints (``ANTHROPIC_API_KEY`` …)
* CLI flags — per-invocation overrides

After this module: one ``~/.fluid/config.yaml`` with sections.
Per-feature files still work as fallback (no breaking change for
v1.5 users) but new operators land in the unified path on first
``fluid ai setup`` run.

Layered priority (highest wins):

1. CLI flags (per-invocation override).
2. Env-vars.
3. ``~/.fluid/config.yaml`` (unified — this module).
4. Per-feature legacy files (fallback for v1.5 installs).
5. Built-in defaults.

Use :func:`load_unified_config` to read; use
:func:`migrate_legacy_to_unified` to consolidate existing per-
feature files into the unified shape.

This module is **read-only on the legacy files**. Migration is
explicit (operator-invoked) so a v1.5 user upgrading to a future
release isn't surprised by their per-feature files getting
merged silently.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml
from pydantic import BaseModel, ConfigDict, Field

_log = logging.getLogger(__name__)


SCHEMA_VERSION = 1
"""Bumped when the unified-config shape changes incompatibly. Older
config files are still readable; new fields default to None /
empty. A v2 schema requires a migration step."""


# ---------------------------------------------------------------------
# Schema sections
# ---------------------------------------------------------------------


class LLMSection(BaseModel):
    """LLM provider config — replaces ``ai_config.json``."""

    model_config = ConfigDict(extra="allow")

    provider: Optional[str] = None
    """Default provider (``"anthropic"`` / ``"openai"`` / etc.)."""

    model: Optional[str] = None
    """Default model id."""

    tiered: bool = False
    """Whether tiered mode is on by default."""

    temperature: Optional[float] = None
    """Optional override; defaults to 0 in the provider layer."""


class SourceEntry(BaseModel):
    """One catalog-source registration — replaces a row in ``sources.yaml``."""

    model_config = ConfigDict(extra="allow")

    catalog: str
    """Catalog kind (``"snowflake"`` / ``"unity"`` / etc.)."""

    auth_method: Optional[str] = None
    """Auth method label (e.g. ``"key_pair"``, ``"pat"``, ``"adc"``)."""

    # All other fields (account, host, region, default_database, …)
    # are catalog-specific and accepted via ``extra="allow"``.


class SourcesSection(BaseModel):
    """Catalog-source registry — replaces ``sources.yaml``."""

    sources: Dict[str, SourceEntry] = Field(default_factory=dict)


class PricesSection(BaseModel):
    """Per-org price overrides — replaces ``prices.json``."""

    prices: Dict[str, Tuple[float, float]] = Field(default_factory=dict)
    """Map of model id → (input_usd_per_1M, output_usd_per_1M)."""


class BehaviorSection(BaseModel):
    """Run-level UX flags."""

    model_config = ConfigDict(extra="allow")

    quiet: bool = False
    """Default to ``--quiet`` mode at every CLI surface."""

    deterministic: bool = False
    """Default to ``--deterministic`` mode (temp=0, seed=42, cache off)."""

    cost_limit_usd_per_run: Optional[float] = None
    """Per-run cost ceiling in USD. When the running tracker total
    exceeds this number, the forge aborts with
    :class:`fluid_build.copilot.cost.CostLimitExceeded`. ``None``
    (default) disables the ceiling. Per-invocation override via
    ``$FLUID_COST_LIMIT_USD``."""


class UnifiedConfig(BaseModel):
    """Top-level shape of ``~/.fluid/config.yaml``."""

    schema_version: int = SCHEMA_VERSION
    llm: LLMSection = Field(default_factory=LLMSection)
    sources_section: SourcesSection = Field(
        default_factory=SourcesSection,
        alias="sources",
    )
    prices_section: PricesSection = Field(
        default_factory=PricesSection,
        alias="prices",
    )
    behavior: BehaviorSection = Field(default_factory=BehaviorSection)

    model_config = ConfigDict(populate_by_name=True)


# ---------------------------------------------------------------------
# Path resolution + load
# ---------------------------------------------------------------------


def unified_config_path() -> Path:
    """Resolve the unified config path.

    Precedence:

    1. ``$FLUID_CONFIG`` — explicit override (used by tests).
    2. ``$FLUID_HOME/config.yaml`` if ``$FLUID_HOME`` is set.
    3. ``~/.fluid/config.yaml`` (default).
    """
    explicit = os.environ.get("FLUID_CONFIG")
    if explicit:
        return Path(explicit)
    home = os.environ.get("FLUID_HOME")
    if home:
        return Path(home) / "config.yaml"
    return Path.home() / ".fluid" / "config.yaml"


def load_unified_config() -> Optional[UnifiedConfig]:
    """Read the unified config, or ``None`` when the file isn't present.

    Malformed YAML / failed Pydantic validation logs DEBUG and
    returns ``None`` so callers fall through to legacy per-feature
    readers without erroring.
    """
    path = unified_config_path()
    try:
        if not path.is_file():
            return None
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        _log.debug("failed to read %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return UnifiedConfig.model_validate(raw)
    except Exception as exc:  # pragma: no cover — defensive
        _log.debug("unified config at %s is malformed: %s", path, exc)
        return None


# ---------------------------------------------------------------------
# Migration from legacy per-feature files
# ---------------------------------------------------------------------


def _legacy_paths() -> Dict[str, Path]:
    """Map legacy-config-section name → path.

    Honours ``$FLUID_HOME`` for hermetic-test isolation; falls back
    to ``~/.fluid``. ``$FLUID_PRICES_JSON`` is also honoured for the
    prices file (same env var the cost module reads) so the
    migrator picks up an explicitly-pointed-at override.
    """
    fluid_home_env = os.environ.get("FLUID_HOME")
    base = Path(fluid_home_env) if fluid_home_env else Path.home() / ".fluid"
    prices_explicit = os.environ.get("FLUID_PRICES_JSON")
    return {
        "ai_config": base / "ai_config.json",
        "sources": base / "sources.yaml",
        "prices": Path(prices_explicit) if prices_explicit else base / "prices.json",
    }


def migrate_legacy_to_unified(
    *,
    target_path: Optional[Path] = None,
    overwrite: bool = False,
) -> Tuple[Path, List[str]]:
    """Consolidate legacy per-feature files into one ``config.yaml``.

    Reads ``~/.fluid/ai_config.json`` (LLM section),
    ``~/.fluid/sources.yaml`` (sources section), and
    ``~/.fluid/prices.json`` (prices section). Writes the merged
    result to ``target_path`` (defaults to
    :func:`unified_config_path`).

    Idempotent: running twice with no legacy file changes produces
    byte-identical output.

    Returns ``(written_path, source_files_consumed)`` so the CLI can
    print "consolidated 3 legacy files into ~/.fluid/config.yaml".

    Refuses to overwrite an existing target unless ``overwrite=True``
    so a re-run doesn't clobber operator edits to the unified file.
    """
    target = target_path or unified_config_path()
    if target.exists() and not overwrite:
        raise FileExistsError(
            f"{target} already exists. Pass overwrite=True to replace it, "
            "or delete the file first."
        )

    legacy = _legacy_paths()
    sources_consumed: List[str] = []

    cfg = UnifiedConfig()

    # LLM section — from ai_config.json.
    ai_cfg_path = legacy["ai_config"]
    if ai_cfg_path.is_file():
        try:
            payload = json.loads(ai_cfg_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                cfg.llm = LLMSection(
                    provider=payload.get("provider"),
                    model=payload.get("model"),
                    tiered=bool(payload.get("tiered", False)),
                    temperature=payload.get("temperature"),
                )
                sources_consumed.append(str(ai_cfg_path))
        except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover
            _log.debug("skipping malformed %s: %s", ai_cfg_path, exc)

    # Sources section — from sources.yaml.
    sources_path = legacy["sources"]
    if sources_path.is_file():
        try:
            payload = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
            raw_sources = (
                payload.get("sources")
                if isinstance(payload, dict) and isinstance(payload.get("sources"), dict)
                else (payload if isinstance(payload, dict) else {})
            )
            entries: Dict[str, SourceEntry] = {}
            for name, body in (raw_sources or {}).items():
                if isinstance(body, dict) and "catalog" in body:
                    entries[name] = SourceEntry.model_validate(body)
            if entries:
                cfg.sources_section = SourcesSection(sources=entries)
                sources_consumed.append(str(sources_path))
        except (OSError, yaml.YAMLError) as exc:  # pragma: no cover
            _log.debug("skipping malformed %s: %s", sources_path, exc)

    # Prices section — from prices.json.
    prices_path = legacy["prices"]
    if prices_path.is_file():
        try:
            payload = json.loads(prices_path.read_text(encoding="utf-8"))
            raw_prices = (
                payload.get("prices")
                if isinstance(payload, dict) and isinstance(payload.get("prices"), dict)
                else (payload if isinstance(payload, dict) else {})
            )
            ok: Dict[str, Tuple[float, float]] = {}
            for model_id, values in (raw_prices or {}).items():
                if (
                    isinstance(model_id, str)
                    and isinstance(values, (list, tuple))
                    and len(values) == 2
                ):
                    try:
                        a, b = float(values[0]), float(values[1])
                        if a >= 0 and b >= 0:
                            ok[model_id] = (a, b)
                    except (TypeError, ValueError):
                        continue
            if ok:
                cfg.prices_section = PricesSection(prices=ok)
                sources_consumed.append(str(prices_path))
        except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover
            _log.debug("skipping malformed %s: %s", prices_path, exc)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(
            cfg.model_dump(by_alias=True, exclude_unset=False),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return target, sources_consumed


__all__ = [
    "SCHEMA_VERSION",
    "LLMSection",
    "SourceEntry",
    "SourcesSection",
    "PricesSection",
    "BehaviorSection",
    "UnifiedConfig",
    "unified_config_path",
    "load_unified_config",
    "migrate_legacy_to_unified",
]
