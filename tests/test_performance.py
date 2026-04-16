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

"""Tests for fluid_build.forge.core.performance — PerformanceMonitor."""

import time

from fluid_build.forge.core.performance import PerformanceMonitor


class TestPerformanceMonitor:
    def _monitor(self):
        return PerformanceMonitor()

    def test_initial_state(self):
        pm = self._monitor()
        assert pm.get_metrics() == {}
        stats = pm.get_cache_stats()
        assert stats["cache_size"] == 0

    # -- _record_metric --

    def test_record_metric_creates_entry(self):
        pm = self._monitor()
        pm._record_metric("op1", 0.05, True, None)
        m = pm.get_metrics()
        assert "op1" in m
        assert m["op1"]["count"] == 1
        assert m["op1"]["success_count"] == 1

    def test_record_metric_tracks_errors(self):
        pm = self._monitor()
        pm._record_metric("op1", 0.01, False, "boom")
        m = pm.get_metrics()
        assert m["op1"]["error_count"] == 1
        assert m["op1"]["last_error"] == "boom"

    def test_record_metric_accumulates(self):
        pm = self._monitor()
        pm._record_metric("op1", 0.1, True, None)
        pm._record_metric("op1", 0.3, True, None)
        m = pm.get_metrics()
        assert m["op1"]["count"] == 2
        assert abs(m["op1"]["avg_time"] - 0.2) < 0.01
        assert m["op1"]["min_time"] == 0.1
        assert m["op1"]["max_time"] == 0.3

    # -- timed decorator --

    def test_timed_decorator_records(self):
        pm = self._monitor()

        @pm.timed("my_func")
        def my_func():
            return 42

        result = my_func()
        assert result == 42
        m = pm.get_metrics()
        assert "my_func" in m
        assert m["my_func"]["success_count"] == 1

    def test_timed_decorator_records_error(self):
        pm = self._monitor()

        @pm.timed("failing")
        def failing():
            raise ValueError("oops")

        try:
            failing()
        except ValueError:
            pass
        m = pm.get_metrics()
        assert m["failing"]["error_count"] == 1

    # -- cached decorator --

    def test_cached_decorator_caches(self):
        pm = self._monitor()
        call_count = 0

        @pm.cached()
        def expensive():
            nonlocal call_count
            call_count += 1
            return "result"

        assert expensive() == "result"
        assert expensive() == "result"
        assert call_count == 1  # Called only once

    def test_cached_with_explicit_key(self):
        pm = self._monitor()

        @pm.cached(cache_key="fixed")
        def func():
            return "value"

        func()
        stats = pm.get_cache_stats()
        assert stats["cache_size"] == 1

    def test_cached_ttl_expires(self):
        pm = self._monitor()
        call_count = 0

        @pm.cached(ttl=0.01)
        def func():
            nonlocal call_count
            call_count += 1
            return call_count

        assert func() == 1
        time.sleep(0.02)
        assert func() == 2  # Cache expired, re-executed

    # -- clear_cache --

    def test_clear_cache(self):
        pm = self._monitor()
        pm._cache["key"] = {"value": "x", "timestamp": time.time()}
        assert pm.get_cache_stats()["cache_size"] == 1
        pm.clear_cache()
        assert pm.get_cache_stats()["cache_size"] == 0

    # -- get_cache_stats --

    def test_cache_stats_memory(self):
        pm = self._monitor()
        pm._cache["k1"] = {"value": "x" * 1000, "timestamp": time.time()}
        stats = pm.get_cache_stats()
        assert stats["cache_size"] == 1
        assert stats["total_memory_mb"] > 0
