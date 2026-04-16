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

"""Tests for fluid_build.cli.market — enums, dataclasses, AdvancedSearchEngine, CircuitBreaker."""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import pytest

from fluid_build.cli.market import (
    AdvancedSearchEngine,
    CatalogType,
    CircuitBreaker,
    DataProductLayer,
    DataProductMetadata,
    DataProductStatus,
    SearchFilters,
    SearchResult,
)


def _make_product(**overrides):
    """Helper to create a DataProductMetadata with sensible defaults."""
    defaults = dict(
        id="p1",
        name="Sales Data",
        description="Daily sales",
        domain="finance",
        owner="alice",
        layer=DataProductLayer.GOLD,
        status=DataProductStatus.ACTIVE,
        version="1.0",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        tags=["sales", "gold"],
    )
    defaults.update(overrides)
    return DataProductMetadata(**defaults)


# ── Enums ──


class TestEnums:
    def test_catalog_type_values(self):
        assert CatalogType.GOOGLE_CLOUD_DATA_CATALOG.value == "google_cloud_data_catalog"
        assert CatalogType.FLUID_COMMAND_CENTER.value == "fluid_command_center"

    def test_data_product_layer_values(self):
        assert DataProductLayer.RAW.value == "raw"
        assert DataProductLayer.GOLD.value == "gold"

    def test_data_product_status_values(self):
        assert DataProductStatus.ACTIVE.value == "active"
        assert DataProductStatus.DEPRECATED.value == "deprecated"


# ── DataProductMetadata ──


class TestDataProductMetadata:
    def test_defaults(self):
        p = _make_product()
        assert p.tags == ["sales", "gold"]
        assert p.schema_url is None
        assert p.quality_score is None

    def test_custom_fields(self):
        p = _make_product(quality_score=0.95, catalog_source="bigquery")
        assert p.quality_score == 0.95
        assert p.catalog_source == "bigquery"


# ── SearchFilters ──


class TestSearchFilters:
    def test_defaults(self):
        f = SearchFilters()
        assert f.limit == 50
        assert f.offset == 0
        assert f.exact_match is False
        assert f.sort_by == "relevance"
        assert f.sort_order == "desc"
        assert f.search_fields == ["name", "description", "tags"]

    def test_custom(self):
        f = SearchFilters(domain="finance", layer=DataProductLayer.GOLD, limit=10)
        assert f.domain == "finance"
        assert f.layer == DataProductLayer.GOLD
        assert f.limit == 10


# ── SearchResult ──


class TestSearchResult:
    def test_basic(self):
        products = [_make_product()]
        sr = SearchResult(products=products, total_count=1, facets={}, query_time=0.01)
        assert sr.total_count == 1
        assert sr.suggestions == []


# ── AdvancedSearchEngine ──


class TestAdvancedSearchEngine:
    def _engine(self):
        return AdvancedSearchEngine(logging.getLogger("test"))

    # -- relevance scoring --

    def test_relevance_no_query(self):
        engine = self._engine()
        f = SearchFilters()  # no text_query
        score = engine.calculate_relevance_score(_make_product(), f)
        assert score == 1.0

    def test_relevance_name_match(self):
        engine = self._engine()
        f = SearchFilters(text_query="Sales")
        score = engine.calculate_relevance_score(_make_product(name="Sales Data"), f)
        assert score > 0

    def test_relevance_quality_boost(self):
        engine = self._engine()
        f = SearchFilters(text_query="Sales")
        base = engine.calculate_relevance_score(_make_product(quality_score=None), f)
        boosted = engine.calculate_relevance_score(_make_product(quality_score=1.0), f)
        assert boosted > base

    def test_relevance_recency_boost(self):
        engine = self._engine()
        f = SearchFilters(text_query="Sales")
        old = _make_product(updated_at=datetime.now(timezone.utc) - timedelta(days=365))
        new = _make_product(updated_at=datetime.now(timezone.utc))
        score_old = engine.calculate_relevance_score(old, f)
        score_new = engine.calculate_relevance_score(new, f)
        assert score_new >= score_old

    # -- facet extraction --

    def test_extract_facets(self):
        engine = self._engine()
        products = [
            _make_product(domain="finance", tags=["a"]),
            _make_product(domain="finance", tags=["a", "b"]),
            _make_product(domain="marketing", tags=["b"]),
        ]
        facets = engine.extract_facets(products)
        assert facets["domain"]["finance"] == 2
        assert facets["domain"]["marketing"] == 1
        assert facets["tags"]["a"] == 2
        assert facets["tags"]["b"] == 2

    # -- advanced filters --

    def test_exclude_deprecated(self):
        engine = self._engine()
        products = [
            _make_product(status=DataProductStatus.ACTIVE),
            _make_product(status=DataProductStatus.DEPRECATED),
        ]
        f = SearchFilters(include_deprecated=False)
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 1

    def test_filter_has_documentation(self):
        engine = self._engine()
        products = [
            _make_product(documentation_url="https://docs.example.com"),
            _make_product(documentation_url=None),
        ]
        f = SearchFilters(has_documentation=True)
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 1

    def test_filter_facets_domain(self):
        engine = self._engine()
        products = [
            _make_product(domain="finance"),
            _make_product(domain="marketing"),
        ]
        f = SearchFilters(facets={"domain": ["finance"]})
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 1

    def test_filter_usage_count(self):
        engine = self._engine()
        products = [
            _make_product(usage_stats={"total_queries": 100}),
            _make_product(usage_stats={"total_queries": 5}),
        ]
        f = SearchFilters(min_usage_count=10)
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 1

    # -- ranking & sorting --

    def test_sort_by_name_asc(self):
        engine = self._engine()
        products = [_make_product(name="Zebra"), _make_product(name="Alpha")]
        f = SearchFilters(sort_by="name", sort_order="asc")
        result = engine.rank_and_sort_products(products, f)
        assert result[0].name == "Alpha"

    def test_sort_by_quality_desc(self):
        engine = self._engine()
        products = [
            _make_product(quality_score=0.5),
            _make_product(quality_score=0.9),
        ]
        f = SearchFilters(sort_by="quality_score", sort_order="desc")
        result = engine.rank_and_sort_products(products, f)
        assert result[0].quality_score == 0.9

    def test_sort_by_relevance_with_query(self):
        engine = self._engine()
        products = [
            _make_product(name="Unrelated"),
            _make_product(name="Sales Report"),
        ]
        f = SearchFilters(text_query="Sales", sort_by="relevance", sort_order="desc")
        result = engine.rank_and_sort_products(products, f)
        assert result[0].name == "Sales Report"

    # -- saved searches --

    def test_save_and_load_search(self):
        engine = self._engine()
        f = SearchFilters(search_name="my-search", domain="finance")
        assert engine.save_search(f) is True
        loaded = engine.load_saved_search("my-search")
        assert loaded is not None
        assert loaded.domain == "finance"

    def test_save_search_without_name(self):
        engine = self._engine()
        f = SearchFilters()
        assert engine.save_search(f) is False

    def test_list_saved_searches(self):
        engine = self._engine()
        engine.save_search(SearchFilters(search_name="s1"))
        engine.save_search(SearchFilters(search_name="s2"))
        assert sorted(engine.list_saved_searches()) == ["s1", "s2"]

    # -- suggestions --

    def test_generate_suggestions(self):
        engine = self._engine()
        products = [_make_product(name="Sales Report", tags=["revenue", "daily"])]
        suggestions = engine.generate_search_suggestions(products, "sal")
        assert any("sales" in s for s in suggestions)


# ── CircuitBreaker ──


class TestCircuitBreaker:
    def test_initial_state(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10)
        assert cb.state == "CLOSED"
        assert cb.failure_count == 0

    def test_on_success_resets(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10)
        cb.failure_count = 2
        cb.state = "HALF_OPEN"
        cb._on_success()
        assert cb.failure_count == 0
        assert cb.state == "CLOSED"

    def test_on_failure_increments(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10)
        cb._on_failure()
        assert cb.failure_count == 1
        assert cb.state == "CLOSED"

    def test_on_failure_opens_at_threshold(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10)
        cb._on_failure()
        cb._on_failure()
        assert cb.state == "OPEN"

    def test_should_attempt_reset_false(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
        cb.last_failure_time = time.time()
        assert cb._should_attempt_reset() is False

    def test_should_attempt_reset_true(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=1)
        cb.last_failure_time = time.time() - 2
        assert cb._should_attempt_reset() is True

    def test_should_attempt_reset_no_failure(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=1)
        assert not cb._should_attempt_reset()

    def test_call_success(self):
        import asyncio

        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10)

        async def good_func():
            return "ok"

        result = asyncio.run(cb.call(good_func))
        assert result == "ok"
        assert cb.state == "CLOSED"

    def test_call_open_raises(self):
        import asyncio

        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=3600)
        cb.state = "OPEN"
        cb.last_failure_time = time.time()

        async def func():
            return "ok"

        with pytest.raises(Exception, match="Circuit breaker is OPEN"):
            asyncio.run(cb.call(func))

    def test_call_failure_increments_count(self):
        import asyncio

        cb = CircuitBreaker(failure_threshold=5, recovery_timeout=10)

        async def bad_func():
            raise ValueError("fail")

        with pytest.raises(ValueError):
            asyncio.run(cb.call(bad_func))
        assert cb.failure_count == 1

    def test_call_open_to_half_open_on_timeout(self):
        import asyncio

        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0)
        cb.state = "OPEN"
        cb.last_failure_time = time.time() - 1

        async def good_func():
            return "recovered"

        result = asyncio.run(cb.call(good_func))
        assert result == "recovered"
        assert cb.state == "CLOSED"


# ---------------------------------------------------------------------------
# Additional imports for extended tests
# ---------------------------------------------------------------------------


import asyncio as _asyncio
import json as _json
from unittest.mock import AsyncMock as _AsyncMock
from unittest.mock import MagicMock as _MagicMock
from unittest.mock import patch as _patch

from fluid_build.cli.market import (
    AlationConnector,
    ApacheAtlasConnector,
    AWSGlueDataCatalogConnector,
    AzurePurviewConnector,
    CacheEntry,
    CollibraConnector,
    ConfluentSchemaRegistryConnector,
    CustomRestApiConnector,
    DataHubConnector,
    GoogleCloudDataCatalogConnector,
    MarketCache,
    MarketDiscoveryEngine,
    MetricsCollector,
    _load_env_config,
    _merge_config,
    build_search_filters,
    format_json_output,
    generate_output,
    handle_config_template,
    handle_list_catalogs,
    handle_marketplace_stats,
    handle_product_details,
    handle_saved_searches,
    handle_search_history,
    load_market_config,
    run,
    run_market_discovery,
)

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _make_test_product(**overrides):
    """Helper to create a DataProductMetadata with sensible defaults."""
    defaults = dict(
        id="tp1",
        name="Test Product",
        description="A test product",
        domain="finance",
        owner="team-a",
        layer=DataProductLayer.GOLD,
        status=DataProductStatus.ACTIVE,
        version="1.0",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        tags=["test"],
    )
    defaults.update(overrides)
    return DataProductMetadata(**defaults)


def _make_test_args(**kwargs):
    """Helper to create a mock args namespace for CLI functions."""
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
    args = _MagicMock()
    for k, v in defaults.items():
        setattr(args, k, v)
    return args


# ---------------------------------------------------------------------------
# Connector subclasses
# ---------------------------------------------------------------------------


class TestGoogleCloudDataCatalogConnector:
    def _connector(self, **cfg):
        logger = logging.getLogger("test")
        return GoogleCloudDataCatalogConnector(cfg, logger)

    def test_connect_success(self):
        connector = self._connector(project_id="my-project")
        result = _asyncio.run(connector._connect_impl())
        assert result is True

    def test_connect_missing_project_id(self):
        connector = self._connector()
        result = _asyncio.run(connector._connect_impl())
        assert result is False

    def test_search_returns_products(self):
        connector = self._connector(project_id="my-project")
        connector.is_connected = True
        products = _asyncio.run(connector._search_data_products_impl(SearchFilters()))
        assert len(products) >= 1
        assert all(p.catalog_type == "google_cloud_data_catalog" for p in products)

    def test_search_with_domain_filter(self):
        connector = self._connector(project_id="my-project")
        connector.is_connected = True
        filters = SearchFilters(domain="marketing")
        products = _asyncio.run(connector._search_data_products_impl(filters))
        assert all("marketing" in p.domain.lower() for p in products)

    def test_get_catalog_stats(self):
        connector = self._connector(project_id="my-project")
        connector.is_connected = True
        stats = _asyncio.run(connector._get_catalog_stats_impl())
        assert "total_products" in stats


class TestAWSGlueDataCatalogConnector:
    def _connector(self, **cfg):
        logger = logging.getLogger("test")
        return AWSGlueDataCatalogConnector(cfg, logger)

    def test_connect_success(self):
        connector = self._connector(region="us-east-1")
        result = _asyncio.run(connector._connect_impl())
        assert result is True

    def test_search_returns_products(self):
        connector = self._connector(region="us-east-1")
        connector.is_connected = True
        products = _asyncio.run(connector._search_data_products_impl(SearchFilters()))
        assert len(products) >= 1
        assert all(p.catalog_type == "aws_glue_data_catalog" for p in products)

    def test_get_catalog_stats(self):
        connector = self._connector(region="us-east-1")
        connector.is_connected = True
        stats = _asyncio.run(connector._get_catalog_stats_impl())
        assert "total_products" in stats


class TestAzurePurviewConnector:
    def _connector(self, **cfg):
        logger = logging.getLogger("test")
        return AzurePurviewConnector(cfg, logger)

    def test_connect_success(self):
        connector = self._connector(account_name="my-account")
        result = _asyncio.run(connector._connect_impl())
        assert result is True

    def test_connect_missing_account_name(self):
        connector = self._connector()
        result = _asyncio.run(connector._connect_impl())
        assert result is False

    def test_search_returns_products(self):
        connector = self._connector(account_name="my-account")
        connector.is_connected = True
        products = _asyncio.run(connector._search_data_products_impl(SearchFilters()))
        assert len(products) >= 1
        assert all(p.catalog_type == "azure_purview" for p in products)

    def test_get_catalog_stats(self):
        connector = self._connector(account_name="my-account")
        connector.is_connected = True
        stats = _asyncio.run(connector._get_catalog_stats_impl())
        assert "total_products" in stats


class TestDataHubConnector:
    def _connector(self, **cfg):
        logger = logging.getLogger("test")
        return DataHubConnector(cfg, logger)

    def test_connect_success(self):
        connector = self._connector(server_url="http://localhost:8080")
        result = _asyncio.run(connector._connect_impl())
        assert result is True

    def test_search_returns_products(self):
        connector = self._connector(server_url="http://localhost:8080")
        connector.is_connected = True
        products = _asyncio.run(connector._search_data_products_impl(SearchFilters()))
        assert len(products) >= 1
        assert all(p.catalog_type == "datahub" for p in products)

    def test_get_catalog_stats(self):
        connector = self._connector()
        connector.is_connected = True
        stats = _asyncio.run(connector._get_catalog_stats_impl())
        assert "total_products" in stats


class TestApacheAtlasConnector:
    def _connector(self, **cfg):
        logger = logging.getLogger("test")
        return ApacheAtlasConnector(cfg, logger)

    def test_connect_missing_credentials(self):
        connector = self._connector(base_url="http://localhost:21000")
        with _patch.dict("os.environ", {"ATLAS_USERNAME": "", "ATLAS_PASSWORD": ""}):
            result = _asyncio.run(connector._connect_impl())
        assert result is False

    def test_connect_success_with_credentials(self):
        connector = self._connector(
            base_url="http://localhost:21000",
            username="admin",
            password="admin",
        )
        result = _asyncio.run(connector._connect_impl())
        assert result is True

    def test_search_returns_products(self):
        connector = self._connector(
            base_url="http://localhost:21000",
            username="admin",
            password="admin",
        )
        connector.is_connected = True
        products = _asyncio.run(connector._search_data_products_impl(SearchFilters()))
        assert len(products) >= 1
        assert all(p.catalog_type == "apache_atlas" for p in products)

    def test_get_catalog_stats(self):
        connector = self._connector(
            base_url="http://localhost:21000",
            username="admin",
            password="admin",
        )
        connector.is_connected = True
        stats = _asyncio.run(connector._get_catalog_stats_impl())
        assert "total_products" in stats


class TestConfluentSchemaRegistryConnector:
    def _connector(self, **cfg):
        logger = logging.getLogger("test")
        return ConfluentSchemaRegistryConnector(cfg, logger)

    def test_connect_success(self):
        connector = self._connector(url="http://localhost:8081")
        result = _asyncio.run(connector._connect_impl())
        assert result is True

    def test_search_returns_products(self):
        connector = self._connector(url="http://localhost:8081")
        connector.is_connected = True
        products = _asyncio.run(connector._search_data_products_impl(SearchFilters()))
        assert len(products) >= 1
        assert all(p.catalog_type == "confluent_schema_registry" for p in products)

    def test_get_catalog_stats(self):
        connector = self._connector()
        connector.is_connected = True
        stats = _asyncio.run(connector._get_catalog_stats_impl())
        assert "total_products" in stats


class TestCollibraConnector:
    def _connector(self, **cfg):
        logger = logging.getLogger("test")
        return CollibraConnector(cfg, logger)

    def test_connect_missing_base_url(self):
        connector = self._connector(username="u", password="p")
        result = _asyncio.run(connector._connect_impl())
        assert result is False

    def test_connect_missing_credentials(self):
        connector = self._connector(base_url="http://collibra.example.com")
        result = _asyncio.run(connector._connect_impl())
        assert result is False

    def test_connect_success(self):
        connector = self._connector(
            base_url="http://collibra.example.com",
            username="user",
            password="pass",
        )
        result = _asyncio.run(connector._connect_impl())
        assert result is True

    def test_search_returns_products(self):
        connector = self._connector(
            base_url="http://collibra.example.com",
            username="user",
            password="pass",
        )
        connector.is_connected = True
        products = _asyncio.run(connector._search_data_products_impl(SearchFilters()))
        assert len(products) >= 1
        assert all(p.catalog_type == "collibra" for p in products)

    def test_get_catalog_stats(self):
        connector = self._connector(
            base_url="http://collibra.example.com",
            username="user",
            password="pass",
        )
        connector.is_connected = True
        stats = _asyncio.run(connector._get_catalog_stats_impl())
        assert "total_products" in stats


class TestAlationConnector:
    def _connector(self, **cfg):
        logger = logging.getLogger("test")
        return AlationConnector(cfg, logger)

    def test_connect_missing_base_url(self):
        connector = self._connector(api_token="tok")
        result = _asyncio.run(connector._connect_impl())
        assert result is False

    def test_connect_missing_api_token(self):
        connector = self._connector(base_url="http://alation.example.com")
        result = _asyncio.run(connector._connect_impl())
        assert result is False

    def test_connect_success(self):
        connector = self._connector(
            base_url="http://alation.example.com",
            api_token="my-token",
        )
        result = _asyncio.run(connector._connect_impl())
        assert result is True

    def test_search_returns_products(self):
        connector = self._connector(
            base_url="http://alation.example.com",
            api_token="my-token",
        )
        connector.is_connected = True
        products = _asyncio.run(connector._search_data_products_impl(SearchFilters()))
        assert len(products) >= 1
        assert all(p.catalog_type == "alation" for p in products)

    def test_get_catalog_stats(self):
        connector = self._connector(
            base_url="http://alation.example.com",
            api_token="my-token",
        )
        connector.is_connected = True
        stats = _asyncio.run(connector._get_catalog_stats_impl())
        assert "total_products" in stats


class TestCustomRestApiConnector:
    def _connector(self, **cfg):
        logger = logging.getLogger("test")
        return CustomRestApiConnector(cfg, logger)

    def test_connect_missing_base_url(self):
        connector = self._connector()
        result = _asyncio.run(connector._connect_impl())
        assert result is False

    def test_connect_success(self):
        connector = self._connector(
            base_url="http://api.example.com",
            auth_type="bearer",
        )
        result = _asyncio.run(connector._connect_impl())
        assert result is True

    def test_search_returns_products(self):
        connector = self._connector(base_url="http://api.example.com")
        connector.is_connected = True
        products = _asyncio.run(connector._search_data_products_impl(SearchFilters()))
        assert len(products) >= 1
        assert all(p.catalog_type == "custom_rest_api" for p in products)

    def test_get_catalog_stats(self):
        connector = self._connector(base_url="http://api.example.com")
        connector.is_connected = True
        stats = _asyncio.run(connector._get_catalog_stats_impl())
        assert "total_products" in stats


# ---------------------------------------------------------------------------
# BaseCatalogConnector._apply_filters
# ---------------------------------------------------------------------------


class TestBaseCatalogApplyFilters:
    def _connector(self):
        logger = logging.getLogger("test")
        return GoogleCloudDataCatalogConnector({"project_id": "p"}, logger)

    def test_no_filters_returns_all(self):
        connector = self._connector()
        products = [_make_test_product(), _make_test_product(id="p2")]
        result = connector._apply_filters(products, SearchFilters())
        assert len(result) == 2

    def test_domain_filter(self):
        connector = self._connector()
        products = [
            _make_test_product(domain="finance"),
            _make_test_product(domain="marketing"),
        ]
        f = SearchFilters(domain="finance")
        result = connector._apply_filters(products, f)
        assert len(result) == 1
        assert result[0].domain == "finance"

    def test_owner_filter(self):
        connector = self._connector()
        products = [_make_test_product(owner="alice"), _make_test_product(owner="bob")]
        f = SearchFilters(owner="alice")
        result = connector._apply_filters(products, f)
        assert len(result) == 1

    def test_layer_filter(self):
        connector = self._connector()
        products = [
            _make_test_product(layer=DataProductLayer.GOLD),
            _make_test_product(layer=DataProductLayer.SILVER),
        ]
        f = SearchFilters(layer=DataProductLayer.GOLD)
        result = connector._apply_filters(products, f)
        assert len(result) == 1

    def test_status_filter(self):
        connector = self._connector()
        products = [
            _make_test_product(status=DataProductStatus.ACTIVE),
            _make_test_product(status=DataProductStatus.DEPRECATED),
        ]
        f = SearchFilters(status=DataProductStatus.ACTIVE)
        result = connector._apply_filters(products, f)
        assert len(result) == 1

    def test_min_quality_filter(self):
        connector = self._connector()
        products = [
            _make_test_product(quality_score=0.9),
            _make_test_product(quality_score=0.5),
        ]
        f = SearchFilters(min_quality_score=0.8)
        result = connector._apply_filters(products, f)
        assert len(result) == 1

    def test_text_query_filter(self):
        connector = self._connector()
        products = [
            _make_test_product(name="Customer Analytics"),
            _make_test_product(name="Inventory Data"),
        ]
        f = SearchFilters(text_query="customer")
        result = connector._apply_filters(products, f)
        assert len(result) == 1

    def test_limit_applied(self):
        connector = self._connector()
        products = [_make_test_product(id=f"p{i}") for i in range(10)]
        f = SearchFilters(limit=3)
        result = connector._apply_filters(products, f)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# MarketCache & CacheEntry
# ---------------------------------------------------------------------------


class TestMarketCache:
    def _cache(self):
        return MarketCache(max_entries=5, default_ttl_minutes=15)

    def test_miss_on_empty_cache(self):
        cache = self._cache()
        result = cache.get("search", "gcp", SearchFilters())
        assert result is None
        assert cache.stats["misses"] == 1

    def test_hit_after_set(self):
        cache = self._cache()
        filters = SearchFilters()
        data = [_make_test_product()]
        cache.set("search", "gcp", filters, data)
        result = cache.get("search", "gcp", filters)
        assert result is not None
        assert len(result) == 1
        assert cache.stats["hits"] == 1

    def test_expired_entry_returns_none(self):
        cache = self._cache()
        filters = SearchFilters()
        data = [_make_test_product()]
        cache.set("search", "gcp", filters, data, ttl_minutes=60)
        for entry in cache.cache.values():
            entry.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
        result = cache.get("search", "gcp", filters)
        assert result is None

    def test_eviction_when_at_capacity(self):
        cache = self._cache()
        for i in range(5):
            cache.set("search", f"cat{i}", SearchFilters(limit=i + 1), [_make_test_product()])
        cache.set("search", "cat6", SearchFilters(limit=6), [_make_test_product()])
        assert cache.stats["evictions"] == 1
        assert len(cache.cache) == 5

    def test_clear(self):
        cache = self._cache()
        cache.set("search", "gcp", SearchFilters(), [_make_test_product()])
        cache.clear()
        assert len(cache.cache) == 0
        assert cache.stats["hits"] == 0

    def test_get_stats(self):
        cache = self._cache()
        cache.set("search", "gcp", SearchFilters(), [_make_test_product()])
        cache.get("search", "gcp", SearchFilters())
        stats = cache.get_stats()
        assert "hit_ratio" in stats
        assert stats["hits"] == 1

    def test_get_stats_zero_requests(self):
        cache = self._cache()
        stats = cache.get_stats()
        assert stats["hit_ratio"] == 0.0


class TestCacheEntry:
    def test_not_expired_fresh(self):
        entry = CacheEntry(
            data="x",
            created_at=datetime.now(timezone.utc),
            ttl_minutes=60,
        )
        assert entry.is_expired() is False

    def test_expired_old_entry(self):
        entry = CacheEntry(
            data="x",
            created_at=datetime.now(timezone.utc) - timedelta(hours=2),
            ttl_minutes=60,
        )
        assert entry.is_expired() is True


# ---------------------------------------------------------------------------
# MetricsCollector
# ---------------------------------------------------------------------------


class TestMetricsCollector:
    def test_record_search_request(self):
        mc = MetricsCollector()
        mc.record_search_request("gcp", 0.5)
        assert mc.search_requests["gcp"] == 1
        assert mc.search_latency["gcp"] == [0.5]

    def test_record_cache_hit_and_miss(self):
        mc = MetricsCollector()
        mc.record_cache_hit("gcp")
        mc.record_cache_miss("gcp")
        assert mc.cache_hits["gcp"] == 1
        assert mc.cache_misses["gcp"] == 1

    def test_record_error(self):
        mc = MetricsCollector()
        mc.record_error("gcp", "TimeoutError")
        assert mc.error_counts["gcp:TimeoutError"] == 1

    def test_update_connector_health(self):
        mc = MetricsCollector()
        mc.update_connector_health("gcp", True, 0.1)
        assert mc.connector_health["gcp"]["healthy"] is True

    def test_get_summary_cache_hit_rates(self):
        mc = MetricsCollector()
        mc.record_search_request("gcp", 1.0)
        mc.record_cache_hit("gcp")
        mc.record_cache_miss("gcp")
        summary = mc.get_summary()
        assert summary["cache_hit_rates"]["gcp"] == 0.5

    def test_update_circuit_breaker_stats(self):
        mc = MetricsCollector()
        mc.update_circuit_breaker_stats("gcp", "OPEN", 3, 10)
        assert mc.circuit_breaker_stats["gcp"]["state"] == "OPEN"

    def test_update_connection_pool_stats(self):
        mc = MetricsCollector()
        mc.update_connection_pool_stats("gcp", 2, 3, 5)
        assert mc.connection_pool_stats["gcp"]["active_connections"] == 2


# ---------------------------------------------------------------------------
# MarketDiscoveryEngine
# ---------------------------------------------------------------------------


def _base_engine_config():
    return {
        "catalogs": ["google_cloud_data_catalog"],
        "google_cloud_data_catalog": {"project_id": "test"},
        "defaults": {"timeout_seconds": 30},
        "cache": {"enabled": False},
    }


class TestMarketDiscoveryEngine:
    def test_initialize_connectors_success(self):
        config = _base_engine_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        _asyncio.run(engine.initialize_connectors(["google_cloud_data_catalog"]))
        assert "google_cloud_data_catalog" in engine.connectors

    def test_initialize_connectors_unknown_type(self):
        config = _base_engine_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        _asyncio.run(engine.initialize_connectors(["unknown_catalog_type"]))
        assert len(engine.connectors) == 0

    def test_initialize_multiple_connector_types(self):
        config = {
            "catalogs": [
                "google_cloud_data_catalog",
                "aws_glue_data_catalog",
                "azure_purview",
                "datahub",
                "confluent_schema_registry",
                "custom_rest_api",
            ],
            "google_cloud_data_catalog": {"project_id": "test"},
            "aws_glue_data_catalog": {"region": "us-east-1"},
            "azure_purview": {"account_name": "test"},
            "datahub": {"server_url": "http://localhost:8080"},
            "confluent_schema_registry": {"url": "http://localhost:8081"},
            "custom_rest_api": {"base_url": "http://api.example.com"},
            "defaults": {"timeout_seconds": 30},
            "cache": {"enabled": False},
        }
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        _asyncio.run(engine.initialize_connectors())
        assert len(engine.connectors) >= 1

    def test_search_all_catalogs_no_cache(self):
        config = _base_engine_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        _asyncio.run(engine.initialize_connectors(["google_cloud_data_catalog"]))
        results = _asyncio.run(engine.search_all_catalogs(SearchFilters()))
        assert "google_cloud_data_catalog" in results
        assert len(results["google_cloud_data_catalog"]) >= 1

    def test_search_all_catalogs_with_cache(self):
        config = {
            "catalogs": ["google_cloud_data_catalog"],
            "google_cloud_data_catalog": {"project_id": "test"},
            "defaults": {"timeout_seconds": 30},
            "cache": {"enabled": True, "ttl_minutes": 15, "max_entries": 100},
        }
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        _asyncio.run(engine.initialize_connectors(["google_cloud_data_catalog"]))
        results1 = _asyncio.run(engine.search_all_catalogs(SearchFilters()))
        results2 = _asyncio.run(engine.search_all_catalogs(SearchFilters()))
        assert len(results1) == len(results2)

    def test_aggregate_results_deduplication(self):
        config = _base_engine_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        catalog_results = {
            "gcp": [_make_test_product(id="p1"), _make_test_product(id="p2")],
            "aws": [_make_test_product(id="p1"), _make_test_product(id="p3")],
        }
        all_products = engine.aggregate_results(catalog_results)
        ids = [p.id for p in all_products]
        assert len(ids) == len(set(ids))
        assert len(all_products) == 3

    def test_aggregate_results_sorted_by_quality(self):
        config = _base_engine_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        catalog_results = {
            "gcp": [
                _make_test_product(id="pa", quality_score=0.5),
                _make_test_product(id="pb", quality_score=0.9),
            ]
        }
        all_products = engine.aggregate_results(catalog_results)
        assert all_products[0].quality_score == 0.9

    def test_get_performance_stats(self):
        config = _base_engine_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        stats = engine.get_performance_stats()
        assert "total_requests" in stats
        assert "connected_catalogs" in stats

    def test_get_performance_stats_with_cache(self):
        config = {
            "catalogs": ["google_cloud_data_catalog"],
            "google_cloud_data_catalog": {"project_id": "test"},
            "defaults": {"timeout_seconds": 30},
            "cache": {"enabled": True, "ttl_minutes": 15, "max_entries": 100},
        }
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        stats = engine.get_performance_stats()
        assert "cache" in stats

    def test_search_with_timeout_normal(self):
        config = _base_engine_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        connector = _MagicMock()
        connector.catalog_type = "test"
        connector.search_data_products = _AsyncMock(return_value=[_make_test_product()])
        result = _asyncio.run(engine._search_with_timeout(connector, SearchFilters()))
        assert len(result) == 1

    def test_search_with_timeout_on_timeout(self):
        config = _base_engine_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        connector = _MagicMock()
        connector.catalog_type = "test"

        async def slow_search(filters):
            await _asyncio.sleep(999)

        connector.search_data_products = slow_search
        result = _asyncio.run(engine._search_with_timeout(connector, SearchFilters()))
        assert result == []

    def test_search_error_returns_empty_list(self):
        config = _base_engine_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        connector = _MagicMock()
        connector.catalog_type = "test"
        connector.search_data_products = _AsyncMock(side_effect=RuntimeError("fail"))
        result = _asyncio.run(engine._search_with_timeout(connector, SearchFilters()))
        assert result == []


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------


class TestFormatTableOutput:
    def test_no_products_no_console(self):
        from fluid_build.cli.market import format_table_output

        with _patch("fluid_build.cli.market.cprint") as mock_cprint:
            format_table_output([], console=None)
            mock_cprint.assert_called_once()

    def test_with_products_no_console(self):
        from fluid_build.cli.market import format_table_output

        products = [_make_test_product()]
        with _patch("fluid_build.cli.market.cprint") as mock_cprint:
            format_table_output(products, console=None)
            assert mock_cprint.call_count >= 2


class TestFormatDetailedOutput:
    def test_no_console(self):
        from fluid_build.cli.market import format_detailed_output

        product = _make_test_product(quality_score=0.9)
        with _patch("fluid_build.cli.market.cprint") as mock_cprint:
            format_detailed_output(product, console=None)
            assert mock_cprint.call_count >= 1

    def test_no_quality_score(self):
        from fluid_build.cli.market import format_detailed_output

        product = _make_test_product(quality_score=None)
        with _patch("fluid_build.cli.market.cprint"):
            format_detailed_output(product, console=None)


class TestFormatJsonOutput:
    def _make_full_product(self, **overrides):
        defaults = {
            "id": "p1",
            "name": "Test Product",
            "description": "desc",
            "domain": "finance",
            "owner": "team-a",
            "layer": DataProductLayer.GOLD,
            "status": DataProductStatus.ACTIVE,
            "version": "1.0.0",
            "created_at": datetime(2024, 1, 1),
            "updated_at": datetime(2024, 6, 1),
            "tags": ["tag1"],
            "quality_score": 0.95,
            "catalog_source": "local",
            "catalog_type": "custom",
        }
        defaults.update(overrides)
        return DataProductMetadata(**defaults)

    def test_single_product(self):
        result = format_json_output([self._make_full_product()])
        data = _json.loads(result)
        assert len(data) == 1
        assert data[0]["id"] == "p1"
        assert data[0]["layer"] == "gold"
        assert data[0]["quality_score"] == 0.95

    def test_empty_list(self):
        result = format_json_output([])
        assert _json.loads(result) == []

    def test_multiple_products(self):
        products = [
            self._make_full_product(id="p1", name="A"),
            self._make_full_product(id="p2", name="B"),
        ]
        data = _json.loads(format_json_output(products))
        assert len(data) == 2

    def test_dates_serialized(self):
        result = format_json_output([self._make_full_product()])
        data = _json.loads(result)
        assert "2024-01-01" in data[0]["created_at"]


# ---------------------------------------------------------------------------
# handle_list_catalogs
# ---------------------------------------------------------------------------


class TestHandleListCatalogs:
    def test_all_unconfigured(self):
        config = {"catalogs": []}
        logger = logging.getLogger("test")
        with _patch("fluid_build.cli.market.cprint") as mock_cprint:
            result = handle_list_catalogs(config, logger)
        assert result == 0
        assert mock_cprint.call_count >= 1

    def test_with_configured_catalog(self):
        config = {"catalogs": ["google_cloud_data_catalog"]}
        logger = logging.getLogger("test")
        calls = []
        with _patch("fluid_build.cli.market.cprint", side_effect=lambda msg: calls.append(msg)):
            result = handle_list_catalogs(config, logger)
        assert result == 0
        assert "Configured" in " ".join(calls)


# ---------------------------------------------------------------------------
# handle_config_template
# ---------------------------------------------------------------------------


class TestHandleConfigTemplate:
    def test_returns_zero_and_prints(self):
        logger = logging.getLogger("test")
        with _patch("fluid_build.cli.market.cprint") as mock_cprint:
            result = handle_config_template(logger)
        assert result == 0
        mock_cprint.assert_called_once()
        assert "market.yaml" in mock_cprint.call_args[0][0]


# ---------------------------------------------------------------------------
# handle_marketplace_stats
# ---------------------------------------------------------------------------


class TestHandleMarketplaceStats:
    def test_no_connectors_returns_1(self):
        config = {
            "catalogs": [],
            "defaults": {"timeout_seconds": 30},
            "cache": {"enabled": False},
        }
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        engine.console = None

        async def run_test():
            with _patch.object(engine, "initialize_connectors", new_callable=_AsyncMock):
                return await handle_marketplace_stats(engine, logger)

        result = _asyncio.run(run_test())
        assert result == 1

    def test_with_connectors_returns_zero(self):
        config = _base_engine_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        engine.console = None

        mock_connector = _MagicMock()
        mock_connector.get_catalog_stats = _AsyncMock(
            return_value={"total_products": 10, "avg_quality": 0.9}
        )
        engine.connectors = {"gcp": mock_connector}

        async def run_test():
            with _patch("fluid_build.cli.market.cprint"), _patch("fluid_build.cli.market.success"):
                return await handle_marketplace_stats(engine, logger)

        result = _asyncio.run(run_test())
        assert result == 0


# ---------------------------------------------------------------------------
# handle_product_details
# ---------------------------------------------------------------------------


class TestHandleProductDetails:
    def test_product_found(self):
        config = _base_engine_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        engine.console = None

        mock_connector = _MagicMock()
        mock_connector.get_data_product = _AsyncMock(return_value=_make_test_product())
        engine.connectors = {"gcp": mock_connector}

        args = _make_test_args()

        async def run_test():
            with _patch("fluid_build.cli.market.format_detailed_output"):
                return await handle_product_details(engine, "tp1", args, logger)

        result = _asyncio.run(run_test())
        assert result == 0

    def test_product_not_found(self):
        config = _base_engine_config()
        logger = logging.getLogger("test")
        engine = MarketDiscoveryEngine(config, logger)
        engine.console = None

        mock_connector = _MagicMock()
        mock_connector.get_data_product = _AsyncMock(return_value=None)
        engine.connectors = {"gcp": mock_connector}

        args = _make_test_args()
        result = _asyncio.run(handle_product_details(engine, "nonexistent", args, logger))
        assert result == 1


# ---------------------------------------------------------------------------
# generate_output
# ---------------------------------------------------------------------------


class TestGenerateOutput:
    def test_no_products(self):
        args = _make_test_args(format="table")
        logger = logging.getLogger("test")
        with _patch("fluid_build.cli.market.cprint"):
            result = generate_output([], args, None, logger)
        assert result == 0

    def test_table_format(self):
        products = [_make_test_product()]
        args = _make_test_args(format="table")
        logger = logging.getLogger("test")
        with _patch("fluid_build.cli.market.format_table_output") as mock_fmt:
            result = generate_output(products, args, None, logger)
        assert result == 0
        mock_fmt.assert_called_once()

    def test_json_format_stdout(self):
        products = [_make_test_product()]
        args = _make_test_args(format="json")
        logger = logging.getLogger("test")
        with _patch("fluid_build.cli.market.cprint") as mock_cprint:
            result = generate_output(products, args, None, logger)
        assert result == 0
        mock_cprint.assert_called_once()

    def test_json_format_to_file(self, tmp_path):
        products = [_make_test_product()]
        output_file = str(tmp_path / "output.json")
        args = _make_test_args(format="json", output=output_file)
        logger = logging.getLogger("test")
        result = generate_output(products, args, None, logger)
        assert result == 0
        with open(output_file) as f:
            data = _json.load(f)
        assert len(data) == 1

    def test_json_format_file_write_error(self):
        products = [_make_test_product()]
        args = _make_test_args(format="json", output="/invalid/path/output.json")
        logger = logging.getLogger("test")
        result = generate_output(products, args, None, logger)
        assert result == 1

    def test_detailed_format(self):
        products = [_make_test_product()]
        args = _make_test_args(format="detailed")
        logger = logging.getLogger("test")
        with _patch("fluid_build.cli.market.format_detailed_output") as mock_fmt:
            result = generate_output(products, args, None, logger)
        assert result == 0
        mock_fmt.assert_called_once()


# ---------------------------------------------------------------------------
# load_market_config
# ---------------------------------------------------------------------------


class TestLoadMarketConfig:
    def test_default_catalogs_set(self, monkeypatch):
        for var in [
            "GCP_PROJECT_ID",
            "AWS_REGION",
            "AZURE_PURVIEW_ACCOUNT",
            "DATAHUB_SERVER_URL",
            "ATLAS_BASE_URL",
            "CONFLUENT_SCHEMA_REGISTRY_URL",
            "COLLIBRA_BASE_URL",
            "ALATION_BASE_URL",
            "CUSTOM_CATALOG_URL",
            "FLUID_MARKET_DEFAULT_LIMIT",
            "FLUID_MARKET_MIN_QUALITY",
            "FLUID_MARKET_TIMEOUT",
            "FLUID_MARKET_CACHE_TTL",
        ]:
            monkeypatch.delenv(var, raising=False)
        args = _make_test_args(catalogs=None)
        logger = logging.getLogger("test")
        config = load_market_config(args, logger)
        assert "catalogs" in config
        assert len(config["catalogs"]) >= 1

    def test_catalogs_from_args(self, monkeypatch):
        for var in [
            "GCP_PROJECT_ID",
            "AWS_REGION",
            "AZURE_PURVIEW_ACCOUNT",
            "DATAHUB_SERVER_URL",
            "ATLAS_BASE_URL",
            "CONFLUENT_SCHEMA_REGISTRY_URL",
            "COLLIBRA_BASE_URL",
            "ALATION_BASE_URL",
            "CUSTOM_CATALOG_URL",
            "FLUID_MARKET_DEFAULT_LIMIT",
            "FLUID_MARKET_MIN_QUALITY",
            "FLUID_MARKET_TIMEOUT",
            "FLUID_MARKET_CACHE_TTL",
        ]:
            monkeypatch.delenv(var, raising=False)
        args = _make_test_args(catalogs="google_cloud_data_catalog,datahub")
        logger = logging.getLogger("test")
        config = load_market_config(args, logger)
        assert "google_cloud_data_catalog" in config["catalogs"]
        assert "datahub" in config["catalogs"]


# ---------------------------------------------------------------------------
# handle_saved_searches
# ---------------------------------------------------------------------------


class TestHandleSavedSearches:
    def test_list_empty(self):
        import fluid_build.cli.market as market_module

        original = market_module.advanced_search_engine.saved_searches.copy()
        market_module.advanced_search_engine.saved_searches = {}
        logger = logging.getLogger("test")
        args = _MagicMock()
        args.list_saved = True
        args.delete_saved = None
        args.show_saved = None
        with _patch("fluid_build.cli.market.cprint"):
            result = _asyncio.run(handle_saved_searches(args, logger))
        market_module.advanced_search_engine.saved_searches = original
        assert result == 0

    def test_list_with_entries(self):
        import fluid_build.cli.market as market_module

        market_module.advanced_search_engine.saved_searches = {
            "my-search": SearchFilters(domain="finance")
        }
        logger = logging.getLogger("test")
        args = _MagicMock()
        args.list_saved = True
        args.delete_saved = None
        args.show_saved = None
        with _patch("fluid_build.cli.market.cprint"):
            result = _asyncio.run(handle_saved_searches(args, logger))
        market_module.advanced_search_engine.saved_searches = {}
        assert result == 0

    def test_delete_existing(self):
        import fluid_build.cli.market as market_module

        market_module.advanced_search_engine.saved_searches = {
            "del-search": SearchFilters(domain="finance")
        }
        logger = logging.getLogger("test")
        args = _MagicMock()
        args.list_saved = False
        args.delete_saved = "del-search"
        args.show_saved = None
        result = _asyncio.run(handle_saved_searches(args, logger))
        assert result == 0
        assert "del-search" not in market_module.advanced_search_engine.saved_searches

    def test_delete_nonexistent(self):
        import fluid_build.cli.market as market_module

        market_module.advanced_search_engine.saved_searches = {}
        logger = logging.getLogger("test")
        args = _MagicMock()
        args.list_saved = False
        args.delete_saved = "nonexistent"
        args.show_saved = None
        result = _asyncio.run(handle_saved_searches(args, logger))
        assert result == 1

    def test_show_existing(self):
        import fluid_build.cli.market as market_module

        market_module.advanced_search_engine.saved_searches = {
            "show-search": SearchFilters(domain="finance", text_query="test")
        }
        logger = logging.getLogger("test")
        args = _MagicMock()
        args.list_saved = False
        args.delete_saved = None
        args.show_saved = "show-search"
        with _patch("fluid_build.cli.market.cprint"):
            result = _asyncio.run(handle_saved_searches(args, logger))
        market_module.advanced_search_engine.saved_searches = {}
        assert result == 0

    def test_show_nonexistent(self):
        import fluid_build.cli.market as market_module

        market_module.advanced_search_engine.saved_searches = {}
        logger = logging.getLogger("test")
        args = _MagicMock()
        args.list_saved = False
        args.delete_saved = None
        args.show_saved = "nonexistent"
        result = _asyncio.run(handle_saved_searches(args, logger))
        assert result == 1

    def test_no_operation_returns_1(self):
        logger = logging.getLogger("test")
        args = _MagicMock()
        args.list_saved = False
        args.delete_saved = None
        args.show_saved = None
        result = _asyncio.run(handle_saved_searches(args, logger))
        assert result == 1


# ---------------------------------------------------------------------------
# handle_search_history
# ---------------------------------------------------------------------------


class TestHandleSearchHistory:
    def test_empty_history(self):
        import fluid_build.cli.market as market_module

        market_module.advanced_search_engine.search_history = []
        logger = logging.getLogger("test")
        with _patch("fluid_build.cli.market.cprint"):
            result = _asyncio.run(handle_search_history(logger))
        assert result == 0

    def test_with_history(self):
        import fluid_build.cli.market as market_module

        market_module.advanced_search_engine.search_history = [
            {
                "timestamp": "2024-01-01T12:00:00.000000",
                "query": "customer",
                "total_results": 5,
                "query_time": 0.1,
            }
        ]
        logger = logging.getLogger("test")
        with _patch("fluid_build.cli.market.cprint"):
            result = _asyncio.run(handle_search_history(logger))
        market_module.advanced_search_engine.search_history = []
        assert result == 0


# ---------------------------------------------------------------------------
# build_search_filters
# ---------------------------------------------------------------------------


class TestBuildSearchFilters:
    def test_empty_args(self):
        args = _make_test_args()
        f = build_search_filters(args)
        assert f.text_query is None
        assert f.limit == 20

    def test_search_query(self):
        args = _make_test_args(search="bitcoin")
        f = build_search_filters(args)
        assert f.text_query == "bitcoin"

    def test_domain_list(self):
        args = _make_test_args(domain=["finance", "trading"])
        f = build_search_filters(args)
        assert f.domain == "finance"

    def test_domain_string(self):
        args = _make_test_args(domain="finance")
        f = build_search_filters(args)
        assert f.domain == "finance"

    def test_layer_filter(self):
        args = _make_test_args(layer=["gold"])
        f = build_search_filters(args)
        assert f.layer == DataProductLayer.GOLD

    def test_status_filter(self):
        args = _make_test_args(status=["active"])
        f = build_search_filters(args)
        assert f.status == DataProductStatus.ACTIVE

    def test_tags_list(self):
        args = _make_test_args(tags=["crypto", "finance"])
        f = build_search_filters(args)
        assert f.tags == ["crypto", "finance"]

    def test_min_quality(self):
        args = _make_test_args(min_quality=0.9)
        f = build_search_filters(args)
        assert f.min_quality_score == 0.9

    def test_date_filters(self):
        args = _make_test_args(created_after="2024-01-01", created_before="2024-12-31")
        f = build_search_filters(args)
        assert f.created_after == datetime(2024, 1, 1)
        assert f.created_before == datetime(2024, 12, 31)

    def test_limit_offset(self):
        args = _make_test_args(limit=50, offset=10)
        f = build_search_filters(args)
        assert f.limit == 50
        assert f.offset == 10


# ---------------------------------------------------------------------------
# _merge_config
# ---------------------------------------------------------------------------


class TestMergeConfigExt:
    def test_flat_merge(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        _merge_config(base, override)
        assert base == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        base = {"x": {"a": 1, "b": 2}}
        override = {"x": {"b": 3, "c": 4}}
        _merge_config(base, override)
        assert base["x"] == {"a": 1, "b": 3, "c": 4}

    def test_empty_override(self):
        base = {"a": 1}
        _merge_config(base, {})
        assert base == {"a": 1}

    def test_override_non_dict_with_scalar(self):
        base = {"x": {"a": 1}}
        override = {"x": "string"}
        _merge_config(base, override)
        assert base["x"] == "string"


# ---------------------------------------------------------------------------
# _load_env_config (extended)
# ---------------------------------------------------------------------------


class TestLoadEnvConfigExt:
    def test_alation_config(self, monkeypatch):
        for var in [
            "GCP_PROJECT_ID",
            "AWS_REGION",
            "AZURE_PURVIEW_ACCOUNT",
            "DATAHUB_SERVER_URL",
            "ATLAS_BASE_URL",
            "CONFLUENT_SCHEMA_REGISTRY_URL",
            "COLLIBRA_BASE_URL",
            "CUSTOM_CATALOG_URL",
            "FLUID_MARKET_DEFAULT_LIMIT",
            "FLUID_MARKET_MIN_QUALITY",
            "FLUID_MARKET_TIMEOUT",
            "FLUID_MARKET_CACHE_TTL",
        ]:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("ALATION_BASE_URL", "http://alation.example.com")
        monkeypatch.setenv("ALATION_API_TOKEN", "my-token")
        config = _load_env_config()
        assert config["alation"]["base_url"] == "http://alation.example.com"
        assert config["alation"]["api_token"] == "my-token"

    def test_collibra_config(self, monkeypatch):
        for var in [
            "GCP_PROJECT_ID",
            "AWS_REGION",
            "AZURE_PURVIEW_ACCOUNT",
            "DATAHUB_SERVER_URL",
            "ATLAS_BASE_URL",
            "CONFLUENT_SCHEMA_REGISTRY_URL",
            "ALATION_BASE_URL",
            "CUSTOM_CATALOG_URL",
            "FLUID_MARKET_DEFAULT_LIMIT",
            "FLUID_MARKET_MIN_QUALITY",
            "FLUID_MARKET_TIMEOUT",
            "FLUID_MARKET_CACHE_TTL",
        ]:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("COLLIBRA_BASE_URL", "http://collibra.example.com")
        monkeypatch.setenv("COLLIBRA_USERNAME", "admin")
        monkeypatch.setenv("COLLIBRA_PASSWORD", "pass")
        config = _load_env_config()
        assert config["collibra"]["base_url"] == "http://collibra.example.com"

    def test_global_defaults(self, monkeypatch):
        for var in [
            "GCP_PROJECT_ID",
            "AWS_REGION",
            "AZURE_PURVIEW_ACCOUNT",
            "DATAHUB_SERVER_URL",
            "ATLAS_BASE_URL",
            "CONFLUENT_SCHEMA_REGISTRY_URL",
            "COLLIBRA_BASE_URL",
            "ALATION_BASE_URL",
            "CUSTOM_CATALOG_URL",
            "FLUID_MARKET_CACHE_TTL",
        ]:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("FLUID_MARKET_DEFAULT_LIMIT", "50")
        monkeypatch.setenv("FLUID_MARKET_MIN_QUALITY", "0.8")
        monkeypatch.setenv("FLUID_MARKET_TIMEOUT", "30")
        config = _load_env_config()
        assert config["defaults"]["limit"] == 50
        assert config["defaults"]["min_quality_score"] == 0.8
        assert config["defaults"]["timeout_seconds"] == 30

    def test_cache_ttl_from_env(self, monkeypatch):
        for var in [
            "GCP_PROJECT_ID",
            "AWS_REGION",
            "AZURE_PURVIEW_ACCOUNT",
            "DATAHUB_SERVER_URL",
            "ATLAS_BASE_URL",
            "CONFLUENT_SCHEMA_REGISTRY_URL",
            "COLLIBRA_BASE_URL",
            "ALATION_BASE_URL",
            "CUSTOM_CATALOG_URL",
            "FLUID_MARKET_DEFAULT_LIMIT",
            "FLUID_MARKET_MIN_QUALITY",
            "FLUID_MARKET_TIMEOUT",
        ]:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("FLUID_MARKET_CACHE_TTL", "30")
        config = _load_env_config()
        assert config["cache"]["ttl_minutes"] == 30


# ---------------------------------------------------------------------------
# run_market_discovery
# ---------------------------------------------------------------------------


class TestRunMarketDiscovery:
    def test_list_catalogs_short_circuits(self):
        args = _make_test_args(list_catalogs=True)
        logger = logging.getLogger("test")
        with _patch("fluid_build.cli.market.handle_list_catalogs", return_value=0) as mock_hlc:
            result = _asyncio.run(run_market_discovery(args, logger))
        assert result == 0
        mock_hlc.assert_called_once()

    def test_config_template_short_circuits(self):
        args = _make_test_args(config_template=True)
        logger = logging.getLogger("test")
        with _patch("fluid_build.cli.market.handle_config_template", return_value=0) as mock_hct:
            result = _asyncio.run(run_market_discovery(args, logger))
        assert result == 0
        mock_hct.assert_called_once()

    def test_no_connectors_returns_1(self):
        args = _make_test_args(catalogs="google_cloud_data_catalog")
        logger = logging.getLogger("test")

        with _patch("fluid_build.cli.market.MarketDiscoveryEngine") as MockEngine:
            instance = MockEngine.return_value
            instance.initialize_connectors = _AsyncMock()
            instance.connectors = {}
            instance.console = None
            result = _asyncio.run(run_market_discovery(args, logger))
        assert result == 1

    def test_successful_search(self):
        args = _make_test_args(catalogs="google_cloud_data_catalog")
        logger = logging.getLogger("test")

        with _patch("fluid_build.cli.market.MarketDiscoveryEngine") as MockEngine:
            instance = MockEngine.return_value
            instance.initialize_connectors = _AsyncMock()
            instance.connectors = {"gcp": _MagicMock()}
            instance.console = None
            instance.search_all_catalogs = _AsyncMock(return_value={"gcp": [_make_test_product()]})
            instance.aggregate_results = _MagicMock(return_value=[_make_test_product()])
            with _patch("fluid_build.cli.market.generate_output", return_value=0):
                result = _asyncio.run(run_market_discovery(args, logger))
        assert result == 0

    def test_exception_returns_1(self):
        args = _make_test_args()
        logger = logging.getLogger("test")
        with _patch("fluid_build.cli.market.load_market_config", side_effect=RuntimeError("boom")):
            result = _asyncio.run(run_market_discovery(args, logger))
        assert result == 1

    def test_marketplace_stats_flag(self):
        args = _make_test_args(marketplace_stats=True)
        logger = logging.getLogger("test")

        with _patch(
            "fluid_build.cli.market.handle_marketplace_stats", new_callable=_AsyncMock
        ) as mock_hms:
            mock_hms.return_value = 0
            result = _asyncio.run(run_market_discovery(args, logger))
        assert result == 0

    def test_product_id_lookup(self):
        args = _make_test_args(catalogs="google_cloud_data_catalog", product_id="tp1")
        logger = logging.getLogger("test")

        with _patch("fluid_build.cli.market.MarketDiscoveryEngine") as MockEngine:
            instance = MockEngine.return_value
            instance.initialize_connectors = _AsyncMock()
            instance.connectors = {"gcp": _MagicMock()}
            instance.console = None
            with _patch(
                "fluid_build.cli.market.handle_product_details", new_callable=_AsyncMock
            ) as mock_hpd:
                mock_hpd.return_value = 0
                result = _asyncio.run(run_market_discovery(args, logger))
        assert result == 0


# ---------------------------------------------------------------------------
# run (sync entry point)
# ---------------------------------------------------------------------------


class TestRun:
    def test_run_no_event_loop(self):
        args = _make_test_args(catalogs="google_cloud_data_catalog")
        logger = logging.getLogger("test")
        with _patch("fluid_build.cli.market.asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.is_running.return_value = False
            with _patch("fluid_build.cli.market.asyncio.run", return_value=0) as mock_run:
                result = run(args, logger)
        assert result == 0
        mock_run.assert_called_once()

    def test_run_keyboard_interrupt(self):
        args = _make_test_args()
        logger = logging.getLogger("test")
        with _patch("fluid_build.cli.market.asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.is_running.return_value = False
            with _patch("fluid_build.cli.market.asyncio.run", side_effect=KeyboardInterrupt):
                result = run(args, logger)
        assert result == 130

    def test_run_with_running_event_loop(self):
        args = _make_test_args(catalogs="google_cloud_data_catalog")
        logger = logging.getLogger("test")
        with _patch("fluid_build.cli.market.asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.is_running.return_value = True
            with _patch(
                "fluid_build.cli.market.run_market_discovery", new_callable=_AsyncMock
            ) as mock_rmd:
                mock_rmd.return_value = 0
                result = run(args, logger)
        assert result == 0
