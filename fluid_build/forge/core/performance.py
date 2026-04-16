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
Performance optimization for FLUID Forge

This module provides caching and performance monitoring to improve
startup time and scaling characteristics.
"""

import functools
import logging
import time
from typing import Any, Callable, Dict, Optional, TypeVar

logger = logging.getLogger(__name__)

# Type variable for cached functions
F = TypeVar("F", bound=Callable[..., Any])


class PerformanceMonitor:
    """Monitor and cache performance metrics"""

    def __init__(self):
        self._metrics: Dict[str, Dict[str, Any]] = {}
        self._cache: Dict[str, Any] = {}

    def timed(self, name: str):
        """Decorator to time function execution"""

        def decorator(func: F) -> F:
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                start_time = time.time()
                try:
                    result = func(*args, **kwargs)
                    success = True
                    error = None
                except Exception as e:
                    result = None
                    success = False
                    error = str(e)
                    raise
                finally:
                    duration = time.time() - start_time
                    self._record_metric(name, duration, success, error)
                return result

            return wrapper

        return decorator

    def cached(self, cache_key: Optional[str] = None, ttl: Optional[float] = None):
        """Decorator to cache function results"""

        def decorator(func: F) -> F:
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                # Generate cache key
                if cache_key:
                    key = cache_key
                else:
                    key = f"{func.__name__}:{hash(str(args) + str(sorted(kwargs.items())))}"

                # Check cache
                if key in self._cache:
                    cache_entry = self._cache[key]
                    if ttl is None or (time.time() - cache_entry["timestamp"]) < ttl:
                        logger.debug(f"Cache hit for {key}")
                        return cache_entry["value"]

                # Execute and cache
                result = func(*args, **kwargs)
                self._cache[key] = {"value": result, "timestamp": time.time()}
                logger.debug(f"Cached result for {key}")
                return result

            return wrapper

        return decorator

    def _record_metric(self, name: str, duration: float, success: bool, error: Optional[str]):
        """Record performance metric"""
        if name not in self._metrics:
            self._metrics[name] = {
                "count": 0,
                "total_time": 0.0,
                "avg_time": 0.0,
                "min_time": float("inf"),
                "max_time": 0.0,
                "success_count": 0,
                "error_count": 0,
                "last_error": None,
            }

        metric = self._metrics[name]
        metric["count"] += 1
        metric["total_time"] += duration
        metric["avg_time"] = metric["total_time"] / metric["count"]
        metric["min_time"] = min(metric["min_time"], duration)
        metric["max_time"] = max(metric["max_time"], duration)

        if success:
            metric["success_count"] += 1
        else:
            metric["error_count"] += 1
            metric["last_error"] = error

        # Log slow operations
        if duration > 1.0:  # More than 1 second
            logger.warning(f"Slow operation {name}: {duration:.2f}s")
        elif duration > 0.1:  # More than 100ms
            logger.info(f"Operation {name}: {duration:.2f}s")
        else:
            logger.debug(f"Operation {name}: {duration:.3f}s")

    def get_metrics(self) -> Dict[str, Dict[str, Any]]:
        """Get all performance metrics"""
        return self._metrics.copy()

    def clear_cache(self):
        """Clear all cached data"""
        self._cache.clear()
        logger.info("Performance cache cleared")

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        return {
            "cache_size": len(self._cache),
            "total_memory_mb": sum(len(str(entry["value"])) for entry in self._cache.values())
            / (1024 * 1024),
        }


# Global performance monitor instance
performance_monitor = PerformanceMonitor()

# Convenience decorators
timed = performance_monitor.timed
cached = performance_monitor.cached


def log_performance_summary():
    """Log performance summary"""
    metrics = performance_monitor.get_metrics()
    cache_stats = performance_monitor.get_cache_stats()

    logger.info("=== Performance Summary ===")
    for name, metric in metrics.items():
        success_rate = metric["success_count"] / metric["count"] * 100 if metric["count"] > 0 else 0
        logger.info(
            f"{name}: {metric['count']} calls, "
            f"avg: {metric['avg_time']:.3f}s, "
            f"success: {success_rate:.1f}%"
        )

    logger.info(
        f"Cache: {cache_stats['cache_size']} entries, {cache_stats['total_memory_mb']:.1f}MB"
    )


def optimize_registry_performance():
    """Apply performance optimizations to registry system"""

    # Patch registry methods with performance monitoring
    from .registry import ComponentRegistry

    # Cache component discovery
    original_discover = ComponentRegistry.discover_builtin_components

    @cached(cache_key="builtin_components", ttl=300)  # Cache for 5 minutes
    @timed("discover_builtin_components")
    def optimized_discover(self):
        return original_discover(self)

    ComponentRegistry.discover_builtin_components = optimized_discover

    logger.info("Applied performance optimizations to registry system")


# Apply optimizations on import
optimize_registry_performance()
