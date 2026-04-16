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

"""Extended tests for market.py: connectors, search engine, circuit breaker, cache, metrics."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fluid_build.cli.market import (
    AdvancedSearchEngine,
    AWSGlueDataCatalogConnector,
    AzurePurviewConnector,
    BaseCatalogConnector,
    CatalogType,
    CircuitBreaker,
    DataProductLayer,
    DataProductMetadata,
    DataProductStatus,
    GoogleCloudDataCatalogConnector,
    MarketCache,
    MetricsCollector,
    SearchFilters,
    SearchResult,
    retry_with_backoff,
)

LOG = logging.getLogger("test_market_ext2")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_product(
    pid: str = "p1",
    name: str = "Product One",
    domain: str = "finance",
    owner: str = "team-a",
    layer: DataProductLayer = DataProductLayer.GOLD,
    status: DataProductStatus = DataProductStatus.ACTIVE,
    tags: Optional[list] = None,
    quality_score: Optional[float] = 0.9,
    documentation_url: Optional[str] = None,
    api_endpoint: Optional[str] = None,
    sample_data_url: Optional[str] = None,
    usage_stats: Optional[dict] = None,
    updated_at: Optional[datetime] = None,
) -> DataProductMetadata:
    return DataProductMetadata(
        id=pid,
        name=name,
        description=f"Description for {name}",
        domain=domain,
        owner=owner,
        layer=layer,
        status=status,
        version="1.0.0",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        updated_at=updated_at or datetime(2024, 10, 1, tzinfo=timezone.utc),
        tags=tags or [],
        quality_score=quality_score,
        documentation_url=documentation_url,
        api_endpoint=api_endpoint,
        sample_data_url=sample_data_url,
        usage_stats=usage_stats or {},
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_starts_closed(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10)
        assert cb.state == "CLOSED"
        assert cb.failure_count == 0

    def test_stays_closed_on_success(self):
        cb = CircuitBreaker(failure_threshold=2)

        async def ok():
            return 42

        assert _run(cb.call(ok)) == 42
        assert cb.state == "CLOSED"

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=9999)

        async def fail():
            raise ValueError("boom")

        for _ in range(2):
            with pytest.raises(ValueError):
                _run(cb.call(fail))

        assert cb.state == "OPEN"

    def test_open_raises_without_calling(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=9999)

        async def fail():
            raise ValueError("x")

        with pytest.raises(ValueError):
            _run(cb.call(fail))

        assert cb.state == "OPEN"

        async def ok():
            return 1

        with pytest.raises(Exception, match="Circuit breaker is OPEN"):
            _run(cb.call(ok))

    def test_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0)

        async def fail():
            raise ValueError("x")

        with pytest.raises(ValueError):
            _run(cb.call(fail))

        assert cb.state == "OPEN"
        cb.last_failure_time = time.time() - 1  # past recovery

        async def ok():
            return "recovered"

        result = _run(cb.call(ok))
        assert result == "recovered"
        assert cb.state == "CLOSED"

    def test_on_success_resets(self):
        cb = CircuitBreaker(failure_threshold=5)
        cb.failure_count = 3
        cb._on_success()
        assert cb.failure_count == 0
        assert cb.state == "CLOSED"


# ---------------------------------------------------------------------------
# retry_with_backoff
# ---------------------------------------------------------------------------


class TestRetryWithBackoff:
    def test_returns_on_first_success(self):
        call_count = 0

        async def ok():
            nonlocal call_count
            call_count += 1
            return "done"

        result = _run(retry_with_backoff(ok, max_retries=3, base_delay=0.001))
        assert result == "done"
        assert call_count == 1

    def test_retries_and_succeeds(self):
        call_count = 0

        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("not yet")
            return "ok"

        result = _run(retry_with_backoff(flaky, max_retries=3, base_delay=0.001))
        assert result == "ok"
        assert call_count == 3

    def test_raises_after_all_retries(self):
        async def always_fail():
            raise RuntimeError("permanent")

        with pytest.raises(RuntimeError, match="permanent"):
            _run(retry_with_backoff(always_fail, max_retries=2, base_delay=0.001))


# ---------------------------------------------------------------------------
# AdvancedSearchEngine
# ---------------------------------------------------------------------------


class TestAdvancedSearchEngine:
    def setup_method(self):
        self.engine = AdvancedSearchEngine(LOG)

    def test_relevance_no_query(self):
        product = _make_product()
        filters = SearchFilters()
        score = self.engine.calculate_relevance_score(product, filters)
        assert score == 1.0

    def test_relevance_with_name_match(self):
        product = _make_product(name="Transaction Analytics")
        filters = SearchFilters(text_query="transaction")
        score = self.engine.calculate_relevance_score(product, filters)
        assert score > 0.0

    def test_relevance_with_tag_match(self):
        product = _make_product(tags=["analytics", "ml"])
        filters = SearchFilters(text_query="analytics")
        score = self.engine.calculate_relevance_score(product, filters)
        assert score > 0.0

    def test_relevance_exact_match(self):
        product = _make_product(name="Exact Term Here")
        filters = SearchFilters(text_query="exact", exact_match=True)
        score = self.engine.calculate_relevance_score(product, filters)
        assert score > 0.0

    def test_relevance_quality_boost(self):
        low_q = _make_product(quality_score=0.1)
        high_q = _make_product(quality_score=0.99)
        filters = SearchFilters(text_query="product")
        score_low = self.engine.calculate_relevance_score(low_q, filters)
        score_high = self.engine.calculate_relevance_score(high_q, filters)
        # Both should be >= 0 (both have "product" in name)
        assert score_high >= score_low

    def test_relevance_recency_boost(self):
        recent = _make_product(
            updated_at=datetime.now(timezone.utc),
        )
        filters = SearchFilters(text_query="product")
        score = self.engine.calculate_relevance_score(recent, filters)
        assert score >= 0.0

    def test_extract_facets(self):
        products = [
            _make_product(domain="finance", layer=DataProductLayer.GOLD, tags=["tag1"]),
            _make_product(pid="p2", domain="finance", layer=DataProductLayer.SILVER, tags=["tag2"]),
            _make_product(pid="p3", domain="marketing", layer=DataProductLayer.GOLD, tags=["tag1"]),
        ]
        facets = self.engine.extract_facets(products)
        assert facets["domain"]["finance"] == 2
        assert facets["domain"]["marketing"] == 1
        assert facets["layer"]["gold"] == 2
        assert facets["tags"]["tag1"] == 2

    def test_apply_advanced_filters_documentation(self):
        with_docs = _make_product(documentation_url="https://docs.example.com")
        without_docs = _make_product(pid="p2")
        filters = SearchFilters(has_documentation=True)
        result = self.engine.apply_advanced_filters([with_docs, without_docs], filters)
        assert len(result) == 1
        assert result[0].id == with_docs.id

    def test_apply_advanced_filters_api_endpoint(self):
        with_api = _make_product(api_endpoint="https://api.example.com")
        without_api = _make_product(pid="p2")
        filters = SearchFilters(has_api_endpoint=True)
        result = self.engine.apply_advanced_filters([with_api, without_api], filters)
        assert len(result) == 1

    def test_apply_advanced_filters_sample_data(self):
        with_sample = _make_product(sample_data_url="https://sample.com/data")
        without_sample = _make_product(pid="p2")
        filters = SearchFilters(has_sample_data=True)
        result = self.engine.apply_advanced_filters([with_sample, without_sample], filters)
        assert len(result) == 1

    def test_apply_advanced_filters_usage_count(self):
        high_usage = _make_product(usage_stats={"total_queries": 100})
        low_usage = _make_product(pid="p2", usage_stats={"total_queries": 5})
        filters = SearchFilters(min_usage_count=50)
        result = self.engine.apply_advanced_filters([high_usage, low_usage], filters)
        assert len(result) == 1

    def test_apply_advanced_filters_max_usage(self):
        high_usage = _make_product(usage_stats={"total_queries": 100})
        low_usage = _make_product(pid="p2", usage_stats={"total_queries": 5})
        filters = SearchFilters(max_usage_count=10)
        result = self.engine.apply_advanced_filters([high_usage, low_usage], filters)
        assert len(result) == 1

    def test_apply_advanced_filters_exclude_deprecated(self):
        active = _make_product()
        deprecated = _make_product(pid="p2", status=DataProductStatus.DEPRECATED)
        filters = SearchFilters(include_deprecated=False)
        result = self.engine.apply_advanced_filters([active, deprecated], filters)
        assert len(result) == 1

    def test_apply_advanced_filters_facets(self):
        finance = _make_product(domain="finance")
        marketing = _make_product(pid="p2", domain="marketing")
        filters = SearchFilters(facets={"domain": ["finance"]})
        result = self.engine.apply_advanced_filters([finance, marketing], filters)
        assert len(result) == 1
        assert result[0].domain == "finance"

    def test_apply_advanced_filters_facets_layer(self):
        gold = _make_product(layer=DataProductLayer.GOLD)
        silver = _make_product(pid="p2", layer=DataProductLayer.SILVER)
        filters = SearchFilters(facets={"layer": ["gold"]})
        result = self.engine.apply_advanced_filters([gold, silver], filters)
        assert len(result) == 1

    def test_apply_advanced_filters_facets_status(self):
        active = _make_product(status=DataProductStatus.ACTIVE)
        dev = _make_product(pid="p2", status=DataProductStatus.DEVELOPMENT)
        filters = SearchFilters(facets={"status": ["active"]})
        result = self.engine.apply_advanced_filters([active, dev], filters)
        assert len(result) == 1

    def test_apply_advanced_filters_facets_owner(self):
        a = _make_product(owner="team-a")
        b = _make_product(pid="p2", owner="team-b")
        filters = SearchFilters(facets={"owner": ["team-a"]})
        result = self.engine.apply_advanced_filters([a, b], filters)
        assert len(result) == 1

    def test_apply_advanced_filters_facets_tags(self):
        tagged = _make_product(tags=["ml", "analytics"])
        untagged = _make_product(pid="p2", tags=["other"])
        filters = SearchFilters(facets={"tags": ["ml"]})
        result = self.engine.apply_advanced_filters([tagged, untagged], filters)
        assert len(result) == 1

    def test_rank_and_sort_by_name(self):
        products = [
            _make_product(name="Zebra"),
            _make_product(pid="p2", name="Alpha"),
        ]
        filters = SearchFilters(sort_by="name", sort_order="asc")
        result = self.engine.rank_and_sort_products(products, filters)
        assert result[0].name == "Alpha"

    def test_rank_and_sort_by_quality(self):
        products = [
            _make_product(quality_score=0.5),
            _make_product(pid="p2", quality_score=0.9),
        ]
        filters = SearchFilters(sort_by="quality_score", sort_order="desc")
        result = self.engine.rank_and_sort_products(products, filters)
        assert result[0].quality_score == 0.9

    def test_rank_and_sort_by_created_at(self):
        products = [
            _make_product(),
            _make_product(pid="p2"),
        ]
        filters = SearchFilters(sort_by="created_at", sort_order="asc")
        result = self.engine.rank_and_sort_products(products, filters)
        assert len(result) == 2

    def test_rank_and_sort_by_updated_at(self):
        products = [_make_product(), _make_product(pid="p2")]
        filters = SearchFilters(sort_by="updated_at", sort_order="desc")
        result = self.engine.rank_and_sort_products(products, filters)
        assert len(result) == 2

    def test_rank_and_sort_by_relevance(self):
        products = [
            _make_product(name="Analytics Dashboard"),
            _make_product(pid="p2", name="Raw Data"),
        ]
        filters = SearchFilters(text_query="analytics", sort_by="relevance", sort_order="desc")
        result = self.engine.rank_and_sort_products(products, filters)
        assert result[0].name == "Analytics Dashboard"

    def test_save_and_load_search(self):
        filters = SearchFilters(text_query="test", search_name="my_search", save_search=True)
        assert self.engine.save_search(filters)
        loaded = self.engine.load_saved_search("my_search")
        assert loaded is not None
        assert loaded.text_query == "test"

    def test_save_search_no_name(self):
        filters = SearchFilters(text_query="test")
        assert not self.engine.save_search(filters)

    def test_list_saved_searches(self):
        filters = SearchFilters(search_name="s1", save_search=True)
        self.engine.save_search(filters)
        assert "s1" in self.engine.list_saved_searches()

    def test_load_nonexistent_search(self):
        assert self.engine.load_saved_search("nonexistent") is None

    def test_generate_search_suggestions(self):
        products = [
            _make_product(name="Transaction Analytics", tags=["transactions"]),
        ]
        suggestions = self.engine.generate_search_suggestions(products, "trans")
        assert isinstance(suggestions, list)
        assert any("trans" in s for s in suggestions)


# ---------------------------------------------------------------------------
# BaseCatalogConnector._apply_filters
# ---------------------------------------------------------------------------


class TestBaseCatalogConnectorFilters:
    def setup_method(self):
        self.connector = BaseCatalogConnector({}, LOG)

    def test_filter_by_domain(self):
        products = [_make_product(domain="finance"), _make_product(pid="p2", domain="marketing")]
        filters = SearchFilters(domain="finance")
        result = self.connector._apply_filters(products, filters)
        assert len(result) == 1

    def test_filter_by_owner(self):
        products = [
            _make_product(owner="team-a"),
            _make_product(pid="p2", owner="team-b"),
        ]
        filters = SearchFilters(owner="team-a")
        result = self.connector._apply_filters(products, filters)
        assert len(result) == 1

    def test_filter_by_layer(self):
        products = [
            _make_product(layer=DataProductLayer.GOLD),
            _make_product(pid="p2", layer=DataProductLayer.SILVER),
        ]
        filters = SearchFilters(layer=DataProductLayer.GOLD)
        result = self.connector._apply_filters(products, filters)
        assert len(result) == 1

    def test_filter_by_status(self):
        products = [
            _make_product(status=DataProductStatus.ACTIVE),
            _make_product(pid="p2", status=DataProductStatus.DEPRECATED),
        ]
        filters = SearchFilters(status=DataProductStatus.ACTIVE)
        result = self.connector._apply_filters(products, filters)
        assert len(result) == 1

    def test_filter_by_quality_score(self):
        products = [
            _make_product(quality_score=0.9),
            _make_product(pid="p2", quality_score=0.3),
        ]
        filters = SearchFilters(min_quality_score=0.5)
        result = self.connector._apply_filters(products, filters)
        assert len(result) == 1

    def test_filter_by_text_query(self):
        products = [
            _make_product(name="Transaction Stream"),
            _make_product(pid="p2", name="Inventory Data"),
        ]
        filters = SearchFilters(text_query="transaction")
        result = self.connector._apply_filters(products, filters)
        assert len(result) == 1

    def test_filter_limit(self):
        products = [_make_product(pid=f"p{i}") for i in range(10)]
        filters = SearchFilters(limit=3)
        result = self.connector._apply_filters(products, filters)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# GoogleCloudDataCatalogConnector
# ---------------------------------------------------------------------------


class TestGoogleCloudCatalogConnector:
    def test_connect_success(self):
        connector = GoogleCloudDataCatalogConnector({"project_id": "my-project"}, LOG)
        result = _run(connector._connect_impl())
        assert result is True

    def test_connect_missing_project(self):
        connector = GoogleCloudDataCatalogConnector({}, LOG)
        result = _run(connector._connect_impl())
        assert result is False

    def test_search_returns_products(self):
        connector = GoogleCloudDataCatalogConnector({"project_id": "my-project"}, LOG)
        products = _run(connector._search_data_products_impl(SearchFilters()))
        assert len(products) > 0

    def test_catalog_stats(self):
        connector = GoogleCloudDataCatalogConnector({"project_id": "p"}, LOG)
        stats = _run(connector._get_catalog_stats_impl())
        assert "total_products" in stats


# ---------------------------------------------------------------------------
# AWSGlueDataCatalogConnector
# ---------------------------------------------------------------------------


class TestAWSGlueCatalogConnector:
    def test_connect_success(self):
        connector = AWSGlueDataCatalogConnector({"region": "us-east-1"}, LOG)
        result = _run(connector._connect_impl())
        assert result is True

    def test_search_returns_products(self):
        connector = AWSGlueDataCatalogConnector({}, LOG)
        products = _run(connector._search_data_products_impl(SearchFilters()))
        assert len(products) > 0

    def test_catalog_stats(self):
        connector = AWSGlueDataCatalogConnector({}, LOG)
        stats = _run(connector._get_catalog_stats_impl())
        assert "total_products" in stats


# ---------------------------------------------------------------------------
# AzurePurviewConnector
# ---------------------------------------------------------------------------


class TestAzurePurviewConnector:
    def test_connect_success(self):
        connector = AzurePurviewConnector({"account_name": "my-account"}, LOG)
        result = _run(connector._connect_impl())
        assert result is True

    def test_connect_missing_account(self):
        connector = AzurePurviewConnector({}, LOG)
        result = _run(connector._connect_impl())
        assert result is False


# ---------------------------------------------------------------------------
# MetricsCollector
# ---------------------------------------------------------------------------


class TestMetricsCollector:
    def test_record_search_request(self):
        mc = MetricsCollector()
        mc.record_search_request("google_cloud", 1.5)
        mc.record_search_request("google_cloud", 2.0)
        summary = mc.get_summary()
        assert summary["search_requests"]["google_cloud"] == 2
        assert summary["average_latencies"]["google_cloud"] == 1.75

    def test_empty_metrics(self):
        mc = MetricsCollector()
        summary = mc.get_summary()
        assert isinstance(summary, dict)
        assert summary["search_requests"] == {}

    def test_record_cache_hit_and_miss(self):
        mc = MetricsCollector()
        mc.record_cache_hit("aws_glue")
        mc.record_cache_hit("aws_glue")
        mc.record_cache_miss("aws_glue")
        summary = mc.get_summary()
        assert summary["cache_hit_rates"]["aws_glue"] == pytest.approx(2 / 3)

    def test_record_error(self):
        mc = MetricsCollector()
        mc.record_error("google_cloud", "timeout")
        summary = mc.get_summary()
        assert summary["error_counts"]["google_cloud:timeout"] == 1

    def test_update_connector_health(self):
        mc = MetricsCollector()
        mc.update_connector_health("aws_glue", True, 0.5)
        assert mc.connector_health["aws_glue"]["healthy"] is True
        assert mc.connector_health["aws_glue"]["response_time"] == 0.5


# ---------------------------------------------------------------------------
# MarketCache
# ---------------------------------------------------------------------------


class TestMarketCache:
    def test_set_and_get(self):
        cache = MarketCache(max_entries=100, default_ttl_minutes=15)
        filters = SearchFilters()
        cache.set("search", "google_cloud", filters, [_make_product()])
        result = cache.get("search", "google_cloud", filters)
        assert result is not None
        assert len(result) == 1

    def test_get_missing(self):
        cache = MarketCache()
        filters = SearchFilters(text_query="nonexistent")
        assert cache.get("search", "google_cloud", filters) is None

    def test_clear(self):
        cache = MarketCache()
        filters = SearchFilters()
        cache.set("search", "google_cloud", filters, [_make_product()])
        cache.clear()
        assert cache.get("search", "google_cloud", filters) is None

    def test_cache_stats(self):
        cache = MarketCache()
        filters = SearchFilters()
        cache.set("search", "aws_glue", filters, [_make_product()])
        cache.get("search", "aws_glue", filters)  # hit
        cache.get("search", "aws_glue", SearchFilters(text_query="miss"))  # miss
        stats = cache.get_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1


# ---------------------------------------------------------------------------
# CatalogType / DataProductLayer / DataProductStatus enums
# ---------------------------------------------------------------------------


class TestEnums:
    def test_catalog_type_values(self):
        assert CatalogType.GOOGLE_CLOUD_DATA_CATALOG.value == "google_cloud_data_catalog"
        assert CatalogType.AWS_GLUE_DATA_CATALOG.value == "aws_glue_data_catalog"
        assert CatalogType.FLUID_COMMAND_CENTER.value == "fluid_command_center"

    def test_data_product_layer_values(self):
        assert DataProductLayer.RAW.value == "raw"
        assert DataProductLayer.GOLD.value == "gold"
        assert DataProductLayer.REAL_TIME.value == "real_time"

    def test_data_product_status_values(self):
        assert DataProductStatus.ACTIVE.value == "active"
        assert DataProductStatus.DEPRECATED.value == "deprecated"
        assert DataProductStatus.RETIRED.value == "retired"


# ---------------------------------------------------------------------------
# SearchFilters defaults
# ---------------------------------------------------------------------------


class TestSearchFilters:
    def test_defaults(self):
        sf = SearchFilters()
        assert sf.limit == 50
        assert sf.offset == 0
        assert sf.exact_match is False
        assert sf.case_sensitive is False
        assert sf.include_deprecated is True
        assert sf.sort_by == "relevance"
        assert sf.sort_order == "desc"

    def test_custom_values(self):
        sf = SearchFilters(
            domain="finance",
            text_query="test",
            limit=10,
            sort_by="name",
        )
        assert sf.domain == "finance"
        assert sf.limit == 10


# ---------------------------------------------------------------------------
# SearchResult
# ---------------------------------------------------------------------------


class TestSearchResult:
    def test_creation(self):
        sr = SearchResult(
            products=[_make_product()],
            total_count=1,
            facets={},
            query_time=0.5,
        )
        assert sr.total_count == 1
        assert sr.query_time == 0.5
        assert len(sr.products) == 1


# ---------------------------------------------------------------------------
# DataProductMetadata
# ---------------------------------------------------------------------------


class TestDataProductMetadata:
    def test_defaults(self):
        p = _make_product()
        assert p.tags == []
        assert p.usage_stats == {}
        assert p.lineage == {}
        assert p.sla == {}
        assert p.contact_info == {}

    def test_with_all_fields(self):
        p = _make_product(
            documentation_url="https://docs.example.com",
            api_endpoint="https://api.example.com",
            sample_data_url="https://sample.example.com",
        )
        assert p.documentation_url is not None
        assert p.api_endpoint is not None
        assert p.sample_data_url is not None


# ---------------------------------------------------------------------------
# BaseCatalogConnector._health_check
# ---------------------------------------------------------------------------


class TestBaseCatalogConnectorHealth:
    def test_health_check_returns_is_connected(self):
        connector = BaseCatalogConnector({}, LOG)
        connector.is_connected = True
        result = _run(connector._health_check())
        assert result is True

        connector.is_connected = False
        result = _run(connector._health_check())
        assert result is False
