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

"""CommandCenterConnector — connector for the Command Center catalog.

Physically extracted from the monolithic ``cli/market.py`` so the
per-catalog auth + search idiosyncrasies stay self-contained.
Inherits the shared :class:`BaseCatalogConnector` from
:mod:`fluid_build.cli.market_catalogs._base`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import yaml

from fluid_build.cli.market import (
    BaseCatalogConnector,
    CatalogType,
    CircuitBreaker,
    DataProductLayer,
    DataProductMetadata,
    DataProductStatus,
    SearchFilters,
)


class CommandCenterConnector(BaseCatalogConnector):
    """
    FLUID Command Center connector

    Discovers published data products from Command Center's catalog.
    Note: This is different from marketplace blueprints - these are
    actual deployed data products with lineage, quality metrics, and SLAs.
    """

    async def _connect_impl(self) -> bool:
        """Connect to Command Center catalog"""
        try:
            # Import here to avoid circular dependency
            from ._command_center import get_command_center_client

            cc = get_command_center_client(logger=self.logger)

            if not cc.available or not cc.features.catalog:
                self.logger.warning("Command Center catalog not available")
                return False

            self.base_url = cc.get_catalog_url()
            self.cc_client = cc

            # Initialize aiohttp session
            import aiohttp

            self.session = aiohttp.ClientSession()

            self.logger.info(f"Connected to Command Center catalog: {self.base_url}")
            return True

        except ImportError as e:
            self.logger.error(f"Failed to import Command Center client: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Failed to connect to Command Center: {e}")
            return False

    async def _search_data_products_impl(self, filters: SearchFilters) -> List[DataProductMetadata]:
        """Search Command Center's data product catalog"""
        try:

            # Build query parameters
            params = {}

            if filters.query:
                params["query"] = filters.query
            if filters.domains:
                params["domains"] = ",".join(filters.domains)
            if filters.owners:
                params["owners"] = ",".join(filters.owners)
            if filters.tags:
                params["tags"] = ",".join(filters.tags)
            if filters.layers:
                params["layers"] = ",".join([layer.value for layer in filters.layers])
            if filters.statuses:
                params["statuses"] = ",".join([status.value for status in filters.statuses])
            if filters.min_quality_score:
                params["min_quality"] = filters.min_quality_score
            if filters.limit:
                params["limit"] = filters.limit
            if filters.offset:
                params["offset"] = filters.offset

            # Make request
            async with self.session.get(self.base_url, params=params) as response:
                if response.status != 200:
                    self.logger.warning(f"Command Center search failed: {response.status}")
                    return []

                data = await response.json()
                products = []

                # Map Command Center schema to DataProductMetadata
                for item in data.get("items", []):
                    try:
                        # Parse layer
                        layer = None
                        if "layer" in item:
                            try:
                                layer = DataProductLayer(item["layer"])
                            except ValueError:
                                pass

                        # Parse status
                        status = DataProductStatus.ACTIVE  # Default
                        if "status" in item:
                            try:
                                status = DataProductStatus(item["status"])
                            except ValueError:
                                pass

                        # Create metadata
                        product = DataProductMetadata(
                            id=item["id"],
                            name=item["name"],
                            description=item.get("description", ""),
                            domain=item.get("domain", "unknown"),
                            owner=(
                                item.get("owner", {}).get("name", "unknown")
                                if isinstance(item.get("owner"), dict)
                                else item.get("owner", "unknown")
                            ),
                            layer=layer,
                            status=status,
                            tags=item.get("tags", []),
                            quality_score=item.get("quality_score", 0.0),
                            created_at=item.get("created_at"),
                            updated_at=item.get("updated_at"),
                            data_location=item.get("data_location", ""),
                            schema_definition=item.get("schema", {}),
                            sla=item.get("sla", {}),
                            documentation_url=item.get("documentation_url"),
                            metadata={
                                "source": "command_center",
                                "cc_url": f"{self.base_url}/{item['id']}",
                                "version": item.get("version", "unknown"),
                                **item.get("metadata", {}),
                            },
                        )

                        products.append(product)

                    except Exception as e:
                        self.logger.warning(f"Failed to parse product {item.get('id')}: {e}")
                        continue

                return products

        except Exception as e:
            self.logger.error(f"Command Center search failed: {e}")
            return []

    async def _get_catalog_stats_impl(self) -> Dict[str, Any]:
        """Get Command Center catalog statistics"""
        try:

            stats_url = f"{self.base_url}/stats"

            async with self.session.get(stats_url) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    return {"total_products": 0, "total_domains": 0, "avg_quality_score": 0.0}

        except Exception as e:
            self.logger.warning(f"Failed to get Command Center stats: {e}")
            return {"total_products": 0, "total_domains": 0, "avg_quality_score": 0.0}
