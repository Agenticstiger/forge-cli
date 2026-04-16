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

"""Tests for market.py helpers: _merge_config, _load_env_config, build_search_filters, format_json_output."""

import json
from datetime import datetime
from unittest.mock import MagicMock

from fluid_build.cli.market import (
    DataProductLayer,
    DataProductMetadata,
    DataProductStatus,
    _load_env_config,
    _merge_config,
    build_search_filters,
    format_json_output,
)


class TestMergeConfig:
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

    def test_override_non_dict_with_dict(self):
        base = {"x": "string"}
        override = {"x": {"nested": True}}
        _merge_config(base, override)
        assert base["x"] == {"nested": True}

    def test_empty_override(self):
        base = {"a": 1}
        _merge_config(base, {})
        assert base == {"a": 1}

    def test_deep_nested(self):
        base = {"l1": {"l2": {"l3": "old"}}}
        override = {"l1": {"l2": {"l3": "new", "l3b": "added"}}}
        _merge_config(base, override)
        assert base["l1"]["l2"]["l3"] == "new"
        assert base["l1"]["l2"]["l3b"] == "added"


class TestLoadEnvConfig:
    def test_empty_env(self, monkeypatch):
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
        config = _load_env_config()
        assert config == {}

    def test_gcp_config(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT_ID", "my-project")
        monkeypatch.setenv("GCP_LOCATION", "europe-west1")
        # Clear other vars
        for var in [
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
        config = _load_env_config()
        assert config["google_cloud_data_catalog"]["project_id"] == "my-project"
        assert config["google_cloud_data_catalog"]["location"] == "europe-west1"

    def test_aws_config(self, monkeypatch):
        monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_PROFILE", "default")
        for var in [
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
        config = _load_env_config()
        assert config["aws_glue_data_catalog"]["region"] == "us-east-1"
        assert config["aws_glue_data_catalog"]["profile"] == "default"

    def test_datahub_config(self, monkeypatch):
        for var in [
            "GCP_PROJECT_ID",
            "AWS_REGION",
            "AZURE_PURVIEW_ACCOUNT",
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
        monkeypatch.setenv("DATAHUB_SERVER_URL", "http://localhost:8080")
        monkeypatch.setenv("DATAHUB_TOKEN", "tok123")
        config = _load_env_config()
        assert config["datahub"]["server_url"] == "http://localhost:8080"
        assert config["datahub"]["token"] == "tok123"

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


class TestBuildSearchFilters:
    def _make_args(self, **kwargs):
        defaults = {
            "search": None,
            "domain": None,
            "owner": None,
            "layer": None,
            "status": None,
            "tags": None,
            "min_quality": None,
            "created_after": None,
            "created_before": None,
            "limit": 20,
            "offset": 0,
        }
        defaults.update(kwargs)
        args = MagicMock()
        for k, v in defaults.items():
            setattr(args, k, v)
        return args

    def test_empty_args(self):
        args = self._make_args()
        f = build_search_filters(args)
        assert f.text_query is None
        assert f.limit == 20

    def test_search_query(self):
        args = self._make_args(search="bitcoin")
        f = build_search_filters(args)
        assert f.text_query == "bitcoin"

    def test_domain_list(self):
        args = self._make_args(domain=["finance", "trading"])
        f = build_search_filters(args)
        assert f.domain == "finance"  # takes first

    def test_domain_string(self):
        args = self._make_args(domain="finance")
        f = build_search_filters(args)
        assert f.domain == "finance"

    def test_layer_filter(self):
        args = self._make_args(layer=["gold"])
        f = build_search_filters(args)
        assert f.layer == DataProductLayer.GOLD

    def test_status_filter(self):
        args = self._make_args(status=["active"])
        f = build_search_filters(args)
        assert f.status == DataProductStatus.ACTIVE

    def test_tags_filter(self):
        args = self._make_args(tags=["crypto", "finance"])
        f = build_search_filters(args)
        assert f.tags == ["crypto", "finance"]

    def test_min_quality(self):
        args = self._make_args(min_quality=0.9)
        f = build_search_filters(args)
        assert f.min_quality_score == 0.9

    def test_date_filters(self):
        args = self._make_args(created_after="2024-01-01", created_before="2024-12-31")
        f = build_search_filters(args)
        assert f.created_after == datetime(2024, 1, 1)
        assert f.created_before == datetime(2024, 12, 31)

    def test_limit_offset(self):
        args = self._make_args(limit=50, offset=10)
        f = build_search_filters(args)
        assert f.limit == 50
        assert f.offset == 10


class TestFormatJsonOutput:
    def _make_product(self, **overrides):
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
            "schema_url": None,
            "documentation_url": None,
            "api_endpoint": None,
            "quality_score": 0.95,
            "catalog_source": "local",
            "catalog_type": "custom",
        }
        defaults.update(overrides)
        return DataProductMetadata(**defaults)

    def test_single_product(self):
        result = format_json_output([self._make_product()])
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["id"] == "p1"
        assert data[0]["name"] == "Test Product"
        assert data[0]["layer"] == "gold"
        assert data[0]["quality_score"] == 0.95

    def test_empty_list(self):
        result = format_json_output([])
        assert json.loads(result) == []

    def test_multiple_products(self):
        products = [
            self._make_product(id="p1", name="A"),
            self._make_product(id="p2", name="B"),
        ]
        data = json.loads(format_json_output(products))
        assert len(data) == 2
        assert data[0]["id"] == "p1"
        assert data[1]["id"] == "p2"

    def test_dates_serialized(self):
        result = format_json_output([self._make_product()])
        data = json.loads(result)
        assert "2024-01-01" in data[0]["created_at"]
        assert "2024-06-01" in data[0]["updated_at"]
