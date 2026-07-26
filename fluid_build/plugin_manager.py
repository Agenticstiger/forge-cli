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

# Role key for third-party LLM providers — named so callers (e.g. ``fluid
# doctor``) reference it without a magic string.
LLM_PROVIDER_GROUP_KEY = "llm_provider"

# CLI-internal entry-point groups that also load + run plugin code. They are not
# fluid_sdk *roles*, but they ARE operator-governable plugin surfaces, so they are
# subject to the SAME allow/block policy and surfaced by ``fluid plugins``. Every
# entry-point group whose plugins the CLI executes belongs in exactly one of
# ROLE_GROUPS / EXTRA_GROUPS so the allow/block + type-only guarantees are total.
EXTRA_GROUPS: Dict[str, str] = {
    "command": "fluid_build.commands",
    "apply_hook": "fluid_build.apply_hooks",
    "extension_schema": "fluid_build.extension_schemas",
    "extension_validator": "fluid_build.extension_validators",
    "modeling_technique": "fluid_build.modeling_techniques",
    "source_adapter": "fluid_build.source_adapters",
    # Third-party LLM providers for `fluid forge --llm-provider <name>`. Loaded +
    # registered lazily in ``cli/_llm_provider_plugins.py``; listed here so
    # ``fluid plugins`` surfaces them and the allow/block policy governs them.
    LLM_PROVIDER_GROUP_KEY: "fluid_build.llm_providers",
}


#: Groups that have **no dispatch site** in this build of the CLI: the role is
#: declared and governed, but nothing ever walks the group to run a plugin.
#:
#: ``fluid plugins`` is an operator inspection/security surface, so it must not
#: render an inert plugin the same way it renders an active one. Listing
#: ``custom_scaffold`` as plain "allowed" told operators a plugin was live when
#: no code path — across validate / plan / apply / publish / forge / providers —
#: ever imported it. Removing the role instead would drop the allow/block
#: governance and the audit visibility, so it stays listed and says what it is.
#:
#: Wire a dispatch site (the way ``iac_provider`` is wired in
#: ``iac/registry.py``) and delete the entry in the same change.
UNDISPATCHED_GROUPS: frozenset = frozenset({"custom_scaffold"})


def is_dispatched(group_key: str) -> bool:
    """Whether this build actually invokes plugins registered under ``group_key``."""
    return group_key not in UNDISPATCHED_GROUPS


def governed_groups() -> Dict[str, str]:
    """Return every entry-point group the operator allow/block policy governs.

    ``ROLE_GROUPS`` (the fluid_sdk roles) plus ``EXTRA_GROUPS`` (CLI-internal
    plugin surfaces). Anything here is gated by :func:`is_allowed` at its walk
    site and listed by ``fluid plugins``.
    """
    return {**ROLE_GROUPS, **EXTRA_GROUPS}


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


def entry_point_distribution(ep: Any) -> Optional[str]:
    """``"<dist-name> <version>"`` for the distribution that ships ``ep``.

    Plugin ``PluginMetadata`` is *self-declared* and unverifiable: a plugin can
    claim any version / author / licence it likes. The distribution that
    registered the entry point is the authoritative fact, and it is the only
    one an operator can act on — it is what ``pip uninstall`` takes. Surfacing
    it next to the declared metadata is the difference between an inspection
    command and a repeat of whatever the plugin says about itself.

    ``None`` when the running interpreter cannot attribute the entry point
    (``EntryPoint.dist`` is 3.10+; older resolvers return nothing).
    """
    dist = getattr(ep, "dist", None)
    if dist is None:
        return None
    name = getattr(dist, "name", None) or getattr(
        getattr(dist, "metadata", None), "get", lambda _k: None
    )("Name")
    if not name:
        return None
    version = getattr(dist, "version", None)
    return f"{name} {version}" if version else str(name)


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


def installed_plugins(role: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Return ``{group_key: [{name, group, allowed, dispatched, distribution}]}``.

    Unlike :func:`list_plugins` (which filters to the allowed set), this surfaces
    EVERY installed plugin in EVERY governed group — the fluid_sdk roles AND the
    CLI-internal groups (commands / apply_hooks / extension_*) — with its
    allow/block status, for the operator inspection command (``fluid plugins``).
    Reads entry-point *names* only — it never imports plugin code.

    ``dispatched`` is False for a role this build never walks
    (:data:`UNDISPATCHED_GROUPS`); ``distribution`` names the pip package that
    actually ships the entry point, which is the only attribution an operator
    can audit or uninstall.
    """
    groups = governed_groups()
    keys = [role] if role else list(groups)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for k in keys:
        group = groups.get(k)
        if not group:
            continue
        out[k] = [
            {
                "name": ep.name,
                "group": group,
                "allowed": is_allowed(ep.name),
                "dispatched": is_dispatched(k),
                "distribution": entry_point_distribution(ep),
            }
            for ep in sorted(_entry_points(group), key=lambda e: e.name)
        ]
    return out


def _plugin_metadata(obj: Any, logger: logging.Logger) -> Optional[Dict[str, Any]]:
    """Best-effort declared metadata for a loaded plugin, or ``None``.

    Reads ``get_plugin_info()`` (a ``@classmethod`` on the SDK ``BasePlugin``) and
    returns its ``PluginMetadata.to_dict()``. Guarded + logged by exception type
    only (no plugin-supplied text), consistent with the trust boundary. Plugins
    that declare no metadata (e.g. plain command functions) return ``None``.
    """
    info_fn = getattr(obj, "get_plugin_info", None)
    if not callable(info_fn):
        return None
    try:
        meta = info_fn()
    except Exception as e:  # noqa: BLE001 - never crash inspection; type only
        logger.warning("get_plugin_info raised: %s", type(e).__name__)
        return None
    to_dict = getattr(meta, "to_dict", None)
    if callable(to_dict):
        try:
            d = to_dict()
            return d if isinstance(d, dict) else None
        except Exception:  # noqa: BLE001
            return None
    # Fallback: pull the common attributes off the metadata object.
    fields = {}
    for attr in ("name", "version", "author", "license", "url", "description"):
        val = getattr(meta, attr, None)
        if val is not None:
            fields[attr] = val
    return fields or None


def _requires_cli_satisfied(metadata: Optional[Dict[str, Any]]) -> Optional[bool]:
    """Whether the running CLI satisfies a plugin's declared ``requires_cli``.

    ``None`` when nothing is declared or the specifier is uncheckable — the
    same fail-open posture as ``providers._check_sdk_compat``, whose
    ``_spec_satisfied`` this reuses so the inspection surface and the
    registration gate can never disagree about the same plugin.
    """
    if not isinstance(metadata, dict):
        return None
    requires = metadata.get("requires_cli")
    if not requires:
        return None
    try:
        from fluid_build.providers import _CLI_VERSION, _spec_satisfied

        return _spec_satisfied(_CLI_VERSION, str(requires))
    except Exception:  # noqa: BLE001 - inspection must never crash
        return None


def detailed_plugins(
    role: Optional[str] = None, logger: Optional[logging.Logger] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """Like :func:`installed_plugins`, plus the declared metadata of ALLOWED plugins.

    ALLOWED plugins are loaded (via ``ep.load()``) so their ``get_plugin_info()``
    metadata can be surfaced; BLOCKED plugins are listed name-only and are NEVER
    loaded — the allow/block trust boundary is preserved. Per-plugin
    fail-isolation: a load or metadata error logs by type only and yields
    ``metadata: None`` rather than dropping the entry or raising.
    """
    log = logger or logging.getLogger(__name__)
    groups = governed_groups()
    keys = [role] if role else list(groups)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for k in keys:
        group = groups.get(k)
        if not group:
            continue
        entries: List[Dict[str, Any]] = []
        for ep in sorted(_entry_points(group), key=lambda e: e.name):
            allowed = is_allowed(ep.name)
            entry: Dict[str, Any] = {
                "name": ep.name,
                "group": group,
                "allowed": allowed,
                "dispatched": is_dispatched(k),
                "distribution": entry_point_distribution(ep),
                "metadata": None,
                "compatible": None,
            }
            if allowed:  # only ALLOWED plugins are ever loaded
                try:
                    obj = ep.load()
                except Exception as e:  # noqa: BLE001 - isolate a bad plugin; type only
                    log.warning("plugin %r failed to load: %s", ep.name, type(e).__name__)
                    obj = None
                if obj is not None:
                    entry["metadata"] = _plugin_metadata(obj, log)
                    entry["compatible"] = _requires_cli_satisfied(entry["metadata"])
            entries.append(entry)
        out[k] = entries
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
    "EXTRA_GROUPS",
    "LLM_PROVIDER_GROUP_KEY",
    "governed_groups",
    "is_allowed",
    "iter_plugins",
    "list_plugins",
    "installed_plugins",
    "detailed_plugins",
    "has_plugins",
    "collect_validator_findings",
    "dispatch_catalog_adapters",
]
