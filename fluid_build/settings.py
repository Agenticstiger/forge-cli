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

"""Single source of truth for the FLUID_* environment variables FLUID
reads at runtime. Replaces the previous pattern of
``os.environ.get("FLUID_X")`` calls scattered across 100+ sites with
a typed accessor, a registry, and a ``fluid env`` introspection
surface.

Each entry carries:

* ``key`` — the canonical env var name.
* ``description`` — what it does, one line.
* ``default`` — the value when unset.
* ``cast`` — coercion function (``str``, ``int``, ``bool``, ``float``).
* ``category`` — one of ``cost``, ``ux``, ``llm``, ``security``,
  ``rollback``, ``runtime``, ``observability``, ``forge``.

The ``Settings`` proxy reads ``os.environ`` lazily so changes between
function calls are honoured (test suites monkey-patch env vars per
test). For one-shot lookups use :func:`get`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


def _to_bool(value: str) -> bool:
    """Accept both numeric (``1`` / ``0``) and word (``true`` / ``false``)
    forms. Anything else evaluates as ``True`` if non-empty — the
    historical ``$FLUID_QUIET=1`` shape stays compatible."""
    if value is None:
        return False
    v = str(value).strip().lower()
    if v in ("0", "false", "no", "off", ""):
        return False
    return True


def _to_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class _SettingSpec:
    """Single env var spec — registered once in ``_REGISTRY`` below."""

    key: str
    description: str
    default: Any
    cast: Callable[[str], Any] = str
    category: str = "runtime"


# ── Registry — alphabetised by category, then by key ──────────────────
# Adding a new FLUID_* var: append a row here. ``fluid env --list``
# enumerates the registry; tests assert that any new env var has a
# row (so every env var is documented + introspectable).

_REGISTRY: List[_SettingSpec] = [
    # ── cost ──────────────────────────────────────────────────────────
    _SettingSpec(
        key="FLUID_COST_LIMIT_USD",
        description="Per-run cost ceiling in USD; raises ``CostLimitExceeded`` "
        "when exceeded. Unset = no ceiling.",
        default=None,
        cast=lambda v: _to_float(v, default=0.0) if v else None,
        category="cost",
    ),
    _SettingSpec(
        key="FLUID_COST_LIMIT_USD_PER_RUN",
        description="Alias for FLUID_COST_LIMIT_USD shown in the forge progress prefix.",
        default=None,
        cast=lambda v: _to_float(v, default=0.0) if v else None,
        category="cost",
    ),
    _SettingSpec(
        key="FLUID_COST_LIMIT_USD_PER_PRODUCT",
        description="Per-PRODUCT cost ceiling in USD; raises ``CostLimitExceeded`` "
        "when ANY single product crosses the cap. Useful for "
        "``--from-product-list`` runs that compose many products in "
        "one invocation. Distinct from FLUID_COST_LIMIT_USD which "
        "caps the aggregate. Unset = no per-product ceiling.",
        default=None,
        cast=lambda v: _to_float(v, default=0.0) if v else None,
        category="cost",
    ),
    # ── llm ───────────────────────────────────────────────────────────
    _SettingSpec(
        key="FLUID_LITELLM_MODEL_PREFIX",
        description="Override the litellm model-name prefix for niche providers.",
        default=None,
        category="llm",
    ),
    _SettingSpec(
        key="FLUID_TIERED",
        description="Enable per-stage model tiers when an LLM is configured.",
        default=False,
        cast=_to_bool,
        category="llm",
    ),
    # ── ux ────────────────────────────────────────────────────────────
    _SettingSpec(
        key="FLUID_QUIET",
        description="Suppress verbose chrome (banners, panels). Same as --quiet.",
        default=False,
        cast=_to_bool,
        category="ux",
    ),
    _SettingSpec(
        key="FLUID_BANNER",
        description="Force the ASCII banner on bare ``fluid`` invocations.",
        default=False,
        cast=_to_bool,
        category="ux",
    ),
    _SettingSpec(
        key="FLUID_FORGE_NO_PICKER",
        description="Suppress the 5-mode forge picker (CI / scripts).",
        default=False,
        cast=_to_bool,
        category="forge",
    ),
    _SettingSpec(
        key="FLUID_FORGE_NO_WELCOME",
        description="Suppress the welcome scan render.",
        default=False,
        cast=_to_bool,
        category="forge",
    ),
    _SettingSpec(
        key="FLUID_FORGE_NO_PREVIEW",
        description="Suppress the pre-write preview panel + prompt.",
        default=False,
        cast=_to_bool,
        category="forge",
    ),
    _SettingSpec(
        key="FLUID_FORGE_NO_STREAMING_PREVIEW",
        description="Suppress the live contract growth panel.",
        default=False,
        cast=_to_bool,
        category="forge",
    ),
    _SettingSpec(
        key="FLUID_FORGE_PICKER_ALWAYS",
        description="Force the picker even for return users.",
        default=False,
        cast=_to_bool,
        category="forge",
    ),
    _SettingSpec(
        key="FLUID_FORGE_LEGACY_COPILOT",
        description="Bypass the staged copilot pipeline; use the linear runtime.",
        default=False,
        cast=_to_bool,
        category="forge",
    ),
    _SettingSpec(
        key="FLUID_LOG_LEVEL",
        description="Logging level: DEBUG | INFO | WARNING | ERROR.",
        default="INFO",
        category="observability",
    ),
    _SettingSpec(
        key="FLUID_RUN_ID",
        description="Override the auto-generated run-id used to correlate "
        "OTel spans across the 11-stage pipeline (bundle→plan→apply→"
        "verify→publish). When unset, fluid generates one on the first "
        "stage and persists it to ``.fluid/run-id.txt`` for subsequent "
        "stages to read. Useful for CI to inject a known id (e.g. "
        "build number) so dashboards group by it.",
        default=None,
        category="observability",
    ),
    # ── rollback ──────────────────────────────────────────────────────
    _SettingSpec(
        key="FLUID_ROLLBACK_KEEP_LAST_N",
        description="Per-product retention cap on .fluid/rollback-state.json. Default 20.",
        default=20,
        cast=lambda v: _to_int(v, default=20),
        category="rollback",
    ),
    # ── runtime / paths ───────────────────────────────────────────────
    _SettingSpec(
        key="FLUID_WORKSPACE_ROOT",
        description="Override the workspace root (where .fluid/ lives). Defaults to CWD.",
        default=None,
        category="runtime",
    ),
    _SettingSpec(
        key="FLUID_USER_HOME",
        description="Override the user-global FLUID home (~/.fluid). "
        "Useful for containerised deployments.",
        default=None,
        category="runtime",
    ),
    _SettingSpec(
        key="FLUID_PROVIDER",
        description="Default provider when the contract doesn't specify one.",
        default=None,
        category="runtime",
    ),
    # ── security ──────────────────────────────────────────────────────
    _SettingSpec(
        key="FLUID_PII_TOKENIZATION_KEY",
        description="HMAC key used by the tokenize_pii pre-land hook. "
        "Set this to make tokens stable across runs (so you can join / "
        "dedup on tokenized values).",
        default=None,
        category="security",
    ),
]


_BY_KEY: Dict[str, _SettingSpec] = {s.key: s for s in _REGISTRY}


def get(key: str, default: Any = None) -> Any:
    """Read a registered FLUID_* env var with the right cast applied.

    Falls back to ``default`` (then to the registry's own default) when
    unset. Raw env access via ``os.environ.get`` still works for
    unregistered names — this function only handles the registered set.
    """
    spec = _BY_KEY.get(key)
    if spec is None:
        # Unregistered env var; honour str default for back-compat.
        raw = os.environ.get(key)
        return raw if raw is not None else default
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default if default is not None else spec.default
    try:
        return spec.cast(raw)
    except Exception:  # pragma: no cover — defensive
        return default if default is not None else spec.default


def is_set(key: str) -> bool:
    """True when the env var is set to a non-empty value."""
    return bool(os.environ.get(key))


def all_specs() -> List[_SettingSpec]:
    """Snapshot of every registered env var. Used by ``fluid env --list``."""
    return list(_REGISTRY)


def by_category(category: str) -> List[_SettingSpec]:
    """All registered env vars in a given category."""
    return [s for s in _REGISTRY if s.category == category]


__all__ = [
    "get",
    "is_set",
    "all_specs",
    "by_category",
]
