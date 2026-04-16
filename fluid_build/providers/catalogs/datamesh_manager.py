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

"""
Entropy Data / Data Mesh Manager catalog adapter.

Wraps :class:`DataMeshManagerProvider` behind the
:class:`BaseCatalogProvider` interface so that
``fluid publish --catalog datamesh-manager`` works.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .base import BaseCatalogProvider, CatalogAsset, PublishResult

LOG = logging.getLogger(__name__)


class DataMeshManagerCatalogProvider(BaseCatalogProvider):
    """Catalog adapter for Entropy Data / Data Mesh Manager."""

    name = "datamesh-manager"

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        # Lazy-import to avoid hard dependency at load time
        from fluid_build.providers.datamesh_manager import DataMeshManagerProvider

        api_key = config.get("api_key") or config.get("auth", {}).get("api_key", "")
        api_url = config.get("endpoint") or config.get("url", "")
        self._provider = DataMeshManagerProvider(
            api_key=api_key or None,
            api_url=api_url or None,
        )

    # -- BaseCatalogProvider interface --------------------------------------

    async def publish(self, asset: CatalogAsset) -> PublishResult:
        """Publish *asset* as a data product to Entropy Data."""
        fluid = self._asset_to_fluid(asset)
        try:
            result = self._provider.apply(fluid, publish_contract=True)
            return PublishResult(
                success=True,
                catalog_id=self.name,
                asset_id=asset.id,
                catalog_url=result.get("url"),
                details=result,
            )
        except Exception as exc:
            return PublishResult(
                success=False,
                catalog_id=self.name,
                asset_id=asset.id,
                error=str(exc),
            )

    async def update(self, asset: CatalogAsset) -> PublishResult:
        # PUT is idempotent — publish == update
        return await self.publish(asset)

    async def verify(self, asset_id: str) -> bool:
        try:
            self._provider.verify(asset_id)
            return True
        except Exception:
            return False

    async def delete(self, asset_id: str) -> bool:
        try:
            return self._provider.delete(asset_id)
        except Exception:
            return False

    async def health_check(self) -> bool:
        try:
            self._provider.list_products()
            return True
        except Exception:
            return False

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _asset_to_fluid(asset: CatalogAsset) -> Dict[str, Any]:
        """Convert a CatalogAsset back to a minimal FLUID dict."""
        fluid: Dict[str, Any] = {
            "id": asset.id,
            "name": asset.name,
            "description": asset.description,
            "metadata": {
                "name": asset.name,
                "description": asset.description,
                "domain": asset.domain,
                "version": asset.version,
                "tags": asset.tags,
                "layer": asset.layer,
                "status": "active",
            },
            "owner": {
                "team": asset.owner,
                "email": asset.owner_email,
            },
        }

        # Build a minimal expose from location info
        if asset.location or asset.platform != "unknown":
            expose: Dict[str, Any] = {
                "id": asset.id,
                "provider": asset.platform,
            }
            if asset.location:
                expose["location"] = (
                    asset.location if isinstance(asset.location, str) else str(asset.location)
                )
            if asset.schema:
                expose["schema"] = {"fields": asset.schema}
            fluid["exposes"] = [expose]

        return fluid
