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

"""Single source of truth for ``fluid forge data-model from-source`` metadata
sources, and the plugin seam that makes them extensible (issue #247).

Before this module the source list was hardcoded as an argparse ``choices``
enum, an MCP ``Literal``, AND two duplicated dispatch dicts (``cli.mcp`` +
``cli.forge_data_model``) that drifted. A new ingest source could not be added
without forking the CLI — inconsistent with the rest of the plugin surface
(``fluid_build.providers`` / ``.commands`` / ``.apply_hooks`` / ``.extension_*``).

This registry merges **built-in** sources with **plugin** sources discovered
from the ``fluid_build.source_adapters`` entry-point group, mirroring
``fluid_build.providers`` (``providers/__init__.py``). Two kinds of source:

* ``catalog`` — a :class:`~fluid_build.copilot.catalog.base.CatalogAdapter`
  subclass with a ``from_resolver(...)`` classmethod (Snowflake / Unity /
  BigQuery / Dataplex / Glue / DataHub / Datamesh-Manager, plus plugins).
* ``jdbc`` — a JDBC-introspectable database (postgres / mysql / sqlite) routed
  through the duckdb-extension scanner; no credential-resolver adapter.

Design constraints:

* **Lightweight.** The CLI (``cli.forge_data_model``) must resolve sources
  without importing ``cli.mcp`` (yaml + history + audit trail …). This module
  imports only the stdlib at top level; adapter classes load lazily.
* **Lazy load.** Discovery records entry points WITHOUT calling ``.load()`` so
  building ``--source`` choices at every ``fluid --help`` stays cheap (the
  startup-budget gate). Plugin classes import only at dispatch time.
* **Fail open.** A broken plugin is logged and skipped — never crashes the CLI.
* **Built-ins win.** A plugin cannot silently shadow a built-in source name.

A plugin registers in its own ``pyproject.toml``::

    [project.entry-points."fluid_build.source_adapters"]
    my-catalog = "my_pkg.adapter:MyCatalogAdapter"

where ``MyCatalogAdapter`` is a ``CatalogAdapter`` subclass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("fluid.source_adapters")

EP_GROUP = "fluid_build.source_adapters"

# ── Built-in sources ─────────────────────────────────────────────────────
# Catalog adapters: ``name -> "module.path:ClassName"`` (lazy-imported).
_BUILTIN_CATALOG_ADAPTERS: Dict[str, str] = {
    "snowflake": "fluid_build.copilot.catalog.snowflake:SnowflakeCatalogAdapter",
    "unity": "fluid_build.copilot.catalog.unity:UnityCatalogAdapter",
    "bigquery": "fluid_build.copilot.catalog.bigquery:BigQueryCatalogAdapter",
    "dataplex": "fluid_build.copilot.catalog.dataplex:DataplexCatalogAdapter",
    "glue": "fluid_build.copilot.catalog.glue:GlueCatalogAdapter",
    "datahub": "fluid_build.copilot.catalog.datahub:DataHubCatalogAdapter",
    "datamesh_manager": (
        "fluid_build.copilot.catalog.datamesh_manager:DataMeshManagerCatalogAdapter"
    ),
}

# JDBC-introspectable databases routed through the duckdb scanner (no adapter
# class). ``postgresql`` is an alias of ``postgres``.
_BUILTIN_JDBC_SOURCES: tuple = ("postgres", "postgresql", "mysql", "sqlite")


@dataclass
class SourceAdapterSpec:
    """One registered metadata source.

    ``kind`` is ``"catalog"`` (a ``CatalogAdapter`` subclass, resolved via
    :func:`resolve_catalog_adapter_class`) or ``"jdbc"`` (duckdb scanner path).
    ``target`` is a ``"module:Class"`` string for built-in catalog adapters, an
    ``importlib.metadata.EntryPoint`` for plugins (loaded lazily), or ``None``
    for JDBC sources. ``origin`` is ``"builtin"`` or ``"entrypoint"``.
    """

    name: str
    kind: str
    target: Any = None
    origin: str = "builtin"


_REGISTRY: Dict[str, SourceAdapterSpec] = {}
_discovered = False


def _seed_builtins() -> None:
    for name, dotted in _BUILTIN_CATALOG_ADAPTERS.items():
        _REGISTRY[name] = SourceAdapterSpec(name=name, kind="catalog", target=dotted)
    for name in _BUILTIN_JDBC_SOURCES:
        _REGISTRY[name] = SourceAdapterSpec(name=name, kind="jdbc", target=None)


def _discover_entrypoints(logger: Optional[logging.Logger]) -> None:
    """Merge ``fluid_build.source_adapters`` plugins into the registry.

    Mirrors ``providers/__init__.py::_discover_entrypoints``: tolerant of both
    the Python <3.10 (``dict``) and >=3.10 (``select``) ``entry_points`` APIs,
    fail-open on discovery errors, and per-plugin try/except so one broken
    plugin never breaks the CLI. Built-in names are NOT overridden.

    Entry points are NOT ``.load()``-ed here — only their ``name`` is recorded
    so listing ``--source`` choices stays import-cheap. The class imports
    lazily in :func:`resolve_catalog_adapter_class`.
    """
    log = logger or LOG
    try:
        import importlib.metadata as md

        all_eps = md.entry_points()
        if hasattr(all_eps, "select"):
            eps = all_eps.select(group=EP_GROUP)
        else:  # Python < 3.10
            eps = all_eps.get(EP_GROUP, [])
    except Exception as exc:  # noqa: BLE001 — discovery itself failed; fail open
        log.warning("source adapter discovery failed: %s", type(exc).__name__)
        return

    for ep in eps:
        name = getattr(ep, "name", None)
        if not name:
            continue
        if name in _REGISTRY and _REGISTRY[name].origin == "builtin":
            log.warning(
                "source adapter plugin %r shadows a built-in source; keeping the built-in",
                name,
            )
            continue
        _REGISTRY[name] = SourceAdapterSpec(
            name=name, kind="catalog", target=ep, origin="entrypoint"
        )


def discover_source_adapters(
    logger: Optional[logging.Logger] = None, *, force: bool = False
) -> None:
    """Populate the registry: built-ins first, then entry-point plugins.

    Idempotent — repeated calls are cheap no-ops unless ``force=True``.
    """
    global _discovered
    if _discovered and not force:
        return
    _REGISTRY.clear()
    _seed_builtins()
    _discover_entrypoints(logger)
    _discovered = True


def _ensure_discovered() -> None:
    if not _discovered:
        discover_source_adapters()


def list_source_adapters(*, include_blocked: bool = True) -> List[str]:
    """All registered source names (built-in + plugin), sorted.

    ``include_blocked=False`` drops plugin adapters the operator allow/block
    policy will refuse to load — for surfaces that *offer* a source to a
    caller (the ``--source`` choice list), where advertising one that raises
    the moment it is selected is a listing lie. Kept default-True for the
    "Supported: ..." hint on an unknown-source error, where naming everything
    installed is the useful answer.
    """
    _ensure_discovered()
    names = sorted(_REGISTRY)
    if include_blocked:
        return names
    return [n for n in names if not is_source_adapter_blocked(n)]


def list_catalog_sources() -> List[str]:
    """Catalog-kind source names (the ones the catalog-only MCP tools accept)."""
    _ensure_discovered()
    return sorted(n for n, s in _REGISTRY.items() if s.kind == "catalog")


def list_jdbc_sources() -> List[str]:
    """JDBC-kind source names (duckdb-scanner path)."""
    _ensure_discovered()
    return sorted(n for n, s in _REGISTRY.items() if s.kind == "jdbc")


def is_jdbc_source(name: str) -> bool:
    _ensure_discovered()
    spec = _REGISTRY.get(str(name).lower().strip())
    return bool(spec and spec.kind == "jdbc")


def get_source_adapter(name: str) -> Optional[SourceAdapterSpec]:
    _ensure_discovered()
    return _REGISTRY.get(str(name).lower().strip())


def is_source_adapter_blocked(name: str) -> bool:
    """Whether the operator allow/block policy will refuse to load ``name``.

    Built-in sources are not entry-point plugins and are never policy-gated —
    only ``origin == "entrypoint"`` specs go through ``is_allowed``, matching
    the gate in :func:`resolve_catalog_adapter_class`.
    """
    _ensure_discovered()
    spec = _REGISTRY.get(str(name).lower().strip())
    if spec is None or spec.origin != "entrypoint":
        return False
    from fluid_build.plugin_manager import is_allowed

    return not is_allowed(spec.name)


def source_adapter_inventory() -> List[Dict[str, str]]:
    """Inventory for the ``list_source_adapters`` MCP tool: one dict per source
    with ``name`` / ``kind`` / ``status`` / ``origin``.

    ``status`` reports the allow/block policy, so a caller is never offered a
    source that will fail the moment it is selected. It used to be the constant
    ``"available"``: a blocklisted plugin adapter was advertised as available
    and only :func:`resolve_catalog_adapter_class` enforced the policy — code
    execution was correctly prevented, but every listing surface lied about it.
    ``fluid plugins`` has always marked blocked entries; this matches it.
    """
    _ensure_discovered()
    return [
        {
            "name": s.name,
            "kind": s.kind,
            "status": "blocked" if is_source_adapter_blocked(s.name) else "available",
            "origin": s.origin,
        }
        for s in sorted(_REGISTRY.values(), key=lambda s: (s.kind, s.name))
    ]


def resolve_catalog_adapter_class(name: str) -> Any:
    """Return the ``CatalogAdapter`` subclass for catalog source ``name``.

    Imports lazily — built-in adapters from their ``"module:Class"`` string,
    plugin adapters via ``EntryPoint.load()``. Raises ``RuntimeError`` for an
    unknown source or one that isn't catalog-kind (JDBC sources route through
    the duckdb scanner, not an adapter class).
    """
    _ensure_discovered()
    key = str(name).lower().strip()
    spec = _REGISTRY.get(key)
    if spec is None:
        supported = ", ".join(list_source_adapters())
        raise RuntimeError(f"Unknown source adapter: {name!r}. Supported: {supported}.")
    if spec.kind != "catalog":
        raise RuntimeError(
            f"Source {name!r} is a {spec.kind} source, not a catalog adapter; "
            "it has no CatalogAdapter class (JDBC sources use the duckdb scanner)."
        )
    target = spec.target
    if isinstance(target, str):  # built-in "module:Class"
        module_path, class_name = target.split(":", 1)
        module = __import__(module_path, fromlist=[class_name])
        return getattr(module, class_name)
    # Third-party plugin EntryPoint — gate by the operator allow/block policy
    # BEFORE the lazy load, so a blocked source adapter's code never executes.
    from fluid_build.plugin_manager import is_allowed

    if not is_allowed(key):
        raise RuntimeError(
            f"Source adapter {name!r} is blocked by the operator allow/block policy "
            f"(FLUID_PLUGINS_ALLOWLIST / FLUID_PLUGINS_BLOCKLIST)."
        )
    return target.load()
