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

"""Native FLUID Command Center integration with resilience patterns

This provider publishes FLUID contracts as assets to the FLUID Command Center API.
Includes:
- Circuit breaker for fault tolerance
- Retry with exponential backoff
- Health checking before operations
- Upsert logic (create or update)
- Comprehensive error handling
"""

import asyncio
import time
from typing import Any, Dict, Optional

import httpx

from ...common import CircuitBreaker, get_auth_headers, metrics_collector
from ..base import BaseCatalogProvider, CatalogAsset, PublishResult


class FluidCommandCenterProvider(BaseCatalogProvider):
    """Native integration with FLUID Command Center

    Leverages patterns from market.py:
    - Circuit breaker for publish failures
    - Retry with exponential backoff
    - Health checking before operations
    - Metrics collection
    """

    name = "fluid_cc"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        # Initialize circuit breaker
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=config.get("circuit_breaker_threshold", 3),
            recovery_timeout=config.get("circuit_breaker_timeout", 60),
            expected_exception=httpx.HTTPError,
        )

    async def publish(self, asset: CatalogAsset) -> PublishResult:
        """Publish to FLUID Command Center API with retry logic and upsert

        Workflow:
        1. Pre-publish health check
        2. Validate asset
        3. Search for existing asset by contract ID
        4. Create new or update existing (upsert)
        5. Retry with exponential backoff on failure
        6. Record metrics
        """
        start_time = time.time()
        metrics_collector.record_publish_request(self.name)

        # Pre-publish health check
        if not await self.health_check():
            result = PublishResult(
                success=False,
                catalog_id=self.name,
                asset_id=asset.id,
                error="Catalog health check failed - endpoint not accessible",
            )
            metrics_collector.record_publish_failure(self.name, "health_check_failed")
            return result

        # Validate asset
        is_valid, error_msg = self.validate_asset(asset)
        if not is_valid:
            result = PublishResult(
                success=False,
                catalog_id=self.name,
                asset_id=asset.id,
                error=f"Validation failed: {error_msg}",
            )
            metrics_collector.record_validation_error(error_msg)
            return result

        # Retry with exponential backoff
        for attempt in range(self.max_retries):
            try:
                result = await self.circuit_breaker.call(self._publish_impl, asset)

                latency = time.time() - start_time
                metrics_collector.record_publish_success(self.name, latency)

                # Update circuit breaker stats
                cb_state = self.circuit_breaker.get_state()
                metrics_collector.update_circuit_breaker_stats(
                    self.name,
                    cb_state["state"],
                    cb_state["failure_count"],
                    cb_state["success_count"],
                )

                self.logger.info(
                    f"✅ Published {asset.name} to Command Center "
                    f"(attempt {attempt + 1}/{self.max_retries}, {latency:.2f}s)"
                )
                return result

            except Exception as e:
                if attempt < self.max_retries - 1:
                    delay = self.config.get("retry_delay", 1.0) * (2**attempt)
                    # Log more details for debugging
                    error_details = str(e)
                    if hasattr(e, "response"):
                        try:
                            error_details = f"{e} - Response: {e.response.text}"
                        except Exception:
                            pass
                    self.logger.warning(
                        f"Publish failed (attempt {attempt + 1}/{self.max_retries}), "
                        f"retrying in {delay}s: {error_details}"
                    )
                    await asyncio.sleep(delay)
                else:
                    error_msg = str(e)
                    if hasattr(e, "response"):
                        try:
                            error_msg = f"{e} - Response: {e.response.text}"
                        except Exception:
                            pass
                    self.logger.error(
                        f"❌ Publish failed after {self.max_retries} attempts: {error_msg}"
                    )
                    metrics_collector.record_publish_failure(self.name, str(type(e).__name__))
                    return PublishResult(
                        success=False, catalog_id=self.name, asset_id=asset.id, error=error_msg
                    )

    async def _publish_impl(self, asset: CatalogAsset) -> PublishResult:
        """Internal publish implementation (wrapped by circuit breaker)"""

        # Map CatalogAsset to Command Center API format
        # Note: owner_id will be overridden by the backend from authenticated user
        # but it's required by the Pydantic model, so we send a placeholder
        asset_data = {
            "name": asset.name,
            "description": asset.description,
            "type": asset.type,
            "owner_id": "placeholder",  # Will be replaced by backend from auth token
            "tags": asset.tags,
            "version": asset.version,
            "is_public": asset.sensitivity in ["public", "internal"],
            "metadata": {
                "fluid_contract_id": asset.id,  # Track contract ID for upsert
                "domain": asset.domain,
                "layer": asset.layer,
                "platform": asset.platform,
                "location": asset.location,
                "schema": asset.schema,
                "owner": asset.owner,
                "owner_email": asset.owner_email,
                "sensitivity": asset.sensitivity,
            },
        }

        # Include the full contract YAML if available
        if asset.contract_yaml:
            import hashlib

            asset_data["contract_yaml"] = asset.contract_yaml
            asset_data["contract_hash"] = hashlib.sha256(
                asset.contract_yaml.encode("utf-8")
            ).hexdigest()

        headers = get_auth_headers(self.endpoint, self.auth)

        # Debug: Log what we're sending
        import json as json_lib

        self.logger.info(
            f"Sending asset_data with {len(asset_data.get('metadata', {}))} metadata keys"
        )
        self.logger.debug(f"Full asset_data: {json_lib.dumps(asset_data, indent=2, default=str)}")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            # Check if asset already exists (by contract ID in metadata)
            existing = await self._find_by_contract_id(client, headers, asset.id)

            if existing:
                # Update existing asset (PATCH)
                self.logger.info(
                    f"Updating existing asset: {existing['id']} for contract {asset.id}"
                )
                response = await client.patch(
                    f"{self.endpoint}/api/v1/assets/{existing['id']}",
                    json=asset_data,
                    headers=headers,
                )
            else:
                # Create new asset (POST)
                self.logger.info(f"Creating new asset for contract: {asset.id}")
                response = await client.post(
                    f"{self.endpoint}/api/v1/assets", json=asset_data, headers=headers
                )

            response.raise_for_status()
            result_data = response.json()

            return PublishResult(
                success=True,
                catalog_id=self.name,
                asset_id=result_data["id"],
                catalog_url=f"{self.endpoint}/assets/{result_data['id']}",
                details={
                    "operation": "update" if existing else "create",
                    "api_asset_id": result_data["id"],
                    "contract_id": asset.id,
                },
            )

    async def _find_by_contract_id(
        self, client: httpx.AsyncClient, headers: Dict[str, str], contract_id: str
    ) -> Optional[Dict[str, Any]]:
        """Find asset by fluid_contract_id in metadata

        This enables upsert behavior - we can update existing assets
        rather than creating duplicates.
        """
        try:
            # Use the dedicated fluid_contract_id filter parameter
            response = await client.get(
                f"{self.endpoint}/api/v1/assets",
                params={"fluid_contract_id": contract_id, "limit": 1},
                headers=headers,
                timeout=10.0,
            )
            response.raise_for_status()

            results = response.json()
            assets = results.get("items", results.get("assets", []))

            if assets:
                return assets[0]

            # Fallback: text search with client-side metadata check
            response = await client.get(
                f"{self.endpoint}/api/v1/assets",
                params={"q": contract_id, "limit": 10},
                headers=headers,
                timeout=10.0,
            )
            response.raise_for_status()

            results = response.json()
            assets = results.get("items", results.get("assets", []))

            for asset in assets:
                metadata = asset.get("metadata", {})
                if metadata.get("fluid_contract_id") == contract_id:
                    return asset

            return None

        except Exception as e:
            self.logger.warning(f"Error searching for existing asset: {e}")
            return None

    async def update(self, asset: CatalogAsset) -> PublishResult:
        """Update existing asset (delegates to publish for upsert logic)"""
        return await self.publish(asset)

    async def verify(self, asset_id: str) -> bool:
        """Verify asset exists in catalog"""
        try:
            headers = get_auth_headers(self.endpoint, self.auth)
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Try to find by contract ID
                existing = await self._find_by_contract_id(client, headers, asset_id)
                return existing is not None
        except Exception as e:
            self.logger.error(f"Verification failed: {e}")
            return False

    async def health_check(self) -> bool:
        """Check if Command Center API is accessible"""
        try:
            headers = get_auth_headers(self.endpoint, self.auth)
            async with httpx.AsyncClient(timeout=5.0) as client:
                # Try to ping the API (GET /api/v1/assets with limit=1)
                response = await client.get(
                    f"{self.endpoint}/api/v1/assets", params={"limit": 1}, headers=headers
                )
                return response.status_code == 200
        except Exception as e:
            self.logger.warning(f"Health check failed: {e}")
            return False
