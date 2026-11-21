# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Importer Protocol, translation report and the importer registry.

``fluid import <tool> <source>`` looks a tool up by name in three places, in this order:

1. importers registered in this process (:func:`register_importer`; also where a built-in
   or a plugin lands once it has been loaded),
2. the built-in table :data:`BUILTIN_IMPORTERS`: name to ``(module, class)``, imported the
   first time the tool is used, so naming the built-ins costs no import (``fluid import
   --help`` stays cold),
3. importers shipped by other packages in the ``fluid_build.importers`` entry-point group::

       [project.entry-points."fluid_build.importers"]
       my-tool = "my_package.forge_importer:MyImporter"

The entry point names an object with the shape of :class:`Importer`, and the package needs
no ``fluid_build`` import to provide it: a class (built with no arguments), an instance, or
a function that returns one. The object has ``import_to_contract(source, *, options)``
returning ``(contract, report)``, or ``import_to_contracts(source, *, options)`` returning
``([contract, ...], report)`` for a tool that splits a source into several products; and may
have ``can_import(source)``. The report is any object with the four lists
:class:`ImportReport` has (``mapped_one_to_one``, ``required_defaults``, ``unsupported``,
``notes``), or ``None``. ``options`` holds ``split_by``, ``conventions`` and ``dags`` when
the user gave them (strings, never opened for the plugin).

Policy, the operator's (the unified plugin manager's, see :mod:`fluid_build.plugin_manager`):

* an entry point is read by NAME only, and loaded only when its tool is asked for by name
  (``fluid import my-tool``): no other command, and not ``fluid --help``, runs plugin code;
* ``FLUID_PLUGINS_BLOCKLIST`` and ``FLUID_PLUGINS_ALLOWLIST`` apply to the entry-point name
  (case-insensitive, ``-`` equal to ``_``) before anything is imported;
* a built-in name is reserved: a plugin adds a source and never replaces one, and one that
  claims a built-in name is skipped without being imported;
* two distributions shipping one name are both refused (fail closed);
* a plugin that fails to import or to build is skipped and logged by exception type only.

Borrowed, not depended on: stevedore's ``DriverManager`` (load one driver, by name, from a
namespace) and the policy of :mod:`fluid_build.iac.registry`, which this follows in a smaller
form (no override grant).
"""

from __future__ import annotations

import importlib
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Protocol, Tuple

_log = logging.getLogger("fluid.import_workflow")


@dataclass
class ImportReport:
    """Translation report for a foreign-config import."""

    mapped_one_to_one: List[str] = field(default_factory=list)
    required_defaults: List[str] = field(default_factory=list)
    unsupported: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


class Importer(Protocol):
    name: str

    def can_import(self, source: str) -> bool: ...

    def import_to_contract(
        self,
        source: str,
        *,
        options: Optional[Dict[str, Any]] = None,
    ) -> tuple[Dict[str, Any], ImportReport]: ...


def coerce_report(report: Any) -> ImportReport:
    """An :class:`ImportReport` from whatever an importer returned.

    A plugin builds its own report object (it need not import this module): any object with
    the four lists works. Anything else in them becomes text, and a missing list is empty.
    """
    if isinstance(report, ImportReport):
        return report
    out = ImportReport()
    for name in ("mapped_one_to_one", "required_defaults", "unsupported", "notes"):
        items = getattr(report, name, None)
        if isinstance(items, (list, tuple)):
            setattr(out, name, [str(item) for item in items])
    return out


#: Importers registered in this process, by name. Built-ins and plugins arrive here on first use.
IMPORTERS: Dict[str, Importer] = {}

#: The built-in importers: tool name to ``(module, class)`` under this package. The order is
#: the order the help lists them in.
BUILTIN_IMPORTERS: Dict[str, Tuple[str, str]] = {
    "meltano": ("meltano", "MeltanoImporter"),
    "airbyte": ("airbyte", "AirbyteImporter"),
    "dlt": ("dlt", "DltImporter"),
    "singer": ("singer", "SingerImporter"),
    "dbt": ("dbt", "DbtManifestImporter"),
    "schema-registry": ("schema_registry", "SchemaRegistryImporter"),
}

_LOCK = threading.RLock()

#: Plugin names whose load was attempted and refused or failed, so one command does not
#: import (and warn about) a broken plugin twice. :func:`reset_importer_cache` clears it.
_REFUSED: set[str] = set()

#: ``(scanner, index)``: one metadata scan per process, keyed on the scanner so a test that
#: swaps ``plugin_manager._entry_points`` reads afresh.
_INDEX: Optional[Tuple[Any, Dict[str, List[Any]]]] = None


def _norm(name: str) -> str:
    """The spelling a tool name is compared in: lower case, ``-`` for ``_``."""
    return str(name).strip().lower().replace("_", "-")


def register_importer(name: str, importer: Importer) -> None:
    IMPORTERS[name] = importer


def _load_builtin(name: str) -> Importer:
    module_name, class_name = BUILTIN_IMPORTERS[name]
    with _LOCK:
        found = IMPORTERS.get(name)
        if found is not None:
            return found
        module = importlib.import_module(f"{__package__}.{module_name}")
        importer = getattr(module, class_name)()
        IMPORTERS[name] = importer
        return importer


def _plugin_index() -> Dict[str, List[Any]]:
    """``{normalised name: [entry points]}`` of the importer group. Reads names, loads nothing."""
    global _INDEX  # noqa: PLW0603
    from fluid_build import plugin_manager

    scanner = plugin_manager._entry_points
    cached = _INDEX
    if cached is None or cached[0] is not scanner:
        index: Dict[str, List[Any]] = {}
        for ep in scanner(plugin_manager.EXTRA_GROUPS["importer"]):
            index.setdefault(_norm(ep.name), []).append(ep)
        cached = (scanner, index)
        _INDEX = cached
    return cached[1]


def _plugin_names() -> List[str]:
    """Names of the installed importer plugins the operator's policy lets load, sorted.

    Names only; nothing is imported. A built-in's name is reserved, so a plugin claiming one
    is left out here as it is at load time.
    """
    from fluid_build.plugin_manager import is_allowed

    reserved = {_norm(name) for name in BUILTIN_IMPORTERS}
    return sorted(key for key in _plugin_index() if key not in reserved and is_allowed(key))


def _who(ep: Any) -> str:
    from fluid_build.plugin_manager import entry_point_distribution

    return entry_point_distribution(ep) or "<unknown distribution>"


def _admit(key: str, eps: List[Any]) -> Optional[Any]:
    """The pre-load gates for one plugin name. Returns the entry point that may load, or None
    (after a WARNING where an operator has something to do). Nothing is imported here."""
    from fluid_build.plugin_manager import is_allowed

    if not is_allowed(key):
        _log.debug("importer plugin %r skipped by allow/block policy", key)
        return None
    if key in {_norm(name) for name in BUILTIN_IMPORTERS}:
        _log.warning(
            "importer_name_reserved: plugin %s (entry point %r) tried to register the built-in "
            "importer %r; it was skipped without being imported",
            ", ".join(repr(_who(ep)) for ep in eps),
            eps[0].name,
            key,
            extra={"evt": "importer_name_reserved", "_name": key},
        )
        return None
    if len(eps) > 1:
        _log.warning(
            "importer_name_ambiguous: %d plugins (%s) ship the importer %r; none was imported; "
            "block the name with FLUID_PLUGINS_BLOCKLIST or uninstall the extras",
            len(eps),
            ", ".join(repr(_who(ep)) for ep in eps),
            key,
            extra={"evt": "importer_name_ambiguous", "_name": key},
        )
        return None
    return eps[0]


def _is_importer(obj: Any) -> bool:
    return callable(getattr(obj, "import_to_contract", None)) or callable(
        getattr(obj, "import_to_contracts", None)
    )


def _instantiate(key: str, obj: Any) -> Optional[Any]:
    """An importer from what the entry point loaded: an instance as it is, a class or a
    function called with no arguments."""
    try:
        if _is_importer(obj) and not isinstance(obj, type):
            importer = obj
        elif callable(obj):
            importer = obj()
        else:
            importer = obj
    except Exception as exc:  # noqa: BLE001 - isolate a bad plugin, log by type only
        _log.warning("importer plugin %r failed to build: %s", key, type(exc).__name__)
        return None
    if not _is_importer(importer):
        _log.warning(
            "importer plugin %r is not an importer: it has neither import_to_contract nor "
            "import_to_contracts",
            key,
        )
        return None
    return importer


def _load_plugin(key: str) -> Optional[Any]:
    with _LOCK:
        if key in _REFUSED:
            return IMPORTERS.get(key)
        found = IMPORTERS.get(key)
        if found is not None:
            return found
        eps = _plugin_index().get(key)
        if not eps:
            return None
        ep = _admit(key, eps)
        importer = None
        if ep is not None:
            try:
                obj = ep.load()
            except Exception as exc:  # noqa: BLE001 - isolate a bad plugin, log by type only
                _log.warning("importer plugin %r failed to load: %s", key, type(exc).__name__)
            else:
                importer = _instantiate(key, obj)
        if importer is None:
            _REFUSED.add(key)
            return None
        IMPORTERS[key] = importer
        return importer


def get_importer(name: str) -> Optional[Importer]:
    """The importer for tool ``name``: registered, built-in, or a plugin (loaded now).

    ``None`` when nothing serves the name, a plugin the operator's policy refuses included.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    found = IMPORTERS.get(name)
    if found is not None:
        return found
    if name in BUILTIN_IMPORTERS:
        return _load_builtin(name)
    return _load_plugin(_norm(name))


def importer_names(*, plugins: bool = True) -> List[str]:
    """Every tool name ``fluid import`` accepts: the built-ins in help order, importers
    registered in this process, then (with ``plugins``) the installed plugins.

    Plugin names are read from entry-point metadata, never loaded.
    """
    names = list(BUILTIN_IMPORTERS)
    names += sorted(name for name in IMPORTERS if name not in BUILTIN_IMPORTERS)
    if plugins:
        names += [name for name in _plugin_names() if name not in names]
    return names


class ImporterChoices:
    """argparse ``choices`` for the ``tool`` argument, backed by the registry.

    ``in`` is what argparse asks while parsing: a built-in is answered from the table, so
    ``fluid import dbt`` reads no entry-point metadata, and any other name looks the
    installed plugins up by name. Iteration (argparse's error message) lists every name. The
    argument sets a ``metavar``, so building the parser and ``--help`` never iterate.
    """

    def __contains__(self, value: object) -> bool:
        if not isinstance(value, str):
            return False
        if value in BUILTIN_IMPORTERS or value in IMPORTERS:
            return True
        return _norm(value) in _plugin_names()

    def __iter__(self) -> Iterator[str]:
        return iter(importer_names())

    def __repr__(self) -> str:
        return f"ImporterChoices({', '.join(importer_names(plugins=False))}, ...)"


def reset_importer_cache() -> None:
    """Forget what was loaded, refused and scanned (a test hook; built-ins reload on use)."""
    global _INDEX  # noqa: PLW0603
    with _LOCK:
        IMPORTERS.clear()
        _REFUSED.clear()
        _INDEX = None
