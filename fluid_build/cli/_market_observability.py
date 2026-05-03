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

"""``fluid market`` observability layer — physical extraction.

Lifted from ``cli/market.py`` (host file was 2493 LOC). Three
classes plus their two module-level singletons:

* :class:`MetricsCollector` — search/cache/error counters, connector
  health, connection-pool stats, circuit-breaker stats.
* :class:`HealthChecker` — system health rollup against a connector
  registry.
* :class:`PerformanceMonitor` — slow-query log + perf-summary getter.

The ``BaseCatalogConnector`` reference inside :class:`HealthChecker`
is resolved lazily via the host module so we don't introduce a
circular import.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from fluid_build.cli.market import BaseCatalogConnector


# Per-catalog connector classes were physically extracted into the
# ``market_catalogs`` sibling package so each catalog's auth + search
# idiosyncrasies stay self-contained. They are imported lazily inside
# ``MarketDiscoveryEngine.initialize_connectors`` (the single dispatch
# site that needs them) so the import graph stays acyclic — connectors
# import :class:`BaseCatalogConnector` from here.


# ==========================================
# Caching and Performance Optimization
# ==========================================

import time as time_module
from collections import Counter, defaultdict
from collections import Counter as TypingCounter
from dataclasses import dataclass, field


@dataclass
class MetricsCollector:
    """Collect and manage metrics for monitoring"""

    search_requests: TypingCounter = field(default_factory=Counter)
    search_latency: dict = field(default_factory=lambda: defaultdict(list))
    connector_health: dict = field(default_factory=dict)
    cache_hits: TypingCounter = field(default_factory=Counter)
    cache_misses: TypingCounter = field(default_factory=Counter)
    error_counts: TypingCounter = field(default_factory=Counter)
    connection_pool_stats: dict = field(default_factory=dict)
    circuit_breaker_stats: dict = field(default_factory=dict)

    def record_search_request(self, catalog_type: str, latency: float):
        """Record a search request"""
        self.search_requests[catalog_type] += 1
        self.search_latency[catalog_type].append(latency)

    def record_cache_hit(self, catalog_type: str):
        """Record a cache hit"""
        self.cache_hits[catalog_type] += 1

    def record_cache_miss(self, catalog_type: str):
        """Record a cache miss"""
        self.cache_misses[catalog_type] += 1

    def record_error(self, catalog_type: str, error_type: str):
        """Record an error"""
        self.error_counts[f"{catalog_type}:{error_type}"] += 1

    def update_connector_health(
        self, catalog_type: str, is_healthy: bool, response_time: float = None
    ):
        """Update connector health status"""
        self.connector_health[catalog_type] = {
            "healthy": is_healthy,
            "last_check": time_module.time(),
            "response_time": response_time,
        }

    def update_connection_pool_stats(self, catalog_type: str, active: int, idle: int, total: int):
        """Update connection pool statistics"""
        self.connection_pool_stats[catalog_type] = {
            "active_connections": active,
            "idle_connections": idle,
            "total_connections": total,
            "timestamp": time_module.time(),
        }

    def update_circuit_breaker_stats(
        self, catalog_type: str, state: str, failure_count: int, success_count: int
    ):
        """Update circuit breaker statistics"""
        self.circuit_breaker_stats[catalog_type] = {
            "state": state,
            "failure_count": failure_count,
            "success_count": success_count,
            "timestamp": time_module.time(),
        }

    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of all metrics"""
        # Calculate average latencies
        avg_latencies = {}
        for catalog_type, latencies in self.search_latency.items():
            if latencies:
                avg_latencies[catalog_type] = sum(latencies) / len(latencies)

        # Calculate cache hit rates
        cache_hit_rates = {}
        for catalog_type in set(list(self.cache_hits.keys()) + list(self.cache_misses.keys())):
            hits = self.cache_hits[catalog_type]
            misses = self.cache_misses[catalog_type]
            total = hits + misses
            if total > 0:
                cache_hit_rates[catalog_type] = hits / total

        return {
            "search_requests": dict(self.search_requests),
            "average_latencies": avg_latencies,
            "cache_hit_rates": cache_hit_rates,
            "connector_health": self.connector_health,
            "connection_pool_stats": self.connection_pool_stats,
            "circuit_breaker_stats": self.circuit_breaker_stats,
            "error_counts": dict(self.error_counts),
            "timestamp": time_module.time(),
        }


# Global metrics collector instance
metrics_collector = MetricsCollector()


class HealthChecker:
    """System health checker with comprehensive monitoring"""

    def __init__(self, connectors: Dict[str, BaseCatalogConnector]):
        self.connectors = connectors
        self.logger = logging.getLogger(__name__)

    async def check_system_health(self) -> Dict[str, Any]:
        """Check overall system health"""
        health_report = {
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "connectors": {},
            "metrics": metrics_collector.get_summary(),
            "overall_health_score": 0.0,
        }

        healthy_connectors = 0
        total_connectors = len(self.connectors)

        # Check each connector
        for name, connector in self.connectors.items():
            try:
                start_time = time_module.time()
                is_healthy = await connector._health_check()
                response_time = time_module.time() - start_time

                health_report["connectors"][name] = {
                    "status": "healthy" if is_healthy else "unhealthy",
                    "response_time": response_time,
                    "circuit_breaker_state": (
                        connector.circuit_breaker.state
                        if hasattr(connector, "circuit_breaker")
                        else "unknown"
                    ),
                }

                if is_healthy:
                    healthy_connectors += 1

                metrics_collector.update_connector_health(name, is_healthy, response_time)

            except Exception as e:
                self.logger.error(f"Health check failed for {name}: {e}")
                health_report["connectors"][name] = {
                    "status": "error",
                    "error": str(e),
                    "response_time": None,
                }
                metrics_collector.record_error(name, "health_check_failed")

        # Calculate overall health score
        if total_connectors > 0:
            health_report["overall_health_score"] = healthy_connectors / total_connectors

            # Determine overall status
            if healthy_connectors == 0:
                health_report["status"] = "critical"
            elif healthy_connectors < total_connectors * 0.5:
                health_report["status"] = "degraded"
            elif healthy_connectors < total_connectors:
                health_report["status"] = "partial"
            else:
                health_report["status"] = "healthy"

        return health_report

    async def check_connector_health(self, connector_name: str) -> Dict[str, Any]:
        """Check health of a specific connector"""
        if connector_name not in self.connectors:
            return {"status": "not_found", "message": f"Connector '{connector_name}' not found"}

        connector = self.connectors[connector_name]
        try:
            start_time = time_module.time()
            is_healthy = await connector._health_check()
            response_time = time_module.time() - start_time

            return {
                "status": "healthy" if is_healthy else "unhealthy",
                "response_time": response_time,
                "circuit_breaker_state": (
                    connector.circuit_breaker.state
                    if hasattr(connector, "circuit_breaker")
                    else "unknown"
                ),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            self.logger.error(f"Health check failed for {connector_name}: {e}")
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }


@dataclass
class PerformanceMonitor:
    """Monitor and track performance metrics"""

    slow_query_threshold: float = 5.0  # seconds

    def __post_init__(self):
        self.logger = logging.getLogger(__name__)
        self.slow_queries: List[Dict[str, Any]] = []

    async def monitor_search(self, catalog_type: str, search_func, *args, **kwargs):
        """Monitor a search operation"""
        start_time = time_module.time()

        try:
            result = await search_func(*args, **kwargs)

            end_time = time_module.time()
            latency = end_time - start_time

            # Record metrics
            metrics_collector.record_search_request(catalog_type, latency)

            # Check for slow queries
            if latency > self.slow_query_threshold:
                slow_query = {
                    "catalog_type": catalog_type,
                    "latency": latency,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "args": str(args)[:200],  # Limit length
                    "kwargs": str({k: str(v)[:100] for k, v in kwargs.items()})[:200],
                }
                self.slow_queries.append(slow_query)
                self.logger.warning(f"Slow query detected: {catalog_type} took {latency:.2f}s")

                # Keep only recent slow queries (last 100)
                if len(self.slow_queries) > 100:
                    self.slow_queries = self.slow_queries[-100:]

            return result

        except Exception as e:
            end_time = time_module.time()
            latency = end_time - start_time

            metrics_collector.record_error(catalog_type, type(e).__name__)
            self.logger.error(f"Search failed for {catalog_type} after {latency:.2f}s: {e}")
            raise

    def get_slow_queries(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent slow queries"""
        return self.slow_queries[-limit:]

    def get_performance_summary(self) -> Dict[str, Any]:
        """Get performance summary"""
        summary = metrics_collector.get_summary()

        # Add slow query information
        summary["slow_queries"] = {
            "count": len(self.slow_queries),
            "threshold": self.slow_query_threshold,
            "recent": self.get_slow_queries(5),
        }

        return summary


# Global performance monitor instance
performance_monitor = PerformanceMonitor()

# ==========================================
# Original Caching Implementation
# ==========================================
