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
Extended unit tests for fluid_build.cli.market covering connector internals,
HealthChecker, PerformanceMonitor, ConnectionPool, handle_* functions, and
advanced MarketDiscoveryEngine behaviour not exercised in test_market.py.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fluid_build.cli.market import (
    ConnectionPool,
    DataProductLayer,
    DataProductMetadata,
    DataProductStatus,
    GoogleCloudDataCatalogConnector,
    HealthChecker,
    MarketDiscoveryEngine,
    PerformanceMonitor,
    SearchFilters,
    build_search_filters,
    format_detailed_output,
    format_table_output,
    generate_output,
    handle_health_check,
    handle_metrics,
    handle_search_suggestions,
    run,
    run_market_discovery,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run a coroutine synchronously via a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_product(**overrides):
    defaults = dict(
        id="test-prod-1",
        name="Test Product",
        description="A test product for unit testing",
        domain="finance",
        owner="test-team",
        layer=DataProductLayer.GOLD,
        status=DataProductStatus.ACTIVE,
        version="1.0.0",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
        tags=["test", "finance"],
        quality_score=0.9,
        catalog_source="Test Catalog",
        catalog_type="test_catalog",
    )
    defaults.update(overrides)
    return DataProductMetadata(**defaults)


def _make_test_args(**kwargs):
    defaults = dict(
        search=None,
        domain=None,
        owner=None,
        layer=None,
        status=None,
        tags=None,
        min_quality=None,
        created_after=None,
        created_before=None,
        limit=20,
        offset=0,
        format="table",
        output=None,
        product_id=None,
        detailed=False,
        marketplace_stats=False,
        list_catalogs=False,
        config_template=False,
        catalogs=None,
        debug=False,
    )
    defaults.update(kwargs)
    args = MagicMock()
    for k, v in defaults.items():
        setattr(args, k, v)
    return args


def _base_config():
    return {
        "catalogs": ["google_cloud_data_catalog"],
        "google_cloud_data_catalog": {"project_id": "test-project"},
        "defaults": {"timeout_seconds": 30},
        "cache": {"enabled": False},
    }


# ---------------------------------------------------------------------------
# BaseCatalogConnector — connect / search_data_products / get_data_product
# ---------------------------------------------------------------------------


class TestBaseCatalogConnectorConnect:
    """Test BaseCatalogConnector.connect() which wraps circuit-breaker retry logic."""

    def _connector(self):
        logger = logging.getLogger("test")
        return GoogleCloudDataCatalogConnector({"project_id": "p"}, logger)

    def test_connect_sets_is_connected_true(self):
        conn = self._connector()
        result = _run(conn.connect())
        assert result is True
        assert conn.is_connected is True

    def test_connect_returns_false_on_missing_config(self):
        logger = logging.getLogger("test")
        conn = GoogleCloudDataCatalogConnector({}, logger)
        result = _run(conn.connect())
        assert result is False
        assert conn.is_connected is False

    def test_connect_exception_is_handled(self):
        conn = self._connector()

        async def bad_connect():
            raise RuntimeError("network failure")

        with patch.object(conn, "_connect_impl", side_effect=RuntimeError("network failure")):
            result = _run(conn.connect())
        assert result is False

    def test_search_data_products_returns_list(self):
        conn = self._connector()
        conn.is_connected = True
        conn.last_health_check = time.time()
        products = _run(conn.search_data_products(SearchFilters()))
        assert isinstance(products, list)
        assert len(products) >= 1

    def test_search_data_products_returns_empty_on_error(self):
        conn = self._connector()
        conn.is_connected = True
        conn.last_health_check = time.time()

        async def boom(filters):
            raise RuntimeError("search exploded")

        with patch.object(conn, "_search_data_products_impl", side_effect=RuntimeError("boom")):
            products = _run(conn.search_data_products(SearchFilters()))
        assert products == []

    def test_get_data_product_returns_product_when_found(self):
        conn = self._connector()
        conn.is_connected = True
        conn.last_health_check = time.time()
        # GCP connector has "gcp-customer-360-v2" in its mock data
        result = _run(conn.get_data_product("gcp-customer-360-v2"))
        assert result is not None
        assert result.id == "gcp-customer-360-v2"

    def test_get_data_product_returns_none_when_not_found(self):
        conn = self._connector()
        conn.is_connected = True
        conn.last_health_check = time.time()
        result = _run(conn.get_data_product("does-not-exist-xyz"))
        assert result is None

    def test_get_catalog_stats_returns_dict(self):
        conn = self._connector()
        conn.is_connected = True
        conn.last_health_check = time.time()
        stats = _run(conn.get_catalog_stats())
        assert isinstance(stats, dict)
        assert "total_products" in stats

    def test_get_catalog_stats_on_error_returns_error_dict(self):
        conn = self._connector()
        conn.is_connected = True
        conn.last_health_check = time.time()

        async def boom():
            raise RuntimeError("stats failure")

        with patch.object(conn, "_get_catalog_stats_impl", side_effect=RuntimeError("boom")):
            stats = _run(conn.get_catalog_stats())
        assert "error" in stats
        assert stats["available"] is False

    def test_health_check_true_when_connected(self):
        conn = self._connector()
        conn.is_connected = True
        result = _run(conn._health_check())
        assert result is True

    def test_health_check_false_when_not_connected(self):
        conn = self._connector()
        conn.is_connected = False
        result = _run(conn._health_check())
        assert result is False

    def test_ensure_healthy_triggers_reconnect_when_needed(self):
        conn = self._connector()
        conn.is_connected = False
        conn.last_health_check = None

        connect_calls = []

        async def fake_connect():
            connect_calls.append(1)
            return True

        with patch.object(conn, "connect", side_effect=fake_connect):
            _run(conn._ensure_healthy_connection())
        assert len(connect_calls) == 1

    def test_ensure_healthy_skips_check_within_interval(self):
        conn = self._connector()
        conn.is_connected = True
        conn.last_health_check = time.time()  # just checked
        # Should not reconnect
        connect_calls = []

        async def fake_connect():
            connect_calls.append(1)
            return True

        with patch.object(conn, "connect", side_effect=fake_connect):
            _run(conn._ensure_healthy_connection())
        assert len(connect_calls) == 0


# ---------------------------------------------------------------------------
# HealthChecker
# ---------------------------------------------------------------------------


class TestHealthChecker:
    def _make_healthy_connector(self):
        conn = MagicMock()
        conn._health_check = AsyncMock(return_value=True)
        conn.circuit_breaker = MagicMock()
        conn.circuit_breaker.state = "CLOSED"
        return conn

    def _make_unhealthy_connector(self):
        conn = MagicMock()
        conn._health_check = AsyncMock(return_value=False)
        conn.circuit_breaker = MagicMock()
        conn.circuit_breaker.state = "OPEN"
        return conn

    def test_check_system_health_all_healthy(self):
        connectors = {
            "gcp": self._make_healthy_connector(),
            "aws": self._make_healthy_connector(),
        }
        checker = HealthChecker(connectors)
        report = _run(checker.check_system_health())
        assert report["status"] == "healthy"
        assert report["overall_health_score"] == 1.0
        assert "gcp" in report["connectors"]
        assert report["connectors"]["gcp"]["status"] == "healthy"

    def test_check_system_health_none_healthy(self):
        connectors = {
            "gcp": self._make_unhealthy_connector(),
        }
        checker = HealthChecker(connectors)
        report = _run(checker.check_system_health())
        assert report["status"] == "critical"
        assert report["overall_health_score"] == 0.0

    def test_check_system_health_partial(self):
        connectors = {
            "gcp": self._make_healthy_connector(),
            "aws": self._make_unhealthy_connector(),
        }
        checker = HealthChecker(connectors)
        report = _run(checker.check_system_health())
        assert report["status"] in ("partial", "degraded")

    def test_check_system_health_no_connectors(self):
        checker = HealthChecker({})
        report = _run(checker.check_system_health())
        assert report["overall_health_score"] == 0.0

    def test_check_system_health_connector_raises(self):
        conn = MagicMock()
        conn._health_check = AsyncMock(side_effect=RuntimeError("boom"))
        checker = HealthChecker({"bad": conn})
        report = _run(checker.check_system_health())
        assert report["connectors"]["bad"]["status"] == "error"

    def test_check_connector_health_not_found(self):
        checker = HealthChecker({})
        result = _run(checker.check_connector_health("missing"))
        assert result["status"] == "not_found"

    def test_check_connector_health_healthy(self):
        conn = self._make_healthy_connector()
        checker = HealthChecker({"gcp": conn})
        result = _run(checker.check_connector_health("gcp"))
        assert result["status"] == "healthy"
        assert "response_time" in result

    def test_check_connector_health_unhealthy(self):
        conn = self._make_unhealthy_connector()
        checker = HealthChecker({"gcp": conn})
        result = _run(checker.check_connector_health("gcp"))
        assert result["status"] == "unhealthy"

    def test_check_connector_health_raises(self):
        conn = MagicMock()
        conn._health_check = AsyncMock(side_effect=RuntimeError("crash"))
        checker = HealthChecker({"bad": conn})
        result = _run(checker.check_connector_health("bad"))
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# PerformanceMonitor
# ---------------------------------------------------------------------------


class TestPerformanceMonitor:
    def test_monitor_search_records_latency(self):
        pm = PerformanceMonitor()

        async def fast_search(filters):
            return [_make_product()]

        with patch("fluid_build.cli.market.metrics_collector") as mc:
            mc.record_search_request = MagicMock()
            result = _run(pm.monitor_search("gcp", fast_search, SearchFilters()))
        assert len(result) == 1

    def test_monitor_search_records_slow_query(self):
        pm = PerformanceMonitor(slow_query_threshold=-1.0)

        async def search_func(f):
            return []

        with patch.object(pm.logger, "warning"):
            _run(pm.monitor_search("gcp", search_func, SearchFilters()))
        assert len(pm.slow_queries) == 1
        assert pm.slow_queries[0]["catalog_type"] == "gcp"

    def test_monitor_search_slow_query_pruned_at_100(self):
        pm = PerformanceMonitor(slow_query_threshold=-1.0)
        pm.slow_queries = [{"x": i} for i in range(100)]

        async def search_func(f):
            return []

        with patch.object(pm.logger, "warning"):
            _run(pm.monitor_search("gcp", search_func, SearchFilters()))
        assert len(pm.slow_queries) == 100  # pruned to last 100

    def test_monitor_search_propagates_exception(self):
        pm = PerformanceMonitor()

        async def bad_search(f):
            raise ValueError("fail")

        try:
            _run(pm.monitor_search("gcp", bad_search, SearchFilters()))
            raise AssertionError("Expected exception")
        except ValueError:
            pass

    def test_get_slow_queries_limit(self):
        pm = PerformanceMonitor()
        pm.slow_queries = [{"n": i} for i in range(20)]
        result = pm.get_slow_queries(limit=5)
        assert len(result) == 5

    def test_get_performance_summary_has_slow_queries(self):
        pm = PerformanceMonitor()
        summary = pm.get_performance_summary()
        assert "slow_queries" in summary
        assert "threshold" in summary["slow_queries"]


# ---------------------------------------------------------------------------
# ConnectionPool
# ---------------------------------------------------------------------------


class TestConnectionPool:
    def test_get_connector_creates_new(self):
        pool = ConnectionPool(max_connections=5)

        async def factory():
            return MagicMock()

        conn = _run(pool.get_connector("gcp", factory))
        assert conn is not None
        assert pool.active_connections["gcp"] == 1

    def test_get_connector_reuses_from_pool(self):
        pool = ConnectionPool(max_connections=5)
        existing = MagicMock()

        async def factory():
            return MagicMock()

        # Manually put a connector in the pool
        _run(pool.get_connector("gcp", factory))  # creates pool
        pool.pools["gcp"].put_nowait(existing)

        conn = _run(pool.get_connector("gcp", factory))
        assert conn is existing  # returned from pool, not factory

    def test_return_connector_puts_back(self):
        async def _test():
            pool = ConnectionPool(max_connections=5)
            conn = MagicMock()
            pool.pools["gcp"] = asyncio.Queue(maxsize=5)
            pool.active_connections["gcp"] = 1
            await pool.return_connector("gcp", conn)
            assert pool.pools["gcp"].qsize() == 1

        _run(_test())

    def test_return_connector_full_pool_discards(self):
        async def _test():
            pool = ConnectionPool(max_connections=1)
            pool.pools["gcp"] = asyncio.Queue(maxsize=1)
            pool.active_connections["gcp"] = 1
            pool.pools["gcp"].put_nowait(MagicMock())
            await pool.return_connector("gcp", MagicMock())
            assert pool.active_connections["gcp"] == 0

        _run(_test())


# ---------------------------------------------------------------------------
# MarketDiscoveryEngine.advanced_search
# ---------------------------------------------------------------------------


class TestMarketDiscoveryEngineAdvancedSearch:
    def _engine(self):
        config = _base_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        mock_connector = MagicMock()
        mock_connector.search_data_products = AsyncMock(
            return_value=[
                _make_product(id="p1", name="Finance Report", domain="finance"),
                _make_product(id="p2", name="Sales Dashboard", domain="sales"),
            ]
        )
        engine.connectors = {"gcp": mock_connector}
        return engine

    def test_advanced_search_returns_search_result(self):
        engine = self._engine()
        filters = SearchFilters(text_query="finance")
        result = _run(engine.advanced_search(filters))
        assert hasattr(result, "products")
        assert hasattr(result, "total_count")
        assert hasattr(result, "facets")
        assert hasattr(result, "query_time")

    def test_advanced_search_with_save(self):
        import fluid_build.cli.market as market_module

        engine = self._engine()
        filters = SearchFilters(text_query="sales", save_search=True, search_name="my-saved")
        _run(engine.advanced_search(filters))
        assert "my-saved" in market_module.advanced_search_engine.saved_searches
        # Cleanup
        del market_module.advanced_search_engine.saved_searches["my-saved"]

    def test_advanced_search_generates_suggestions_for_few_results(self):
        config = _base_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        # Only 1 product returned → suggestions generated
        mock_connector = MagicMock()
        mock_connector.search_data_products = AsyncMock(
            return_value=[_make_product(id="p1", name="Sales Report", tags=["sales", "revenue"])]
        )
        engine.connectors = {"gcp": mock_connector}
        filters = SearchFilters(text_query="sal")
        result = _run(engine.advanced_search(filters))
        # Should have produced some suggestions since < 5 results
        assert isinstance(result.suggestions, list)

    def test_advanced_search_pagination(self):
        config = _base_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        products = [_make_product(id=f"p{i}", name=f"Product {i}") for i in range(10)]
        mock_connector = MagicMock()
        mock_connector.search_data_products = AsyncMock(return_value=products)
        engine.connectors = {"gcp": mock_connector}

        filters = SearchFilters(limit=3, offset=0)
        result = _run(engine.advanced_search(filters))
        assert len(result.products) <= 3

    def test_advanced_search_records_history(self):
        import fluid_build.cli.market as market_module

        original_len = len(market_module.advanced_search_engine.search_history)
        engine = self._engine()
        _run(engine.advanced_search(SearchFilters(text_query="test-query-xyz")))
        assert len(market_module.advanced_search_engine.search_history) == original_len + 1


# ---------------------------------------------------------------------------
# handle_health_check
# ---------------------------------------------------------------------------


class TestHandleHealthCheck:
    def test_no_health_checker_returns_1(self):
        config = _base_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        engine.console = None

        async def run_test():
            with patch.object(engine, "initialize_connectors", new_callable=AsyncMock):
                engine.health_checker = None
                return await handle_health_check(engine, MagicMock(), logger)

        result = _run(run_test())
        assert result == 1

    def test_healthy_system_returns_0(self):
        config = _base_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        engine.console = None

        mock_checker = MagicMock()
        mock_checker.check_system_health = AsyncMock(
            return_value={"status": "healthy", "connectors": {}, "overall_health_score": 1.0}
        )
        engine.health_checker = mock_checker

        args = MagicMock()
        args.connector = None

        async def run_test():
            with patch.object(engine, "initialize_connectors", new_callable=AsyncMock):
                with patch("fluid_build.cli.market.cprint"):
                    return await handle_health_check(engine, args, logger)

        result = _run(run_test())
        assert result == 0

    def test_specific_connector_health_check(self):
        config = _base_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        engine.console = None

        mock_checker = MagicMock()
        mock_checker.check_connector_health = AsyncMock(
            return_value={
                "status": "healthy",
                "response_time": 0.01,
                "timestamp": "2024-01-01T00:00:00",
            }
        )
        engine.health_checker = mock_checker

        args = MagicMock()
        args.connector = "gcp"

        async def run_test():
            with patch.object(engine, "initialize_connectors", new_callable=AsyncMock):
                with patch("fluid_build.cli.market.cprint"):
                    return await handle_health_check(engine, args, logger)

        result = _run(run_test())
        assert result == 0

    def test_critical_system_returns_1(self):
        config = _base_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        engine.console = None

        mock_checker = MagicMock()
        mock_checker.check_system_health = AsyncMock(
            return_value={"status": "critical", "connectors": {}, "overall_health_score": 0.0}
        )
        engine.health_checker = mock_checker

        args = MagicMock()
        args.connector = None

        async def run_test():
            with patch.object(engine, "initialize_connectors", new_callable=AsyncMock):
                with patch("fluid_build.cli.market.cprint"):
                    return await handle_health_check(engine, args, logger)

        result = _run(run_test())
        assert result == 1

    def test_exception_returns_1(self):
        config = _base_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        engine.console = None

        async def run_test():
            with patch.object(
                engine,
                "initialize_connectors",
                AsyncMock(side_effect=RuntimeError("init fail")),
            ):
                return await handle_health_check(engine, MagicMock(), logger)

        result = _run(run_test())
        assert result == 1


# ---------------------------------------------------------------------------
# handle_metrics
# ---------------------------------------------------------------------------


class TestHandleMetrics:
    def test_returns_0_on_success(self):
        config = _base_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        engine.console = None

        async def run_test():
            with patch.object(engine, "initialize_connectors", new_callable=AsyncMock):
                with patch("fluid_build.cli.market.cprint"):
                    return await handle_metrics(engine, logger)

        result = _run(run_test())
        assert result == 0

    def test_exception_returns_1(self):
        config = _base_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        engine.console = None

        async def run_test():
            with patch.object(
                engine,
                "initialize_connectors",
                AsyncMock(side_effect=RuntimeError("boom")),
            ):
                return await handle_metrics(engine, logger)

        result = _run(run_test())
        assert result == 1


# ---------------------------------------------------------------------------
# handle_search_suggestions
# ---------------------------------------------------------------------------


class TestHandleSearchSuggestions:
    def test_returns_0_with_suggestions(self):
        config = _base_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        engine.console = None

        mock_connector = MagicMock()
        mock_connector.search_data_products = AsyncMock(
            return_value=[_make_product(name="Sales Report", tags=["sales", "revenue"])]
        )
        engine.connectors = {"gcp": mock_connector}

        async def run_test():
            with patch.object(engine, "initialize_connectors", new_callable=AsyncMock):
                with patch("fluid_build.cli.market.cprint"):
                    return await handle_search_suggestions("sal", engine, logger)

        result = _run(run_test())
        assert result == 0

    def test_returns_0_with_no_suggestions(self):
        config = _base_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        engine.console = None

        mock_connector = MagicMock()
        mock_connector.search_data_products = AsyncMock(return_value=[])
        engine.connectors = {"gcp": mock_connector}

        async def run_test():
            with patch.object(engine, "initialize_connectors", new_callable=AsyncMock):
                with patch("fluid_build.cli.market.hint"):
                    return await handle_search_suggestions("zzz_no_match", engine, logger)

        result = _run(run_test())
        assert result == 0

    def test_exception_returns_1(self):
        config = _base_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        engine.console = None

        async def run_test():
            with patch.object(
                engine,
                "initialize_connectors",
                AsyncMock(side_effect=RuntimeError("fail")),
            ):
                return await handle_search_suggestions("test", engine, logger)

        result = _run(run_test())
        assert result == 1


# ---------------------------------------------------------------------------
# format_table_output — text-only path with products
# ---------------------------------------------------------------------------


class TestFormatTableOutputTextPath:
    def test_multiple_products_text_output(self):
        products = [
            _make_product(id="p1", name="Alpha Product", quality_score=0.95),
            _make_product(id="p2", name="Beta Product", quality_score=0.75),
            _make_product(id="p3", name="Gamma Product", quality_score=None),
        ]
        calls = []
        with patch("fluid_build.cli.market.cprint", side_effect=lambda msg: calls.append(msg)):
            format_table_output(products, console=None)
        assert len(calls) >= 4  # header + separator + one per product

    def test_empty_list_prints_no_results_message(self):
        calls = []
        with patch("fluid_build.cli.market.cprint", side_effect=lambda msg: calls.append(msg)):
            format_table_output([], console=None)
        assert any("no data products" in c.lower() for c in calls)


# ---------------------------------------------------------------------------
# format_detailed_output — text-only path variants
# ---------------------------------------------------------------------------


class TestFormatDetailedOutputExtended:
    def test_with_api_endpoint(self):
        product = _make_product(api_endpoint="https://api.example.com/v1/data")
        with patch("fluid_build.cli.market.cprint"):
            format_detailed_output(product, console=None)  # should not raise

    def test_with_documentation_url(self):
        product = _make_product(documentation_url="https://docs.example.com")
        with patch("fluid_build.cli.market.cprint"):
            format_detailed_output(product, console=None)

    def test_with_schema_url(self):
        product = _make_product(schema_url="https://schema.example.com/v1.json")
        with patch("fluid_build.cli.market.cprint"):
            format_detailed_output(product, console=None)

    def test_no_tags(self):
        product = _make_product(tags=[])
        with patch("fluid_build.cli.market.cprint"):
            format_detailed_output(product, console=None)

    def test_deprecated_status(self):
        product = _make_product(status=DataProductStatus.DEPRECATED)
        with patch("fluid_build.cli.market.cprint"):
            format_detailed_output(product, console=None)


# ---------------------------------------------------------------------------
# generate_output — detailed format with multiple products
# ---------------------------------------------------------------------------


class TestGenerateOutputExtended:
    def test_detailed_format_multiple_products(self):
        products = [_make_product(id="p1"), _make_product(id="p2")]
        args = _make_test_args(format="detailed")
        logger = logging.getLogger("test")
        with patch("fluid_build.cli.market.format_detailed_output") as mock_fmt:
            result = generate_output(products, args, None, logger)
        assert result == 0
        assert mock_fmt.call_count == 2

    def test_unknown_format_falls_through_to_table(self):
        products = [_make_product()]
        args = _make_test_args(format="unknown_format")
        logger = logging.getLogger("test")
        with patch("fluid_build.cli.market.format_table_output") as mock_fmt:
            result = generate_output(products, args, None, logger)
        assert result == 0
        mock_fmt.assert_called_once()


# ---------------------------------------------------------------------------
# run_market_discovery — additional branches
# ---------------------------------------------------------------------------


class TestRunMarketDiscoveryExtended:
    def test_list_catalogs_flag(self):
        args = _make_test_args(list_catalogs=True)
        logger = logging.getLogger("test")
        with patch("fluid_build.cli.market.handle_list_catalogs", return_value=0) as mock_lc:
            result = _run(run_market_discovery(args, logger))
        assert result == 0
        mock_lc.assert_called_once()

    def test_config_template_flag(self):
        args = _make_test_args(config_template=True)
        logger = logging.getLogger("test")
        with patch("fluid_build.cli.market.handle_config_template", return_value=0) as mock_ct:
            result = _run(run_market_discovery(args, logger))
        assert result == 0
        mock_ct.assert_called_once()

    def test_debug_flag_on_exception(self):
        args = _make_test_args(debug=True)
        logger = logging.getLogger("test")
        with patch(
            "fluid_build.cli.market.load_market_config",
            side_effect=RuntimeError("debug test"),
        ):
            result = _run(run_market_discovery(args, logger))
        assert result == 1

    def test_no_connectors_returns_1(self):
        args = _make_test_args(catalogs="google_cloud_data_catalog")
        logger = logging.getLogger("test")

        with patch("fluid_build.cli.market.MarketDiscoveryEngine") as MockEngine:
            instance = MockEngine.return_value
            instance.initialize_connectors = AsyncMock()
            instance.connectors = {}
            instance.console = None
            result = _run(run_market_discovery(args, logger))
        assert result == 1


# ---------------------------------------------------------------------------
# run() — sync wrapper edge cases
# ---------------------------------------------------------------------------


class TestRunExtended:
    def test_run_raises_cli_error_on_unhandled_exception(self):
        from fluid_build.cli._common import CLIError

        args = _make_test_args()
        logger = logging.getLogger("test")
        with patch("fluid_build.cli.market.asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.is_running.return_value = False
            with patch(
                "fluid_build.cli.market.asyncio.run",
                side_effect=RuntimeError("unhandled"),
            ):
                try:
                    run(args, logger)
                    raise AssertionError("Expected CLIError")
                except CLIError as e:
                    assert e.exit_code == 1

    def test_run_in_thread_when_loop_running(self):
        args = _make_test_args(catalogs="google_cloud_data_catalog")
        logger = logging.getLogger("test")
        with patch("fluid_build.cli.market.asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.is_running.return_value = True
            with patch(
                "fluid_build.cli.market.run_market_discovery", new_callable=AsyncMock
            ) as mock_rmd:
                mock_rmd.return_value = 42
                result = run(args, logger)
        assert result == 42


# ---------------------------------------------------------------------------
# MarketDiscoveryEngine.initialize_connectors — collibra/alation paths
# ---------------------------------------------------------------------------


class TestMarketDiscoveryEngineInitializeConnectors:
    def test_collibra_connector_initialized(self):
        config = {
            "catalogs": ["collibra"],
            "collibra": {
                "base_url": "http://collibra.example.com",
                "username": "user",
                "password": "pass",
            },
            "defaults": {"timeout_seconds": 30},
            "cache": {"enabled": False},
        }
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        _run(engine.initialize_connectors(["collibra"]))
        assert "collibra" in engine.connectors

    def test_alation_connector_initialized(self):
        config = {
            "catalogs": ["alation"],
            "alation": {
                "base_url": "http://alation.example.com",
                "api_token": "my-token",
            },
            "defaults": {"timeout_seconds": 30},
            "cache": {"enabled": False},
        }
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        _run(engine.initialize_connectors(["alation"]))
        assert "alation" in engine.connectors

    def test_apache_atlas_initialized_with_credentials(self):
        config = {
            "catalogs": ["apache_atlas"],
            "apache_atlas": {
                "base_url": "http://atlas.example.com",
                "username": "admin",
                "password": "admin",
            },
            "defaults": {"timeout_seconds": 30},
            "cache": {"enabled": False},
        }
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        _run(engine.initialize_connectors(["apache_atlas"]))
        assert "apache_atlas" in engine.connectors

    def test_health_checker_initialized_with_connectors(self):
        config = _base_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        _run(engine.initialize_connectors(["google_cloud_data_catalog"]))
        assert engine.health_checker is not None

    def test_health_checker_not_initialized_without_connectors(self):
        config = {
            "catalogs": [],
            "defaults": {"timeout_seconds": 30},
            "cache": {"enabled": False},
        }
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        _run(engine.initialize_connectors([]))
        assert engine.health_checker is None


# ---------------------------------------------------------------------------
# build_search_filters — owner string path
# ---------------------------------------------------------------------------


class TestBuildSearchFiltersExtra:
    def test_owner_string_not_list(self):
        args = _make_test_args(owner="alice")
        f = build_search_filters(args)
        assert f.owner == "alice"

    def test_tags_string_not_list(self):
        args = _make_test_args(tags="crypto")
        f = build_search_filters(args)
        assert f.tags == ["crypto"]

    def test_layer_string_not_list(self):
        args = _make_test_args(layer="gold")
        f = build_search_filters(args)
        assert f.layer == DataProductLayer.GOLD

    def test_status_string_not_list(self):
        args = _make_test_args(status="active")
        f = build_search_filters(args)
        assert f.status == DataProductStatus.ACTIVE
