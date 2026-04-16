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

"""Tests for market.py AdvancedSearchEngine: relevance scoring, filtering, facets, ranking."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from fluid_build.cli.market import (
    AdvancedSearchEngine,
    DataProductLayer,
    DataProductMetadata,
    DataProductStatus,
    SearchFilters,
)


def _product(
    name="test-product",
    description="A test product",
    domain="finance",
    owner="data-team",
    layer=DataProductLayer.GOLD,
    status=DataProductStatus.ACTIVE,
    tags=None,
    quality_score=None,
    documentation_url=None,
    api_endpoint=None,
    sample_data_url=None,
    usage_stats=None,
    updated_at=None,
    created_at=None,
):
    now = datetime.now(timezone.utc)
    return DataProductMetadata(
        id="test-id",
        name=name,
        description=description,
        domain=domain,
        owner=owner,
        layer=layer,
        status=status,
        version="1.0.0",
        created_at=created_at or now,
        updated_at=updated_at or now,
        tags=tags or [],
        quality_score=quality_score,
        documentation_url=documentation_url,
        api_endpoint=api_endpoint,
        sample_data_url=sample_data_url,
        usage_stats=usage_stats or {},
    )


@pytest.fixture
def engine():
    return AdvancedSearchEngine(logger=MagicMock())


class TestCalculateRelevanceScore:
    def test_name_match(self, engine):
        p = _product(name="bitcoin tracker")
        f = SearchFilters(text_query="bitcoin", search_fields=["name"])
        score = engine.calculate_relevance_score(p, f)
        assert score > 0

    def test_description_match(self, engine):
        p = _product(description="tracks bitcoin prices")
        f = SearchFilters(text_query="bitcoin", search_fields=["description"])
        score = engine.calculate_relevance_score(p, f)
        assert score > 0

    def test_tags_match(self, engine):
        p = _product(tags=["bitcoin", "crypto"])
        f = SearchFilters(text_query="bitcoin", search_fields=["tags"])
        score = engine.calculate_relevance_score(p, f)
        assert score > 0

    def test_domain_match(self, engine):
        p = _product(domain="finance")
        f = SearchFilters(text_query="finance", search_fields=["domain"])
        score = engine.calculate_relevance_score(p, f)
        assert score > 0

    def test_owner_match(self, engine):
        p = _product(owner="data-team")
        f = SearchFilters(text_query="data-team", search_fields=["owner"])
        score = engine.calculate_relevance_score(p, f)
        assert score > 0

    def test_no_match(self, engine):
        p = _product(name="something else")
        f = SearchFilters(text_query="nonexistent", search_fields=["name"])
        score = engine.calculate_relevance_score(p, f)
        assert score == 0

    def test_exact_match(self, engine):
        p = _product(name="bitcoin tracker")
        f = SearchFilters(text_query="bitcoin", search_fields=["name"], exact_match=True)
        score = engine.calculate_relevance_score(p, f)
        assert score > 0

    def test_quality_boost(self, engine):
        p_low = _product(name="bitcoin", quality_score=0.0)
        p_high = _product(name="bitcoin", quality_score=1.0)
        f = SearchFilters(text_query="bitcoin", search_fields=["name"])
        score_low = engine.calculate_relevance_score(p_low, f)
        score_high = engine.calculate_relevance_score(p_high, f)
        assert score_high > score_low

    def test_recency_boost(self, engine):
        recent = _product(name="bitcoin", updated_at=datetime.now(timezone.utc))
        old = _product(name="bitcoin", updated_at=datetime.now(timezone.utc) - timedelta(days=60))
        f = SearchFilters(text_query="bitcoin", search_fields=["name"])
        score_recent = engine.calculate_relevance_score(recent, f)
        score_old = engine.calculate_relevance_score(old, f)
        assert score_recent >= score_old

    def test_fuzzy_partial_match(self, engine):
        p = _product(name="bitcoin-price-tracker")
        f = SearchFilters(text_query="bit", search_fields=["name"], exact_match=False)
        score = engine.calculate_relevance_score(p, f)
        assert score > 0

    def test_unknown_field_skipped(self, engine):
        p = _product(name="bitcoin")
        f = SearchFilters(text_query="bitcoin", search_fields=["unknown_field"])
        score = engine.calculate_relevance_score(p, f)
        assert score == 0


class TestExtractFacets:
    def test_basic(self, engine):
        products = [
            _product(domain="finance", owner="team-a", tags=["tag1"]),
            _product(domain="finance", owner="team-b", tags=["tag1", "tag2"]),
            _product(domain="marketing", owner="team-a", tags=["tag2"]),
        ]
        facets = engine.extract_facets(products)
        assert facets["domain"]["finance"] == 2
        assert facets["domain"]["marketing"] == 1
        assert facets["owner"]["team-a"] == 2
        assert facets["tags"]["tag1"] == 2
        assert facets["tags"]["tag2"] == 2

    def test_empty(self, engine):
        facets = engine.extract_facets([])
        assert facets["domain"] == {}


class TestApplyAdvancedFilters:
    def test_has_documentation(self, engine):
        products = [
            _product(name="with-docs", documentation_url="https://example.com"),
            _product(name="no-docs"),
        ]
        f = SearchFilters(has_documentation=True)
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 1
        assert result[0].name == "with-docs"

    def test_has_documentation_false(self, engine):
        products = [
            _product(name="with-docs", documentation_url="https://example.com"),
            _product(name="no-docs"),
        ]
        f = SearchFilters(has_documentation=False)
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 1
        assert result[0].name == "no-docs"

    def test_has_api_endpoint(self, engine):
        products = [
            _product(name="with-api", api_endpoint="https://api.example.com"),
            _product(name="no-api"),
        ]
        f = SearchFilters(has_api_endpoint=True)
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 1

    def test_has_sample_data(self, engine):
        products = [
            _product(name="with-sample", sample_data_url="https://sample.example.com"),
            _product(name="no-sample"),
        ]
        f = SearchFilters(has_sample_data=True)
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 1

    def test_min_usage_count(self, engine):
        products = [
            _product(name="high-use", usage_stats={"total_queries": 100}),
            _product(name="low-use", usage_stats={"total_queries": 5}),
        ]
        f = SearchFilters(min_usage_count=50)
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 1
        assert result[0].name == "high-use"

    def test_max_usage_count(self, engine):
        products = [
            _product(name="high-use", usage_stats={"total_queries": 100}),
            _product(name="low-use", usage_stats={"total_queries": 5}),
        ]
        f = SearchFilters(max_usage_count=50)
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 1
        assert result[0].name == "low-use"

    def test_exclude_deprecated(self, engine):
        products = [
            _product(name="active", status=DataProductStatus.ACTIVE),
            _product(name="deprecated", status=DataProductStatus.DEPRECATED),
        ]
        f = SearchFilters(include_deprecated=False)
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 1
        assert result[0].name == "active"

    def test_include_deprecated(self, engine):
        products = [
            _product(name="active", status=DataProductStatus.ACTIVE),
            _product(name="deprecated", status=DataProductStatus.DEPRECATED),
        ]
        f = SearchFilters(include_deprecated=True)
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 2

    def test_facet_domain_filter(self, engine):
        products = [
            _product(name="p1", domain="finance"),
            _product(name="p2", domain="marketing"),
        ]
        f = SearchFilters(facets={"domain": ["finance"]})
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 1
        assert result[0].domain == "finance"

    def test_facet_owner_filter(self, engine):
        products = [
            _product(name="p1", owner="team-a"),
            _product(name="p2", owner="team-b"),
        ]
        f = SearchFilters(facets={"owner": ["team-a"]})
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 1

    def test_facet_layer_filter(self, engine):
        products = [
            _product(name="p1", layer=DataProductLayer.GOLD),
            _product(name="p2", layer=DataProductLayer.RAW),
        ]
        f = SearchFilters(facets={"layer": ["gold"]})
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 1

    def test_facet_status_filter(self, engine):
        products = [
            _product(name="p1", status=DataProductStatus.ACTIVE),
            _product(name="p2", status=DataProductStatus.STAGING),
        ]
        f = SearchFilters(facets={"status": ["active"]})
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 1

    def test_facet_tags_filter(self, engine):
        products = [
            _product(name="p1", tags=["python", "data"]),
            _product(name="p2", tags=["sql"]),
        ]
        f = SearchFilters(facets={"tags": ["python"]})
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 1

    def test_no_filters_returns_all(self, engine):
        products = [_product(name="p1"), _product(name="p2")]
        f = SearchFilters()
        result = engine.apply_advanced_filters(products, f)
        assert len(result) == 2


class TestRankAndSortProducts:
    def test_sort_by_name_asc(self, engine):
        products = [_product(name="Zebra"), _product(name="Alpha")]
        f = SearchFilters(sort_by="name", sort_order="asc")
        result = engine.rank_and_sort_products(products, f)
        assert result[0].name == "Alpha"

    def test_sort_by_name_desc(self, engine):
        products = [_product(name="Alpha"), _product(name="Zebra")]
        f = SearchFilters(sort_by="name", sort_order="desc")
        result = engine.rank_and_sort_products(products, f)
        assert result[0].name == "Zebra"

    def test_sort_by_created_at(self, engine):
        now = datetime.now(timezone.utc)
        products = [
            _product(name="old", created_at=now - timedelta(days=10)),
            _product(name="new", created_at=now),
        ]
        f = SearchFilters(sort_by="created_at", sort_order="desc")
        result = engine.rank_and_sort_products(products, f)
        assert result[0].name == "new"

    def test_sort_by_updated_at(self, engine):
        now = datetime.now(timezone.utc)
        products = [
            _product(name="old", updated_at=now - timedelta(days=10)),
            _product(name="new", updated_at=now),
        ]
        f = SearchFilters(sort_by="updated_at", sort_order="desc")
        result = engine.rank_and_sort_products(products, f)
        assert result[0].name == "new"

    def test_sort_by_quality_score(self, engine):
        products = [
            _product(name="low", quality_score=0.3),
            _product(name="high", quality_score=0.9),
        ]
        f = SearchFilters(sort_by="quality_score", sort_order="desc")
        result = engine.rank_and_sort_products(products, f)
        assert result[0].name == "high"

    def test_sort_by_relevance(self, engine):
        products = [
            _product(name="unrelated stuff"),
            _product(name="bitcoin tracker"),
        ]
        f = SearchFilters(
            sort_by="relevance", sort_order="desc", text_query="bitcoin", search_fields=["name"]
        )
        result = engine.rank_and_sort_products(products, f)
        assert result[0].name == "bitcoin tracker"

    def test_sort_by_unknown_returns_original(self, engine):
        products = [_product(name="a"), _product(name="b")]
        f = SearchFilters(sort_by="unknown_field", sort_order="asc")
        result = engine.rank_and_sort_products(products, f)
        assert len(result) == 2


class TestSaveAndLoadSearch:
    def test_save_and_load(self, engine):
        f = SearchFilters(text_query="bitcoin", search_name="my-search", save_search=True)
        assert engine.save_search(f) is True
        loaded = engine.load_saved_search("my-search")
        assert loaded is not None

    def test_save_without_name(self, engine):
        f = SearchFilters(text_query="bitcoin")
        assert engine.save_search(f) is False

    def test_list_saved(self, engine):
        f = SearchFilters(text_query="q", search_name="s1")
        engine.save_search(f)
        assert "s1" in engine.list_saved_searches()

    def test_load_nonexistent(self, engine):
        assert engine.load_saved_search("nonexistent") is None
