# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""IaC plugin registry.

Keyed by cloud name — a distinct axis from ``infra.register_generator``
(artifact target) and ``providers.register_provider`` (apply provider).
Mirrors the shape of those registries so the pattern is familiar.
"""

from __future__ import annotations

from typing import Dict, Optional

from .base import IacProviderPlugin

IAC_PLUGINS: Dict[str, IacProviderPlugin] = {}


def register_iac_plugin(name: str, plugin: IacProviderPlugin) -> None:
    """Register an IaC plugin under a cloud name (``gcp``/``aws``/...)."""
    IAC_PLUGINS[name] = plugin


def get_iac_plugin(name: str) -> Optional[IacProviderPlugin]:
    """Return the registered plugin for ``name``, or ``None`` if unknown."""
    return IAC_PLUGINS.get(name)
