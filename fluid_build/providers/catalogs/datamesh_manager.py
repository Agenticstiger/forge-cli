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

import yaml

from .base import BaseCatalogProvider, CatalogAsset, PublishResult

LOG = logging.getLogger(__name__)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class DataMeshManagerCatalogProvider(BaseCatalogProvider):
    """Catalog adapter for Entropy Data / Data Mesh Manager."""

    name = "datamesh-manager"

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        # Lazy-import to avoid hard dependency at load time
        from fluid_build.providers.datamesh_manager import DataMeshManagerProvider

        api_key = config.get("api_key") or config.get("auth", {}).get("api_key", "")
        api_url = config.get("endpoint") or config.get("url", "")
        # Default to ODPS-Bitol: the Entropy / Data Mesh Manager server treats
        # DPS as legacy and returns a 500 (wrapped by urllib3 retry-exhaust
        # into "too many 500 error responses") for DPS payloads on organizations
        # configured as ODPS-only. Explicit config still wins.
        self._data_product_specification = (
            config.get("data_product_specification")
            or config.get("dataProductSpecification")
            or "odps"
        )
        self._provider_hint = config.get("provider_hint") or "odps"
        self._odps_lineage_mode = (
            config.get("odps_lineage_mode")
            or config.get("odpsLineageMode")
            or config.get("lineage_mode")
        )
        self._auto_approve_access = _as_bool(
            config.get("auto_approve_access") or config.get("autoApproveAccess")
        )
        self._provider = DataMeshManagerProvider(
            api_key=api_key or None,
            api_url=api_url or None,
            odps_lineage_mode=self._odps_lineage_mode,
            auto_approve_access=self._auto_approve_access,
        )

    # -- BaseCatalogProvider interface --------------------------------------

    async def publish(self, asset: CatalogAsset) -> PublishResult:
        """Publish *asset* as a data product to Entropy Data."""
        fluid = self._asset_to_fluid(asset)
        try:
            result = self._provider.apply(
                fluid,
                publish_contract=True,
                data_product_specification=self._data_product_specification,
                provider_hint=self._provider_hint,
                auto_approve_access=self._auto_approve_access,
            )
        except Exception as exc:
            if self._should_retry_with_odps(exc):
                result = self._provider.apply(
                    fluid,
                    publish_contract=True,
                    data_product_specification="odps",
                    provider_hint="odps",
                    auto_approve_access=self._auto_approve_access,
                )
            else:
                return PublishResult(
                    success=False,
                    catalog_id=self.name,
                    asset_id=asset.id,
                    error=str(exc),
                )

        try:
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
        """Ping the catalog with a cheap read to confirm auth + reachability.

        Surfaces the underlying exception at ``WARNING`` before returning
        ``False`` so the generic "endpoint not accessible" error downstream
        is debuggable. Common causes and what they look like here:

        - 403 from Entropy: ``DMM_API_KEY`` missing/stale (or the project's
          project ``.env`` clobbered a shell-set key — see
          ``credentials/dotenv_store.py`` for the override semantics).
        - 5xx / connection refused: server not running on ``DMM_API_URL``.
        - DNS / TLS / proxy: network layer wrong URL or blocked egress.
        """
        try:
            self._provider.list_products()
            return True
        except Exception as exc:  # noqa: BLE001 — we deliberately log any
            LOG.warning(
                "Catalog %s health check failed: %s: %s",
                self.name,
                type(exc).__name__,
                exc,
            )
            return False

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _asset_to_fluid(asset: CatalogAsset) -> Dict[str, Any]:
        """Convert a CatalogAsset back to a minimal FLUID dict."""
        if asset.contract_yaml:
            try:
                parsed = yaml.safe_load(asset.contract_yaml)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                LOG.debug(
                    "Falling back to minimal FLUID mapping for asset %s", asset.id, exc_info=True
                )

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

    def _should_retry_with_odps(self, exc: Exception) -> bool:
        """Retry with ODPS only when the server explicitly rejects DPS."""
        if self._data_product_specification or self._provider_hint:
            return False

        message = str(exc).lower()
        return (
            "supported types: odps" in message
            or "type 'dps' is not supported" in message
            or 'type "dps" is not supported' in message
        )
