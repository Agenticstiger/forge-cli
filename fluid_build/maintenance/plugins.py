# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Maintenance actions other packages ship: the ``fluid_build.maintenance_actions`` group.

::

    [project.entry-points."fluid_build.maintenance_actions"]
    gdpr-erasure = "acme_pack.maintenance:GdprErasure"

An entry point loads to a class (built with no arguments), an instance or a function that
returns one, with the attributes of :class:`fluid_build.maintenance.MaintenanceAction`: the
package needs no forge import. Its ``statements(table, options)`` returns the SQL forge's Spark
runner executes on the table (:mod:`fluid_build.maintenance.statements` checks each).

Policy, the operator's (:mod:`fluid_build.plugin_manager`), as for every plugin group:

* an entry point is read by NAME and loaded only when a contract's action names its kind,
  never by ``fluid --help`` or by listing plugins;
* ``FLUID_PLUGINS_BLOCKLIST`` and ``FLUID_PLUGINS_ALLOWLIST`` apply before anything is imported;
* two distributions shipping one name are both refused;
* a kind that is missing, refused, fails to load or is not an action refuses the contract's
  action (a declared step never drops out quietly), naming the exception type only.

Borrowed, not depended on: stevedore's ``DriverManager`` and
:mod:`fluid_build.cli.import_workflow.registry`, which this follows.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger("fluid.maintenance")

#: Entry-point group of maintenance actions.
ACTIONS_GROUP = "fluid_build.maintenance_actions"

_LOCK = threading.RLock()
_LOADED: Dict[str, Any] = {}
_REFUSED: Dict[str, str] = {}
_INDEX: Dict[str, Tuple[Any, Dict[str, List[Any]]]] = {}


class MaintenanceActionError(ValueError):
    """A maintenance action's kind cannot be used; the message says why."""


def normalise_kind(name: str) -> str:
    """The spelling a kind is compared in: lower case, ``-`` for ``_``."""
    return str(name).strip().lower().replace("_", "-")


def reset_maintenance_plugin_cache() -> None:
    """Forget loaded and refused plugins (tests, and a process that installs one)."""
    with _LOCK:
        _LOADED.clear()
        _REFUSED.clear()
        _INDEX.clear()


def _index() -> Dict[str, List[Any]]:
    from fluid_build import plugin_manager

    scanner = plugin_manager._entry_points
    cached = _INDEX.get(ACTIONS_GROUP)
    if cached is None or cached[0] is not scanner:
        index: Dict[str, List[Any]] = {}
        for ep in scanner(ACTIONS_GROUP):
            index.setdefault(normalise_kind(ep.name), []).append(ep)
        cached = (scanner, index)
        _INDEX[ACTIONS_GROUP] = cached
    return cached[1]


def _who(ep: Any) -> str:
    from fluid_build.plugin_manager import entry_point_distribution

    return entry_point_distribution(ep) or "<unknown distribution>"


def _is_action(obj: Any) -> bool:
    return callable(getattr(obj, "statements", None))


def _load(key: str) -> Tuple[Optional[Any], str]:
    from fluid_build.plugin_manager import is_allowed

    eps = _index().get(key)
    if not eps:
        return None, (
            f"no installed package registers it under the {ACTIONS_GROUP} entry-point group"
        )
    if not is_allowed(key):
        return None, "the operator's FLUID_PLUGINS_ALLOWLIST / FLUID_PLUGINS_BLOCKLIST refuse it"
    if len(eps) > 1:
        return None, (
            f"{len(eps)} distributions ship it ({', '.join(_who(ep) for ep in eps)}), so "
            "neither was imported; uninstall one or block the name with FLUID_PLUGINS_BLOCKLIST"
        )
    try:
        obj = eps[0].load()
    except Exception as exc:  # noqa: BLE001 - isolate a bad plugin, name the type only
        _log.warning("maintenance action plugin %r failed to load: %s", key, type(exc).__name__)
        return None, f"importing it raised {type(exc).__name__}"
    try:
        if isinstance(obj, type) or (callable(obj) and not _is_action(obj)):
            obj = obj()
    except Exception as exc:  # noqa: BLE001
        return None, f"building it raised {type(exc).__name__}"
    if not _is_action(obj):
        return None, "it is not a maintenance action: it has no statements(table, options)"
    return obj, ""


def action_for(name: str) -> Any:
    """The maintenance action kind ``name``, loaded now. Raises
    :class:`MaintenanceActionError` saying why when nothing can serve it."""
    key = normalise_kind(name)
    with _LOCK:
        found = _LOADED.get(key)
        if found is not None:
            return found
        why = _REFUSED.get(key)
        if why is None:
            found, why = _load(key)
            if found is not None:
                _LOADED[key] = found
                return found
            _REFUSED[key] = why
    raise MaintenanceActionError(f"no maintenance action kind {key!r} can be used: {why}")


__all__ = [
    "ACTIONS_GROUP",
    "MaintenanceActionError",
    "action_for",
    "normalise_kind",
    "reset_maintenance_plugin_cache",
]
