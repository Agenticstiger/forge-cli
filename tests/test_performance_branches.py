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

"""Branch-coverage tests for fluid_build/cli/performance.py"""

import time
from unittest.mock import MagicMock, patch

import pytest

# ---- PerformanceMetrics ----


class TestPerformanceMetrics:
    def test_defaults(self):
        from fluid_build.cli.performance import PerformanceMetrics

        m = PerformanceMetrics()
        assert m.startup_time is None
        assert m.cache_hits == 0
        assert m.file_reads == 0

    def test_to_dict_defaults(self):
        from fluid_build.cli.performance import PerformanceMetrics

        d = PerformanceMetrics().to_dict()
        assert d["startup_time_ms"] == 0.0
        assert d["cache_hit_ratio"] == 0
        assert d["peak_memory_mb"] is None
        assert d["memory_growth_mb"] is None

    def test_to_dict_with_values(self):
        from fluid_build.cli.performance import PerformanceMetrics

        m = PerformanceMetrics(
            startup_time=0.5,
            command_time=1.0,
            import_time=0.2,
            cache_hits=3,
            cache_misses=7,
            peak_memory_mb=200.0,
            initial_memory_mb=100.0,
            file_reads=5,
            file_writes=2,
            subprocess_calls=1,
        )
        d = m.to_dict()
        assert d["startup_time_ms"] == 500.0
        assert d["command_time_ms"] == 1000.0
        assert d["import_time_ms"] == 200.0
        assert d["cache_hit_ratio"] == pytest.approx(0.3)
        assert d["memory_growth_mb"] == 100.0
        assert d["file_operations"] == 7
        assert d["subprocess_calls"] == 1

    def test_to_dict_no_memory_growth_partial(self):
        from fluid_build.cli.performance import PerformanceMetrics

        m = PerformanceMetrics(peak_memory_mb=100.0)
        d = m.to_dict()
        assert d["memory_growth_mb"] is None


# ---- PerformanceCache ----


class TestPerformanceCache:
    def test_get_miss(self):
        from fluid_build.cli.performance import PerformanceCache

        c = PerformanceCache(max_size=10, ttl_seconds=60)
        assert c.get("f", (), {}) is None

    def test_set_and_get(self):
        from fluid_build.cli.performance import PerformanceCache

        c = PerformanceCache(max_size=10, ttl_seconds=60)
        c.set("f", (1,), {}, 42)
        assert c.get("f", (1,), {}) == 42

    def test_get_expired(self):
        from fluid_build.cli.performance import PerformanceCache

        c = PerformanceCache(max_size=10, ttl_seconds=0)
        c.set("f", (), {}, "val")
        time.sleep(0.01)
        assert c.get("f", (), {}) is None

    def test_evict_old_entries_expired(self):
        from fluid_build.cli.performance import PerformanceCache

        c = PerformanceCache(max_size=100, ttl_seconds=0)
        c.set("f", (), {}, "val")
        time.sleep(0.01)
        c._evict_old_entries()
        assert len(c._cache) == 0

    def test_evict_lru(self):
        from fluid_build.cli.performance import PerformanceCache

        c = PerformanceCache(max_size=2, ttl_seconds=3600)
        c.set("a", (), {}, 1)
        c.set("b", (), {}, 2)
        c.set("c", (), {}, 3)
        # Should have evicted oldest to stay within max_size
        assert len(c._cache) <= 2

    def test_clear(self):
        from fluid_build.cli.performance import PerformanceCache

        c = PerformanceCache()
        c.set("f", (), {}, 1)
        c.clear()
        assert len(c._cache) == 0
        assert len(c._access_times) == 0

    def test_get_stats(self):
        from fluid_build.cli.performance import PerformanceCache

        c = PerformanceCache(max_size=50, ttl_seconds=120)
        c.set("f", (), {}, 1)
        c.get("f", (), {})
        c.get("g", (), {})
        stats = c.get_stats()
        assert stats["size"] == 1
        assert stats["max_size"] == 50
        assert stats["ttl_seconds"] == 120
        assert stats["hits"] == 1
        assert stats["misses"] == 1

    def test_generate_key_deterministic(self):
        from fluid_build.cli.performance import PerformanceCache

        c = PerformanceCache()
        k1 = c._generate_key("f", (1, 2), {"a": "b"})
        k2 = c._generate_key("f", (1, 2), {"a": "b"})
        assert k1 == k2
        assert isinstance(k1, str)
        assert len(k1) == 16

    def test_generate_key_different_inputs(self):
        from fluid_build.cli.performance import PerformanceCache

        c = PerformanceCache()
        k1 = c._generate_key("f", (1,), {})
        k2 = c._generate_key("g", (1,), {})
        assert k1 != k2

    def test_is_expired_false(self):
        from fluid_build.cli.performance import PerformanceCache

        c = PerformanceCache(ttl_seconds=3600)
        assert not c._is_expired(time.time())

    def test_is_expired_true(self):
        from fluid_build.cli.performance import PerformanceCache

        c = PerformanceCache(ttl_seconds=0)
        assert c._is_expired(time.time() - 1)

    def test_cache_hit_updates_access_time(self):
        from fluid_build.cli.performance import PerformanceCache

        c = PerformanceCache()
        c.set("f", (), {}, "v")
        first_access = c._access_times[list(c._access_times.keys())[0]]
        time.sleep(0.01)
        c.get("f", (), {})
        second_access = c._access_times[list(c._access_times.keys())[0]]
        assert second_access >= first_access


# ---- @cached decorator ----


class TestCachedDecorator:
    def test_cached_returns_cached_value(self):
        from fluid_build.cli.performance import cached

        call_count = 0

        @cached(ttl_seconds=60)
        def expensive(x):
            nonlocal call_count
            call_count += 1
            return x * 2

        assert expensive(5) == 10
        assert expensive(5) == 10
        assert call_count == 1

    def test_cached_different_args(self):
        from fluid_build.cli.performance import cached

        call_count = 0

        @cached()
        def fn(x):
            nonlocal call_count
            call_count += 1
            return x

        fn(1)
        fn(2)
        assert call_count == 2

    def test_cached_cache_clear(self):
        from fluid_build.cli.performance import cached

        @cached()
        def fn(x):
            return x

        fn(1)
        fn.cache_clear()
        stats = fn.cache_stats()
        assert stats["size"] == 0

    def test_cached_cache_stats(self):
        from fluid_build.cli.performance import cached

        @cached()
        def fn(x):
            return x

        fn(1)
        fn(1)
        stats = fn.cache_stats()
        assert stats["hits"] >= 1


# ---- LazyImporter ----


class TestLazyImporter:
    def test_import_module(self):
        from fluid_build.cli.performance import LazyImporter

        li = LazyImporter()
        result = li.lazy_import("json")
        import json

        assert result is json

    def test_import_attribute(self):
        from fluid_build.cli.performance import LazyImporter

        li = LazyImporter()
        result = li.lazy_import("os.path", "join")
        from os.path import join

        assert result is join

    def test_import_cached(self):
        from fluid_build.cli.performance import LazyImporter

        li = LazyImporter()
        r1 = li.lazy_import("json")
        r2 = li.lazy_import("json")
        assert r1 is r2

    def test_import_failure_returns_callable(self):
        from fluid_build.cli.performance import LazyImporter

        li = LazyImporter()
        result = li.lazy_import("nonexistent_module_xyz_12345")
        with pytest.raises(ImportError):
            result()

    def test_get_import_stats(self):
        from fluid_build.cli.performance import LazyImporter

        li = LazyImporter()
        li.lazy_import("json")
        stats = li.get_import_stats()
        assert "json" in stats
        assert stats["json"] >= 0

    def test_global_lazy_import(self):
        from fluid_build.cli.performance import lazy_import

        result = lazy_import("sys")
        import sys

        assert result is sys


# ---- MemoryMonitor ----


class TestMemoryMonitor:
    def test_init_defaults(self):
        from fluid_build.cli.performance import MemoryMonitor

        m = MemoryMonitor()
        assert m.psutil is None
        assert m._initial_memory is None

    def test_start_monitoring_no_psutil(self):
        from fluid_build.cli.performance import MemoryMonitor

        m = MemoryMonitor()
        with patch.dict("sys.modules", {"psutil": None}):
            m.start_monitoring()
        # Should not fail even without psutil

    def test_get_stats_no_monitoring(self):
        from fluid_build.cli.performance import MemoryMonitor

        m = MemoryMonitor()
        stats = m.get_stats()
        assert stats["initial_mb"] is None
        assert stats["peak_mb"] is None
        assert stats["current_mb"] is None
        assert stats["growth_mb"] is None

    def test_update_peak_no_process(self):
        from fluid_build.cli.performance import MemoryMonitor

        m = MemoryMonitor()
        m.update_peak()  # Should not raise

    def test_get_memory_mb_no_process(self):
        from fluid_build.cli.performance import MemoryMonitor

        m = MemoryMonitor()
        assert m._get_memory_mb() is None

    def test_start_monitoring_with_mock_psutil(self):
        from fluid_build.cli.performance import MemoryMonitor

        m = MemoryMonitor()
        mock_psutil = MagicMock()
        mock_process = MagicMock()
        mock_process.memory_info.return_value = MagicMock(rss=100 * 1024 * 1024)
        mock_psutil.Process.return_value = mock_process
        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            m.start_monitoring()
        assert m._initial_memory == pytest.approx(100.0)

    def test_get_memory_mb_exception(self):
        from fluid_build.cli.performance import MemoryMonitor

        m = MemoryMonitor()
        m._process = MagicMock()
        m._process.memory_info.side_effect = RuntimeError("fail")
        assert m._get_memory_mb() is None


# ---- CommandProfiler ----


class TestCommandProfiler:
    def test_disabled_profiler_yields(self):
        from fluid_build.cli.performance import CommandProfiler

        cp = CommandProfiler(enabled=False)
        with cp.profile_command("test"):
            pass
        assert cp.get_profile("test") is None

    def test_enabled_profiler(self):
        from fluid_build.cli.performance import CommandProfiler

        cp = CommandProfiler(enabled=True)
        with cp.profile_command("mycommand"):
            time.sleep(0.01)
        prof = cp.get_profile("mycommand")
        assert prof is not None
        assert prof["execution_time"] >= 0
        assert "memory_stats" in prof

    def test_get_all_profiles(self):
        from fluid_build.cli.performance import CommandProfiler

        cp = CommandProfiler(enabled=True)
        with cp.profile_command("a"):
            pass
        with cp.profile_command("b"):
            pass
        assert len(cp.get_all_profiles()) == 2

    def test_clear_profiles(self):
        from fluid_build.cli.performance import CommandProfiler

        cp = CommandProfiler(enabled=True)
        with cp.profile_command("a"):
            pass
        cp.clear_profiles()
        assert cp.get_all_profiles() == {}

    def test_end_profiling_when_disabled(self):
        from fluid_build.cli.performance import CommandProfiler

        cp = CommandProfiler(enabled=False)
        cp._end_profiling()  # Should not raise


# ---- StartupOptimizer ----


class TestStartupOptimizer:
    def test_optimize_startup(self):
        from fluid_build.cli.performance import StartupOptimizer

        so = StartupOptimizer()
        before = time.time()
        so.optimize_startup()
        assert so.startup_time >= before

    def test_get_startup_stats(self):
        from fluid_build.cli.performance import StartupOptimizer

        so = StartupOptimizer()
        so.optimize_startup()
        stats = so.get_startup_stats()
        assert "startup_time" in stats
        assert "cache_enabled" in stats
        assert "import_cache_stats" in stats


# ---- Global functions ----


class TestGlobalFunctions:
    def test_optimize_startup_global(self):
        from fluid_build.cli.performance import optimize_startup

        optimize_startup()  # Should not raise

    def test_get_performance_stats(self):
        from fluid_build.cli.performance import get_performance_stats

        stats = get_performance_stats()
        assert "startup_stats" in stats
        assert "cache_stats" in stats
        assert "import_stats" in stats
        assert "profiling_enabled" in stats

    def test_clear_all_caches(self):
        from fluid_build.cli.performance import _global_cache, clear_all_caches

        _global_cache.set("f", (), {}, 1)
        clear_all_caches()
        assert _global_cache.get_stats()["size"] == 0

    def test_profile_command_context_manager(self):
        from fluid_build.cli.performance import profile_command

        with profile_command("test_cmd"):
            pass  # Should not raise


# ---- OperationalMonitoring ----


class TestOperationalMonitoring:
    def test_add_health_check(self):
        from fluid_build.cli.performance import OperationalMonitoring

        om = OperationalMonitoring()
        om.add_health_check(lambda: True)
        assert len(om._health_checks) == 1

    def test_run_health_checks_all_pass(self):
        from fluid_build.cli.performance import OperationalMonitoring

        om = OperationalMonitoring()
        om.add_health_check(lambda: True)
        result = om.run_health_checks()
        assert result["overall_healthy"] is True
        assert result["checks"]["<lambda>"]["status"] == "pass"

    def test_run_health_checks_one_fails(self):
        from fluid_build.cli.performance import OperationalMonitoring

        om = OperationalMonitoring()

        def ok():
            return True

        def bad():
            return False

        om.add_health_check(ok)
        om.add_health_check(bad)
        result = om.run_health_checks()
        assert result["overall_healthy"] is False
        assert result["checks"]["ok"]["healthy"] is True
        assert result["checks"]["bad"]["healthy"] is False

    def test_run_health_checks_exception(self):
        from fluid_build.cli.performance import OperationalMonitoring

        om = OperationalMonitoring()

        def boom():
            raise RuntimeError("kaboom")

        om.add_health_check(boom)
        result = om.run_health_checks()
        assert result["overall_healthy"] is False
        assert result["checks"]["boom"]["status"] == "error"
        assert "kaboom" in result["checks"]["boom"]["error"]

    def test_run_health_checks_empty(self):
        from fluid_build.cli.performance import OperationalMonitoring

        om = OperationalMonitoring()
        result = om.run_health_checks()
        assert result["overall_healthy"] is True
        assert result["checks"] == {}

    def test_check_disk_space_no_psutil(self):
        from fluid_build.cli.performance import OperationalMonitoring

        om = OperationalMonitoring()
        with patch.dict("sys.modules", {"psutil": None}):
            assert om.check_disk_space() is True

    def test_check_memory_usage_no_psutil(self):
        from fluid_build.cli.performance import OperationalMonitoring

        om = OperationalMonitoring()
        with patch.dict("sys.modules", {"psutil": None}):
            assert om.check_memory_usage() is True

    def test_check_disk_space_with_mock(self):
        from fluid_build.cli.performance import OperationalMonitoring

        om = OperationalMonitoring()
        mock_psutil = MagicMock()
        mock_psutil.disk_usage.return_value = MagicMock(free=5 * 1024**3)
        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            assert om.check_disk_space(min_free_gb=1.0) is True
            assert om.check_disk_space(min_free_gb=10.0) is False

    def test_check_memory_usage_with_mock(self):
        from fluid_build.cli.performance import OperationalMonitoring

        om = OperationalMonitoring()
        mock_psutil = MagicMock()
        mock_psutil.virtual_memory.return_value = MagicMock(percent=75.0)
        with patch.dict("sys.modules", {"psutil": mock_psutil}):
            assert om.check_memory_usage(max_usage_percent=90.0) is True
            assert om.check_memory_usage(max_usage_percent=50.0) is False


# ---- Constants ----


class TestConstants:
    def test_constants_exist(self):
        from fluid_build.cli.performance import (
            CACHE_TTL_SECONDS,
            COMMAND_PROFILING_ENABLED,
            MAX_CACHE_SIZE,
            STARTUP_CACHE_ENABLED,
        )

        assert CACHE_TTL_SECONDS == 3600
        assert MAX_CACHE_SIZE == 100
        assert STARTUP_CACHE_ENABLED is True
        assert COMMAND_PROFILING_ENABLED is False
