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

"""Catalog provider registry"""

from .base import BaseCatalogProvider, CatalogAsset, PublishResult
from .fluid_cc import FluidCommandCenterProvider

# Lazy-import optional catalog backends ─ don't crash if deps are missing
try:
    from .datamesh_manager import DataMeshManagerCatalogProvider
except Exception:
    DataMeshManagerCatalogProvider = None  # type: ignore[assignment,misc]

# Registry of available catalog providers
CATALOG_PROVIDERS = {
    "fluid-command-center": FluidCommandCenterProvider,
    "fluid_cc": FluidCommandCenterProvider,
}

# Register optional backends only when their import succeeded
if DataMeshManagerCatalogProvider is not None:
    CATALOG_PROVIDERS["datamesh-manager"] = DataMeshManagerCatalogProvider
    CATALOG_PROVIDERS["entropy-data"] = DataMeshManagerCatalogProvider
    CATALOG_PROVIDERS["dmm"] = DataMeshManagerCatalogProvider


def get_catalog_provider(catalog_type: str, config: dict) -> BaseCatalogProvider:
    """Get catalog provider instance by type

    Args:
        catalog_type: Type of catalog ('fluid-command-center', 'collibra', etc.)
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
            f"Available: {', '.join(CATALOG_PROVIDERS.keys())}"
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
