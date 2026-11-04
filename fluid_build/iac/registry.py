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

"""IaC plugin registry.

Keyed by cloud name, a distinct axis from ``providers.register_provider``
(the apply-provider registry).

In-tree clouds register on import (see ``iac/__init__.py``). External packages
supply a cloud (an OpenTofu emitter) through an entry point in the
``fluid_build.iac_providers`` group, the same naming as ``fluid_build.providers``::

    [project.entry-points."fluid_build.iac_providers"]
    onprem = "my_pkg.iac:OnPremIacPlugin"

Design, borrowed rather than invented:

* **Names without importing** are read with stdlib
  ``importlib.metadata.entry_points(group=...)`` (``.name`` / ``.dist``), via
  :func:`fluid_build.plugin_manager._entry_points`; nothing is ``ep.load()``-ed.
* **One plugin is loaded, by name, when a platform needs it**, as stevedore's
  ``DriverManager`` does (https://docs.openstack.org/stevedore/latest/user/
  essays/pbr_and_stevedore.html, ``stevedore.driver.DriverManager``). Nothing
  scans or loads this group at import time, so ``fluid --help`` and
  ``fluid providers`` never run third-party code. :func:`get_iac_plugin` is the
  load point.
* **Policy** is the unified plugin manager's: the allow/block lists (names match
  case-insensitively, ``-`` == ``_``) and the reserved names are checked before
  ``ep.load()``; the plugin's ``get_plugin_info().requires_cli`` is read from the
  loaded plugin, so it is checked right after the import, with the provider
  registry's own gate (``providers._check_sdk_compat``: advisory warning by
  default, rejection under ``FLUID_PLUGIN_STRICT_COMPAT=1``).
* **Reserved names**, as ``providers._claims_retired_name`` does for retired
  provider names: an entry point that claims a built-in platform (``aws``,
  ``gcp``, ``snowflake``, ``confluent``, ``databricks``, any alias
  ``iac.provider_match`` resolves, or a retired provider name) is skipped
  BEFORE ``ep.load()`` with a WARNING naming the distribution and the opt-in. A
  plugin can add a platform; it replaces one only when the operator grants it
  with a distribution-qualified ``FLUID_PLUGINS_ALLOWLIST`` entry,
  ``<distribution>:<entry-point name>`` (``plugin_manager.override_granted``,
  which reads the distribution's real name, never its display string). A grant
  needs the name to pass the bare allowlist too: a bare allowlist that omits it
  cancels the grant, with a WARNING. A granted override replaces the emitter
  plugin only, not the in-tree helpers that modules import directly.
* **Duplicate names.** Several distributions shipping one reserved name: each
  ungranted claim is warned about, and the one granted claim loads. Several
  shipping one non-reserved name: all are refused (fail closed) with one WARNING
  naming them; the operator uses ``FLUID_PLUGINS_BLOCKLIST`` or uninstalls.

**What still requires a core edit**:

* ``binding.platform`` / ``builds[].execution.runtime.platform`` is a **closed
  enum** in every shipped ``fluid-schema-*.json``. A contract naming a platform
  the schema does not list is rejected at validation. A plugin therefore serves
  a platform the schema admits and no built-in emitter serves (``kubernetes``,
  ``postgres``, ``kafka`` ...). Opening the enum is a schema decision.
* ``fluid --provider`` is validated against the apply-provider registry
  (``cli/__init__.py::_validate_global_args``), so for now it accepts built-ins
  only. A plugin platform is reached by auto-detection from the contract's
  ``binding.platform``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from .base import IacProviderPlugin

_log = logging.getLogger(__name__)

IAC_PLUGINS: Dict[str, IacProviderPlugin] = {}

#: Names loaded from the ``fluid_build.iac_providers`` entry-point group, i.e.
#: clouds that live OUTSIDE this repo. In-tree clouds are NOT recorded; the
#: ``cutover.OPENTOFU_DEFAULT_PROVIDERS`` frozenset stays their cutover switch.
IAC_ENTRYPOINT_PLUGINS: Set[str] = set()

#: The in-tree registrations, snapshotted as they register (not while an entry
#: point is loading), so ``reset_iac_plugin_cache`` can restore a built-in a
#: granted override replaced.
_INTREE: Dict[str, IacProviderPlugin] = {}
_LOADING = False

#: Entry-point names whose load was attempted and refused or failed, so a
#: platform that needs the plugin repeatedly does not re-import, and re-warn
#: about, a broken one. ``reset_iac_plugin_cache`` clears it.
_REFUSED: Set[str] = set()
_LOCK = threading.RLock()


def _norm(name: str) -> str:
    """The spelling ``provider_match.canonical_cloud`` and ``providers`` normalise to."""
    return str(name).strip().lower().replace("-", "_")


def register_iac_plugin(name: str, plugin: IacProviderPlugin) -> None:
    """Register an IaC plugin under a cloud name (``gcp``/``aws``/...).

    The operator allow/block policy is enforced here, at the registration
    chokepoint, so it holds for the in-tree clouds registered on import as
    well as for entry-point plugins. Without this, blocking ``snowflake``
    removed it from the provider registry but left the IaC plugin registered,
    and ``fluid apply`` reaches the warehouse through *this* registry.
    """
    from fluid_build.plugin_manager import is_allowed

    if not is_allowed(name):
        _log.debug("iac plugin %r skipped by allow/block policy", name)
        return
    # Not a boundary: a loaded plugin runs at host trust and can call this (or
    # write IAC_PLUGINS) directly. Reserved names are enforced BEFORE the import.
    IAC_PLUGINS[name] = plugin
    if not _LOADING:
        _INTREE[name] = plugin


def reserved_iac_names() -> FrozenSet[str]:
    """Platform names an entry-point plugin may not claim (normalised spellings).

    Everything the in-tree resolution already gives a meaning to: the cutover
    set, every ``provider_match`` alias (``s3``, ``bigquery``, ``local`` ...),
    the in-tree plugins registered on import, and the retired provider names.
    Static, so a built-in the operator BLOCKED stays reserved.
    """
    from .cutover import OPENTOFU_DEFAULT_PROVIDERS
    from .provider_match import PROVIDER_ALIASES

    names: Set[str] = set(OPENTOFU_DEFAULT_PROVIDERS) | set(PROVIDER_ALIASES)
    names |= {_norm(n) for n in IAC_PLUGINS if n not in IAC_ENTRYPOINT_PLUGINS}
    from fluid_build.providers import RETIRED_PROVIDERS

    names |= set(RETIRED_PROVIDERS)
    return frozenset(names)


def _claims_reserved_name(ep: Any) -> bool:
    """True (after a WARNING naming the plugin) when ``ep`` claims a reserved name
    without the operator's grant.

    Called BEFORE ``ep.load()``, so a package squatting a built-in platform never
    gets to run its import-time code. The distribution is in the message itself,
    repr'd so control characters are escaped: the CLI's formatters print the
    message only, and this line is how an operator learns what to uninstall, or
    how to opt in. A distribution-qualified ``FLUID_PLUGINS_ALLOWLIST`` entry
    (``plugin_manager.override_granted``) lifts the reservation for that one
    entry point; it is then logged at INFO and loaded. A retired name has no
    built-in to replace, so no grant lifts it (as in ``providers``).
    """
    key = _norm(ep.name)
    if key not in reserved_iac_names():
        return False
    from fluid_build.plugin_manager import (
        entry_point_distribution,
        granted_distribution,
        override_granted,
    )
    from fluid_build.providers import RETIRED_PROVIDERS

    who = entry_point_distribution(ep) or "<unknown distribution>"
    if key in RETIRED_PROVIDERS:
        _log.warning(
            "iac_provider_name_reserved: plugin %r (entry point %r) tried to register the retired "
            "platform %r; it was skipped without being imported",
            who,
            ep.name,
            key,
            extra={"evt": "iac_provider_name_reserved", "_name": key, "plugin": who},
        )
        return True
    if override_granted(ep):
        _log.info(
            "iac_provider_override_granted: plugin %r replaces the built-in IaC platform %r "
            "(granted in FLUID_PLUGINS_ALLOWLIST)",
            who,
            key,
            extra={"evt": "iac_provider_override_granted", "_name": key, "plugin": who},
        )
        return False
    dist = granted_distribution(ep)
    hint = (
        f"; to allow this override add {f'{dist}:{ep.name}'!r} to FLUID_PLUGINS_ALLOWLIST"
        if dist is not None
        else ""
    )
    _log.warning(
        "iac_provider_name_reserved: plugin %r (entry point %r) tried to register the built-in "
        "IaC platform %r; it was skipped without being imported%s",
        who,
        ep.name,
        key,
        hint,
        extra={"evt": "iac_provider_name_reserved", "_name": key, "plugin": who},
    )
    return True


#: ``(scanner, index)``: one metadata scan per process (about 8 ms), reused by
#: the per-exposure platform lookups. Keyed on the scanner so a test that swaps
#: ``plugin_manager._entry_points`` gets a fresh read.
_INDEX: Optional[Tuple[Any, Dict[str, List[Any]]]] = None


def _entrypoints_by_name() -> Dict[str, List[Any]]:
    """``{normalised name: [entry points]}`` for the group. Reads names; never loads.

    A list per name: two distributions can ship the same one.
    """
    global _INDEX  # noqa: PLW0603
    from fluid_build import plugin_manager

    scanner = plugin_manager._entry_points
    cached = _INDEX
    if cached is None or cached[0] is not scanner:
        group = plugin_manager.ROLE_GROUPS["iac_provider"]
        index: Dict[str, List[Any]] = {}
        for ep in scanner(group):
            index.setdefault(_norm(ep.name), []).append(ep)
        cached = (scanner, index)
        _INDEX = cached  # read back above on the next call
    return cached[1]


def _who(ep: Any) -> str:
    from fluid_build.plugin_manager import entry_point_distribution

    return entry_point_distribution(ep) or "<unknown distribution>"


def _cancelled_grant_reason(key: str) -> str:
    """Why a grant for ``key`` is cancelled when policy refuses the name."""
    from fluid_build.plugin_manager import _BLOCKLIST_ENV, _env_names, _policy_key

    if key in {_policy_key(n) for n in _env_names(_BLOCKLIST_ENV)}:
        return f"{key!r} is in FLUID_PLUGINS_BLOCKLIST"
    return f"the bare names in FLUID_PLUGINS_ALLOWLIST do not include {key!r}"


def _resolve(key: str, eps: List[Any], logger: logging.Logger) -> Optional[Any]:
    """The pre-load gates for one name: policy, reserved names, duplicates.

    Returns the one entry point that may load, else ``None`` (after a WARNING
    where an operator needs to act). Nothing is imported here.
    """
    from fluid_build.plugin_manager import is_allowed, override_granted

    if not is_allowed(key):
        logger.debug("iac plugin %r skipped by allow/block policy", key)
        for ep in eps:
            if override_granted(ep):
                logger.warning(
                    "iac_provider_grant_cancelled: the grant for plugin %r (entry point %r) is "
                    "cancelled because %s; the platform %r stays unserved by it",
                    _who(ep),
                    ep.name,
                    _cancelled_grant_reason(key),
                    key,
                    extra={"evt": "iac_provider_grant_cancelled", "_name": key},
                )
        return None
    if key in reserved_iac_names():
        # Warns about every ungranted claim; the granted ones come back.
        granted = [ep for ep in eps if not _claims_reserved_name(ep)]
        if len(granted) > 1:
            logger.warning(
                "iac_provider_name_ambiguous: several plugins (%s) are granted the built-in "
                "IaC platform %r; none was imported",
                ", ".join(repr(_who(ep)) for ep in granted),
                key,
                extra={"evt": "iac_provider_name_ambiguous", "_name": key},
            )
            return None
        return granted[0] if granted else None
    if len(eps) > 1:
        logger.warning(
            "iac_provider_name_ambiguous: %d plugins (%s) ship the IaC platform %r; none was "
            "imported; block the name with FLUID_PLUGINS_BLOCKLIST or uninstall the extras",
            len(eps),
            ", ".join(repr(_who(ep)) for ep in eps),
            key,
            extra={"evt": "iac_provider_name_ambiguous", "_name": key},
        )
        return None
    return eps[0]


def _instantiate(name: str, obj: Any, logger: logging.Logger) -> Optional[IacProviderPlugin]:
    try:
        return obj() if isinstance(obj, type) else obj
    except Exception as e:  # noqa: BLE001 - isolate a bad plugin, log by type only
        logger.warning("iac plugin %r failed to instantiate: %s", name, type(e).__name__)
        return None


def _compatible(name: str, plugin: Any, logger: logging.Logger) -> bool:
    """The version gate, shared with the provider registry (advisory unless strict)."""
    from fluid_build.providers import _CLI_VERSION, _check_sdk_compat, _safe_log, _strict_compat

    if _check_sdk_compat(name, plugin, logger) or not _strict_compat():
        return True
    _safe_log(
        logger,
        logging.ERROR,
        "iac_plugin_compat_rejected",
        name=name,
        cli_version=_CLI_VERSION,
    )
    return False


def _load_key(key: str, eps: List[Any], logger: logging.Logger) -> Optional[IacProviderPlugin]:
    """Run the gates for ``key``, load the admitted entry point and register what it
    yields. ``None`` if refused."""
    global _LOADING  # noqa: PLW0603
    with _LOCK:
        ep = _resolve(key, eps, logger)
        if ep is None:
            return None
        _LOADING = True
        try:
            try:
                obj = ep.load()
            except Exception as e:  # noqa: BLE001 - isolate a bad plugin, log by type only
                logger.warning("plugin %r failed to load: %s", ep.name, type(e).__name__)
                return None
            plugin = _instantiate(ep.name, obj, logger)
            if plugin is None or not _compatible(ep.name, plugin, logger):
                return None
            # Keyed by the entry-point name, the one that was vetted: the plugin's own
            # ``name`` is the Terraform provider's local name and may legitimately be
            # ``aws`` (an on-prem S3 emitter driving the aws provider).
            register_iac_plugin(key, plugin)
        finally:
            _LOADING = False
        if IAC_PLUGINS.get(key) is not plugin:
            return None
        IAC_ENTRYPOINT_PLUGINS.add(key)
        logger.debug("registered external iac plugin %r", ep.name)
        return plugin


def get_iac_plugin(name: str) -> Optional[IacProviderPlugin]:
    """Return the plugin for ``name``, loading it from an entry point if need be.

    The load point. A registered (in-tree or already loaded) plugin is returned
    as is. Otherwise the ``fluid_build.iac_providers`` group is consulted by
    NAME (normalised: case-insensitive, ``-`` == ``_``), and only a matching,
    admitted entry point is imported (``stevedore.driver.DriverManager``
    semantics). ``None`` when nothing serves ``name``;
    :func:`iac_plugin_unavailable_reason` says why an installed one was refused.
    """
    if not isinstance(name, str) or not name.strip():
        return IAC_PLUGINS.get(name)
    key = _norm(name)
    plugin = IAC_PLUGINS.get(key)
    if key in _REFUSED or key in IAC_ENTRYPOINT_PLUGINS:
        return plugin
    if key in reserved_iac_names():
        # Served by the in-tree table alone, with no metadata scan, unless the
        # operator listed a distribution-qualified grant for this name.
        if not _override_listed(key):
            return plugin
    elif plugin is not None:
        return plugin
    with _LOCK:
        # Re-check inside the lock: another thread may have loaded or refused it.
        if key in IAC_ENTRYPOINT_PLUGINS or key in _REFUSED:
            return IAC_PLUGINS.get(key)
        eps = _entrypoints_by_name().get(key)
        if not eps:
            return IAC_PLUGINS.get(key)
        if _load_key(key, eps, _log) is None:
            _REFUSED.add(key)
        return IAC_PLUGINS.get(key)


def _override_listed(key: str) -> bool:
    """Whether ``FLUID_PLUGINS_ALLOWLIST`` holds any ``<dist>:<key>`` entry (env only)."""
    from fluid_build.plugin_manager import _ALLOWLIST_ENV, _QUALIFIED_SEP, _env_names

    for entry in _env_names(_ALLOWLIST_ENV):
        _dist, sep, ep_name = entry.partition(_QUALIFIED_SEP)
        if sep and _norm(ep_name) == key:
            return True
    return False


def is_iac_entrypoint_platform(name: str) -> bool:
    """Whether an ALLOWED, non-reserved entry point is installed for ``name``.

    Names only, nothing imported: this is what lets contract detection and
    ``--provider`` choices see a plugin platform without loading it.
    """
    if not isinstance(name, str) or not name.strip():
        return False
    key = _norm(name)
    if key in reserved_iac_names():
        return False
    from fluid_build.plugin_manager import is_allowed

    return bool(_entrypoints_by_name().get(key)) and is_allowed(key)


def known_iac_platforms() -> List[str]:
    """Sorted platforms ``generate iac`` can serve: in-tree plus installed plugins.

    Plugin names are read, not loaded (blocked and reserved ones are left out).
    """
    from fluid_build.plugin_manager import is_allowed

    names = set(IAC_PLUGINS)
    reserved = reserved_iac_names()
    for key in _entrypoints_by_name():
        if key not in reserved and is_allowed(key):
            names.add(key)
    return sorted(names)


def iac_plugin_unavailable_reason(name: str) -> Optional[str]:
    """Why an installed plugin for ``name`` is not serving it, else ``None``."""
    from fluid_build.plugin_manager import is_allowed, override_granted

    key = _norm(name)
    eps = _entrypoints_by_name().get(key)
    if not eps:
        return None
    if key in reserved_iac_names():
        if not is_allowed(key) and any(override_granted(ep) for ep in eps):
            return (
                f"the grant for the IaC plugin from {_who(eps[0])!r} is cancelled because "
                f"{_cancelled_grant_reason(key)}"
            )
        return None
    if not is_allowed(key):
        return (
            f"the IaC plugin from {_who(eps[0])!r} is blocked by FLUID_PLUGINS_ALLOWLIST/BLOCKLIST"
        )
    if len(eps) > 1:
        dists = ", ".join(repr(_who(ep)) for ep in eps)
        return (
            f"the IaC platform {key!r} is shipped by several distributions ({dists}) and none "
            "was loaded; block the name with FLUID_PLUGINS_BLOCKLIST or uninstall the extras"
        )
    return f"the IaC plugin from {_who(eps[0])!r} was refused or failed to load (see the log)"


class IacProviderChoices:
    """argparse ``choices=`` for ``--provider``: ``auto``, the in-tree clouds, and
    the installed IaC plugin platforms.

    Membership is tested per value, and iteration (``--help``, error text) reads
    entry-point NAMES; neither imports a plugin, and nothing is read when the
    parser is built.
    """

    def __contains__(self, value: object) -> bool:
        if not isinstance(value, str):
            return False
        return value == "auto" or value in IAC_PLUGINS or is_iac_entrypoint_platform(value)

    def __iter__(self):
        return iter(["auto", *known_iac_platforms()])

    def __len__(self) -> int:
        return len(list(iter(self)))


def reset_iac_plugin_cache() -> None:
    """Forget external plugins and refusals (tests; an install into a live process).

    A built-in an external plugin replaced (a granted override) is restored from
    the snapshot taken when it registered.
    """
    global _INDEX  # noqa: PLW0603
    with _LOCK:
        _INDEX = None
        for key in list(IAC_ENTRYPOINT_PLUGINS):
            IAC_PLUGINS.pop(key, None)
            builtin = _INTREE.get(key)
            if builtin is not None:
                IAC_PLUGINS[key] = builtin
        IAC_ENTRYPOINT_PLUGINS.clear()
        _REFUSED.clear()


def discover_iac_entrypoints(logger: Optional[logging.Logger] = None) -> None:
    """Eagerly load EVERY allowed entry-point plugin. Nothing calls this at import.

    For callers that need the whole set (the state-ownership guard before a
    destructive apply, an inspection command, a test); the CLI otherwise loads by
    name through :func:`get_iac_plugin`. Same gates, per-plugin fail-isolation: a
    bad plugin is skipped and never drops the others.
    """
    log = logger or _log
    with _LOCK:
        for key, eps in list(_entrypoints_by_name().items()):
            if key in IAC_ENTRYPOINT_PLUGINS:
                continue
            _load_key(key, eps, log)
