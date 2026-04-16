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

"""Branch coverage tests for market.py."""

import argparse
import logging
from datetime import datetime, timedelta, timezone

import pytest

# ---- Enums ----


class TestEnums:
    def test_catalog_type_values(self):
        from fluid_build.cli.market import CatalogType

        assert len(list(CatalogType)) >= 9
        assert CatalogType.GOOGLE_CLOUD_DATA_CATALOG.value == "google_cloud_data_catalog"

    def test_data_product_layer(self):
        from fluid_build.cli.market import DataProductLayer

        assert DataProductLayer.RAW.value == "raw"
        assert DataProductLayer.GOLD.value == "gold"
        assert len(list(DataProductLayer)) >= 5

    def test_data_product_status(self):
        from fluid_build.cli.market import DataProductStatus

        assert DataProductStatus.ACTIVE.value == "active"
        assert DataProductStatus.DEPRECATED.value == "deprecated"


# ---- Data Structures ----


class TestDataProductMetadata:
    def _make_product(self, **overrides):
        from fluid_build.cli.market import DataProductLayer, DataProductMetadata, DataProductStatus

        defaults = dict(
            id="dp-1",
            name="Test Product",
            description="A test product",
            domain="finance",
            owner="team-a",
            layer=DataProductLayer.GOLD,
            status=DataProductStatus.ACTIVE,
            version="1.0",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        defaults.update(overrides)
        return DataProductMetadata(**defaults)

    def test_create(self):
        product = self._make_product()
        assert product.id == "dp-1"

    def test_with_optional_fields(self):
        product = self._make_product(
            tags=["finance", "analytics"],
            quality_score=0.95,
            schema_url="http://example.com/schema",
            documentation_url="http://example.com/docs",
            api_endpoint="http://example.com/api",
            sample_data_url="http://example.com/sample",
            usage_stats={"total_queries": 100},
            lineage={"upstream": ["src"]},
            catalog_source="my-catalog",
            catalog_type="gcp",
        )
        assert product.quality_score == 0.95
        assert len(product.tags) == 2


class TestSearchFilters:
    def test_defaults(self):
        from fluid_build.cli.market import SearchFilters

        f = SearchFilters()
        assert f.limit == 50
        assert f.offset == 0
        assert f.exact_match is False
        assert f.include_deprecated is True
        assert f.sort_by == "relevance"

    def test_with_fields(self):
        from fluid_build.cli.market import DataProductLayer, DataProductStatus, SearchFilters

        f = SearchFilters(
            domain="finance",
            owner="team-a",
            layer=DataProductLayer.GOLD,
            status=DataProductStatus.ACTIVE,
            tags=["important"],
            text_query="customer",
            min_quality_score=0.8,
            limit=10,
        )
        assert f.domain == "finance"
        assert f.text_query == "customer"

    def test_advanced_options(self):
        from fluid_build.cli.market import SearchFilters

        f = SearchFilters(
            exact_match=True,
            case_sensitive=True,
            include_deprecated=False,
            search_fields=["name", "tags"],
            facets={"domain": ["finance"]},
            sort_by="quality_score",
            sort_order="asc",
            boost_fields={"name": 5.0},
            has_documentation=True,
            has_api_endpoint=False,
            min_usage_count=10,
            max_usage_count=1000,
        )
        assert f.exact_match is True
        assert f.has_documentation is True


class TestSearchResult:
    def test_create(self):
        from fluid_build.cli.market import SearchResult

        r = SearchResult(products=[], total_count=0, facets={}, query_time=0.1)
        assert r.total_count == 0
        assert r.query_time == 0.1


# ---- AdvancedSearchEngine ----


def _make_product(**overrides):
    from fluid_build.cli.market import DataProductLayer, DataProductMetadata, DataProductStatus

    defaults = dict(
        id="dp-1",
        name="Customer Analytics",
        description="Customer analytics product",
        domain="finance",
        owner="team-a",
        layer=DataProductLayer.GOLD,
        status=DataProductStatus.ACTIVE,
        version="1.0",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        tags=["customer", "analytics"],
    )
    defaults.update(overrides)
    return DataProductMetadata(**defaults)


class TestAdvancedSearchEngine:
    def test_init(self):
        from fluid_build.cli.market import AdvancedSearchEngine

        engine = AdvancedSearchEngine(logging.getLogger("test"))
        assert engine.saved_searches == {}

    def test_relevance_no_query(self):
        from fluid_build.cli.market import AdvancedSearchEngine, SearchFilters

        engine = AdvancedSearchEngine(logging.getLogger("test"))
        product = _make_product()
        score = engine.calculate_relevance_score(product, SearchFilters())
        assert score == 1.0

    def test_relevance_with_query(self):
        from fluid_build.cli.market import AdvancedSearchEngine, SearchFilters

        engine = AdvancedSearchEngine(logging.getLogger("test"))
        product = _make_product(name="Customer Analytics")
        filters = SearchFilters(text_query="customer")
        score = engine.calculate_relevance_score(product, filters)
        assert score > 0

    def test_relevance_exact_match(self):
        from fluid_build.cli.market import AdvancedSearchEngine, SearchFilters

        engine = AdvancedSearchEngine(logging.getLogger("test"))
        product = _make_product(name="Customer Analytics")
        filters = SearchFilters(text_query="customer", exact_match=True)
        score = engine.calculate_relevance_score(product, filters)
        assert score > 0

    def test_relevance_no_match(self):
        from fluid_build.cli.market import AdvancedSearchEngine, SearchFilters

        engine = AdvancedSearchEngine(logging.getLogger("test"))
        product = _make_product(name="Sales Dashboard")
        filters = SearchFilters(text_query="zzzznonexistent")
        score = engine.calculate_relevance_score(product, filters)
        assert score == 0.0

    def test_relevance_quality_boost(self):
        from fluid_build.cli.market import AdvancedSearchEngine, SearchFilters

        engine = AdvancedSearchEngine(logging.getLogger("test"))
        product = _make_product(quality_score=0.9)
        filters = SearchFilters(text_query="customer")
        score = engine.calculate_relevance_score(product, filters)
        assert score > 0

    def test_relevance_recency_boost(self):
        from fluid_build.cli.market import AdvancedSearchEngine, SearchFilters

        engine = AdvancedSearchEngine(logging.getLogger("test"))
        product = _make_product(updated_at=datetime.now(timezone.utc) - timedelta(days=5))
        filters = SearchFilters(text_query="customer")
        score = engine.calculate_relevance_score(product, filters)
        assert score > 0

    def test_relevance_no_recency_boost(self):
        from fluid_build.cli.market import AdvancedSearchEngine, SearchFilters

        engine = AdvancedSearchEngine(logging.getLogger("test"))
        product = _make_product(updated_at=datetime.now(timezone.utc) - timedelta(days=60))
        filters = SearchFilters(text_query="customer")
        score = engine.calculate_relevance_score(product, filters)
        assert score > 0

    def test_relevance_custom_boost(self):
        from fluid_build.cli.market import AdvancedSearchEngine, SearchFilters

        engine = AdvancedSearchEngine(logging.getLogger("test"))
        product = _make_product()
        filters = SearchFilters(text_query="customer", boost_fields={"name": 10.0})
        score = engine.calculate_relevance_score(product, filters)
        assert score > 0

    @pytest.mark.parametrize("search_field", ["name", "description", "tags", "domain", "owner"])
    def test_relevance_different_fields(self, search_field):
        from fluid_build.cli.market import AdvancedSearchEngine, SearchFilters

        engine = AdvancedSearchEngine(logging.getLogger("test"))
        product = _make_product()
        filters = SearchFilters(text_query="customer", search_fields=[search_field])
        score = engine.calculate_relevance_score(product, filters)
        assert isinstance(score, float)

    def test_relevance_unknown_field(self):
        from fluid_build.cli.market import AdvancedSearchEngine, SearchFilters

        engine = AdvancedSearchEngine(logging.getLogger("test"))
        product = _make_product()
        filters = SearchFilters(text_query="customer", search_fields=["nonexistent"])
        score = engine.calculate_relevance_score(product, filters)
        assert score == 0.0  # No matching fields


class TestApplyAdvancedFilters:
    def _make_engine(self):
        from fluid_build.cli.market import AdvancedSearchEngine

        return AdvancedSearchEngine(logging.getLogger("test"))

    def test_no_filters(self):
        from fluid_build.cli.market import SearchFilters

        engine = self._make_engine()
        products = [_make_product()]
        result = engine.apply_advanced_filters(products, SearchFilters())
        assert len(result) == 1

    def test_has_documentation_true(self):
        from fluid_build.cli.market import SearchFilters

        engine = self._make_engine()
        products = [
            _make_product(id="dp-1", documentation_url="http://docs"),
            _make_product(id="dp-2", documentation_url=None),
        ]
        result = engine.apply_advanced_filters(products, SearchFilters(has_documentation=True))
        assert len(result) == 1

    def test_has_documentation_false(self):
        from fluid_build.cli.market import SearchFilters

        engine = self._make_engine()
        products = [
            _make_product(id="dp-1", documentation_url="http://docs"),
            _make_product(id="dp-2", documentation_url=None),
        ]
        result = engine.apply_advanced_filters(products, SearchFilters(has_documentation=False))
        assert len(result) == 1

    def test_has_api_endpoint(self):
        from fluid_build.cli.market import SearchFilters

        engine = self._make_engine()
        products = [_make_product(id="dp-1", api_endpoint="http://api"), _make_product(id="dp-2")]
        result = engine.apply_advanced_filters(products, SearchFilters(has_api_endpoint=True))
        assert len(result) == 1

    def test_has_sample_data(self):
        from fluid_build.cli.market import SearchFilters

        engine = self._make_engine()
        products = [
            _make_product(id="dp-1", sample_data_url="http://sample"),
            _make_product(id="dp-2"),
        ]
        result = engine.apply_advanced_filters(products, SearchFilters(has_sample_data=True))
        assert len(result) == 1

    def test_min_usage_count(self):
        from fluid_build.cli.market import SearchFilters

        engine = self._make_engine()
        products = [
            _make_product(id="dp-1", usage_stats={"total_queries": 100}),
            _make_product(id="dp-2", usage_stats={"total_queries": 5}),
        ]
        result = engine.apply_advanced_filters(products, SearchFilters(min_usage_count=10))
        assert len(result) == 1

    def test_max_usage_count(self):
        from fluid_build.cli.market import SearchFilters

        engine = self._make_engine()
        products = [
            _make_product(id="dp-1", usage_stats={"total_queries": 100}),
            _make_product(id="dp-2", usage_stats={"total_queries": 5}),
        ]
        result = engine.apply_advanced_filters(products, SearchFilters(max_usage_count=50))
        assert len(result) == 1

    def test_exclude_deprecated(self):
        from fluid_build.cli.market import DataProductStatus, SearchFilters

        engine = self._make_engine()
        products = [
            _make_product(id="dp-1", status=DataProductStatus.ACTIVE),
            _make_product(id="dp-2", status=DataProductStatus.DEPRECATED),
        ]
        result = engine.apply_advanced_filters(products, SearchFilters(include_deprecated=False))
        assert len(result) == 1

    def test_facet_domain_filter(self):
        from fluid_build.cli.market import SearchFilters

        engine = self._make_engine()
        products = [
            _make_product(id="dp-1", domain="finance"),
            _make_product(id="dp-2", domain="marketing"),
        ]
        result = engine.apply_advanced_filters(
            products, SearchFilters(facets={"domain": ["finance"]})
        )
        assert len(result) == 1

    def test_facet_owner_filter(self):
        from fluid_build.cli.market import SearchFilters

        engine = self._make_engine()
        products = [
            _make_product(id="dp-1", owner="team-a"),
            _make_product(id="dp-2", owner="team-b"),
        ]
        result = engine.apply_advanced_filters(
            products, SearchFilters(facets={"owner": ["team-a"]})
        )
        assert len(result) == 1

    def test_facet_layer_filter(self):
        from fluid_build.cli.market import DataProductLayer, SearchFilters

        engine = self._make_engine()
        products = [
            _make_product(id="dp-1", layer=DataProductLayer.GOLD),
            _make_product(id="dp-2", layer=DataProductLayer.RAW),
        ]
        result = engine.apply_advanced_filters(products, SearchFilters(facets={"layer": ["gold"]}))
        assert len(result) == 1

    def test_facet_status_filter(self):
        from fluid_build.cli.market import DataProductStatus, SearchFilters

        engine = self._make_engine()
        products = [
            _make_product(id="dp-1", status=DataProductStatus.ACTIVE),
            _make_product(id="dp-2", status=DataProductStatus.DEPRECATED),
        ]
        result = engine.apply_advanced_filters(
            products, SearchFilters(facets={"status": ["active"]})
        )
        assert len(result) == 1

    def test_facet_tags_filter(self):
        from fluid_build.cli.market import SearchFilters

        engine = self._make_engine()
        products = [
            _make_product(id="dp-1", tags=["finance", "analytics"]),
            _make_product(id="dp-2", tags=["marketing"]),
        ]
        result = engine.apply_advanced_filters(
            products, SearchFilters(facets={"tags": ["finance"]})
        )
        assert len(result) == 1


class TestExtractFacets:
    def test_extract(self):
        from fluid_build.cli.market import AdvancedSearchEngine

        engine = AdvancedSearchEngine(logging.getLogger("test"))
        products = [
            _make_product(id="dp-1", domain="finance", tags=["a", "b"]),
            _make_product(id="dp-2", domain="finance", tags=["b", "c"]),
            _make_product(id="dp-3", domain="marketing", tags=["c"]),
        ]
        facets = engine.extract_facets(products)
        assert facets["domain"]["finance"] == 2
        assert facets["domain"]["marketing"] == 1
        assert facets["tags"]["b"] == 2


# ---- CircuitBreaker ----


class TestCircuitBreaker:
    def test_create(self):
        from fluid_build.cli.market import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0)
        assert cb is not None

    def test_initial_state(self):
        from fluid_build.cli.market import CircuitBreaker

        cb = CircuitBreaker()
        assert cb.state == "CLOSED"
        assert cb.failure_count == 0
        assert cb.last_failure_time is None

    def test_on_failure_keeps_closed_under_threshold(self):
        from fluid_build.cli.market import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=3)
        cb._on_failure()
        assert cb.state == "CLOSED"
        assert cb.failure_count == 1

    def test_on_failure_opens_at_threshold(self):
        from fluid_build.cli.market import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=2)
        cb._on_failure()
        assert cb.state == "CLOSED"
        cb._on_failure()
        assert cb.state == "OPEN"
        assert cb.failure_count == 2
        assert cb.last_failure_time is not None

    def test_on_success_resets(self):
        from fluid_build.cli.market import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=2)
        cb._on_failure()
        assert cb.failure_count == 1
        cb._on_success()
        assert cb.failure_count == 0
        assert cb.state == "CLOSED"

    def test_call_success(self):
        import asyncio

        from fluid_build.cli.market import CircuitBreaker

        cb = CircuitBreaker()

        async def ok():
            return 42

        result = asyncio.run(cb.call(ok))
        assert result == 42
        assert cb.state == "CLOSED"

    def test_call_failure(self):
        import asyncio

        from fluid_build.cli.market import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=2)

        async def fail():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            asyncio.run(cb.call(fail))
        assert cb.failure_count == 1

    def test_call_open_circuit_raises(self):
        import asyncio

        from fluid_build.cli.market import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=1)

        async def fail():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            asyncio.run(cb.call(fail))
        assert cb.state == "OPEN"

        async def ok():
            return 1

        with pytest.raises(Exception, match="Circuit breaker is OPEN"):
            asyncio.run(cb.call(ok))

    def test_should_attempt_reset_half_open(self):
        import asyncio

        from fluid_build.cli.market import CircuitBreaker

        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0)

        async def fail():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            asyncio.run(cb.call(fail))
        assert cb.state == "OPEN"

        # recovery_timeout=0 means immediate reset attempt
        async def ok():
            return "recovered"

        result = asyncio.run(cb.call(ok))
        assert result == "recovered"
        assert cb.state == "CLOSED"


# ---- Register ----


class TestRegister:
    def test_register(self):
        from fluid_build.cli.market import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
