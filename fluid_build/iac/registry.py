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
entry-point under ``fluid_build.iac_providers`` — so adding a cloud needs **zero
edits to forge-cli core**, fulfilling the framework's modularity promise.
Discovery routes through the unified :mod:`fluid_build.plugin_manager` (shared
allow/block policy + per-plugin fail-isolation).
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

from .base import IacProviderPlugin

IAC_PLUGINS: Dict[str, IacProviderPlugin] = {}


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
        logging.getLogger(__name__).debug(
            "iac plugin %r skipped by allow/block policy", name
        )
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
        register_iac_plugin(getattr(plugin, "name", name) or name, plugin)
        log.debug("registered external iac plugin %r", name)
