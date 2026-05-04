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
"""``fluid market`` resilience layer — physical extraction.

Lifted from ``cli/market.py`` (host file was 1657 LOC). ~290 LOC of
:class:`CircuitBreaker`, :func:`retry_with_backoff`, and
:class:`BaseCatalogConnector`. Re-exported by ``cli/market.py``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional


def _market_module():
    from fluid_build.cli import market as _m

    return _m


def _bind_from_host() -> None:
    host = _market_module()
    g = globals()
    for name in (
        "DataProductMetadata",
        "SearchFilters",
        "SearchResult",
        "metrics_collector",
    ):
        if hasattr(host, name):
            g[name] = getattr(host, name)


_bind_from_host()


class CircuitBreaker:
    """Circuit breaker pattern for handling service failures"""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        expected_exception: type = Exception,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN

    async def call(self, func, *args, **kwargs):
        """Call function with circuit breaker protection"""
        if self.state == "OPEN":
            if self._should_attempt_reset():
                self.state = "HALF_OPEN"
            else:
                raise Exception(f"Circuit breaker is OPEN for {func.__name__}")

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except self.expected_exception as e:
            self._on_failure()
            raise e

    def _should_attempt_reset(self) -> bool:
        """Check if we should attempt to reset the circuit breaker"""
        return (
            self.last_failure_time and time.time() - self.last_failure_time >= self.recovery_timeout
        )

    def _on_success(self):
        """Handle successful call"""
        self.failure_count = 0
        self.state = "CLOSED"

    def _on_failure(self):
        """Handle failed call"""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"


async def retry_with_backoff(
    func,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
):
    """Retry function with exponential backoff"""
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            return await func()
        except Exception as e:
            last_exception = e

            if attempt == max_retries:
                break

            # Calculate delay with exponential backoff
            delay = min(base_delay * (backoff_factor**attempt), max_delay)
            await asyncio.sleep(delay)

    raise last_exception


# ==========================================
# Catalog Connectors
# ==========================================


class BaseCatalogConnector:
    """Base class for data catalog connectors with resilience features"""

    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.catalog_type = self.__class__.__name__

        # Resilience configuration
        self.max_retries = config.get("max_retries", 3)
        self.retry_delay = config.get("retry_delay", 1.0)
        self.timeout_seconds = config.get("timeout_seconds", 30)

        # Circuit breaker for connection failures
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=config.get("circuit_breaker_threshold", 5),
            recovery_timeout=config.get("circuit_breaker_timeout", 60),
        )

        # Connection state
        self.is_connected = False
        self.last_health_check = None
        self.health_check_interval = 300  # 5 minutes

    async def connect(self) -> bool:
        """Establish connection to the catalog with retry logic"""

        async def _connect_impl():
            return await self._connect_impl()

        try:
            result = await self.circuit_breaker.call(
                retry_with_backoff,
                _connect_impl,
                max_retries=self.max_retries,
                base_delay=self.retry_delay,
            )
            self.is_connected = result
            return result
        except Exception as e:
            self.logger.error(f"Failed to connect to {self.catalog_type} after retries: {e}")
            self.is_connected = False
            return False

    async def _connect_impl(self) -> bool:
        """Actual connection implementation - to be overridden by subclasses"""
        raise NotImplementedError

    async def search_data_products(self, filters: SearchFilters) -> List[DataProductMetadata]:
        """Search for data products with filters and resilience"""
        await self._ensure_healthy_connection()

        async def _search_impl():
            return await self._search_data_products_impl(filters)

        try:
            return await self.circuit_breaker.call(
                retry_with_backoff,
                _search_impl,
                max_retries=self.max_retries,
                base_delay=self.retry_delay,
            )
        except Exception as e:
            self.logger.error(f"Search failed for {self.catalog_type}: {e}")
            return []

    async def _search_data_products_impl(self, filters: SearchFilters) -> List[DataProductMetadata]:
        """Actual search implementation - to be overridden by subclasses"""
        raise NotImplementedError

    async def get_data_product(self, product_id: str) -> Optional[DataProductMetadata]:
        """Get detailed information about a specific data product with resilience"""
        await self._ensure_healthy_connection()

        async def _get_impl():
            return await self._get_data_product_impl(product_id)

        try:
            return await self.circuit_breaker.call(
                retry_with_backoff,
                _get_impl,
                max_retries=self.max_retries,
                base_delay=self.retry_delay,
            )
        except Exception as e:
            self.logger.error(f"Get product failed for {self.catalog_type}: {e}")
            return None

    async def _get_data_product_impl(self, product_id: str) -> Optional[DataProductMetadata]:
        """Actual get product implementation - to be overridden by subclasses"""
        # Default implementation - search and filter
        all_products = await self._search_data_products_impl(SearchFilters())
        for product in all_products:
            if product.id == product_id:
                return product
        return None

    async def get_catalog_stats(self) -> Dict[str, Any]:
        """Get catalog statistics with resilience"""
        await self._ensure_healthy_connection()

        async def _stats_impl():
            return await self._get_catalog_stats_impl()

        try:
            return await self.circuit_breaker.call(
                retry_with_backoff,
                _stats_impl,
                max_retries=self.max_retries,
                base_delay=self.retry_delay,
            )
        except Exception as e:
            self.logger.error(f"Get stats failed for {self.catalog_type}: {e}")
            return {"error": str(e), "available": False}

    async def _get_catalog_stats_impl(self) -> Dict[str, Any]:
        """Actual stats implementation - to be overridden by subclasses"""
        raise NotImplementedError

    async def _ensure_healthy_connection(self) -> None:
        """Ensure connection is healthy, reconnect if needed"""
        now = time.time()

        # Check if we need to perform a health check
        if (
            self.last_health_check is None
            or now - self.last_health_check > self.health_check_interval
        ):

            if not await self._health_check():
                self.logger.warning(
                    f"Health check failed for {self.catalog_type}, attempting reconnection"
                )
                await self.connect()

            self.last_health_check = now

    async def _health_check(self) -> bool:
        """Check if connection is healthy"""
        return self.is_connected

    def _apply_filters(
        self, products: List[DataProductMetadata], filters: SearchFilters
    ) -> List[DataProductMetadata]:
        """Apply search filters to product list"""
        filtered_products = []
        for product in products:
            if filters.domain and filters.domain.lower() not in product.domain.lower():
                continue
            if filters.owner and filters.owner.lower() not in product.owner.lower():
                continue
            if filters.layer and product.layer != filters.layer:
                continue
            if filters.product_type:
                # Match by stored product_type, or fall through to the
                # canonical layer mapping so legacy products without the
                # field still respond to Data Mesh filters. Routes through
                # the registry at fluid_build.forge.product_types.
                from fluid_build.forge.product_types import LAYER_TO_PRODUCT_TYPE

                pt = product.product_type or LAYER_TO_PRODUCT_TYPE.get(
                    product.layer.value.capitalize()
                )
                if pt != filters.product_type.upper():
                    continue
            if filters.status and product.status != filters.status:
                continue
            if filters.min_quality_score and (
                not product.quality_score or product.quality_score < filters.min_quality_score
            ):
                continue
            if filters.text_query:
                query_lower = filters.text_query.lower()
                if not any(
                    query_lower in text.lower()
                    for text in [
                        product.name,
                        product.description,
                        product.domain,
                        " ".join(product.tags),
                    ]
                ):
                    continue

            filtered_products.append(product)

        return filtered_products[: filters.limit]


# Imports the observability extraction left at this position; restored
# here because :class:`MarketDiscoveryEngine` (below) still uses them.
import time as time_module  # noqa: E402
from collections import Counter, defaultdict  # noqa: E402,F401

# Observability layer (MetricsCollector + HealthChecker +
# PerformanceMonitor + their singletons) physically extracted to
# ``cli/_market_observability.py`` (~285 LOC). Re-exported here so
# existing imports keep resolving.
from fluid_build.cli._market_observability import (  # noqa: E402,F401
    HealthChecker,
    MetricsCollector,
    PerformanceMonitor,
    metrics_collector,
    performance_monitor,
)
