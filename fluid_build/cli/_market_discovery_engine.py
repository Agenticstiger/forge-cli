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

# ruff: noqa: F821 — this helper resolves host-module symbols
# (catalog adapters, registry, etc.) at call-time via a _host()
# indirection accessor; ruff cannot statically see those bindings.
"""``fluid market`` discovery engine — physical extraction.

Lifted from ``cli/market.py`` (host file was 1971 LOC). ~329 LOC of
the :class:`MarketDiscoveryEngine` orchestration logic.
``cli/market.py`` re-imports the class so existing call sites keep
resolving.
"""

from __future__ import annotations

import asyncio
import logging
import time
import time as time_module
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Discovery catalogs whose connectors still ship illustrative/demo
# metadata rather than performing a real catalog query. Until a real
# discovery integration lands for each, the engine SKIPS them entirely
# instead of surfacing fabricated data products through ``fluid market``
# — serving demo rows as if they were live catalog results is exactly the
# dishonest UX this guard exists to prevent. An entry graduates out of
# this set the moment its connector performs a real lookup.
#
# Real discovery is delivered through the generic ``mcp`` connector
# (``market_catalogs/mcp_catalog.py``), which speaks Model Context Protocol
# to any catalog that exposes an MCP server (DataHub, OpenMetadata, Data
# Mesh Manager, and increasingly AWS/GCP) — so the demo-only per-catalog
# connectors below are skipped rather than reimplemented bespoke. Point the
# ``mcp`` connector at the relevant catalog's MCP server to discover them
# for real.
_ROADMAP_CATALOGS: frozenset[str] = frozenset(
    {
        # Proprietary demo connectors (no fabricated data — see above).
        "azure_purview",
        "apache_atlas",
        "confluent_schema_registry",
        "collibra",
        "alation",
        # Cloud / generic demo connectors — real discovery flows through the
        # ``mcp`` connector instead of a bespoke client for each.
        "aws_glue_data_catalog",
        "google_cloud_data_catalog",
        "custom_rest_api",
    }
)


def _market_module():
    from fluid_build.cli import market as _m

    return _m


# Module-level globals from the host module that ``MarketDiscoveryEngine``
# references by bare name. Bound at import time.
def _bind_from_host() -> None:
    host = _market_module()
    g = globals()
    for name in (
        "DataProductMetadata",
        "SearchFilters",
        "SearchResult",
        "CatalogType",
        "BaseCatalogConnector",
        "MarketCache",
        "ConnectionPool",
        "advanced_search_engine",
        "metrics_collector",
        "performance_monitor",
        "HealthChecker",
        # UI symbols (Rich + RICH_AVAILABLE) — referenced by bare name
        # inside ``MarketDiscoveryEngine``'s methods.
        "RICH_AVAILABLE",
        "Console",
        "Panel",
        "Table",
        "Tree",
        "Progress",
        "SpinnerColumn",
        "TextColumn",
        "Text",
    ):
        if hasattr(host, name):
            g[name] = getattr(host, name)


_bind_from_host()


class MarketDiscoveryEngine:
    """
    Unified data product discovery engine that searches across
    multiple data catalogs and marketplaces with caching, performance optimization,
    and comprehensive monitoring
    """

    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.connectors: Dict[str, BaseCatalogConnector] = {}
        self.console = Console() if RICH_AVAILABLE else None

        # Initialize caching and connection pooling
        cache_config = config.get("cache", {})
        self.cache_enabled = cache_config.get("enabled", True)
        if self.cache_enabled:
            self.cache = MarketCache(
                max_entries=cache_config.get("max_entries", 1000),
                default_ttl_minutes=cache_config.get("ttl_minutes", 15),
            )
        else:
            self.cache = None

        self.connection_pool = ConnectionPool(max_connections=10)
        self.start_time = time_module.time()
        self.request_count = 0
        self.error_count = 0

        # Initialize monitoring components
        self.health_checker = None  # Will be initialized after connectors are ready
        self.performance_monitor = performance_monitor
        self.error_count = 0

    async def initialize_connectors(self, catalog_types: List[str] = None):
        """Initialize connectors for specified catalog types.

        Per-catalog connector classes are lazy-imported here so the
        ``cli.market`` module can be imported without dragging in
        every catalog's runtime dependencies (yaml, datetime, etc.).
        Each catalog module also imports back from ``cli.market``
        (for ``BaseCatalogConnector`` + the dataclass primitives), so
        importing them at module-top-level would create a circular.
        """
        if catalog_types is None:
            catalog_types = self.config.get("catalogs", [])

        from fluid_build.cli.market_catalogs.alation import AlationConnector
        from fluid_build.cli.market_catalogs.apache_atlas import ApacheAtlasConnector
        from fluid_build.cli.market_catalogs.aws_glue import AWSGlueDataCatalogConnector
        from fluid_build.cli.market_catalogs.azure_purview import AzurePurviewConnector
        from fluid_build.cli.market_catalogs.collibra import CollibraConnector
        from fluid_build.cli.market_catalogs.command_center import CommandCenterConnector
        from fluid_build.cli.market_catalogs.confluent_schema_reg import (
            ConfluentSchemaRegistryConnector,
        )
        from fluid_build.cli.market_catalogs.datahub import DataHubConnector
        from fluid_build.cli.market_catalogs.google_data_catalog import (
            GoogleCloudDataCatalogConnector,
        )
        from fluid_build.cli.market_catalogs.rest_api import CustomRestApiConnector

        connector_classes = {
            "google_cloud_data_catalog": GoogleCloudDataCatalogConnector,
            "aws_glue_data_catalog": AWSGlueDataCatalogConnector,
            "azure_purview": AzurePurviewConnector,
            "datahub": DataHubConnector,
            "apache_atlas": ApacheAtlasConnector,
            "confluent_schema_registry": ConfluentSchemaRegistryConnector,
            "collibra": CollibraConnector,
            "alation": AlationConnector,
            "custom_rest_api": CustomRestApiConnector,
            "fluid_command_center": CommandCenterConnector,
        }

        for catalog_type in catalog_types:
            if catalog_type in _ROADMAP_CATALOGS:
                self.logger.info(
                    f"⏭️  '{catalog_type}' discovery is on the roadmap — not yet "
                    "implemented; skipping (no demo data served)."
                )
                continue
            if catalog_type in connector_classes:
                catalog_config = self.config.get(catalog_type, {})
                connector = connector_classes[catalog_type](catalog_config, self.logger)

                if await connector.connect():
                    self.connectors[catalog_type] = connector
                    self.logger.info(f"✅ Connected to {catalog_type}")
                else:
                    self.logger.warning(f"❌ Failed to connect to {catalog_type}")

        # Initialize health checker after connectors are ready
        if self.connectors:
            self.health_checker = HealthChecker(self.connectors)
            self.logger.info(f"🔍 Initialized health checker for {len(self.connectors)} connectors")

    async def advanced_search(self, filters: SearchFilters) -> SearchResult:
        """Enhanced search with advanced features, ranking, and faceting"""
        start_time = time_module.time()

        # Save search if requested
        if filters.save_search and filters.search_name:
            advanced_search_engine.save_search(filters)

        # Perform basic search across all catalogs
        catalog_results = await self.search_all_catalogs(filters)

        # Combine all results
        all_products = []
        for products in catalog_results.values():
            all_products.extend(products)

        # Apply advanced filters
        filtered_products = advanced_search_engine.apply_advanced_filters(all_products, filters)

        # Extract facets from all products (before ranking/sorting)
        facets = advanced_search_engine.extract_facets(filtered_products)

        # Rank and sort products
        ranked_products = advanced_search_engine.rank_and_sort_products(filtered_products, filters)

        # Apply pagination
        total_count = len(ranked_products)
        start_index = filters.offset
        end_index = start_index + filters.limit
        paginated_products = ranked_products[start_index:end_index]

        # Generate search suggestions
        suggestions = []
        if filters.text_query and len(filtered_products) < 5:  # Only suggest if few results
            suggestions = advanced_search_engine.generate_search_suggestions(
                all_products, filters.text_query
            )

        query_time = time_module.time() - start_time

        # Create search result
        result = SearchResult(
            products=paginated_products,
            total_count=total_count,
            facets=facets,
            query_time=query_time,
            suggestions=suggestions,
            ranking_info={
                "sort_by": filters.sort_by,
                "sort_order": filters.sort_order,
                "has_text_query": bool(filters.text_query),
                "relevance_scoring": filters.sort_by == "relevance" and bool(filters.text_query),
            },
        )

        # Record search in history
        advanced_search_engine.search_history.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "query": filters.text_query,
                "total_results": total_count,
                "query_time": query_time,
            }
        )

        # Keep only last 100 searches in history
        if len(advanced_search_engine.search_history) > 100:
            advanced_search_engine.search_history = advanced_search_engine.search_history[-100:]

        return result

    async def search_all_catalogs(
        self, filters: SearchFilters
    ) -> Dict[str, List[DataProductMetadata]]:
        """Search across all connected catalogs with caching support"""
        results = {}

        if self.console and RICH_AVAILABLE:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=self.console,
            ) as progress:
                for catalog_name, connector in self.connectors.items():
                    task = progress.add_task(f"Searching {catalog_name}...", total=1)
                    try:
                        self.request_count += 1

                        # Check cache first
                        cached_result = None
                        if self.cache_enabled and self.cache:
                            cached_result = self.cache.get(
                                "search_data_products", catalog_name, filters
                            )

                        if cached_result is not None:
                            results[catalog_name] = cached_result
                            self.logger.debug(f"Cache hit for {catalog_name}")
                            metrics_collector.record_cache_hit(catalog_name)
                        else:
                            # Fetch from catalog with monitoring
                            products = await self.performance_monitor.monitor_search(
                                catalog_name, self._search_with_timeout, connector, filters
                            )
                            results[catalog_name] = products
                            metrics_collector.record_cache_miss(catalog_name)

                            # Cache the result
                            if self.cache_enabled and self.cache:
                                self.cache.set(
                                    "search_data_products", catalog_name, filters, products
                                )
                                self.logger.debug(f"Cached results for {catalog_name}")

                        progress.update(task, completed=1)
                    except Exception as e:
                        self.error_count += 1
                        self.logger.error(f"Error searching {catalog_name}: {e}")
                        results[catalog_name] = []
                        progress.update(task, completed=1)
        else:
            for catalog_name, connector in self.connectors.items():
                try:
                    self.request_count += 1
                    self.logger.info(f"Searching {catalog_name}...")

                    # Check cache first
                    cached_result = None
                    if self.cache_enabled and self.cache:
                        cached_result = self.cache.get(
                            "search_data_products", catalog_name, filters
                        )

                    if cached_result is not None:
                        results[catalog_name] = cached_result
                        self.logger.debug(f"Cache hit for {catalog_name}")
                        metrics_collector.record_cache_hit(catalog_name)
                    else:
                        # Fetch from catalog with monitoring
                        products = await self.performance_monitor.monitor_search(
                            catalog_name, self._search_with_timeout, connector, filters
                        )
                        results[catalog_name] = products
                        metrics_collector.record_cache_miss(catalog_name)

                        # Cache the result
                        if self.cache_enabled and self.cache:
                            self.cache.set("search_data_products", catalog_name, filters, products)
                            self.logger.debug(f"Cached results for {catalog_name}")

                except Exception as e:
                    self.error_count += 1
                    self.logger.error(f"Error searching {catalog_name}: {e}")
                    results[catalog_name] = []

        return results

    async def _search_with_timeout(
        self, connector: BaseCatalogConnector, filters: SearchFilters
    ) -> List[DataProductMetadata]:
        """Search with timeout and error handling"""
        timeout_seconds = self.config.get("defaults", {}).get("timeout_seconds", 30)

        try:
            return await asyncio.wait_for(
                connector.search_data_products(filters), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            self.logger.warning(f"Search timeout for {connector.catalog_type}")
            return []
        except Exception as e:
            self.logger.error(f"Search error for {connector.catalog_type}: {e}")
            return []

    def get_performance_stats(self) -> Dict[str, Any]:
        """Get performance statistics"""
        runtime = time.time() - self.start_time
        success_rate = (
            (self.request_count - self.error_count) / self.request_count
            if self.request_count > 0
            else 0.0
        )

        stats = {
            "runtime_seconds": runtime,
            "total_requests": self.request_count,
            "error_count": self.error_count,
            "success_rate": success_rate,
            "requests_per_second": self.request_count / runtime if runtime > 0 else 0,
            "connected_catalogs": len(self.connectors),
        }

        if self.cache_enabled and self.cache:
            stats["cache"] = self.cache.get_stats()

        return stats

    def aggregate_results(
        self, catalog_results: Dict[str, List[DataProductMetadata]]
    ) -> List[DataProductMetadata]:
        """Aggregate and deduplicate results from multiple catalogs"""
        all_products = []
        seen_ids = set()

        for catalog_name, products in catalog_results.items():
            for product in products:
                # Simple deduplication by ID
                if product.id not in seen_ids:
                    all_products.append(product)
                    seen_ids.add(product.id)

        # Sort by quality score (descending) then by name
        all_products.sort(key=lambda p: (-p.quality_score if p.quality_score else 0, p.name))

        return all_products


# ==========================================
# Output Formatters
# ==========================================


# Render helpers — physically extracted to ``cli/_market_render.py``
# (see that module's docstring for the indirection rationale). Test
# patches on ``fluid_build.cli.market.RICH_AVAILABLE`` still flow
# through via the ``_rich_available`` shim there.
from fluid_build.cli._market_render import (  # noqa: E402
    format_detailed_output,
    format_json_output,
    format_table_output,
)

# ==========================================
# CLI Command Registration & Execution
# ==========================================
