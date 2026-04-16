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
FLUID CLI Performance and Production Optimizations

Performance enhancements, caching, monitoring, and production-ready improvements
for the FLUID CLI system. Includes startup optimization, command caching,
and operational monitoring capabilities.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, TypeVar

T = TypeVar("T")

# Performance configuration
CACHE_TTL_SECONDS = 3600  # 1 hour default cache TTL
MAX_CACHE_SIZE = 100  # Maximum number of cached items
STARTUP_CACHE_ENABLED = True
COMMAND_PROFILING_ENABLED = False


@dataclass
class PerformanceMetrics:
    """Track performance metrics for CLI operations"""

    # Timing metrics
    startup_time: Optional[float] = None
    command_time: Optional[float] = None
    import_time: Optional[float] = None
    cache_hits: int = 0
    cache_misses: int = 0

    # Memory metrics
    peak_memory_mb: Optional[float] = None
    initial_memory_mb: Optional[float] = None

    # Operation counts
    file_reads: int = 0
    file_writes: int = 0
    subprocess_calls: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert metrics to dictionary for logging"""
        return {
            "startup_time_ms": round((self.startup_time or 0) * 1000, 2),
            "command_time_ms": round((self.command_time or 0) * 1000, 2),
            "import_time_ms": round((self.import_time or 0) * 1000, 2),
            "cache_hit_ratio": (
                self.cache_hits / max(1, self.cache_hits + self.cache_misses)
                if (self.cache_hits + self.cache_misses) > 0
                else 0
            ),
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "peak_memory_mb": self.peak_memory_mb,
            "memory_growth_mb": (
                (self.peak_memory_mb or 0) - (self.initial_memory_mb or 0)
                if self.peak_memory_mb and self.initial_memory_mb
                else None
            ),
            "file_operations": self.file_reads + self.file_writes,
            "subprocess_calls": self.subprocess_calls,
        }


class PerformanceCache:
    """Thread-safe LRU cache with TTL for CLI operations"""

    def __init__(self, max_size: int = MAX_CACHE_SIZE, ttl_seconds: int = CACHE_TTL_SECONDS):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._access_times: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._metrics = PerformanceMetrics()

    def _generate_key(self, func_name: str, args: tuple, kwargs: dict) -> str:
        """Generate a cache key from function name and arguments"""
        # Create a hash of the arguments for the cache key
        key_data = {"func": func_name, "args": args, "kwargs": kwargs}
        key_str = json.dumps(key_data, sort_keys=True, default=str)
        return hashlib.sha256(key_str.encode()).hexdigest()[:16]

    def _is_expired(self, timestamp: float) -> bool:
        """Check if a cache entry is expired"""
        return time.time() - timestamp > self.ttl_seconds

    def _evict_old_entries(self) -> None:
        """Remove expired and least recently used entries"""
        time.time()

        # Remove expired entries
        expired_keys = [
            key for key, entry in self._cache.items() if self._is_expired(entry["timestamp"])
        ]
        for key in expired_keys:
            del self._cache[key]
            del self._access_times[key]

        # Remove LRU entries if over max size
        while len(self._cache) > self.max_size:
            lru_key = min(self._access_times.keys(), key=lambda k: self._access_times[k])
            del self._cache[lru_key]
            del self._access_times[lru_key]

    def get(self, func_name: str, args: tuple, kwargs: dict) -> Optional[Any]:
        """Get a cached result if available and not expired"""
        with self._lock:
            key = self._generate_key(func_name, args, kwargs)

            if key in self._cache:
                entry = self._cache[key]
                if not self._is_expired(entry["timestamp"]):
                    self._access_times[key] = time.time()
                    self._metrics.cache_hits += 1
                    return entry["result"]
                else:
                    # Remove expired entry
                    del self._cache[key]
                    del self._access_times[key]

            self._metrics.cache_misses += 1
            return None

    def set(self, func_name: str, args: tuple, kwargs: dict, result: Any) -> None:
        """Cache a result"""
        with self._lock:
            key = self._generate_key(func_name, args, kwargs)
            current_time = time.time()

            self._cache[key] = {"result": result, "timestamp": current_time}
            self._access_times[key] = current_time

            # Clean up if needed
            self._evict_old_entries()

    def clear(self) -> None:
        """Clear all cached entries"""
        with self._lock:
            self._cache.clear()
            self._access_times.clear()

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        with self._lock:
            return {
                "size": len(self._cache),
                "max_size": self.max_size,
                "ttl_seconds": self.ttl_seconds,
                "hit_ratio": (
                    self._metrics.cache_hits
                    / max(1, self._metrics.cache_hits + self._metrics.cache_misses)
                    if (self._metrics.cache_hits + self._metrics.cache_misses) > 0
                    else 0
                ),
                "hits": self._metrics.cache_hits,
                "misses": self._metrics.cache_misses,
            }


# Global cache instance
_global_cache = PerformanceCache()


def cached(ttl_seconds: Optional[int] = None, max_size: Optional[int] = None):
    """Decorator for caching function results with TTL"""

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        cache = PerformanceCache(
            max_size=max_size or MAX_CACHE_SIZE, ttl_seconds=ttl_seconds or CACHE_TTL_SECONDS
        )

        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            # Try to get from cache
            cached_result = cache.get(func.__name__, args, kwargs)
            if cached_result is not None:
                return cached_result

            # Execute function and cache result
            result = func(*args, **kwargs)
            cache.set(func.__name__, args, kwargs, result)
            return result

        # Add cache management methods
        wrapper.cache_clear = cache.clear  # type: ignore
        wrapper.cache_stats = cache.get_stats  # type: ignore
        return wrapper

    return decorator


class LazyImporter:
    """Lazy import manager to speed up CLI startup"""

    def __init__(self):
        self._imports: Dict[str, Any] = {}
        self._import_times: Dict[str, float] = {}

    def lazy_import(self, module_name: str, attribute: str = None):
        """Lazy import a module or attribute"""
        cache_key = f"{module_name}.{attribute}" if attribute else module_name

        if cache_key not in self._imports:
            start_time = time.time()

            try:
                module = __import__(module_name, fromlist=[attribute] if attribute else [])
                if attribute:
                    self._imports[cache_key] = getattr(module, attribute)
                else:
                    self._imports[cache_key] = module

                self._import_times[cache_key] = time.time() - start_time

            except ImportError as e:
                # Return a placeholder that raises the error when used
                _err_msg = f"Failed to import {cache_key}: {e}"

                def _import_error(*args, _msg=_err_msg, **kwargs):
                    raise ImportError(_msg)

                self._imports[cache_key] = _import_error

        return self._imports[cache_key]

    def get_import_stats(self) -> Dict[str, float]:
        """Get import timing statistics"""
        return self._import_times.copy()


# Global lazy importer
_lazy_importer = LazyImporter()


def lazy_import(module_name: str, attribute: str = None):
    """Convenience function for lazy importing"""
    return _lazy_importer.lazy_import(module_name, attribute)


class MemoryMonitor:
    """Monitor memory usage during CLI operations"""

    def __init__(self):
        self.psutil = None
        self._process = None
        self._initial_memory = None
        self._peak_memory = None

    def start_monitoring(self) -> None:
        """Start memory monitoring"""
        try:
            import psutil

            self.psutil = psutil
            self._process = psutil.Process()
            self._initial_memory = self._get_memory_mb()
            self._peak_memory = self._initial_memory
        except (ImportError, Exception):
            # psutil not available or other error, monitoring disabled
            self.psutil = None
            self._process = None

    def update_peak(self) -> None:
        """Update peak memory usage"""
        if self._process:
            current_memory = self._get_memory_mb()
            if current_memory and (not self._peak_memory or current_memory > self._peak_memory):
                self._peak_memory = current_memory

    def _get_memory_mb(self) -> Optional[float]:
        """Get current memory usage in MB"""
        if self._process:
            try:
                memory_info = self._process.memory_info()
                return memory_info.rss / (1024 * 1024)  # Convert bytes to MB
            except Exception:
                pass
        return None

    def get_stats(self) -> Dict[str, Optional[float]]:
        """Get memory statistics"""
        self.update_peak()
        return {
            "initial_mb": self._initial_memory,
            "peak_mb": self._peak_memory,
            "current_mb": self._get_memory_mb(),
            "growth_mb": (
                (self._peak_memory or 0) - (self._initial_memory or 0)
                if self._peak_memory and self._initial_memory
                else None
            ),
        }


class CommandProfiler:
    """Profile CLI command execution"""

    def __init__(self, enabled: bool = COMMAND_PROFILING_ENABLED):
        self.enabled = enabled
        self._profiles: Dict[str, Dict[str, Any]] = {}
        self._current_command = None
        self._start_time = None
        self._memory_monitor = MemoryMonitor()

    @contextmanager
    def profile_command(self, command_name: str):
        """Context manager for profiling a command"""
        if not self.enabled:
            yield
            return

        self._current_command = command_name
        self._start_time = time.time()
        self._memory_monitor.start_monitoring()

        try:
            yield
        finally:
            self._end_profiling()

    def _end_profiling(self) -> None:
        """End profiling and record results"""
        if not self.enabled or not self._current_command:
            return

        end_time = time.time()
        execution_time = end_time - (self._start_time or end_time)
        memory_stats = self._memory_monitor.get_stats()

        self._profiles[self._current_command] = {
            "execution_time": execution_time,
            "start_time": self._start_time,
            "end_time": end_time,
            "memory_stats": memory_stats,
            "cache_stats": _global_cache.get_stats(),
            "import_stats": _lazy_importer.get_import_stats(),
        }

        self._current_command = None
        self._start_time = None

    def get_profile(self, command_name: str) -> Optional[Dict[str, Any]]:
        """Get profile data for a command"""
        return self._profiles.get(command_name)

    def get_all_profiles(self) -> Dict[str, Dict[str, Any]]:
        """Get all profile data"""
        return self._profiles.copy()

    def clear_profiles(self) -> None:
        """Clear all profile data"""
        self._profiles.clear()


# Global profiler instance
_global_profiler = CommandProfiler()


@contextmanager
def profile_command(command_name: str):
    """Context manager for profiling CLI commands"""
    with _global_profiler.profile_command(command_name):
        yield


class StartupOptimizer:
    """Track CLI startup performance"""

    def __init__(self):
        self.startup_time = None
        self._import_cache_file = None

    def optimize_startup(self) -> None:
        """Record startup timing baseline"""
        self.startup_time = time.time()

    def get_startup_stats(self) -> Dict[str, Any]:
        """Get startup optimization statistics"""
        return {
            "startup_time": self.startup_time,
            "cache_enabled": STARTUP_CACHE_ENABLED,
            "import_cache_stats": _lazy_importer.get_import_stats(),
        }


# Global startup optimizer
_startup_optimizer = StartupOptimizer()


def optimize_startup() -> None:
    """Apply CLI startup optimizations"""
    _startup_optimizer.optimize_startup()


def get_performance_stats() -> Dict[str, Any]:
    """Get comprehensive performance statistics"""
    return {
        "startup_stats": _startup_optimizer.get_startup_stats(),
        "cache_stats": _global_cache.get_stats(),
        "import_stats": _lazy_importer.get_import_stats(),
        "memory_stats": MemoryMonitor().get_stats() if MemoryMonitor().psutil else None,
        "profiling_enabled": COMMAND_PROFILING_ENABLED,
        "profiles": _global_profiler.get_all_profiles(),
    }


def clear_all_caches() -> None:
    """Clear all performance caches"""
    _global_cache.clear()
    _global_profiler.clear_profiles()


class OperationalMonitoring:
    """Operational monitoring and health checks for production"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self._health_checks: List[Callable[[], bool]] = []
        self._metrics = PerformanceMetrics()

    def add_health_check(self, check_func: Callable[[], bool]) -> None:
        """Add a health check function"""
        self._health_checks.append(check_func)

    def run_health_checks(self) -> Dict[str, Any]:
        """Run all health checks and return results"""
        results = {"timestamp": time.time(), "checks": {}, "overall_healthy": True}

        for i, check in enumerate(self._health_checks):
            check_name = getattr(check, "__name__", f"check_{i}")
            try:
                result = check()
                results["checks"][check_name] = {
                    "status": "pass" if result else "fail",
                    "healthy": result,
                }
                if not result:
                    results["overall_healthy"] = False
            except Exception as e:
                results["checks"][check_name] = {
                    "status": "error",
                    "healthy": False,
                    "error": str(e),
                }
                results["overall_healthy"] = False

        return results

    def check_disk_space(self, min_free_gb: float = 1.0) -> bool:
        """Check if sufficient disk space is available"""
        try:
            import psutil

            disk_usage = psutil.disk_usage("/")
            free_gb = disk_usage.free / (1024**3)
            return free_gb >= min_free_gb
        except Exception:
            return True  # Assume OK if can't check

    def check_memory_usage(self, max_usage_percent: float = 90.0) -> bool:
        """Check if memory usage is within limits"""
        try:
            import psutil

            memory = psutil.virtual_memory()
            return memory.percent <= max_usage_percent
        except Exception:
            return True  # Assume OK if can't check

    def check_file_descriptors(self, max_usage_percent: float = 80.0) -> bool:
        """Check file descriptor usage"""
        try:
            import psutil

            process = psutil.Process()
            open_files = len(process.open_files())
            # Rough estimation of limit (varies by system)
            estimated_limit = 1024  # Conservative estimate
            usage_percent = (open_files / estimated_limit) * 100
            return usage_percent <= max_usage_percent
        except Exception:
            return True  # Assume OK if can't check


# Global monitoring instance
_operational_monitoring = OperationalMonitoring()

# Register default health checks
_operational_monitoring.add_health_check(_operational_monitoring.check_disk_space)
_operational_monitoring.add_health_check(_operational_monitoring.check_memory_usage)
_operational_monitoring.add_health_check(_operational_monitoring.check_file_descriptors)


def run_health_checks() -> Dict[str, Any]:
    """Run operational health checks"""
    return _operational_monitoring.run_health_checks()


def add_health_check(check_func: Callable[[], bool]) -> None:
    """Add a custom health check"""
    _operational_monitoring.add_health_check(check_func)


# Export public interface
__all__ = [
    "PerformanceMetrics",
    "PerformanceCache",
    "cached",
    "lazy_import",
    "MemoryMonitor",
    "CommandProfiler",
    "profile_command",
    "StartupOptimizer",
    "optimize_startup",
    "get_performance_stats",
    "clear_all_caches",
    "OperationalMonitoring",
    "run_health_checks",
    "add_health_check",
]
