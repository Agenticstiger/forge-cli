# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""IaC plugin registry.

Keyed by cloud name — a distinct axis from ``providers.register_provider``
(the apply-provider registry). Mirrors that registry's shape so the
pattern is familiar.

In-tree clouds register on import (see ``iac/__init__.py``). External packages
register a NEW cloud the same way a provider/validator plugin does — via an
entry-point under ``fluid_build.iac_providers``. Discovery routes through the
unified :mod:`fluid_build.plugin_manager` (shared allow/block policy +
per-plugin fail-isolation), and :func:`iac.cutover.default_engine` reads this
registry, so a registered plugin cloud routes to the OpenTofu apply engine
with no core edit.

**What still requires a core edit** (this module previously claimed "zero
edits to forge-cli core", which was not true end-to-end):

* ``binding.platform`` / ``builds[].execution.runtime.platform`` is a **closed
  enum** in every shipped ``fluid-schema-*.json``. A contract naming a plugin
  cloud is rejected at validation, before the provider is ever consulted.
  Opening the enum is a schema-evolution decision, not a registry change.
* The global ``fluid --provider`` flag's ``choices=`` list is hardcoded in
  ``cli/__init__.py``. Deriving it from this registry would force plugin
  discovery — i.e. third-party ``ep.load()`` — on every ``fluid --help``,
  which is the attack surface the lazy ``command`` role deliberately avoids.

So an out-of-tree cloud is reachable through ``--provider`` only for
contracts whose declared platform the schema already admits.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Set

from .base import IacProviderPlugin

IAC_PLUGINS: Dict[str, IacProviderPlugin] = {}

#: Names registered from the ``fluid_build.iac_providers`` entry-point group,
#: i.e. clouds that live OUTSIDE this repo. ``iac.cutover.default_engine``
#: reads it: an out-of-tree cloud cannot be listed in
#: ``OPENTOFU_DEFAULT_PROVIDERS`` without the core edit the plugin system
#: exists to avoid, so its presence here is what routes it to the OpenTofu
#: engine. In-tree clouds are NOT recorded — the frozenset stays their single
#: cutover switch.
IAC_ENTRYPOINT_PLUGINS: Set[str] = set()


def register_iac_plugin(name: str, plugin: IacProviderPlugin) -> None:
    """Register an IaC plugin under a cloud name (``gcp``/``aws``/...).

    The operator allow/block policy is enforced here, at the registration
    chokepoint, so it holds for the in-tree clouds registered on import as
    well as for entry-point plugins (which ``discover_iac_entrypoints``
    already gated). Without this, blocking ``snowflake`` removed it from the
    provider registry but left the IaC plugin registered — and
    ``fluid apply`` reaches the warehouse through *this* registry, so the
    block would still have been inert on the path that emits DDL.
    """
    from fluid_build.plugin_manager import is_allowed

    if not is_allowed(name):
        logging.getLogger(__name__).debug("iac plugin %r skipped by allow/block policy", name)
        return
    IAC_PLUGINS[name] = plugin


def get_iac_plugin(name: str) -> Optional[IacProviderPlugin]:
    """Return the registered plugin for ``name``, or ``None`` if unknown."""
    return IAC_PLUGINS.get(name)


def discover_iac_entrypoints(logger: Optional[logging.Logger] = None) -> None:
    """Register external IaC cloud plugins from the ``fluid_build.iac_providers`` group.

    A third-party package declares::

        [project.entry-points."fluid_build.iac_providers"]
        mycloud = "my_pkg.iac:MyCloudIacPlugin"

    where the referenced object is an :class:`IacProviderPlugin` (a class is
    instantiated; an instance is used as-is). Per-plugin fail-isolation: a plugin
    that fails to load/instantiate is skipped (logged by type only) and never
    drops the others. Runs after the in-tree built-ins, so an external plugin may
    add a new cloud — and intentionally override a built-in of the same name.
    """
    log = logger or logging.getLogger(__name__)
    # Imported lazily to keep the IaC package decoupled from the plugin manager
    # at import time during early CLI bootstrap.
    from fluid_build.plugin_manager import ROLE_GROUPS, iter_plugins

    for name, obj in iter_plugins(ROLE_GROUPS["iac_provider"], logger=log):
        try:
            plugin = obj() if isinstance(obj, type) else obj
        except Exception as e:  # noqa: BLE001 - isolate a bad plugin, log by type only
            log.warning("iac plugin %r failed to instantiate: %s", name, type(e).__name__)
            continue
        cloud = getattr(plugin, "name", name) or name
        register_iac_plugin(cloud, plugin)
        if cloud in IAC_PLUGINS:
            # Only record what actually registered — a name the allow/block
            # policy rejected must not route to the OpenTofu engine.
            IAC_ENTRYPOINT_PLUGINS.add(cloud)
        log.debug("registered external iac plugin %r", name)
