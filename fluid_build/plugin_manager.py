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

"""Unified host-side plugin discovery + dispatch.

ONE place that walks the role-tagged entry-point groups and dispatches by role,
so every plugin role shares a single discovery substrate, allow/block policy,
and fail-isolation idiom instead of ad-hoc per-call ``importlib.metadata`` walks.

:data:`ROLE_GROUPS` is the single source of truth mapping a
:class:`fluid_sdk.BasePlugin` ``role`` tag to its entry-point group. A plugin
author registers under the group for their role and the CLI discovers + invokes
it through this manager.

Borrowed model (adapted, **not** depended on — zero new dependencies):

* pluggy's ``PluginManager`` lifecycle — entry-point load, allow/**block**, and
  inspection (https://github.com/pytest-dev/pluggy).
* stevedore's "dispatch a named driver within a namespace"
  (https://github.com/openstack/stevedore).

**Trust boundary.** An entry-point plugin runs in-process at host trust — the
industry-standard property (dbt / pluggy / Backstage don't sandbox third-party
plugins either; see ``SECURITY``). The operator pins exactly which plugins load
via ``FLUID_PLUGINS_ALLOWLIST`` / ``FLUID_PLUGINS_BLOCKLIST`` (comma-separated
entry-point names; allowlist wins — if set, only listed names load). Every load
and invocation is fail-isolated and logged **by exception type only**, so a
plugin bug or a secret-bearing exception message can neither crash the CLI nor
leak into logs.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple

# role tag (fluid_sdk.BasePlugin.role) -> entry-point group. Single source of truth.
ROLE_GROUPS: Dict[str, str] = {
    "provider": "fluid_build.providers",
    "validator": "fluid_build.validators",
    "catalog": "fluid_build.catalog_adapters",
    "custom_scaffold": "fluid_build.custom_scaffolds",
    "iac_provider": "fluid_build.iac_providers",
}

_ALLOWLIST_ENV = "FLUID_PLUGINS_ALLOWLIST"
_BLOCKLIST_ENV = "FLUID_PLUGINS_BLOCKLIST"


def _env_names(var: str) -> set:
    return {x.strip() for x in os.environ.get(var, "").split(",") if x.strip()}


def is_allowed(name: str) -> bool:
    """Return whether entry-point ``name`` may load under the allow/block policy.

    Blocklist always wins; if an allowlist is set, only listed names load.
    """
    block = _env_names(_BLOCKLIST_ENV)
    if name in block:
        return False
    allow = _env_names(_ALLOWLIST_ENV)
    if allow and name not in allow:
        return False
    return True


def _entry_points(group: str) -> List[Any]:
    import importlib.metadata as md

    try:
        return list(md.entry_points(group=group))
    except TypeError:  # Python < 3.10
        return list(md.entry_points().get(group, []))


def iter_plugins(group: str, logger: Optional[logging.Logger] = None) -> Iterator[Tuple[str, Any]]:
    """Yield ``(name, loaded_object)`` for each allowed, loadable plugin in ``group``.

    Per-plugin fail-isolation: a plugin that is blocked, not allow-listed, or
    fails to import is skipped (logged by type only); one bad plugin never drops
    the others and the walk never raises.
    """
    log = logger or logging.getLogger(__name__)
    for ep in _entry_points(group):
        if not is_allowed(ep.name):
            log.debug("plugin %r skipped by allow/block policy", ep.name)
            continue
        try:
            obj = ep.load()
        except Exception as e:  # noqa: BLE001 - isolate a bad plugin, log by type only
            log.warning("plugin %r failed to load: %s", ep.name, type(e).__name__)
            continue
        yield ep.name, obj


def list_plugins(role: Optional[str] = None) -> Dict[str, List[str]]:
    """Return ``{role: [entry-point names]}`` for discovered plugins (inspection)."""
    roles = [role] if role else list(ROLE_GROUPS)
    out: Dict[str, List[str]] = {}
    for r in roles:
        group = ROLE_GROUPS.get(r)
        if not group:
            continue
        out[r] = sorted(ep.name for ep in _entry_points(group) if is_allowed(ep.name))
    return out


def _normalize_severity(value: Any) -> str:
    """Map a raw severity onto {info, warn, error, critical} (CLI-local, zero-dep).

    Mirrors ``fluid_sdk.domains.Severity.coerce``'s fail-safe posture without
    importing the SDK (which is not a CLI dependency): an unrecognised severity
    is treated as ``error`` so a typo can't downgrade a failing finding.
    """
    s = ("" if value is None else str(value)).strip().lower()
    if not s:
        return "info"
    aliases = {
        "information": "info",
        "informational": "info",
        "notice": "info",
        "debug": "info",
        "trace": "info",
        "warning": "warn",
        "low": "warn",
        "err": "error",
        "failure": "error",
        "fail": "error",
        "high": "error",
        "fatal": "critical",
        "crit": "critical",
        "severe": "critical",
        "blocker": "critical",
    }
    if s in {"info", "warn", "error", "critical"}:
        return s
    return aliases.get(s, "error")


def collect_validator_findings(
    contract: Dict[str, Any], logger: Optional[logging.Logger] = None
) -> List[Dict[str, Any]]:
    """Run every installed ``fluid_build.validators`` plugin against ``contract``.

    Each plugin is a :class:`fluid_sdk.Validator` (role ``"validator"``): we
    instantiate it, call ``plan(contract)``, and translate its ``emit_finding``
    actions into normalised finding dicts
    ``{severity, code, message, path, plugin}``. A validator that raises yields a
    single ``error`` finding (typed, no exception text leaked). Returns ``[]``
    when nothing is installed — the backward-compatible no-op path.
    """
    log = logger or logging.getLogger(__name__)
    findings: List[Dict[str, Any]] = []
    for name, obj in iter_plugins(ROLE_GROUPS["validator"], logger=log):
        try:
            plugin = obj() if isinstance(obj, type) else obj
            actions = plugin.plan(contract) or []
        except Exception as e:  # noqa: BLE001 - validator bug, surface typed
            findings.append(
                {
                    "severity": "error",
                    "code": f"VALIDATOR_{name.upper()}_FAILED",
                    "message": f"validator {name!r} raised {type(e).__name__}",
                    "path": None,
                    "plugin": name,
                }
            )
            continue
        for action in actions:
            d = action.to_dict() if hasattr(action, "to_dict") else action
            if not isinstance(d, dict) or d.get("op") != "emit_finding":
                continue
            params = d.get("params") or {}
            findings.append(
                {
                    "severity": _normalize_severity(params.get("severity")),
                    "code": params.get("code") or d.get("resource_id") or "",
                    "message": params.get("message") or "",
                    "path": params.get("path"),
                    "plugin": name,
                }
            )
    return findings


def has_plugins(group: str) -> bool:
    """True if any allowed plugin is registered under ``group`` (no load).

    Cheap pre-check that reads entry-point *names* only — it never imports plugin
    code — so a caller can short-circuit work when nothing is installed.
    """
    return any(is_allowed(ep.name) for ep in _entry_points(group))


def dispatch_catalog_adapters(
    contract: Dict[str, Any],
    *,
    dry_run: bool = False,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """Run every installed ``fluid_build.catalog_adapters`` plugin against ``contract``.

    Each plugin is a :class:`fluid_sdk.CatalogAdapter` (role ``"catalog"``): we
    instantiate it, call ``plan(contract)``, and — unless ``dry_run`` — call
    ``apply(actions)`` to sync to the catalog. Returns one summary per plugin
    ``{plugin, planned, applied, failed, ok, error?}``. A plugin that raises is
    fail-isolated (typed, no exception text leaked) and reported with ``ok=False``.
    Returns ``[]`` when nothing is installed — the backward-compatible no-op path.
    """
    log = logger or logging.getLogger(__name__)
    summaries: List[Dict[str, Any]] = []
    for name, obj in iter_plugins(ROLE_GROUPS["catalog"], logger=log):
        summary: Dict[str, Any] = {
            "plugin": name,
            "planned": 0,
            "applied": 0,
            "failed": 0,
            "ok": True,
        }
        try:
            plugin = obj() if isinstance(obj, type) else obj
            actions = list(plugin.plan(contract) or [])
            summary["planned"] = len(actions)
            if not dry_run:
                result = plugin.apply(actions)
                summary["applied"] = int(getattr(result, "applied", 0) or 0)
                summary["failed"] = int(getattr(result, "failed", 0) or 0)
                summary["ok"] = summary["failed"] == 0
        except Exception as e:  # noqa: BLE001 - catalog adapter bug, surface typed
            summary["ok"] = False
            summary["error"] = type(e).__name__
            log.warning("catalog adapter %r failed: %s", name, type(e).__name__)
        summaries.append(summary)
    return summaries


__all__ = [
    "ROLE_GROUPS",
    "is_allowed",
    "iter_plugins",
    "list_plugins",
    "has_plugins",
    "collect_validator_findings",
    "dispatch_catalog_adapters",
]
