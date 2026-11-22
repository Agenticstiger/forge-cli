# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Route kinds and default routes other packages ship: two entry-point groups.

``fluid_build.alert_notifiers``: a route kind (:class:`fluid_build.alerts.AlertNotifier`)::

    [project.entry-points."fluid_build.alert_notifiers"]
    teams = "acme_alerts.forge:TeamsNotifier"

``fluid_build.alert_defaults``: the routes an organisation's pack adds to every product's
DAGs (a contract opts out with ``alerts.defaults: false``, or replaces one by naming a route
of its own the same)::

    [project.entry-points."fluid_build.alert_defaults"]
    acme = "acme_pack.alerts:default_routes"

A defaults entry point loads to a list of routes, written as a contract writes them, or to a
function that returns one. A kind entry point loads to a class (built with no arguments), an
instance or a function that returns one, with the attributes of ``AlertNotifier``: the package
needs no forge import.

Policy, the operator's (the unified plugin manager's, :mod:`fluid_build.plugin_manager`):

* a kind's entry point is read by NAME and loaded only when a route names that kind; the
  defaults are loaded when a DAG is rendered or a contract validated, never by ``fluid --help``
  or by listing plugins;
* ``FLUID_PLUGINS_BLOCKLIST`` and ``FLUID_PLUGINS_ALLOWLIST`` apply to the entry-point name
  before anything is imported;
* a built-in kind's name is reserved: a plugin adds a kind and never replaces one;
* two distributions shipping one name are both refused;
* nothing a route names is ever dropped quietly: a kind that is missing, refused, fails to
  load or is not a kind refuses the route (the DAG would otherwise not alert), as does a
  defaults entry point that fails, naming the exception type only.

Borrowed, not depended on: stevedore's ``DriverManager`` (one driver, by name, from a
namespace) and :mod:`fluid_build.cli.import_workflow.registry`, which this follows.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fluid_build.alerts.builtin import BUILTIN_KINDS
from fluid_build.alerts.model import EVENTS, AlertRouteError

_log = logging.getLogger("fluid.alerts")

#: Entry-point group of route kinds.
NOTIFIER_GROUP = "fluid_build.alert_notifiers"

#: Entry-point group of an organisation's default routes.
DEFAULTS_GROUP = "fluid_build.alert_defaults"

#: ``module.path:ClassName`` of an Airflow notifier: dotted identifiers only, written into the
#: DAG as an import.
_NOTIFIER_PATH = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*\Z"
)

#: A pip requirement as the DAG's documentation lists it: no quote, backslash or newline.
_REQUIREMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~ ]{0,119}\Z")

_LOCK = threading.RLock()

#: Kinds loaded in this process, by normalised name.
_KINDS: Dict[str, Any] = {}

#: Why a kind could not be loaded, by normalised name, so one command reports it once.
_REFUSED: Dict[str, str] = {}

#: ``(scanner, index)``: one metadata scan per process per group, keyed on the scanner so a
#: test that swaps ``plugin_manager._entry_points`` reads afresh.
_INDEX: Dict[str, Tuple[Any, Dict[str, List[Any]]]] = {}


def normalise_kind(name: str) -> str:
    """The spelling a kind is compared in: lower case, ``-`` for ``_``."""
    return str(name).strip().lower().replace("_", "-")


def reset_alert_plugin_cache() -> None:
    """Forget loaded and refused plugins (tests, and a process that installs one)."""
    with _LOCK:
        _KINDS.clear()
        _REFUSED.clear()
        _INDEX.clear()


def _index(group: str) -> Dict[str, List[Any]]:
    """``{normalised name: [entry points]}`` of ``group``. Reads names, loads nothing."""
    from fluid_build import plugin_manager

    scanner = plugin_manager._entry_points
    cached = _INDEX.get(group)
    if cached is None or cached[0] is not scanner:
        index: Dict[str, List[Any]] = {}
        for ep in scanner(group):
            index.setdefault(normalise_kind(ep.name), []).append(ep)
        cached = (scanner, index)
        _INDEX[group] = cached
    return cached[1]


def _who(ep: Any) -> str:
    from fluid_build.plugin_manager import entry_point_distribution

    return entry_point_distribution(ep) or "<unknown distribution>"


def _admit(group: str, key: str, eps: List[Any]) -> Tuple[Optional[Any], str]:
    """The pre-load gates for one name: ``(entry point, "")`` or ``(None, why)``. Imports
    nothing."""
    from fluid_build.plugin_manager import is_allowed

    if not is_allowed(key):
        return None, "the operator's FLUID_PLUGINS_ALLOWLIST / FLUID_PLUGINS_BLOCKLIST refuse it"
    if group == NOTIFIER_GROUP and key in BUILTIN_KINDS:
        _log.warning(
            "alert_notifier_name_reserved: plugin %s (entry point %r) tried to register the "
            "built-in alert kind %r; it was skipped without being imported",
            ", ".join(repr(_who(ep)) for ep in eps),
            eps[0].name,
            key,
            extra={"evt": "alert_notifier_name_reserved", "_name": key},
        )
        return None, "it is a built-in kind's name, which a plugin may not take"
    if len(eps) > 1:
        return None, (
            f"{len(eps)} distributions ship it ({', '.join(_who(ep) for ep in eps)}), so "
            "neither was imported; uninstall one or block the name with FLUID_PLUGINS_BLOCKLIST"
        )
    return eps[0], ""


def _kind_problem(obj: Any) -> str:
    """Why ``obj`` is not a route kind, or ``""`` when it is one."""
    path = getattr(obj, "notifier", None)
    if not isinstance(path, str) or not _NOTIFIER_PATH.match(path):
        return "its `notifier` is not a '<module>:<class>' path of an Airflow notifier"
    if not callable(getattr(obj, "kwargs", None)):
        return "it has no `kwargs(route, alert)` method"
    for attr in ("requirements", "async_requirements"):
        values = getattr(obj, attr, ())
        if not isinstance(values, (list, tuple)) or not all(
            isinstance(v, str) and _REQUIREMENT.match(v) for v in values
        ):
            return f"its `{attr}` is not a list of plain pip requirements"
    events = getattr(obj, "events", EVENTS)
    try:
        unknown = sorted(set(events) - set(EVENTS))
    except TypeError:
        return "its `events` is not a set of event names"
    if unknown:
        return f"its `events` names unknown events ({', '.join(map(str, unknown))})"
    return ""


def _build(obj: Any) -> Tuple[Optional[Any], str]:
    """A kind from what an entry point loaded: an instance as it is, a class or a function
    called with no arguments."""
    try:
        if isinstance(obj, type) or (callable(obj) and _kind_problem(obj)):
            obj = obj()
    except Exception as exc:  # noqa: BLE001 - isolate a bad plugin, name the type only
        return None, f"building it raised {type(exc).__name__}"
    problem = _kind_problem(obj)
    return (None, f"it is not an alert kind: {problem}") if problem else (obj, "")


def kind_for(name: str) -> Any:
    """The route kind ``name``: a built-in, or the plugin that ships it (loaded now).

    Raises :class:`AlertRouteError` saying why when nothing serves the name, so a route is
    never left out of a DAG without a word.
    """
    key = normalise_kind(name)
    builtin = BUILTIN_KINDS.get(key)
    if builtin is not None:
        return builtin
    with _LOCK:
        found = _KINDS.get(key)
        if found is not None:
            return found
        why = _REFUSED.get(key)
        if why is None:
            found, why = _load_kind(key)
            if found is not None:
                _KINDS[key] = found
                return found
            _REFUSED[key] = why
    raise AlertRouteError(f"no alert kind {key!r} can be used: {why}")


def _load_kind(key: str) -> Tuple[Optional[Any], str]:
    eps = _index(NOTIFIER_GROUP).get(key)
    if not eps:
        return None, (
            f"it is not a built-in kind ({', '.join(BUILTIN_KINDS)}) and no installed package "
            f"registers it under the {NOTIFIER_GROUP} entry-point group"
        )
    ep, why = _admit(NOTIFIER_GROUP, key, eps)
    if ep is None:
        return None, why
    try:
        obj = ep.load()
    except Exception as exc:  # noqa: BLE001 - isolate a bad plugin, name the type only
        _log.warning("alert notifier plugin %r failed to load: %s", key, type(exc).__name__)
        return None, f"importing it raised {type(exc).__name__}"
    return _build(obj)


def refused_default_packs() -> List[str]:
    """The installed ``fluid_build.alert_defaults`` entry points the operator's allow/block
    policy leaves out, sorted. Names only: nothing is imported."""
    from fluid_build.plugin_manager import is_allowed

    return sorted(key for key in _index(DEFAULTS_GROUP) if not is_allowed(key))


def default_routes() -> List[Tuple[str, Sequence[Any]]]:
    """``[(entry point name, routes)]`` of every allowed ``fluid_build.alert_defaults`` entry
    point, sorted by name. Raises :class:`AlertRouteError` when one cannot be read: the
    organisation counts on its routes, so a DAG without them is not rendered."""
    out: List[Tuple[str, Sequence[Any]]] = []
    for key, eps in sorted(_index(DEFAULTS_GROUP).items()):
        from fluid_build.plugin_manager import is_allowed

        if not is_allowed(key):
            continue
        ep, why = _admit(DEFAULTS_GROUP, key, eps)
        if ep is None:
            raise AlertRouteError(f"the default alert routes {key!r} cannot be used: {why}")
        try:
            loaded = ep.load()
            routes = loaded() if callable(loaded) else loaded
        except Exception as exc:  # noqa: BLE001 - name the type only, never the text
            raise AlertRouteError(
                f"the default alert routes {key!r} could not be read: loading them raised "
                f"{type(exc).__name__}"
            ) from None
        if not isinstance(routes, (list, tuple)):
            raise AlertRouteError(
                f"the default alert routes {key!r} are not a list of routes "
                f"(got {type(routes).__name__})"
            )
        out.append((key, routes))
    return out


__all__ = [
    "DEFAULTS_GROUP",
    "NOTIFIER_GROUP",
    "default_routes",
    "kind_for",
    "normalise_kind",
    "refused_default_packs",
    "reset_alert_plugin_cache",
]
