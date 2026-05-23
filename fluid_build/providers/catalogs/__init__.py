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

"""Catalog provider registry.

Single source of truth for ``fluid publish --target`` targets. Two
classes of backend land here:

1. **Native async providers** (``fluid-command-center``,
   ``datamesh-manager``) — full :class:`BaseCatalogProvider` subclasses
   with their own custom config shapes (multiple auth modes, ODCS /
   ODPS toggles, circuit breakers). Hand-wired below.

2. **Registrar-backed plug-in backends** — every catalog declared via
   :func:`~fluid_build.api.catalog_backend.register_catalog_backend`.
   Each registrar module under
   ``fluid_build/build_runners/catalog_registrars/<name>.py`` calls
   that function at import time; here we import the registrar package
   to trigger those calls, then auto-populate ``CATALOG_PROVIDERS``
   with one entry per declared backend (canonical name + each alias).

The contract-driven acquisition path
(``properties.catalog.register: [...]``) goes through
``build_runners/_catalog.py::build_registrar``, which consults this
same registry plus the symmetric ``ProviderBackedRegistrar`` adapter
so the native async providers are also reachable from contracts.

Adding a new catalog: drop a new ``catalog_registrars/<name>.py`` file
with a registrar dataclass + a ``register_catalog_backend(...)`` call.
No edits to this module, ``config_manager``, or test fixtures
required.
"""

from .base import BaseCatalogProvider, CatalogAsset, PublishResult
from .fluid_cc import FluidCommandCenterProvider

# Lazy-import optional native async backends ─ don't crash if deps are missing
try:
    from .datamesh_manager import DataMeshManagerCatalogProvider
except Exception:
    DataMeshManagerCatalogProvider = None  # type: ignore[assignment,misc]

# Importing the registrar package triggers each module's
# ``register_catalog_backend(...)`` side effect, populating the
# backend registry consulted below. Guarded only against future
# dependency rearrangement — every module currently in-tree imports
# cleanly.
try:
    import fluid_build.build_runners.catalog_registrars  # noqa: F401
    from fluid_build.api.catalog_backend import all_catalog_backend_specs

    from ._registrar_adapter import build_registrar_backed_provider

    _backend_specs = all_catalog_backend_specs()
except Exception:  # pragma: no cover — defensive
    _backend_specs = []
    build_registrar_backed_provider = None  # type: ignore[assignment]


# Native async providers (hand-wired — they don't fit the registrar shape)
CATALOG_PROVIDERS = {
    "fluid-command-center": FluidCommandCenterProvider,
    "fluid_cc": FluidCommandCenterProvider,
}

if DataMeshManagerCatalogProvider is not None:
    CATALOG_PROVIDERS["datamesh-manager"] = DataMeshManagerCatalogProvider
    CATALOG_PROVIDERS["entropy-data"] = DataMeshManagerCatalogProvider
    CATALOG_PROVIDERS["dmm"] = DataMeshManagerCatalogProvider

# Auto-register every plug-in backend declared via register_catalog_backend.
# Native providers above keep priority over any backend that shares a name —
# this allows opt-in shadowing during migrations without breaking either path.
if build_registrar_backed_provider is not None:
    for _spec in _backend_specs:
        _provider_cls = build_registrar_backed_provider(_spec)
        for _name in _spec.all_names:
            CATALOG_PROVIDERS.setdefault(_name, _provider_cls)


def get_catalog_provider(catalog_type: str, config: dict) -> BaseCatalogProvider:
    """Get catalog provider instance by type

    Args:
        catalog_type: Type of catalog ('fluid-command-center',
            'datahub', etc.). Aliases (e.g. ``aws-glue``) resolve to
            the same backend as the canonical name (``glue``).
        config: Configuration dictionary for the provider

    Returns:
        Instantiated catalog provider

    Raises:
        ValueError: If catalog type is not supported
    """
    provider_class = CATALOG_PROVIDERS.get(catalog_type)
    if not provider_class:
        raise ValueError(
            f"Unsupported catalog type: {catalog_type}. "
            f"Available: {', '.join(sorted(CATALOG_PROVIDERS.keys()))}"
        )

    return provider_class(config)


__all__ = [
    "BaseCatalogProvider",
    "CatalogAsset",
    "PublishResult",
    "FluidCommandCenterProvider",
    "CATALOG_PROVIDERS",
    "get_catalog_provider",
]
