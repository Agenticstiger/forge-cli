# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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
            # Import here to avoid circular dependency. The client lives in
            # ``fluid_build.cli``, one package up: a relative ``._command_center``
            # would name ``market_catalogs._command_center``, which does not exist.
            from fluid_build.cli._command_center import get_command_center_client

            cc = get_command_center_client(logger=self.logger)

            if not cc.available or not cc.features.catalog:
                self.logger.warning("Command Center catalog not available")
                return False

            self.base_url = cc.get_catalog_url()
            self.cc_client = cc

            # Initialize the HTTP session. Every request on it follows a
            # redirect only within the Command Center's origin: the client's
            # probes passed the host gate for that origin, not for wherever
            # the Command Center redirects (the reporter's redirect rule).
            import httpx

            from fluid_build.observability.reporter import refuse_foreign_redirect
            from fluid_build.util.http_client import new_async_client

            # aiohttp's defaults, kept: 10 redirects, 300s total / 30s connect,
            # and no proxy-environment lookup.
            self.session = new_async_client(
                timeout=httpx.Timeout(300.0, connect=30.0),
                response_hooks=[refuse_foreign_redirect],
                max_redirects=10,
                trust_env=False,
                # aiohttp's default context: the system CA store, so a corporate
                # CA installed only there is still trusted.
                system_trust=True,
            )

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

            if filters.text_query:
                params["query"] = filters.text_query
            if filters.domain:
                params["domains"] = filters.domain
            if filters.owner:
                params["owners"] = filters.owner
            if filters.tags:
                params["tags"] = ",".join(filters.tags)
            if filters.layer:
                params["layers"] = filters.layer.value
            if filters.status:
                params["statuses"] = filters.status.value
            if filters.min_quality_score:
                params["min_quality"] = filters.min_quality_score
            if filters.limit:
                params["limit"] = filters.limit
            if filters.offset:
                params["offset"] = filters.offset

            # Make request
            from fluid_build.util.http_client import merge_query

            response = await self.session.get(merge_query(self.base_url, params))
            if response.status_code != 200:
                self.logger.warning(f"Command Center search failed: {response.status_code}")
                return []

            data = response.json()
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
                            self.logger.debug(
                                "Command Center product %r has an unknown layer %r; left unset",
                                item.get("id"),
                                item["layer"],
                            )

                    # Parse status
                    status = DataProductStatus.ACTIVE  # Default
                    if "status" in item:
                        try:
                            status = DataProductStatus(item["status"])
                        except ValueError:
                            self.logger.debug(
                                "Command Center product %r has an unknown status %r; kept %s",
                                item.get("id"),
                                item["status"],
                                status.value,
                            )

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

            response = await self.session.get(stats_url)
            if response.status_code == 200:
                return response.json()
            else:
                return {"total_products": 0, "total_domains": 0, "avg_quality_score": 0.0}

        except Exception as e:
            self.logger.warning(f"Failed to get Command Center stats: {e}")
            return {"total_products": 0, "total_domains": 0, "avg_quality_score": 0.0}
