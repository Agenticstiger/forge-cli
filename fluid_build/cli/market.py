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
FLUID Market Command - Enterprise Data Product Discovery

This command connects to enterprise Data Catalogs and marketplaces to discover
published data products. It provides a unified interface for browsing and
searching data products across multiple catalog systems.

Supported Marketplaces:
- Google Cloud Data Catalog
- AWS Glue Data Catalog
- Azure Purview
- Apache Atlas
- Confluent Schema Registry
- DataHub
- Collibra
- Alation
- Custom REST API catalogs
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from fluid_build.cli.console import cprint, hint, success

# Rich imports for enhanced output
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.table import Table
    from rich.text import Text
    from rich.tree import Tree

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from ._common import CLIError

COMMAND = "market"

# ==========================================
# Data Structures & Enums
# ==========================================


class CatalogType(Enum):
    """Supported data catalog types"""

    GOOGLE_CLOUD_DATA_CATALOG = "google_cloud_data_catalog"
    AWS_GLUE_DATA_CATALOG = "aws_glue_data_catalog"
    AZURE_PURVIEW = "azure_purview"
    APACHE_ATLAS = "apache_atlas"
    CONFLUENT_SCHEMA_REGISTRY = "confluent_schema_registry"
    DATAHUB = "datahub"
    COLLIBRA = "collibra"
    ALATION = "alation"
    CUSTOM_REST_API = "custom_rest_api"
    FLUID_COMMAND_CENTER = "fluid_command_center"  # NEW: FLUID Command Center catalog


class DataProductLayer(Enum):
    """Data product layers"""

    RAW = "raw"
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"
    ANALYTICAL = "analytical"
    OPERATIONAL = "operational"
    REAL_TIME = "real_time"


class DataProductStatus(Enum):
    """Data product status"""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DEVELOPMENT = "development"
    STAGING = "staging"
    RETIRED = "retired"


@dataclass
class DataProductMetadata:
    """Comprehensive data product metadata"""

    id: str
    name: str
    description: str
    domain: str
    owner: str
    layer: DataProductLayer
    status: DataProductStatus
    version: str
    created_at: datetime
    updated_at: datetime
    tags: List[str] = field(default_factory=list)
    schema_url: Optional[str] = None
    documentation_url: Optional[str] = None
    api_endpoint: Optional[str] = None
    sample_data_url: Optional[str] = None
    quality_score: Optional[float] = None
    usage_stats: Dict[str, Any] = field(default_factory=dict)
    lineage: Dict[str, Any] = field(default_factory=dict)
    sla: Dict[str, Any] = field(default_factory=dict)
    contact_info: Dict[str, str] = field(default_factory=dict)
    catalog_source: str = ""
    catalog_type: str = ""


@dataclass
class SearchFilters:
    """Enhanced search and filter criteria with advanced operators"""

    # Basic filters
    domain: Optional[str] = None
    owner: Optional[str] = None
    layer: Optional[DataProductLayer] = None
    status: Optional[DataProductStatus] = None
    tags: List[str] = field(default_factory=list)
    text_query: Optional[str] = None
    min_quality_score: Optional[float] = None
    created_after: Optional[datetime] = None
    created_before: Optional[datetime] = None
    limit: int = 50
    offset: int = 0

    # Advanced search operators
    exact_match: bool = False  # Exact text matching vs fuzzy
    case_sensitive: bool = False  # Case sensitive search
    include_deprecated: bool = True  # Include deprecated products
    search_fields: List[str] = field(
        default_factory=lambda: ["name", "description", "tags"]
    )  # Fields to search

    # Faceted search
    facets: Dict[str, List[str]] = field(
        default_factory=dict
    )  # e.g., {'domain': ['finance', 'marketing']}

    # Ranking and sorting
    sort_by: str = "relevance"  # relevance, name, created_at, updated_at, quality_score
    sort_order: str = "desc"  # asc, desc
    boost_fields: Dict[str, float] = field(default_factory=dict)  # Field boosting for relevance

    # Advanced filters
    has_documentation: Optional[bool] = None
    has_api_endpoint: Optional[bool] = None
    has_sample_data: Optional[bool] = None
    min_usage_count: Optional[int] = None
    max_usage_count: Optional[int] = None

    # Saved search metadata
    search_name: Optional[str] = None
    save_search: bool = False


@dataclass
class SearchResult:
    """Enhanced search result with ranking information"""

    products: List[DataProductMetadata]
    total_count: int
    facets: Dict[str, Dict[str, int]]  # Facet counts
    query_time: float
    suggestions: List[str] = field(default_factory=list)  # Search suggestions
    ranking_info: Dict[str, Any] = field(default_factory=dict)  # Ranking details


class AdvancedSearchEngine:
    """Advanced search engine with ranking, faceting, and suggestions"""

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.saved_searches: Dict[str, SearchFilters] = {}
        self.search_history: List[Dict[str, Any]] = []

    def calculate_relevance_score(
        self, product: DataProductMetadata, filters: SearchFilters
    ) -> float:
        """Calculate relevance score for a product based on search criteria"""
        score = 0.0

        if not filters.text_query:
            return 1.0  # No text query, all results equally relevant

        query_terms = filters.text_query.lower().split()

        # Base scoring for different fields
        field_weights = {
            "name": filters.boost_fields.get("name", 3.0),
            "description": filters.boost_fields.get("description", 1.0),
            "tags": filters.boost_fields.get("tags", 2.0),
            "domain": filters.boost_fields.get("domain", 1.5),
            "owner": filters.boost_fields.get("owner", 0.5),
        }

        # Search in specified fields
        for field in filters.search_fields:
            if field == "name":
                text = product.name.lower()
            elif field == "description":
                text = product.description.lower()
            elif field == "tags":
                text = " ".join(product.tags).lower()
            elif field == "domain":
                text = product.domain.lower()
            elif field == "owner":
                text = product.owner.lower()
            else:
                continue

            field_score = 0.0
            for term in query_terms:
                if filters.exact_match:
                    if term in text:
                        field_score += 1.0
                else:
                    # Fuzzy matching - check for partial matches
                    if term in text:
                        field_score += 1.0
                    elif any(term in word for word in text.split()):
                        field_score += 0.5

            score += field_score * field_weights.get(field, 1.0)

        # Quality score boost
        if product.quality_score:
            score *= 1.0 + product.quality_score * 0.2  # Up to 20% boost for high quality

        # Recency boost
        if product.updated_at:
            days_old = (datetime.now(timezone.utc) - product.updated_at).days
            if days_old < 30:
                score *= 1.1  # 10% boost for recently updated

        return score

    def extract_facets(self, products: List[DataProductMetadata]) -> Dict[str, Dict[str, int]]:
        """Extract facet counts from products"""
        facets = {"domain": {}, "owner": {}, "layer": {}, "status": {}, "tags": {}}

        for product in products:
            # Domain facets
            domain = product.domain
            facets["domain"][domain] = facets["domain"].get(domain, 0) + 1

            # Owner facets
            owner = product.owner
            facets["owner"][owner] = facets["owner"].get(owner, 0) + 1

            # Layer facets
            layer = product.layer.value
            facets["layer"][layer] = facets["layer"].get(layer, 0) + 1

            # Status facets
            status = product.status.value
            facets["status"][status] = facets["status"].get(status, 0) + 1

            # Tag facets
            for tag in product.tags:
                facets["tags"][tag] = facets["tags"].get(tag, 0) + 1

        return facets

    def apply_advanced_filters(
        self, products: List[DataProductMetadata], filters: SearchFilters
    ) -> List[DataProductMetadata]:
        """Apply advanced filters to product list"""
        filtered_products = []

        for product in products:
            # Apply existing basic filters (handled in connectors)

            # Apply advanced filters
            if filters.has_documentation is not None:
                has_docs = bool(product.documentation_url)
                if filters.has_documentation != has_docs:
                    continue

            if filters.has_api_endpoint is not None:
                has_api = bool(product.api_endpoint)
                if filters.has_api_endpoint != has_api:
                    continue

            if filters.has_sample_data is not None:
                has_sample = bool(product.sample_data_url)
                if filters.has_sample_data != has_sample:
                    continue

            # Usage count filters (if usage stats available)
            if filters.min_usage_count is not None or filters.max_usage_count is not None:
                usage_count = product.usage_stats.get("total_queries", 0)
                if filters.min_usage_count is not None and usage_count < filters.min_usage_count:
                    continue
                if filters.max_usage_count is not None and usage_count > filters.max_usage_count:
                    continue

            # Deprecated filter
            if not filters.include_deprecated and product.status == DataProductStatus.DEPRECATED:
                continue

            # Facet filters
            if filters.facets:
                skip_product = False
                for facet_field, facet_values in filters.facets.items():
                    if facet_field == "domain" and product.domain not in facet_values:
                        skip_product = True
                        break
                    elif facet_field == "owner" and product.owner not in facet_values:
                        skip_product = True
                        break
                    elif facet_field == "layer" and product.layer.value not in facet_values:
                        skip_product = True
                        break
                    elif facet_field == "status" and product.status.value not in facet_values:
                        skip_product = True
                        break
                    elif facet_field == "tags" and not any(
                        tag in product.tags for tag in facet_values
                    ):
                        skip_product = True
                        break

                if skip_product:
                    continue

            filtered_products.append(product)

        return filtered_products

    def rank_and_sort_products(
        self, products: List[DataProductMetadata], filters: SearchFilters
    ) -> List[DataProductMetadata]:
        """Rank and sort products based on search criteria"""
        if filters.sort_by == "relevance" and filters.text_query:
            # Calculate relevance scores and sort by them
            product_scores = []
            for product in products:
                score = self.calculate_relevance_score(product, filters)
                product_scores.append((product, score))

            # Sort by score (descending for relevance)
            product_scores.sort(key=lambda x: x[1], reverse=(filters.sort_order == "desc"))
            return [product for product, score in product_scores]

        else:
            # Sort by specified field
            reverse = filters.sort_order == "desc"

            if filters.sort_by == "name":
                return sorted(products, key=lambda p: p.name.lower(), reverse=reverse)
            elif filters.sort_by == "created_at":
                return sorted(products, key=lambda p: p.created_at, reverse=reverse)
            elif filters.sort_by == "updated_at":
                return sorted(products, key=lambda p: p.updated_at, reverse=reverse)
            elif filters.sort_by == "quality_score":
                return sorted(products, key=lambda p: p.quality_score or 0, reverse=reverse)
            else:
                return products

    def save_search(self, filters: SearchFilters) -> bool:
        """Save a search configuration"""
        if not filters.search_name:
            return False

        # Create a copy without the save_search flag
        saved_filters = SearchFilters(
            **{k: v for k, v in filters.__dict__.items() if k not in ["save_search", "search_name"]}
        )

        self.saved_searches[filters.search_name] = saved_filters
        self.logger.info(f"Saved search '{filters.search_name}'")
        return True

    def load_saved_search(self, search_name: str) -> Optional[SearchFilters]:
        """Load a saved search configuration"""
        return self.saved_searches.get(search_name)

    def list_saved_searches(self) -> List[str]:
        """List all saved search names"""
        return list(self.saved_searches.keys())

    def generate_search_suggestions(
        self, products: List[DataProductMetadata], query: str
    ) -> List[str]:
        """Generate search suggestions based on available products"""
        suggestions = set()
        query_lower = query.lower()

        # Collect terms from all products
        all_terms = set()
        for product in products:
            all_terms.update(product.name.lower().split())
            all_terms.update(product.description.lower().split())
            all_terms.update(tag.lower() for tag in product.tags)
            all_terms.add(product.domain.lower())

        # Find similar terms
        for term in all_terms:
            if query_lower in term and term != query_lower:
                suggestions.add(term)
            elif term.startswith(query_lower) and len(term) > len(query_lower):
                suggestions.add(term)

        return sorted(list(suggestions))[:5]  # Return top 5 suggestions


# Global advanced search engine instance
advanced_search_engine = AdvancedSearchEngine(logging.getLogger(__name__))

# ==========================================
# Resilience and Error Handling
# ==========================================


class CircuitBreaker:
    """Circuit breaker pattern for handling service failures"""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        expected_exception: type = Exception,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN

    async def call(self, func, *args, **kwargs):
        """Call function with circuit breaker protection"""
        if self.state == "OPEN":
            if self._should_attempt_reset():
                self.state = "HALF_OPEN"
            else:
                raise Exception(f"Circuit breaker is OPEN for {func.__name__}")

        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except self.expected_exception as e:
            self._on_failure()
            raise e

    def _should_attempt_reset(self) -> bool:
        """Check if we should attempt to reset the circuit breaker"""
        return (
            self.last_failure_time and time.time() - self.last_failure_time >= self.recovery_timeout
        )

    def _on_success(self):
        """Handle successful call"""
        self.failure_count = 0
        self.state = "CLOSED"

    def _on_failure(self):
        """Handle failed call"""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"


async def retry_with_backoff(
    func,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
):
    """Retry function with exponential backoff"""
    last_exception = None

    for attempt in range(max_retries + 1):
        try:
            return await func()
        except Exception as e:
            last_exception = e

            if attempt == max_retries:
                break

            # Calculate delay with exponential backoff
            delay = min(base_delay * (backoff_factor**attempt), max_delay)
            await asyncio.sleep(delay)

    raise last_exception


# ==========================================
# Catalog Connectors
# ==========================================


class BaseCatalogConnector:
    """Base class for data catalog connectors with resilience features"""

    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.catalog_type = self.__class__.__name__

        # Resilience configuration
        self.max_retries = config.get("max_retries", 3)
        self.retry_delay = config.get("retry_delay", 1.0)
        self.timeout_seconds = config.get("timeout_seconds", 30)

        # Circuit breaker for connection failures
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=config.get("circuit_breaker_threshold", 5),
            recovery_timeout=config.get("circuit_breaker_timeout", 60),
        )

        # Connection state
        self.is_connected = False
        self.last_health_check = None
        self.health_check_interval = 300  # 5 minutes

    async def connect(self) -> bool:
        """Establish connection to the catalog with retry logic"""

        async def _connect_impl():
            return await self._connect_impl()

        try:
            result = await self.circuit_breaker.call(
                retry_with_backoff,
                _connect_impl,
                max_retries=self.max_retries,
                base_delay=self.retry_delay,
            )
            self.is_connected = result
            return result
        except Exception as e:
            self.logger.error(f"Failed to connect to {self.catalog_type} after retries: {e}")
            self.is_connected = False
            return False

    async def _connect_impl(self) -> bool:
        """Actual connection implementation - to be overridden by subclasses"""
        raise NotImplementedError

    async def search_data_products(self, filters: SearchFilters) -> List[DataProductMetadata]:
        """Search for data products with filters and resilience"""
        await self._ensure_healthy_connection()

        async def _search_impl():
            return await self._search_data_products_impl(filters)

        try:
            return await self.circuit_breaker.call(
                retry_with_backoff,
                _search_impl,
                max_retries=self.max_retries,
                base_delay=self.retry_delay,
            )
        except Exception as e:
            self.logger.error(f"Search failed for {self.catalog_type}: {e}")
            return []

    async def _search_data_products_impl(self, filters: SearchFilters) -> List[DataProductMetadata]:
        """Actual search implementation - to be overridden by subclasses"""
        raise NotImplementedError

    async def get_data_product(self, product_id: str) -> Optional[DataProductMetadata]:
        """Get detailed information about a specific data product with resilience"""
        await self._ensure_healthy_connection()

        async def _get_impl():
            return await self._get_data_product_impl(product_id)

        try:
            return await self.circuit_breaker.call(
                retry_with_backoff,
                _get_impl,
                max_retries=self.max_retries,
                base_delay=self.retry_delay,
            )
        except Exception as e:
            self.logger.error(f"Get product failed for {self.catalog_type}: {e}")
            return None

    async def _get_data_product_impl(self, product_id: str) -> Optional[DataProductMetadata]:
        """Actual get product implementation - to be overridden by subclasses"""
        # Default implementation - search and filter
        all_products = await self._search_data_products_impl(SearchFilters())
        for product in all_products:
            if product.id == product_id:
                return product
        return None

    async def get_catalog_stats(self) -> Dict[str, Any]:
        """Get catalog statistics with resilience"""
        await self._ensure_healthy_connection()

        async def _stats_impl():
            return await self._get_catalog_stats_impl()

        try:
            return await self.circuit_breaker.call(
                retry_with_backoff,
                _stats_impl,
                max_retries=self.max_retries,
                base_delay=self.retry_delay,
            )
        except Exception as e:
            self.logger.error(f"Get stats failed for {self.catalog_type}: {e}")
            return {"error": str(e), "available": False}

    async def _get_catalog_stats_impl(self) -> Dict[str, Any]:
        """Actual stats implementation - to be overridden by subclasses"""
        raise NotImplementedError

    async def _ensure_healthy_connection(self) -> None:
        """Ensure connection is healthy, reconnect if needed"""
        now = time.time()

        # Check if we need to perform a health check
        if (
            self.last_health_check is None
            or now - self.last_health_check > self.health_check_interval
        ):

            if not await self._health_check():
                self.logger.warning(
                    f"Health check failed for {self.catalog_type}, attempting reconnection"
                )
                await self.connect()

            self.last_health_check = now

    async def _health_check(self) -> bool:
        """Check if connection is healthy"""
        return self.is_connected

    def _apply_filters(
        self, products: List[DataProductMetadata], filters: SearchFilters
    ) -> List[DataProductMetadata]:
        """Apply search filters to product list"""
        filtered_products = []
        for product in products:
            if filters.domain and filters.domain.lower() not in product.domain.lower():
                continue
            if filters.owner and filters.owner.lower() not in product.owner.lower():
                continue
            if filters.layer and product.layer != filters.layer:
                continue
            if filters.status and product.status != filters.status:
                continue
            if filters.min_quality_score and (
                not product.quality_score or product.quality_score < filters.min_quality_score
            ):
                continue
            if filters.text_query:
                query_lower = filters.text_query.lower()
                if not any(
                    query_lower in text.lower()
                    for text in [
                        product.name,
                        product.description,
                        product.domain,
                        " ".join(product.tags),
                    ]
                ):
                    continue

            filtered_products.append(product)

        return filtered_products[: filters.limit]


class GoogleCloudDataCatalogConnector(BaseCatalogConnector):
    """Google Cloud Data Catalog connector"""

    async def _connect_impl(self) -> bool:
        """Connect to Google Cloud Data Catalog"""
        try:
            # Initialize Google Cloud Data Catalog client
            project_id = self.config.get("project_id")
            if not project_id:
                raise ValueError("project_id required for Google Cloud Data Catalog")

            self.logger.info(f"Connecting to Google Cloud Data Catalog (project: {project_id})")

            # In a real implementation, you would:
            # from google.cloud import datacatalog_v1
            # self.client = datacatalog_v1.DataCatalogClient()

            return True
        except Exception as e:
            self.logger.error(f"Failed to connect to Google Cloud Data Catalog: {e}")
            return False

    async def _search_data_products_impl(self, filters: SearchFilters) -> List[DataProductMetadata]:
        """Search Google Cloud Data Catalog"""
        # Mock implementation - in reality, this would use the Google Cloud Data Catalog API
        mock_products = [
            DataProductMetadata(
                id="gcp-customer-360-v2",
                name="Customer 360 Analytics",
                description="Comprehensive customer analytics dataset with 360-degree view",
                domain="marketing",
                owner="data-platform-team",
                layer=DataProductLayer.GOLD,
                status=DataProductStatus.ACTIVE,
                version="2.1.0",
                created_at=datetime(2024, 1, 15, tzinfo=timezone.utc),
                updated_at=datetime(2024, 10, 10, tzinfo=timezone.utc),
                tags=["customer", "analytics", "pii-compliant", "real-time"],
                schema_url="gs://data-catalog/schemas/customer-360-v2.json",
                documentation_url="https://docs.company.com/data/customer-360",
                api_endpoint="https://api.company.com/v2/customer-360",
                quality_score=0.96,
                catalog_source="Google Cloud Data Catalog",
                catalog_type="google_cloud_data_catalog",
            ),
            DataProductMetadata(
                id="gcp-sales-forecasting-v1",
                name="Sales Forecasting ML Dataset",
                description="ML-ready sales forecasting data with feature engineering",
                domain="sales",
                owner="ml-platform-team",
                layer=DataProductLayer.ANALYTICAL,
                status=DataProductStatus.ACTIVE,
                version="1.5.2",
                created_at=datetime(2024, 3, 20, tzinfo=timezone.utc),
                updated_at=datetime(2024, 10, 12, tzinfo=timezone.utc),
                tags=["sales", "ml", "forecasting", "time-series"],
                schema_url="gs://data-catalog/schemas/sales-forecasting-v1.json",
                quality_score=0.91,
                catalog_source="Google Cloud Data Catalog",
                catalog_type="google_cloud_data_catalog",
            ),
        ]

        return self._apply_filters(mock_products, filters)

    async def _get_catalog_stats_impl(self) -> Dict[str, Any]:
        """Get catalog statistics"""
        # Mock implementation
        return {"total_products": 50, "avg_quality": 0.92, "last_updated": "2024-10-15T10:00:00Z"}


class AWSGlueDataCatalogConnector(BaseCatalogConnector):
    """AWS Glue Data Catalog connector"""

    async def _connect_impl(self) -> bool:
        """Connect to AWS Glue Data Catalog"""
        try:
            region = self.config.get("region", "us-east-1")
            self.logger.info(f"Connecting to AWS Glue Data Catalog (region: {region})")

            # In a real implementation:
            # import boto3
            # self.client = boto3.client('glue', region_name=region)

            return True
        except Exception as e:
            self.logger.error(f"Failed to connect to AWS Glue Data Catalog: {e}")
            return False

    async def _search_data_products_impl(self, filters: SearchFilters) -> List[DataProductMetadata]:
        """Search AWS Glue Data Catalog"""
        # Mock implementation
        mock_products = [
            DataProductMetadata(
                id="aws-transaction-stream-v3",
                name="Real-time Transaction Stream",
                description="High-velocity transaction stream with fraud detection signals",
                domain="finance",
                owner="fintech-platform-team",
                layer=DataProductLayer.REAL_TIME,
                status=DataProductStatus.ACTIVE,
                version="3.0.1",
                created_at=datetime(2024, 2, 10, tzinfo=timezone.utc),
                updated_at=datetime(2024, 10, 14, tzinfo=timezone.utc),
                tags=["transactions", "real-time", "fraud-detection", "streaming"],
                schema_url="s3://data-catalog/schemas/transaction-stream-v3.json",
                quality_score=0.94,
                catalog_source="AWS Glue Data Catalog",
                catalog_type="aws_glue_data_catalog",
            )
        ]

        return self._apply_filters(mock_products, filters)

    async def _get_catalog_stats_impl(self) -> Dict[str, Any]:
        """Get catalog statistics"""
        return {"total_products": 25, "avg_quality": 0.88, "last_updated": "2024-10-15T09:30:00Z"}


class AzurePurviewConnector(BaseCatalogConnector):
    """Azure Purview connector"""

    async def _connect_impl(self) -> bool:
        """Connect to Azure Purview"""
        try:
            account_name = self.config.get("account_name")
            if not account_name:
                raise ValueError("account_name required for Azure Purview")

            self.logger.info(f"Connecting to Azure Purview (account: {account_name})")
            return True
        except Exception as e:
            self.logger.error(f"Failed to connect to Azure Purview: {e}")
            return False

    async def _search_data_products_impl(self, filters: SearchFilters) -> List[DataProductMetadata]:
        """Search Azure Purview"""
        # Mock implementation
        mock_products = [
            DataProductMetadata(
                id="azure-supply-chain-v1",
                name="Supply Chain Analytics",
                description="End-to-end supply chain visibility and analytics",
                domain="operations",
                owner="supply-chain-team",
                layer=DataProductLayer.SILVER,
                status=DataProductStatus.ACTIVE,
                version="1.2.0",
                created_at=datetime(2024, 4, 5, tzinfo=timezone.utc),
                updated_at=datetime(2024, 10, 8, tzinfo=timezone.utc),
                tags=["supply-chain", "logistics", "analytics", "operational"],
                schema_url="https://purview.azure.com/schemas/supply-chain-v1.json",
                quality_score=0.89,
                catalog_source="Azure Purview",
                catalog_type="azure_purview",
            )
        ]

        return self._apply_filters(mock_products, filters)

    async def _get_catalog_stats_impl(self) -> Dict[str, Any]:
        """Get catalog statistics"""
        return {"total_products": 18, "avg_quality": 0.83, "last_updated": "2024-10-15T08:45:00Z"}


class DataHubConnector(BaseCatalogConnector):
    """DataHub connector"""

    async def _connect_impl(self) -> bool:
        """Connect to DataHub"""
        try:
            server_url = self.config.get("server_url", "http://localhost:8080")
            self.logger.info(f"Connecting to DataHub (server: {server_url})")
            return True
        except Exception as e:
            self.logger.error(f"Failed to connect to DataHub: {e}")
            return False

    async def _search_data_products_impl(self, filters: SearchFilters) -> List[DataProductMetadata]:
        """Search DataHub"""
        # Mock implementation
        mock_products = [
            DataProductMetadata(
                id="datahub-user-behavior-v2",
                name="User Behavior Analytics",
                description="Comprehensive user behavior tracking and analytics dataset",
                domain="product",
                owner="product-analytics-team",
                layer=DataProductLayer.GOLD,
                status=DataProductStatus.ACTIVE,
                version="2.3.1",
                created_at=datetime(2024, 1, 30, tzinfo=timezone.utc),
                updated_at=datetime(2024, 10, 11, tzinfo=timezone.utc),
                tags=["user-behavior", "analytics", "product", "gdpr-compliant"],
                schema_url="http://datahub.company.com/schemas/user-behavior-v2.json",
                quality_score=0.93,
                catalog_source="DataHub",
                catalog_type="datahub",
            )
        ]

        return self._apply_filters(mock_products, filters)

    async def _get_catalog_stats_impl(self) -> Dict[str, Any]:
        """Get catalog statistics"""
        return {"total_products": 35, "avg_quality": 0.91, "last_updated": "2024-10-15T11:15:00Z"}


class ApacheAtlasConnector(BaseCatalogConnector):
    """Apache Atlas connector"""

    async def _connect_impl(self) -> bool:
        """Connect to Apache Atlas"""
        try:
            base_url = self.config.get("base_url", "http://localhost:21000")
            username = self.config.get("username", os.environ.get("ATLAS_USERNAME", ""))
            password = self.config.get("password", os.environ.get("ATLAS_PASSWORD", ""))

            if not username or not password:
                self.logger.error(
                    "Apache Atlas credentials required. Set 'username'/'password' in config or ATLAS_USERNAME/ATLAS_PASSWORD env vars."
                )
                return False

            self.logger.info(f"Connecting to Apache Atlas (server: {base_url})")

            # In a real implementation:
            # from atlasclient.client import Atlas
            # self.client = Atlas(base_url, username=username, password=password)

            return True
        except Exception as e:
            self.logger.error(f"Failed to connect to Apache Atlas: {e}")
            return False

    async def _search_data_products_impl(self, filters: SearchFilters) -> List[DataProductMetadata]:
        """Search Apache Atlas"""
        # Mock implementation
        mock_products = [
            DataProductMetadata(
                id="atlas-governance-dataset-v1",
                name="Governance Data Lineage",
                description="Enterprise data governance and lineage tracking dataset",
                domain="governance",
                owner="data-governance-team",
                layer=DataProductLayer.OPERATIONAL,
                status=DataProductStatus.ACTIVE,
                version="1.0.3",
                created_at=datetime(2024, 5, 15, tzinfo=timezone.utc),
                updated_at=datetime(2024, 10, 12, tzinfo=timezone.utc),
                tags=["governance", "lineage", "compliance", "metadata"],
                schema_url="http://atlas.company.com/api/atlas/v2/entity/governance-dataset-v1",
                quality_score=0.87,
                catalog_source="Apache Atlas",
                catalog_type="apache_atlas",
            ),
            DataProductMetadata(
                id="atlas-risk-assessment-v2",
                name="Risk Assessment Analytics",
                description="Comprehensive risk assessment data with ML predictions",
                domain="risk",
                owner="risk-management-team",
                layer=DataProductLayer.GOLD,
                status=DataProductStatus.ACTIVE,
                version="2.0.1",
                created_at=datetime(2024, 6, 20, tzinfo=timezone.utc),
                updated_at=datetime(2024, 10, 14, tzinfo=timezone.utc),
                tags=["risk", "ml", "predictions", "compliance"],
                schema_url="http://atlas.company.com/api/atlas/v2/entity/risk-assessment-v2",
                quality_score=0.92,
                catalog_source="Apache Atlas",
                catalog_type="apache_atlas",
            ),
        ]

        return self._apply_filters(mock_products, filters)

    async def _get_catalog_stats_impl(self) -> Dict[str, Any]:
        """Get catalog statistics"""
        return {"total_products": 42, "avg_quality": 0.89, "last_updated": "2024-10-15T09:00:00Z"}


class ConfluentSchemaRegistryConnector(BaseCatalogConnector):
    """Confluent Schema Registry connector"""

    async def _connect_impl(self) -> bool:
        """Connect to Confluent Schema Registry"""
        try:
            url = self.config.get("url", "http://localhost:8081")
            self.config.get("api_key")
            self.config.get("api_secret")

            self.logger.info(f"Connecting to Confluent Schema Registry (server: {url})")

            # In a real implementation:
            # from confluent_kafka.schema_registry import SchemaRegistryClient
            # auth = None
            # if api_key and api_secret:
            #     auth = (api_key, api_secret)
            # self.client = SchemaRegistryClient({'url': url, 'basic.auth.user.info': f'{api_key}:{api_secret}'})

            return True
        except Exception as e:
            self.logger.error(f"Failed to connect to Confluent Schema Registry: {e}")
            return False

    async def _search_data_products_impl(self, filters: SearchFilters) -> List[DataProductMetadata]:
        """Search Confluent Schema Registry"""
        # Mock implementation
        mock_products = [
            DataProductMetadata(
                id="confluent-events-stream-v3",
                name="Real-time Events Stream",
                description="High-throughput event streaming platform with schema evolution",
                domain="events",
                owner="streaming-platform-team",
                layer=DataProductLayer.REAL_TIME,
                status=DataProductStatus.ACTIVE,
                version="3.2.1",
                created_at=datetime(2024, 3, 10, tzinfo=timezone.utc),
                updated_at=datetime(2024, 10, 13, tzinfo=timezone.utc),
                tags=["streaming", "events", "real-time", "kafka"],
                schema_url="http://schema-registry.company.com/subjects/events-stream-v3",
                quality_score=0.95,
                catalog_source="Confluent Schema Registry",
                catalog_type="confluent_schema_registry",
            ),
            DataProductMetadata(
                id="confluent-audit-logs-v1",
                name="Audit Log Stream",
                description="Comprehensive audit logging for compliance and security",
                domain="security",
                owner="security-platform-team",
                layer=DataProductLayer.OPERATIONAL,
                status=DataProductStatus.ACTIVE,
                version="1.4.0",
                created_at=datetime(2024, 7, 5, tzinfo=timezone.utc),
                updated_at=datetime(2024, 10, 15, tzinfo=timezone.utc),
                tags=["audit", "security", "compliance", "logs"],
                schema_url="http://schema-registry.company.com/subjects/audit-logs-v1",
                quality_score=0.93,
                catalog_source="Confluent Schema Registry",
                catalog_type="confluent_schema_registry",
            ),
        ]

        return self._apply_filters(mock_products, filters)

    async def _get_catalog_stats_impl(self) -> Dict[str, Any]:
        """Get catalog statistics"""
        return {"total_products": 28, "avg_quality": 0.94, "last_updated": "2024-10-15T12:30:00Z"}


class CollibraConnector(BaseCatalogConnector):
    """Collibra connector"""

    async def _connect_impl(self) -> bool:
        """Connect to Collibra"""
        try:
            base_url = self.config.get("base_url")
            username = self.config.get("username")
            password = self.config.get("password")

            if not base_url:
                raise ValueError("base_url required for Collibra")
            if not username or not password:
                raise ValueError("username and password required for Collibra")

            self.logger.info(f"Connecting to Collibra (server: {base_url})")

            # In a real implementation:
            # import collibra_core
            # self.client = collibra_core.ApiClient(
            #     configuration=collibra_core.Configuration(
            #         host=base_url,
            #         username=username,
            #         password=password
            #     )
            # )

            return True
        except Exception as e:
            self.logger.error(f"Failed to connect to Collibra: {e}")
            return False

    async def _search_data_products_impl(self, filters: SearchFilters) -> List[DataProductMetadata]:
        """Search Collibra"""
        # Mock implementation
        mock_products = [
            DataProductMetadata(
                id="collibra-regulatory-reports-v2",
                name="Regulatory Reporting Dataset",
                description="Comprehensive regulatory reporting data with compliance tracking",
                domain="regulatory",
                owner="compliance-team",
                layer=DataProductLayer.GOLD,
                status=DataProductStatus.ACTIVE,
                version="2.1.5",
                created_at=datetime(2024, 2, 28, tzinfo=timezone.utc),
                updated_at=datetime(2024, 10, 11, tzinfo=timezone.utc),
                tags=["regulatory", "compliance", "reporting", "governance"],
                schema_url="https://collibra.company.com/asset/regulatory-reports-v2",
                documentation_url="https://collibra.company.com/docs/regulatory-reports",
                quality_score=0.98,
                catalog_source="Collibra",
                catalog_type="collibra",
            ),
            DataProductMetadata(
                id="collibra-master-data-v3",
                name="Master Data Management",
                description="Enterprise master data with golden records and data quality metrics",
                domain="master-data",
                owner="data-architecture-team",
                layer=DataProductLayer.GOLD,
                status=DataProductStatus.ACTIVE,
                version="3.0.2",
                created_at=datetime(2024, 4, 12, tzinfo=timezone.utc),
                updated_at=datetime(2024, 10, 9, tzinfo=timezone.utc),
                tags=["master-data", "golden-records", "data-quality", "enterprise"],
                schema_url="https://collibra.company.com/asset/master-data-v3",
                documentation_url="https://collibra.company.com/docs/master-data",
                quality_score=0.96,
                catalog_source="Collibra",
                catalog_type="collibra",
            ),
        ]

        return self._apply_filters(mock_products, filters)

    async def _get_catalog_stats_impl(self) -> Dict[str, Any]:
        """Get catalog statistics"""
        return {"total_products": 67, "avg_quality": 0.95, "last_updated": "2024-10-15T08:15:00Z"}


class AlationConnector(BaseCatalogConnector):
    """Alation connector"""

    async def _connect_impl(self) -> bool:
        """Connect to Alation"""
        try:
            base_url = self.config.get("base_url")
            api_token = self.config.get("api_token")

            if not base_url:
                raise ValueError("base_url required for Alation")
            if not api_token:
                raise ValueError("api_token required for Alation")

            self.logger.info(f"Connecting to Alation (server: {base_url})")

            # In a real implementation:
            # import requests
            # self.session = requests.Session()
            # self.session.headers.update({'Token': api_token})
            # self.base_url = base_url

            return True
        except Exception as e:
            self.logger.error(f"Failed to connect to Alation: {e}")
            return False

    async def _search_data_products_impl(self, filters: SearchFilters) -> List[DataProductMetadata]:
        """Search Alation"""
        # Mock implementation
        mock_products = [
            DataProductMetadata(
                id="alation-customer-insights-v4",
                name="Customer Insights Platform",
                description="Advanced customer analytics with behavioral insights and segmentation",
                domain="customer-analytics",
                owner="customer-insights-team",
                layer=DataProductLayer.ANALYTICAL,
                status=DataProductStatus.ACTIVE,
                version="4.1.2",
                created_at=datetime(2024, 1, 20, tzinfo=timezone.utc),
                updated_at=datetime(2024, 10, 8, tzinfo=timezone.utc),
                tags=["customer", "insights", "segmentation", "behavioral"],
                schema_url="https://alation.company.com/catalog/customer-insights-v4",
                documentation_url="https://alation.company.com/articles/customer-insights",
                api_endpoint="https://api.company.com/v4/customer-insights",
                quality_score=0.94,
                catalog_source="Alation",
                catalog_type="alation",
            ),
            DataProductMetadata(
                id="alation-financial-metrics-v2",
                name="Financial Metrics Dashboard",
                description="Real-time financial KPIs and performance metrics for executive reporting",
                domain="finance",
                owner="financial-analytics-team",
                layer=DataProductLayer.GOLD,
                status=DataProductStatus.ACTIVE,
                version="2.3.1",
                created_at=datetime(2024, 3, 5, tzinfo=timezone.utc),
                updated_at=datetime(2024, 10, 13, tzinfo=timezone.utc),
                tags=["finance", "kpis", "metrics", "executive"],
                schema_url="https://alation.company.com/catalog/financial-metrics-v2",
                documentation_url="https://alation.company.com/articles/financial-metrics",
                api_endpoint="https://api.company.com/v2/financial-metrics",
                quality_score=0.97,
                catalog_source="Alation",
                catalog_type="alation",
            ),
        ]

        return self._apply_filters(mock_products, filters)

    async def _get_catalog_stats_impl(self) -> Dict[str, Any]:
        """Get catalog statistics"""
        return {"total_products": 89, "avg_quality": 0.93, "last_updated": "2024-10-15T07:45:00Z"}


class CustomRestApiConnector(BaseCatalogConnector):
    """Custom REST API connector"""

    async def _connect_impl(self) -> bool:
        """Connect to Custom REST API"""
        try:
            base_url = self.config.get("base_url")
            auth_type = self.config.get("auth_type", "bearer")

            if not base_url:
                raise ValueError("base_url required for Custom REST API")

            self.logger.info(
                f"Connecting to Custom REST API (server: {base_url}, auth: {auth_type})"
            )

            # In a real implementation:
            # import aiohttp
            # self.session = aiohttp.ClientSession()
            # self.base_url = base_url
            #
            # # Setup authentication
            # if auth_type == 'bearer':
            #     token = self.config.get('auth_token')
            #     if token:
            #         self.session.headers.update({'Authorization': f'Bearer {token}'})
            # elif auth_type == 'basic':
            #     username = self.config.get('username')
            #     password = self.config.get('password')
            #     if username and password:
            #         import base64
            #         credentials = base64.b64encode(f'{username}:{password}'.encode()).decode()
            #         self.session.headers.update({'Authorization': f'Basic {credentials}'})
            # elif auth_type == 'api_key':
            #     api_key_header = self.config.get('api_key_header', 'X-API-Key')
            #     api_key = self.config.get('api_key')
            #     if api_key:
            #         self.session.headers.update({api_key_header: api_key})

            return True
        except Exception as e:
            self.logger.error(f"Failed to connect to Custom REST API: {e}")
            return False

    async def _search_data_products_impl(self, filters: SearchFilters) -> List[DataProductMetadata]:
        """Search Custom REST API"""
        # Mock implementation
        mock_products = [
            DataProductMetadata(
                id="custom-api-iot-sensors-v1",
                name="IoT Sensor Data Stream",
                description="Real-time IoT sensor data from manufacturing floor with predictive maintenance",
                domain="iot",
                owner="iot-platform-team",
                layer=DataProductLayer.REAL_TIME,
                status=DataProductStatus.ACTIVE,
                version="1.7.3",
                created_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
                updated_at=datetime(2024, 10, 14, tzinfo=timezone.utc),
                tags=["iot", "sensors", "manufacturing", "predictive-maintenance"],
                schema_url="https://api.company.com/v1/schemas/iot-sensors",
                documentation_url="https://docs.company.com/iot/sensors",
                api_endpoint="https://api.company.com/v1/iot/sensors",
                quality_score=0.89,
                catalog_source="Custom REST API",
                catalog_type="custom_rest_api",
            )
        ]

        return self._apply_filters(mock_products, filters)

    async def _get_catalog_stats_impl(self) -> Dict[str, Any]:
        """Get catalog statistics"""
        return {"total_products": 15, "avg_quality": 0.86, "last_updated": "2024-10-15T11:00:00Z"}


class CommandCenterConnector(BaseCatalogConnector):
    """
    FLUID Command Center connector

    Discovers published data products from Command Center's catalog.
    Note: This is different from marketplace blueprints - these are
    actual deployed data products with lineage, quality metrics, and SLAs.
    """

    async def _connect_impl(self) -> bool:
        """Connect to Command Center catalog"""
        try:
            # Import here to avoid circular dependency
            from ._command_center import get_command_center_client

            cc = get_command_center_client(logger=self.logger)

            if not cc.available or not cc.features.catalog:
                self.logger.warning("Command Center catalog not available")
                return False

            self.base_url = cc.get_catalog_url()
            self.cc_client = cc

            # Initialize aiohttp session
            import aiohttp

            self.session = aiohttp.ClientSession()

            self.logger.info(f"Connected to Command Center catalog: {self.base_url}")
            return True

        except ImportError as e:
            self.logger.error(f"Failed to import Command Center client: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Failed to connect to Command Center: {e}")
            return False

    async def _search_data_products_impl(self, filters: SearchFilters) -> List[DataProductMetadata]:
        """Search Command Center's data product catalog"""
        try:

            # Build query parameters
            params = {}

            if filters.query:
                params["query"] = filters.query
            if filters.domains:
                params["domains"] = ",".join(filters.domains)
            if filters.owners:
                params["owners"] = ",".join(filters.owners)
            if filters.tags:
                params["tags"] = ",".join(filters.tags)
            if filters.layers:
                params["layers"] = ",".join([layer.value for layer in filters.layers])
            if filters.statuses:
                params["statuses"] = ",".join([status.value for status in filters.statuses])
            if filters.min_quality_score:
                params["min_quality"] = filters.min_quality_score
            if filters.limit:
                params["limit"] = filters.limit
            if filters.offset:
                params["offset"] = filters.offset

            # Make request
            async with self.session.get(self.base_url, params=params) as response:
                if response.status != 200:
                    self.logger.warning(f"Command Center search failed: {response.status}")
                    return []

                data = await response.json()
                products = []

                # Map Command Center schema to DataProductMetadata
                for item in data.get("items", []):
                    try:
                        # Parse layer
                        layer = None
                        if "layer" in item:
                            try:
                                layer = DataProductLayer(item["layer"])
                            except ValueError:
                                pass

                        # Parse status
                        status = DataProductStatus.ACTIVE  # Default
                        if "status" in item:
                            try:
                                status = DataProductStatus(item["status"])
                            except ValueError:
                                pass

                        # Create metadata
                        product = DataProductMetadata(
                            id=item["id"],
                            name=item["name"],
                            description=item.get("description", ""),
                            domain=item.get("domain", "unknown"),
                            owner=(
                                item.get("owner", {}).get("name", "unknown")
                                if isinstance(item.get("owner"), dict)
                                else item.get("owner", "unknown")
                            ),
                            layer=layer,
                            status=status,
                            tags=item.get("tags", []),
                            quality_score=item.get("quality_score", 0.0),
                            created_at=item.get("created_at"),
                            updated_at=item.get("updated_at"),
                            data_location=item.get("data_location", ""),
                            schema_definition=item.get("schema", {}),
                            sla=item.get("sla", {}),
                            documentation_url=item.get("documentation_url"),
                            metadata={
                                "source": "command_center",
                                "cc_url": f"{self.base_url}/{item['id']}",
                                "version": item.get("version", "unknown"),
                                **item.get("metadata", {}),
                            },
                        )

                        products.append(product)

                    except Exception as e:
                        self.logger.warning(f"Failed to parse product {item.get('id')}: {e}")
                        continue

                return products

        except Exception as e:
            self.logger.error(f"Command Center search failed: {e}")
            return []

    async def _get_catalog_stats_impl(self) -> Dict[str, Any]:
        """Get Command Center catalog statistics"""
        try:

            stats_url = f"{self.base_url}/stats"

            async with self.session.get(stats_url) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    return {"total_products": 0, "total_domains": 0, "avg_quality_score": 0.0}

        except Exception as e:
            self.logger.warning(f"Failed to get Command Center stats: {e}")
            return {"total_products": 0, "total_domains": 0, "avg_quality_score": 0.0}


# ==========================================
# Caching and Performance Optimization
# ==========================================

import time as time_module
from collections import Counter, defaultdict
from collections import Counter as TypingCounter
from dataclasses import dataclass, field


@dataclass
class MetricsCollector:
    """Collect and manage metrics for monitoring"""

    search_requests: TypingCounter = field(default_factory=Counter)
    search_latency: dict = field(default_factory=lambda: defaultdict(list))
    connector_health: dict = field(default_factory=dict)
    cache_hits: TypingCounter = field(default_factory=Counter)
    cache_misses: TypingCounter = field(default_factory=Counter)
    error_counts: TypingCounter = field(default_factory=Counter)
    connection_pool_stats: dict = field(default_factory=dict)
    circuit_breaker_stats: dict = field(default_factory=dict)

    def record_search_request(self, catalog_type: str, latency: float):
        """Record a search request"""
        self.search_requests[catalog_type] += 1
        self.search_latency[catalog_type].append(latency)

    def record_cache_hit(self, catalog_type: str):
        """Record a cache hit"""
        self.cache_hits[catalog_type] += 1

    def record_cache_miss(self, catalog_type: str):
        """Record a cache miss"""
        self.cache_misses[catalog_type] += 1

    def record_error(self, catalog_type: str, error_type: str):
        """Record an error"""
        self.error_counts[f"{catalog_type}:{error_type}"] += 1

    def update_connector_health(
        self, catalog_type: str, is_healthy: bool, response_time: float = None
    ):
        """Update connector health status"""
        self.connector_health[catalog_type] = {
            "healthy": is_healthy,
            "last_check": time_module.time(),
            "response_time": response_time,
        }

    def update_connection_pool_stats(self, catalog_type: str, active: int, idle: int, total: int):
        """Update connection pool statistics"""
        self.connection_pool_stats[catalog_type] = {
            "active_connections": active,
            "idle_connections": idle,
            "total_connections": total,
            "timestamp": time_module.time(),
        }

    def update_circuit_breaker_stats(
        self, catalog_type: str, state: str, failure_count: int, success_count: int
    ):
        """Update circuit breaker statistics"""
        self.circuit_breaker_stats[catalog_type] = {
            "state": state,
            "failure_count": failure_count,
            "success_count": success_count,
            "timestamp": time_module.time(),
        }

    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of all metrics"""
        # Calculate average latencies
        avg_latencies = {}
        for catalog_type, latencies in self.search_latency.items():
            if latencies:
                avg_latencies[catalog_type] = sum(latencies) / len(latencies)

        # Calculate cache hit rates
        cache_hit_rates = {}
        for catalog_type in set(list(self.cache_hits.keys()) + list(self.cache_misses.keys())):
            hits = self.cache_hits[catalog_type]
            misses = self.cache_misses[catalog_type]
            total = hits + misses
            if total > 0:
                cache_hit_rates[catalog_type] = hits / total

        return {
            "search_requests": dict(self.search_requests),
            "average_latencies": avg_latencies,
            "cache_hit_rates": cache_hit_rates,
            "connector_health": self.connector_health,
            "connection_pool_stats": self.connection_pool_stats,
            "circuit_breaker_stats": self.circuit_breaker_stats,
            "error_counts": dict(self.error_counts),
            "timestamp": time_module.time(),
        }


# Global metrics collector instance
metrics_collector = MetricsCollector()


class HealthChecker:
    """System health checker with comprehensive monitoring"""

    def __init__(self, connectors: Dict[str, BaseCatalogConnector]):
        self.connectors = connectors
        self.logger = logging.getLogger(__name__)

    async def check_system_health(self) -> Dict[str, Any]:
        """Check overall system health"""
        health_report = {
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "connectors": {},
            "metrics": metrics_collector.get_summary(),
            "overall_health_score": 0.0,
        }

        healthy_connectors = 0
        total_connectors = len(self.connectors)

        # Check each connector
        for name, connector in self.connectors.items():
            try:
                start_time = time_module.time()
                is_healthy = await connector._health_check()
                response_time = time_module.time() - start_time

                health_report["connectors"][name] = {
                    "status": "healthy" if is_healthy else "unhealthy",
                    "response_time": response_time,
                    "circuit_breaker_state": (
                        connector.circuit_breaker.state
                        if hasattr(connector, "circuit_breaker")
                        else "unknown"
                    ),
                }

                if is_healthy:
                    healthy_connectors += 1

                metrics_collector.update_connector_health(name, is_healthy, response_time)

            except Exception as e:
                self.logger.error(f"Health check failed for {name}: {e}")
                health_report["connectors"][name] = {
                    "status": "error",
                    "error": str(e),
                    "response_time": None,
                }
                metrics_collector.record_error(name, "health_check_failed")

        # Calculate overall health score
        if total_connectors > 0:
            health_report["overall_health_score"] = healthy_connectors / total_connectors

            # Determine overall status
            if healthy_connectors == 0:
                health_report["status"] = "critical"
            elif healthy_connectors < total_connectors * 0.5:
                health_report["status"] = "degraded"
            elif healthy_connectors < total_connectors:
                health_report["status"] = "partial"
            else:
                health_report["status"] = "healthy"

        return health_report

    async def check_connector_health(self, connector_name: str) -> Dict[str, Any]:
        """Check health of a specific connector"""
        if connector_name not in self.connectors:
            return {"status": "not_found", "message": f"Connector '{connector_name}' not found"}

        connector = self.connectors[connector_name]
        try:
            start_time = time_module.time()
            is_healthy = await connector._health_check()
            response_time = time_module.time() - start_time

            return {
                "status": "healthy" if is_healthy else "unhealthy",
                "response_time": response_time,
                "circuit_breaker_state": (
                    connector.circuit_breaker.state
                    if hasattr(connector, "circuit_breaker")
                    else "unknown"
                ),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except Exception as e:
            self.logger.error(f"Health check failed for {connector_name}: {e}")
            return {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }


@dataclass
class PerformanceMonitor:
    """Monitor and track performance metrics"""

    slow_query_threshold: float = 5.0  # seconds

    def __post_init__(self):
        self.logger = logging.getLogger(__name__)
        self.slow_queries: List[Dict[str, Any]] = []

    async def monitor_search(self, catalog_type: str, search_func, *args, **kwargs):
        """Monitor a search operation"""
        start_time = time_module.time()

        try:
            result = await search_func(*args, **kwargs)

            end_time = time_module.time()
            latency = end_time - start_time

            # Record metrics
            metrics_collector.record_search_request(catalog_type, latency)

            # Check for slow queries
            if latency > self.slow_query_threshold:
                slow_query = {
                    "catalog_type": catalog_type,
                    "latency": latency,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "args": str(args)[:200],  # Limit length
                    "kwargs": str({k: str(v)[:100] for k, v in kwargs.items()})[:200],
                }
                self.slow_queries.append(slow_query)
                self.logger.warning(f"Slow query detected: {catalog_type} took {latency:.2f}s")

                # Keep only recent slow queries (last 100)
                if len(self.slow_queries) > 100:
                    self.slow_queries = self.slow_queries[-100:]

            return result

        except Exception as e:
            end_time = time_module.time()
            latency = end_time - start_time

            metrics_collector.record_error(catalog_type, type(e).__name__)
            self.logger.error(f"Search failed for {catalog_type} after {latency:.2f}s: {e}")
            raise

    def get_slow_queries(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent slow queries"""
        return self.slow_queries[-limit:]

    def get_performance_summary(self) -> Dict[str, Any]:
        """Get performance summary"""
        summary = metrics_collector.get_summary()

        # Add slow query information
        summary["slow_queries"] = {
            "count": len(self.slow_queries),
            "threshold": self.slow_query_threshold,
            "recent": self.get_slow_queries(5),
        }

        return summary


# Global performance monitor instance
performance_monitor = PerformanceMonitor()

# ==========================================
# Original Caching Implementation
# ==========================================


@dataclass
class CacheEntry:
    """Cache entry with expiration"""

    data: Any
    created_at: datetime
    ttl_minutes: int

    def is_expired(self) -> bool:
        """Check if cache entry is expired"""
        return datetime.now(timezone.utc) > self.created_at + timedelta(minutes=self.ttl_minutes)


class MarketCache:
    """In-memory cache for market data with TTL support"""

    def __init__(self, max_entries: int = 1000, default_ttl_minutes: int = 15):
        self.cache: Dict[str, CacheEntry] = {}
        self.max_entries = max_entries
        self.default_ttl_minutes = default_ttl_minutes
        self.stats = {"hits": 0, "misses": 0, "evictions": 0}

    def _generate_key(self, method: str, catalog_type: str, filters: SearchFilters) -> str:
        """Generate cache key for method and filters"""
        filter_dict = {
            "domain": filters.domain,
            "owner": filters.owner,
            "layer": filters.layer.value if filters.layer else None,
            "status": filters.status.value if filters.status else None,
            "tags": sorted(filters.tags) if filters.tags else None,
            "text_query": filters.text_query,
            "min_quality_score": filters.min_quality_score,
            "created_after": filters.created_after.isoformat() if filters.created_after else None,
            "created_before": (
                filters.created_before.isoformat() if filters.created_before else None
            ),
            "limit": filters.limit,
            "offset": filters.offset,
        }
        key_data = f"{method}:{catalog_type}:{json.dumps(filter_dict, sort_keys=True)}"
        return hashlib.md5(key_data.encode()).hexdigest()

    def get(self, method: str, catalog_type: str, filters: SearchFilters) -> Optional[Any]:
        """Get cached data if available and not expired"""
        key = self._generate_key(method, catalog_type, filters)

        if key in self.cache:
            entry = self.cache[key]
            if not entry.is_expired():
                self.stats["hits"] += 1
                return entry.data
            else:
                # Remove expired entry
                del self.cache[key]

        self.stats["misses"] += 1
        return None

    def set(
        self,
        method: str,
        catalog_type: str,
        filters: SearchFilters,
        data: Any,
        ttl_minutes: Optional[int] = None,
    ) -> None:
        """Store data in cache with TTL"""
        key = self._generate_key(method, catalog_type, filters)
        ttl = ttl_minutes or self.default_ttl_minutes

        # Evict oldest entries if at capacity
        if len(self.cache) >= self.max_entries:
            # Remove oldest entry
            oldest_key = min(self.cache.keys(), key=lambda k: self.cache[k].created_at)
            del self.cache[oldest_key]
            self.stats["evictions"] += 1

        self.cache[key] = CacheEntry(
            data=data, created_at=datetime.now(timezone.utc), ttl_minutes=ttl
        )

    def clear(self) -> None:
        """Clear all cache entries"""
        self.cache.clear()
        self.stats = {"hits": 0, "misses": 0, "evictions": 0}

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        total_requests = self.stats["hits"] + self.stats["misses"]
        hit_ratio = self.stats["hits"] / total_requests if total_requests > 0 else 0.0

        return {
            "size": len(self.cache),
            "max_entries": self.max_entries,
            "hit_ratio": hit_ratio,
            "hits": self.stats["hits"],
            "misses": self.stats["misses"],
            "evictions": self.stats["evictions"],
        }


class ConnectionPool:
    """Simple connection pool for catalog connectors"""

    def __init__(self, max_connections: int = 10):
        self.max_connections = max_connections
        self.pools: Dict[str, asyncio.Queue] = {}
        self.active_connections: Dict[str, int] = {}

    async def get_connector(self, catalog_type: str, connector_factory) -> Any:
        """Get connector from pool or create new one"""
        if catalog_type not in self.pools:
            self.pools[catalog_type] = asyncio.Queue(maxsize=self.max_connections)
            self.active_connections[catalog_type] = 0

        pool = self.pools[catalog_type]

        try:
            # Try to get existing connector from pool
            connector = pool.get_nowait()
            return connector
        except asyncio.QueueEmpty:
            # Create new connector if under limit
            if self.active_connections[catalog_type] < self.max_connections:
                connector = await connector_factory()
                self.active_connections[catalog_type] += 1
                return connector
            else:
                # Wait for available connector
                connector = await pool.get()
                return connector

    async def return_connector(self, catalog_type: str, connector: Any) -> None:
        """Return connector to pool"""
        if catalog_type in self.pools:
            try:
                self.pools[catalog_type].put_nowait(connector)
            except asyncio.QueueFull:
                # Pool is full, discard connector
                self.active_connections[catalog_type] -= 1


# ==========================================
# Market Discovery Engine
# ==========================================


class MarketDiscoveryEngine:
    """
    Unified data product discovery engine that searches across
    multiple data catalogs and marketplaces with caching, performance optimization,
    and comprehensive monitoring
    """

    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.connectors: Dict[str, BaseCatalogConnector] = {}
        self.console = Console() if RICH_AVAILABLE else None

        # Initialize caching and connection pooling
        cache_config = config.get("cache", {})
        self.cache_enabled = cache_config.get("enabled", True)
        if self.cache_enabled:
            self.cache = MarketCache(
                max_entries=cache_config.get("max_entries", 1000),
                default_ttl_minutes=cache_config.get("ttl_minutes", 15),
            )
        else:
            self.cache = None

        self.connection_pool = ConnectionPool(max_connections=10)
        self.start_time = time_module.time()
        self.request_count = 0
        self.error_count = 0

        # Initialize monitoring components
        self.health_checker = None  # Will be initialized after connectors are ready
        self.performance_monitor = performance_monitor
        self.error_count = 0

    async def initialize_connectors(self, catalog_types: List[str] = None):
        """Initialize connectors for specified catalog types"""
        if catalog_types is None:
            catalog_types = self.config.get("catalogs", [])

        connector_classes = {
            "google_cloud_data_catalog": GoogleCloudDataCatalogConnector,
            "aws_glue_data_catalog": AWSGlueDataCatalogConnector,
            "azure_purview": AzurePurviewConnector,
            "datahub": DataHubConnector,
            "apache_atlas": ApacheAtlasConnector,
            "confluent_schema_registry": ConfluentSchemaRegistryConnector,
            "collibra": CollibraConnector,
            "alation": AlationConnector,
            "custom_rest_api": CustomRestApiConnector,
            "fluid_command_center": CommandCenterConnector,  # NEW: Command Center catalog
        }

        for catalog_type in catalog_types:
            if catalog_type in connector_classes:
                catalog_config = self.config.get(catalog_type, {})
                connector = connector_classes[catalog_type](catalog_config, self.logger)

                if await connector.connect():
                    self.connectors[catalog_type] = connector
                    self.logger.info(f"✅ Connected to {catalog_type}")
                else:
                    self.logger.warning(f"❌ Failed to connect to {catalog_type}")

        # Initialize health checker after connectors are ready
        if self.connectors:
            self.health_checker = HealthChecker(self.connectors)
            self.logger.info(f"🔍 Initialized health checker for {len(self.connectors)} connectors")

    async def advanced_search(self, filters: SearchFilters) -> SearchResult:
        """Enhanced search with advanced features, ranking, and faceting"""
        start_time = time_module.time()

        # Save search if requested
        if filters.save_search and filters.search_name:
            advanced_search_engine.save_search(filters)

        # Perform basic search across all catalogs
        catalog_results = await self.search_all_catalogs(filters)

        # Combine all results
        all_products = []
        for products in catalog_results.values():
            all_products.extend(products)

        # Apply advanced filters
        filtered_products = advanced_search_engine.apply_advanced_filters(all_products, filters)

        # Extract facets from all products (before ranking/sorting)
        facets = advanced_search_engine.extract_facets(filtered_products)

        # Rank and sort products
        ranked_products = advanced_search_engine.rank_and_sort_products(filtered_products, filters)

        # Apply pagination
        total_count = len(ranked_products)
        start_index = filters.offset
        end_index = start_index + filters.limit
        paginated_products = ranked_products[start_index:end_index]

        # Generate search suggestions
        suggestions = []
        if filters.text_query and len(filtered_products) < 5:  # Only suggest if few results
            suggestions = advanced_search_engine.generate_search_suggestions(
                all_products, filters.text_query
            )

        query_time = time_module.time() - start_time

        # Create search result
        result = SearchResult(
            products=paginated_products,
            total_count=total_count,
            facets=facets,
            query_time=query_time,
            suggestions=suggestions,
            ranking_info={
                "sort_by": filters.sort_by,
                "sort_order": filters.sort_order,
                "has_text_query": bool(filters.text_query),
                "relevance_scoring": filters.sort_by == "relevance" and bool(filters.text_query),
            },
        )

        # Record search in history
        advanced_search_engine.search_history.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "query": filters.text_query,
                "total_results": total_count,
                "query_time": query_time,
            }
        )

        # Keep only last 100 searches in history
        if len(advanced_search_engine.search_history) > 100:
            advanced_search_engine.search_history = advanced_search_engine.search_history[-100:]

        return result

    async def search_all_catalogs(
        self, filters: SearchFilters
    ) -> Dict[str, List[DataProductMetadata]]:
        """Search across all connected catalogs with caching support"""
        results = {}

        if self.console and RICH_AVAILABLE:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=self.console,
            ) as progress:
                for catalog_name, connector in self.connectors.items():
                    task = progress.add_task(f"Searching {catalog_name}...", total=1)
                    try:
                        self.request_count += 1

                        # Check cache first
                        cached_result = None
                        if self.cache_enabled and self.cache:
                            cached_result = self.cache.get(
                                "search_data_products", catalog_name, filters
                            )

                        if cached_result is not None:
                            results[catalog_name] = cached_result
                            self.logger.debug(f"Cache hit for {catalog_name}")
                            metrics_collector.record_cache_hit(catalog_name)
                        else:
                            # Fetch from catalog with monitoring
                            products = await self.performance_monitor.monitor_search(
                                catalog_name, self._search_with_timeout, connector, filters
                            )
                            results[catalog_name] = products
                            metrics_collector.record_cache_miss(catalog_name)

                            # Cache the result
                            if self.cache_enabled and self.cache:
                                self.cache.set(
                                    "search_data_products", catalog_name, filters, products
                                )
                                self.logger.debug(f"Cached results for {catalog_name}")

                        progress.update(task, completed=1)
                    except Exception as e:
                        self.error_count += 1
                        self.logger.error(f"Error searching {catalog_name}: {e}")
                        results[catalog_name] = []
                        progress.update(task, completed=1)
        else:
            for catalog_name, connector in self.connectors.items():
                try:
                    self.request_count += 1
                    self.logger.info(f"Searching {catalog_name}...")

                    # Check cache first
                    cached_result = None
                    if self.cache_enabled and self.cache:
                        cached_result = self.cache.get(
                            "search_data_products", catalog_name, filters
                        )

                    if cached_result is not None:
                        results[catalog_name] = cached_result
                        self.logger.debug(f"Cache hit for {catalog_name}")
                        metrics_collector.record_cache_hit(catalog_name)
                    else:
                        # Fetch from catalog with monitoring
                        products = await self.performance_monitor.monitor_search(
                            catalog_name, self._search_with_timeout, connector, filters
                        )
                        results[catalog_name] = products
                        metrics_collector.record_cache_miss(catalog_name)

                        # Cache the result
                        if self.cache_enabled and self.cache:
                            self.cache.set("search_data_products", catalog_name, filters, products)
                            self.logger.debug(f"Cached results for {catalog_name}")

                except Exception as e:
                    self.error_count += 1
                    self.logger.error(f"Error searching {catalog_name}: {e}")
                    results[catalog_name] = []

        return results

    async def _search_with_timeout(
        self, connector: BaseCatalogConnector, filters: SearchFilters
    ) -> List[DataProductMetadata]:
        """Search with timeout and error handling"""
        timeout_seconds = self.config.get("defaults", {}).get("timeout_seconds", 30)

        try:
            return await asyncio.wait_for(
                connector.search_data_products(filters), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            self.logger.warning(f"Search timeout for {connector.catalog_type}")
            return []
        except Exception as e:
            self.logger.error(f"Search error for {connector.catalog_type}: {e}")
            return []

    def get_performance_stats(self) -> Dict[str, Any]:
        """Get performance statistics"""
        runtime = time.time() - self.start_time
        success_rate = (
            (self.request_count - self.error_count) / self.request_count
            if self.request_count > 0
            else 0.0
        )

        stats = {
            "runtime_seconds": runtime,
            "total_requests": self.request_count,
            "error_count": self.error_count,
            "success_rate": success_rate,
            "requests_per_second": self.request_count / runtime if runtime > 0 else 0,
            "connected_catalogs": len(self.connectors),
        }

        if self.cache_enabled and self.cache:
            stats["cache"] = self.cache.get_stats()

        return stats

    def aggregate_results(
        self, catalog_results: Dict[str, List[DataProductMetadata]]
    ) -> List[DataProductMetadata]:
        """Aggregate and deduplicate results from multiple catalogs"""
        all_products = []
        seen_ids = set()

        for catalog_name, products in catalog_results.items():
            for product in products:
                # Simple deduplication by ID
                if product.id not in seen_ids:
                    all_products.append(product)
                    seen_ids.add(product.id)

        # Sort by quality score (descending) then by name
        all_products.sort(key=lambda p: (-p.quality_score if p.quality_score else 0, p.name))

        return all_products


# ==========================================
# Output Formatters
# ==========================================


def format_table_output(
    products: List[DataProductMetadata], console: Optional[Console] = None
) -> None:
    """Format products as a rich table"""
    if not products:
        if console:
            console.print("[yellow]No data products found matching your criteria.[/yellow]")
        else:
            cprint("No data products found matching your criteria.")
        return

    if console and RICH_AVAILABLE:
        table = Table(title="🏪 Data Product Marketplace")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Name", style="bold")
        table.add_column("Domain", style="green")
        table.add_column("Layer", style="blue")
        table.add_column("Owner", style="magenta")
        table.add_column("Quality", justify="center")
        table.add_column("Version", justify="center")
        table.add_column("Source", style="dim")

        for product in products:
            quality_str = f"{product.quality_score:.2f}" if product.quality_score else "N/A"
            quality_color = (
                "green"
                if product.quality_score and product.quality_score >= 0.9
                else "yellow" if product.quality_score and product.quality_score >= 0.7 else "red"
            )

            table.add_row(
                product.id,
                product.name,
                product.domain,
                product.layer.value,
                product.owner,
                f"[{quality_color}]{quality_str}[/{quality_color}]",
                product.version,
                product.catalog_source,
            )

        console.print(table)
    else:
        # Fallback text output
        cprint("\n🏪 Data Product Marketplace\n")
        cprint(
            f"{'ID':<25} {'Name':<30} {'Domain':<15} {'Layer':<12} {'Quality':<8} {'Source':<20}"
        )
        cprint("-" * 120)

        for product in products:
            quality_str = f"{product.quality_score:.2f}" if product.quality_score else "N/A"
            cprint(
                f"{product.id:<25} {product.name[:29]:<30} {product.domain:<15} {product.layer.value:<12} {quality_str:<8} {product.catalog_source:<20}"
            )


def format_detailed_output(product: DataProductMetadata, console: Optional[Console] = None) -> None:
    """Format detailed product information"""
    if console and RICH_AVAILABLE:
        panel_content = f"""
[bold cyan]ID:[/bold cyan] {product.id}
[bold cyan]Name:[/bold cyan] {product.name}
[bold cyan]Description:[/bold cyan] {product.description}

[bold green]Metadata:[/bold green]
• Domain: {product.domain}
• Owner: {product.owner}
• Layer: {product.layer.value}
• Status: {product.status.value}
• Version: {product.version}
• Quality Score: {product.quality_score if product.quality_score else 'N/A'}

[bold blue]Timestamps:[/bold blue]
• Created: {product.created_at.strftime('%Y-%m-%d %H:%M:%S UTC')}
• Updated: {product.updated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}

[bold magenta]Tags:[/bold magenta] {', '.join(product.tags) if product.tags else 'None'}

[bold yellow]Resources:[/bold yellow]
• Schema: {product.schema_url or 'Not available'}
• Documentation: {product.documentation_url or 'Not available'}
• API Endpoint: {product.api_endpoint or 'Not available'}

[bold dim]Source:[/bold dim] {product.catalog_source} ({product.catalog_type})
        """

        console.print(Panel(panel_content, title=f"📊 {product.name}", border_style="blue"))
    else:
        # Fallback text output
        cprint(f"\n📊 {product.name}")
        cprint("=" * 60)
        cprint(f"ID: {product.id}")
        cprint(f"Description: {product.description}")
        cprint(f"Domain: {product.domain}")
        cprint(f"Owner: {product.owner}")
        cprint(f"Layer: {product.layer.value}")
        cprint(f"Status: {product.status.value}")
        cprint(f"Version: {product.version}")
        cprint(f"Quality Score: {product.quality_score if product.quality_score else 'N/A'}")
        cprint(f"Tags: {', '.join(product.tags) if product.tags else 'None'}")
        cprint(f"Source: {product.catalog_source}")


def format_json_output(products: List[DataProductMetadata]) -> str:
    """Format products as JSON"""
    product_dicts = []
    for product in products:
        product_dict = {
            "id": product.id,
            "name": product.name,
            "description": product.description,
            "domain": product.domain,
            "owner": product.owner,
            "layer": product.layer.value,
            "status": product.status.value,
            "version": product.version,
            "created_at": product.created_at.isoformat(),
            "updated_at": product.updated_at.isoformat(),
            "tags": product.tags,
            "schema_url": product.schema_url,
            "documentation_url": product.documentation_url,
            "api_endpoint": product.api_endpoint,
            "quality_score": product.quality_score,
            "catalog_source": product.catalog_source,
            "catalog_type": product.catalog_type,
        }
        product_dicts.append(product_dict)

    return json.dumps(product_dicts, indent=2)


# ==========================================
# CLI Command Registration & Execution
# ==========================================


def register(subparsers: argparse._SubParsersAction):
    """Register the market command"""
    p = subparsers.add_parser(
        COMMAND,
        help="Discover data products from enterprise catalogs and marketplaces",
        epilog="""
🏪 FLUID Market - Enterprise Data Product Discovery

The market command provides unified access to enterprise data catalogs and
marketplaces, enabling easy discovery of published data products across
your organization and external data providers.

Examples:
  # Browse all available data products
  fluid market

  # Search by domain
  fluid market --domain marketing --domain sales

  # Filter by layer and quality
  fluid market --layer gold --min-quality 0.9

  # Search for specific terms
  fluid market --search "customer analytics"

  # Filter by owner and status
  fluid market --owner data-platform-team --status active

  # Get detailed information about a specific product
  fluid market --product-id customer-360-v2 --detailed

  # Export results to JSON
  fluid market --domain finance --format json --output finance_products.json

  # Search specific catalogs only
  fluid market --catalogs google_cloud_data_catalog,datahub

  # Show marketplace statistics
  fluid market --marketplace-stats

Advanced Examples:
  # Comprehensive search with multiple filters
  fluid market \\
    --domain marketing,sales \\
    --layer gold,analytical \\
    --tags customer,real-time \\
    --min-quality 0.8 \\
    --created-after 2024-01-01

  # Enterprise-wide discovery for AI/ML use cases
  fluid market \\
    --search "machine learning feature" \\
    --layer analytical,gold \\
    --format json \\
    --output ml_datasets.json

  # Compliance and governance discovery
  fluid market \\
    --tags gdpr-compliant,pii-approved \\
    --min-quality 0.95 \\
    --status active

Supported Catalogs:
  • Google Cloud Data Catalog
  • AWS Glue Data Catalog  
  • Azure Purview
  • Apache Atlas
  • DataHub
  • Confluent Schema Registry
  • Collibra
  • Alation
  • Custom REST API catalogs

The market command makes data discovery a simple, command-line-driven process,
allowing engineers and AI agents to easily find the upstream dependencies
they need for their data products.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Core search arguments
    search_group = p.add_argument_group("Search & Discovery")
    search_group.add_argument(
        "--search", "-s", help="Text search across product names, descriptions, and tags"
    )
    search_group.add_argument(
        "--domain", "-d", action="append", help="Filter by domain(s) (can be used multiple times)"
    )
    search_group.add_argument(
        "--owner", "-o", action="append", help="Filter by owner(s) (can be used multiple times)"
    )
    search_group.add_argument(
        "--layer",
        "-l",
        choices=[layer.value for layer in DataProductLayer],
        action="append",
        help="Filter by data layer(s) (can be used multiple times)",
    )
    search_group.add_argument(
        "--status",
        choices=[status.value for status in DataProductStatus],
        action="append",
        help="Filter by status(es) (can be used multiple times)",
    )
    search_group.add_argument(
        "--tags", "-t", action="append", help="Filter by tags (can be used multiple times)"
    )

    # Quality and date filters
    filter_group = p.add_argument_group("Quality & Date Filters")
    filter_group.add_argument(
        "--min-quality", type=float, help="Minimum quality score (0.0 to 1.0)"
    )
    filter_group.add_argument(
        "--created-after", help="Show products created after date (YYYY-MM-DD)"
    )
    filter_group.add_argument(
        "--created-before", help="Show products created before date (YYYY-MM-DD)"
    )

    # Catalog selection
    catalog_group = p.add_argument_group("Catalog Selection")
    catalog_group.add_argument(
        "--catalogs", help="Comma-separated list of catalogs to search (default: all configured)"
    )
    catalog_group.add_argument(
        "--list-catalogs",
        action="store_true",
        help="List available catalog types and configurations",
    )

    # Output and formatting
    output_group = p.add_argument_group("Output & Formatting")
    output_group.add_argument(
        "--format",
        "-f",
        choices=["table", "json", "detailed"],
        default="table",
        help="Output format (default: table)",
    )
    output_group.add_argument("--output", "-O", help="Output file path (default: stdout)")
    output_group.add_argument(
        "--limit", type=int, default=50, help="Maximum number of results per catalog (default: 50)"
    )
    output_group.add_argument(
        "--offset", type=int, default=0, help="Offset for pagination (default: 0)"
    )

    # Specific product details
    detail_group = p.add_argument_group("Product Details")
    detail_group.add_argument(
        "--product-id", help="Get detailed information about a specific product"
    )
    detail_group.add_argument(
        "--detailed", action="store_true", help="Show detailed information for all results"
    )

    # Statistics and info
    info_group = p.add_argument_group("Information & Statistics")
    info_group.add_argument(
        "--marketplace-stats", action="store_true", help="Show marketplace statistics and summary"
    )
    info_group.add_argument(
        "--config-template",
        action="store_true",
        help="Generate configuration template for catalog connections",
    )

    # Blueprint marketplace (absorbed from 'fluid marketplace')
    blueprint_group = p.add_argument_group("Blueprints")
    blueprint_group.add_argument(
        "--blueprints",
        action="store_true",
        help="Search blueprint marketplace templates instead of catalogs",
    )
    blueprint_group.add_argument(
        "--blueprint-id",
        help="Get info or instantiate a specific blueprint",
    )
    blueprint_group.add_argument(
        "--instantiate",
        action="store_true",
        help="Instantiate a blueprint (requires --blueprint-id)",
    )

    p.set_defaults(cmd=COMMAND, func=run)


async def run_market_discovery(args, logger: logging.Logger) -> int:
    """Main market discovery execution"""
    try:
        # Load configuration
        config = load_market_config(args, logger)

        # Initialize discovery engine
        engine = MarketDiscoveryEngine(config, logger)

        # Handle special operations first
        if args.list_catalogs:
            return handle_list_catalogs(config, logger)

        if args.config_template:
            return handle_config_template(logger)

        if args.marketplace_stats:
            return await handle_marketplace_stats(engine, logger)

        # Initialize connectors
        catalog_types = None
        if args.catalogs:
            catalog_types = [cat.strip() for cat in args.catalogs.split(",")]

        await engine.initialize_connectors(catalog_types)

        if not engine.connectors:
            logger.error("❌ No catalog connectors available. Check your configuration.")
            return 1

        # Handle specific product lookup
        if args.product_id:
            return await handle_product_details(engine, args.product_id, args, logger)

        # Build search filters
        filters = build_search_filters(args)

        # Execute search across catalogs
        logger.info("🔍 Searching data product marketplaces...")
        catalog_results = await engine.search_all_catalogs(filters)

        # Aggregate results
        all_products = engine.aggregate_results(catalog_results)

        # Generate output
        return generate_output(all_products, args, engine.console, logger)

    except Exception as e:
        logger.error(f"💥 Market discovery failed: {e}")
        if args.debug:
            import traceback

            logger.error(traceback.format_exc())
        return 1


def load_market_config(args, logger: logging.Logger) -> Dict[str, Any]:
    """Load market configuration from various sources with proper precedence"""
    config = {}

    # 1. Load default configuration
    default_config = {
        "catalogs": [],  # Will be populated from available providers
        "defaults": {
            "limit": 50,
            "min_quality_score": 0.7,
            "include_deprecated": False,
            "timeout_seconds": 30,
            "max_retries": 3,
            "retry_delay": 1.0,
        },
        "cache": {"enabled": True, "ttl_minutes": 15, "max_entries": 1000},
        "output": {
            "default_format": "table",
            "show_quality_scores": True,
            "show_catalog_source": True,
            "color_output": True,
        },
    }
    config.update(default_config)

    # 2. Load from configuration file (lowest precedence)
    config_paths = [
        Path.home() / ".fluid" / "market.yaml",
        Path.home() / ".fluid" / "market.yml",
        Path("market.yaml"),
        Path("market.yml"),
    ]

    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path) as f:
                    file_config = yaml.safe_load(f) or {}
                    _merge_config(config, file_config)
                    logger.debug(f"Loaded configuration from {config_path}")
                    break
            except Exception as e:
                logger.warning(f"Failed to load config from {config_path}: {e}")

    # 3. Load from environment variables (higher precedence)
    env_config = _load_env_config()
    if env_config:
        _merge_config(config, env_config)
        logger.debug("Loaded configuration from environment variables")

    # 4. Command line arguments override everything (highest precedence)
    if hasattr(args, "catalogs") and args.catalogs:
        config["catalogs"] = [cat.strip() for cat in args.catalogs.split(",")]

    # Set default catalogs if none specified
    if not config["catalogs"]:
        config["catalogs"] = ["google_cloud_data_catalog", "datahub"]

    # Ensure all configured catalogs have at least empty config
    for catalog in config["catalogs"]:
        if catalog not in config:
            config[catalog] = {}

    return config


def _merge_config(base: Dict[str, Any], override: Dict[str, Any]) -> None:
    """Recursively merge configuration dictionaries"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _merge_config(base[key], value)
        else:
            base[key] = value


def _load_env_config() -> Dict[str, Any]:
    """Load configuration from environment variables"""
    config = {}

    # Google Cloud Data Catalog
    if os.getenv("GCP_PROJECT_ID"):
        config["google_cloud_data_catalog"] = {
            "project_id": os.getenv("GCP_PROJECT_ID"),
            "location": os.getenv("GCP_LOCATION", "us-central1"),
            "credentials_file": os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
        }

    # AWS Glue Data Catalog
    if os.getenv("AWS_REGION"):
        config["aws_glue_data_catalog"] = {
            "region": os.getenv("AWS_REGION"),
            "profile": os.getenv("AWS_PROFILE"),
            "access_key_id": os.getenv("AWS_ACCESS_KEY_ID"),
            "secret_access_key": os.getenv("AWS_SECRET_ACCESS_KEY"),
            "session_token": os.getenv("AWS_SESSION_TOKEN"),
        }

    # Azure Purview
    if os.getenv("AZURE_PURVIEW_ACCOUNT"):
        config["azure_purview"] = {
            "account_name": os.getenv("AZURE_PURVIEW_ACCOUNT"),
            "tenant_id": os.getenv("AZURE_TENANT_ID"),
            "client_id": os.getenv("AZURE_CLIENT_ID"),
            "client_secret": os.getenv("AZURE_CLIENT_SECRET"),
        }

    # DataHub
    if os.getenv("DATAHUB_SERVER_URL"):
        config["datahub"] = {
            "server_url": os.getenv("DATAHUB_SERVER_URL"),
            "token": os.getenv("DATAHUB_TOKEN"),
        }

    # Apache Atlas
    if os.getenv("ATLAS_BASE_URL"):
        config["apache_atlas"] = {
            "base_url": os.getenv("ATLAS_BASE_URL"),
            "username": os.getenv("ATLAS_USERNAME"),
            "password": os.getenv("ATLAS_PASSWORD"),
        }

    # Confluent Schema Registry
    if os.getenv("CONFLUENT_SCHEMA_REGISTRY_URL"):
        config["confluent_schema_registry"] = {
            "url": os.getenv("CONFLUENT_SCHEMA_REGISTRY_URL"),
            "api_key": os.getenv("CONFLUENT_API_KEY"),
            "api_secret": os.getenv("CONFLUENT_API_SECRET"),
        }

    # Collibra
    if os.getenv("COLLIBRA_BASE_URL"):
        config["collibra"] = {
            "base_url": os.getenv("COLLIBRA_BASE_URL"),
            "username": os.getenv("COLLIBRA_USERNAME"),
            "password": os.getenv("COLLIBRA_PASSWORD"),
        }

    # Alation
    if os.getenv("ALATION_BASE_URL"):
        config["alation"] = {
            "base_url": os.getenv("ALATION_BASE_URL"),
            "api_token": os.getenv("ALATION_API_TOKEN"),
        }

    # Custom REST API
    if os.getenv("CUSTOM_CATALOG_URL"):
        config["custom_rest_api"] = {
            "base_url": os.getenv("CUSTOM_CATALOG_URL"),
            "auth_type": os.getenv("CUSTOM_CATALOG_AUTH_TYPE", "bearer"),
            "auth_token": os.getenv("CUSTOM_CATALOG_TOKEN"),
            "username": os.getenv("CUSTOM_CATALOG_USERNAME"),
            "password": os.getenv("CUSTOM_CATALOG_PASSWORD"),
            "api_key_header": os.getenv("CUSTOM_CATALOG_API_KEY_HEADER", "X-API-Key"),
            "api_key": os.getenv("CUSTOM_CATALOG_API_KEY"),
        }

    # Global settings
    if os.getenv("FLUID_MARKET_DEFAULT_LIMIT"):
        config.setdefault("defaults", {})["limit"] = int(os.getenv("FLUID_MARKET_DEFAULT_LIMIT"))

    if os.getenv("FLUID_MARKET_MIN_QUALITY"):
        config.setdefault("defaults", {})["min_quality_score"] = float(
            os.getenv("FLUID_MARKET_MIN_QUALITY")
        )

    if os.getenv("FLUID_MARKET_TIMEOUT"):
        config.setdefault("defaults", {})["timeout_seconds"] = int(
            os.getenv("FLUID_MARKET_TIMEOUT")
        )

    if os.getenv("FLUID_MARKET_CACHE_TTL"):
        config.setdefault("cache", {})["ttl_minutes"] = int(os.getenv("FLUID_MARKET_CACHE_TTL"))

    return config


def build_search_filters(args) -> SearchFilters:
    """Build search filters from command line arguments"""
    filters = SearchFilters()

    if args.search:
        filters.text_query = args.search

    if args.domain:
        # For simplicity, use the first domain if multiple specified
        filters.domain = args.domain[0] if isinstance(args.domain, list) else args.domain

    if args.owner:
        filters.owner = args.owner[0] if isinstance(args.owner, list) else args.owner

    if args.layer:
        layer_str = args.layer[0] if isinstance(args.layer, list) else args.layer
        filters.layer = DataProductLayer(layer_str)

    if args.status:
        status_str = args.status[0] if isinstance(args.status, list) else args.status
        filters.status = DataProductStatus(status_str)

    if args.tags:
        filters.tags = args.tags if isinstance(args.tags, list) else [args.tags]

    if args.min_quality:
        filters.min_quality_score = args.min_quality

    if args.created_after:
        filters.created_after = datetime.fromisoformat(args.created_after)

    if args.created_before:
        filters.created_before = datetime.fromisoformat(args.created_before)

    filters.limit = args.limit
    filters.offset = args.offset

    return filters


def generate_output(
    products: List[DataProductMetadata], args, console: Optional[Console], logger: logging.Logger
) -> int:
    """Generate and output results"""
    if not products:
        if console and RICH_AVAILABLE:
            console.print("[yellow]No data products found matching your criteria.[/yellow]")
        else:
            cprint("No data products found matching your criteria.")
        return 0

    # Generate output based on format
    if args.format == "json":
        output_content = format_json_output(products)
    elif args.format == "detailed":
        if console and RICH_AVAILABLE:
            for product in products:
                format_detailed_output(product, console)
        else:
            for product in products:
                format_detailed_output(product, None)
        return 0
    else:  # table format
        format_table_output(products, console)
        return 0

    # Handle file output
    if args.output:
        try:
            with open(args.output, "w") as f:
                f.write(output_content)
            logger.info(f"📄 Results written to {args.output}")
        except Exception as e:
            logger.error(f"Failed to write output file: {e}")
            return 1
    else:
        cprint(output_content)

    return 0


async def handle_health_check(engine: MarketDiscoveryEngine, args, logger: logging.Logger) -> int:
    """Handle health check command"""
    try:
        await engine.initialize_connectors()

        if not engine.health_checker:
            logger.error("Health checker not available")
            return 1

        if hasattr(args, "connector") and args.connector:
            # Check specific connector
            health_status = await engine.health_checker.check_connector_health(args.connector)
        else:
            # Check overall system health
            health_status = await engine.health_checker.check_system_health()

        if engine.console and RICH_AVAILABLE:
            engine.console.print_json(health_status)
        else:
            import json

            cprint(json.dumps(health_status, indent=2))

        return 0 if health_status.get("status") in ["healthy", "partial"] else 1
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return 1


async def handle_metrics(engine: MarketDiscoveryEngine, logger: logging.Logger) -> int:
    """Handle metrics command"""
    try:
        await engine.initialize_connectors()

        # Get performance summary
        performance_summary = engine.performance_monitor.get_performance_summary()

        if engine.console and RICH_AVAILABLE:
            engine.console.print("\n📊 Performance Metrics", style="bold blue")
            engine.console.print("=" * 50)

            # Search metrics
            search_requests = performance_summary.get("search_requests", {})
            if search_requests:
                engine.console.print("\n🔍 Search Requests:")
                for catalog, count in search_requests.items():
                    engine.console.print(f"  {catalog}: {count}")

            # Latency metrics
            avg_latencies = performance_summary.get("average_latencies", {})
            if avg_latencies:
                engine.console.print("\n⏱️  Average Latencies:")
                for catalog, latency in avg_latencies.items():
                    engine.console.print(f"  {catalog}: {latency:.2f}s")

            # Cache metrics
            cache_hit_rates = performance_summary.get("cache_hit_rates", {})
            if cache_hit_rates:
                engine.console.print("\n💾 Cache Hit Rates:")
                for catalog, rate in cache_hit_rates.items():
                    engine.console.print(f"  {catalog}: {rate:.1%}")

            # Slow queries
            slow_queries = performance_summary.get("slow_queries", {})
            if slow_queries.get("count", 0) > 0:
                engine.console.print(
                    f"\n🐌 Slow Queries (>{slow_queries.get('threshold', 5)}s): {slow_queries['count']}"
                )
                recent = slow_queries.get("recent", [])
                for query in recent:
                    engine.console.print(
                        f"  {query['catalog_type']}: {query['latency']:.2f}s at {query['timestamp']}"
                    )

            # Error counts
            error_counts = performance_summary.get("error_counts", {})
            if error_counts:
                engine.console.print("\n❌ Error Counts:")
                for error_key, count in error_counts.items():
                    engine.console.print(f"  {error_key}: {count}")
        else:
            import json

            cprint(json.dumps(performance_summary, indent=2))

        return 0
    except Exception as e:
        logger.error(f"Failed to get metrics: {e}")
        return 1


async def handle_monitor_mode(engine: MarketDiscoveryEngine, args, logger: logging.Logger) -> int:
    """Handle monitor mode - continuous monitoring"""
    try:
        await engine.initialize_connectors()

        if not engine.health_checker:
            logger.error("Health checker not available")
            return 1

        monitor_interval = getattr(args, "interval", 30)  # Default 30 seconds

        logger.info(f"🔍 Starting continuous monitoring (interval: {monitor_interval}s)")
        logger.info("Press Ctrl+C to stop monitoring")

        try:
            while True:
                # Perform health check
                health_status = await engine.health_checker.check_system_health()
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if engine.console and RICH_AVAILABLE:
                    engine.console.clear()
                    engine.console.print(
                        f"[bold blue]🔍 Market Health Monitor - {current_time}[/bold blue]"
                    )
                    engine.console.print("=" * 60)

                    # Overall status
                    status = health_status["status"]
                    status_color = {
                        "healthy": "green",
                        "partial": "yellow",
                        "degraded": "orange",
                        "critical": "red",
                    }.get(status, "white")

                    engine.console.print(
                        f"Overall Status: [{status_color}]{status.upper()}[/{status_color}]"
                    )
                    engine.console.print(
                        f"Health Score: {health_status['overall_health_score']:.1%}"
                    )

                    # Connector status
                    engine.console.print("\n🔌 Connector Status:")
                    for name, connector_health in health_status["connectors"].items():
                        connector_status = connector_health["status"]
                        status_emoji = {"healthy": "✅", "unhealthy": "❌", "error": "💥"}.get(
                            connector_status, "❓"
                        )
                        response_time = connector_health.get("response_time")
                        if response_time is not None:
                            engine.console.print(f"  {status_emoji} {name}: {response_time:.2f}s")
                        else:
                            engine.console.print(f"  {status_emoji} {name}")

                    # Quick metrics
                    metrics = health_status.get("metrics", {})
                    total_requests = sum(metrics.get("search_requests", {}).values())
                    total_errors = sum(metrics.get("error_counts", {}).values())

                    engine.console.print("\n📊 Quick Stats:")
                    engine.console.print(f"  Total Requests: {total_requests}")
                    engine.console.print(f"  Total Errors: {total_errors}")

                    if total_requests > 0:
                        error_rate = total_errors / total_requests
                        engine.console.print(f"  Error Rate: {error_rate:.1%}")
                else:
                    cprint(
                        f"[{current_time}] Status: {health_status['status']} | Score: {health_status['overall_health_score']:.1%}"
                    )

                # Wait for next check
                await asyncio.sleep(monitor_interval)

        except KeyboardInterrupt:
            logger.info("🛑 Monitoring stopped by user")
            return 0

    except Exception as e:
        logger.error(f"Monitoring failed: {e}")
        return 1


async def handle_saved_searches(args, logger: logging.Logger) -> int:
    """Handle saved search operations"""
    try:
        if hasattr(args, "list_saved") and args.list_saved:
            # List all saved searches
            saved_searches = advanced_search_engine.list_saved_searches()

            if not saved_searches:
                cprint("📭 No saved searches found")
                return 0

            cprint("\n💾 Saved Searches:")
            cprint("=" * 30)
            for i, search_name in enumerate(saved_searches, 1):
                cprint(f"  {i}. {search_name}")

            return 0

        elif hasattr(args, "delete_saved") and args.delete_saved:
            # Delete a saved search
            search_name = args.delete_saved
            if search_name in advanced_search_engine.saved_searches:
                del advanced_search_engine.saved_searches[search_name]
                logger.info(f"🗑️  Deleted saved search '{search_name}'")
                return 0
            else:
                logger.error(f"❌ Saved search '{search_name}' not found")
                return 1

        elif hasattr(args, "show_saved") and args.show_saved:
            # Show details of a saved search
            search_name = args.show_saved
            saved_filter = advanced_search_engine.load_saved_search(search_name)

            if not saved_filter:
                logger.error(f"❌ Saved search '{search_name}' not found")
                return 1

            cprint(f"\n💾 Saved Search: {search_name}")
            cprint("=" * 40)

            # Display search parameters
            if saved_filter.text_query:
                cprint(f"🔍 Query: {saved_filter.text_query}")
            if saved_filter.domain:
                cprint(f"🏢 Domain: {saved_filter.domain}")
            if saved_filter.owner:
                cprint(f"👤 Owner: {saved_filter.owner}")
            if saved_filter.layer:
                cprint(f"📊 Layer: {saved_filter.layer.value}")
            if saved_filter.status:
                cprint(f"📈 Status: {saved_filter.status.value}")
            if saved_filter.tags:
                cprint(f"🏷️  Tags: {', '.join(saved_filter.tags)}")
            if saved_filter.min_quality_score:
                cprint(f"⭐ Min Quality: {saved_filter.min_quality_score}")

            cprint(f"📄 Limit: {saved_filter.limit}")
            cprint(f"🔄 Sort By: {saved_filter.sort_by} ({saved_filter.sort_order})")

            return 0

        else:
            logger.error("❌ No saved search operation specified")
            return 1

    except Exception as e:
        logger.error(f"❌ Saved search operation failed: {e}")
        return 1


async def handle_search_history(logger: logging.Logger) -> int:
    """Handle search history display"""
    try:
        history = advanced_search_engine.search_history

        if not history:
            cprint("📭 No search history found")
            return 0

        cprint("\n📊 Recent Search History:")
        cprint("=" * 50)

        for i, search in enumerate(reversed(history[-10:]), 1):  # Show last 10
            timestamp = search["timestamp"][:19]  # Remove microseconds
            query = search.get("query", "No query")
            results = search.get("total_results", 0)
            time_taken = search.get("query_time", 0)

            cprint(f"  {i}. [{timestamp}] '{query}' -> {results} results ({time_taken:.2f}s)")

        return 0

    except Exception as e:
        logger.error(f"❌ Failed to show search history: {e}")
        return 1


async def handle_search_suggestions(
    query: str, engine: MarketDiscoveryEngine, logger: logging.Logger
) -> int:
    """Handle search suggestions"""
    try:
        await engine.initialize_connectors()

        # Get all products for suggestion generation
        basic_filters = SearchFilters(limit=1000)  # Get more products for better suggestions
        catalog_results = await engine.search_all_catalogs(basic_filters)

        all_products = []
        for products in catalog_results.values():
            all_products.extend(products)

        suggestions = advanced_search_engine.generate_search_suggestions(all_products, query)

        if not suggestions:
            hint(f"No suggestions found for '{query}'")
            return 0

        cprint(f"\n💡 Search Suggestions for '{query}':")
        cprint("=" * 40)

        for i, suggestion in enumerate(suggestions, 1):
            cprint(f"  {i}. {suggestion}")

        return 0

    except Exception as e:
        logger.error(f"❌ Failed to generate suggestions: {e}")
        return 1


def handle_list_catalogs(config: Dict[str, Any], logger: logging.Logger) -> int:
    """Handle --list-catalogs command"""
    cprint("\n🏪 Available Data Catalog Types")
    cprint("=" * 50)

    catalog_info = {
        "google_cloud_data_catalog": "Google Cloud Data Catalog",
        "aws_glue_data_catalog": "AWS Glue Data Catalog",
        "azure_purview": "Azure Purview",
        "apache_atlas": "Apache Atlas",
        "datahub": "DataHub",
        "confluent_schema_registry": "Confluent Schema Registry",
        "collibra": "Collibra",
        "alation": "Alation",
        "custom_rest_api": "Custom REST API",
    }

    configured_catalogs = config.get("catalogs", [])

    for catalog_type, catalog_name in catalog_info.items():
        status = "✅ Configured" if catalog_type in configured_catalogs else "⚪ Available"
        cprint(f"  {status} {catalog_name} ({catalog_type})")

    return 0


def handle_config_template(logger: logging.Logger) -> int:
    """Generate configuration template"""
    template = """
# FLUID Market Configuration Template
# Save this as ~/.fluid/market.yaml

# Default catalogs to search (uncomment and configure as needed)
catalogs:
  - google_cloud_data_catalog
  - datahub

# Google Cloud Data Catalog configuration
google_cloud_data_catalog:
  project_id: "your-gcp-project-id"
  # location: "us-central1"  # Optional

# AWS Glue Data Catalog configuration  
aws_glue_data_catalog:
  region: "us-east-1"
  # profile: "default"  # Optional AWS profile

# Azure Purview configuration
azure_purview:
  account_name: "your-purview-account"
  # tenant_id: "your-tenant-id"  # Optional

# DataHub configuration
datahub:
  server_url: "http://localhost:8080"
  # token: "your-api-token"  # Optional for authenticated access

# Apache Atlas configuration
apache_atlas:
  base_url: "http://localhost:21000"
  # username: "admin"  # Optional
  # password: "admin"  # Optional

# Confluent Schema Registry configuration
confluent_schema_registry:
  url: "http://localhost:8081"
  # api_key: "your-api-key"  # Optional
  # api_secret: "your-api-secret"  # Optional

# Default search settings
defaults:
  limit: 50
  min_quality_score: 0.7
  include_deprecated: false
"""

    cprint(template)
    return 0


async def handle_marketplace_stats(engine: MarketDiscoveryEngine, logger: logging.Logger) -> int:
    """Handle --stats command"""
    try:
        await engine.initialize_connectors()

        if not engine.connectors:
            logger.error("No catalog connectors available")
            return 1

        if engine.console and RICH_AVAILABLE:
            engine.console.print("\n📊 Marketplace Statistics", style="bold blue")
            engine.console.print("=" * 50)

            for catalog_name, connector in engine.connectors.items():
                try:
                    stats = await connector.get_catalog_stats()
                    engine.console.print(f"\n🏪 {catalog_name}")
                    engine.console.print(
                        f"  📦 Total Products: {stats.get('total_products', 'N/A')}"
                    )
                    engine.console.print(f"  🏆 Avg Quality: {stats.get('avg_quality', 'N/A')}")
                    engine.console.print(f"  📅 Last Updated: {stats.get('last_updated', 'N/A')}")
                except Exception:
                    engine.console.print(f"\n❌ {catalog_name}: Error retrieving stats")
        else:
            cprint("\n📊 Marketplace Statistics")
            cprint("=" * 50)
            for catalog_name in engine.connectors:
                success(f"Connected to {catalog_name}")

        return 0
    except Exception as e:
        logger.error(f"Failed to get marketplace stats: {e}")
        return 1


async def handle_product_details(
    engine: MarketDiscoveryEngine, product_id: str, args, logger: logging.Logger
) -> int:
    """Handle product detail lookup"""
    try:
        # Search for the product across all catalogs
        for catalog_name, connector in engine.connectors.items():
            product = await connector.get_data_product(product_id)
            if product:
                format_detailed_output(product, engine.console)
                return 0

        logger.error(f"Product '{product_id}' not found in any connected catalogs")
        return 1
    except Exception as e:
        logger.error(f"Failed to get product details: {e}")
        return 1


def run(args, logger: logging.Logger) -> int:
    """Main entry point for market command"""
    # --- Blueprint mode (absorbed from 'fluid marketplace') ---
    _blueprints = getattr(args, "blueprints", False)
    _blueprint_id = getattr(args, "blueprint_id", None)
    _instantiate = getattr(args, "instantiate", False)

    # Validate: --instantiate requires --blueprint-id
    if _instantiate is True and not (isinstance(_blueprint_id, str) and _blueprint_id):
        cprint("Error: --instantiate requires --blueprint-id <id>")
        return 1

    if (_blueprints is True) or (isinstance(_blueprint_id, str) and _blueprint_id):
        try:
            from .marketplace import run as marketplace_run

            # Translate args for marketplace module
            if getattr(args, "blueprint_id", None):
                if getattr(args, "instantiate", False):
                    args.marketplace_action = "instantiate"
                else:
                    args.marketplace_action = "info"
            else:
                args.marketplace_action = "search"
                args.query = getattr(args, "search", None)
                args.category = None
                args.tags = None
                args.maturity = None
                args.state = "published"
                args.sort = "downloads"
                args.limit = getattr(args, "limit", 20)
            return marketplace_run(args, logger)
        except ImportError:
            cprint("Blueprint marketplace not available. Install required dependencies.")
            return 1

    try:
        # Run the async discovery function
        if asyncio.get_event_loop().is_running():
            # If we're already in an async context, create a new loop
            import threading

            result = {}
            exception = {}

            def run_in_thread():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    result["value"] = loop.run_until_complete(run_market_discovery(args, logger))
                except Exception as e:
                    exception["value"] = e
                finally:
                    loop.close()

            thread = threading.Thread(target=run_in_thread)
            thread.start()
            thread.join()

            if "value" in exception:
                raise exception["value"]

            return result["value"]
        else:
            # Normal async execution
            return asyncio.run(run_market_discovery(args, logger))

    except KeyboardInterrupt:
        logger.warning("⚠️ Market discovery interrupted by user")
        return 130
    except Exception as e:
        logger.error(f"💥 Unexpected error: {e}")
        if hasattr(args, "debug") and args.debug:
            import traceback

            logger.error(traceback.format_exc())
        raise CLIError(1, "market_discovery_failed", {"error": str(e)})
