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

from fluid_build.cli.console import cprint, cprint_json, hint, success

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
    # NEW in v0.7.3: Data Mesh productType vocabulary, equivalent to
    # ``layer`` via Bronze↔SDP, Silver↔ADP, Gold↔CDP. Surfaced as a
    # marketplace facet so teams that prefer the Data Mesh terminology
    # can filter without having to mentally translate.
    product_type: Optional[str] = None
    # Column/field-level schema of the underlying data asset, populated by the
    # detail/enrichment path (e.g. the MCP connector's ``list_schema_fields``
    # call). Each entry: ``{"name", "type", "description", "nullable"}``. Empty
    # for the shallow listing path — enrichment is a per-product detail lookup.
    schema_fields: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class SearchFilters:
    """Enhanced search and filter criteria with advanced operators"""

    # Basic filters
    domain: Optional[str] = None
    owner: Optional[str] = None
    layer: Optional[DataProductLayer] = None
    product_type: Optional[str] = None  # SDP / ADP / CDP filter
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


# Search engine — physically extracted to
# ``cli/_market_search_engine.py`` (~265 LOC). Re-exported here so
# existing call sites keep resolving.
# Observability layer (MetricsCollector, HealthChecker,
# PerformanceMonitor, plus their singletons) physically extracted to
# ``cli/_market_observability.py`` (~285 LOC). Re-exported here so
# call sites and test patches keep resolving.
import time as time_module  # noqa: E402  — used by MarketDiscoveryEngine
from collections import Counter, defaultdict  # noqa: E402,F401

from fluid_build.cli._market_observability import (  # noqa: E402,F401
    HealthChecker,
    MetricsCollector,
    PerformanceMonitor,
    metrics_collector,
    performance_monitor,
)

# Resilience layer (CircuitBreaker, retry_with_backoff,
# BaseCatalogConnector) physically extracted to
# ``cli/_market_resilience.py`` (~290 LOC). Re-exported here.
from fluid_build.cli._market_resilience import (  # noqa: E402,F401
    BaseCatalogConnector,
    CircuitBreaker,
    retry_with_backoff,
)
from fluid_build.cli._market_search_engine import (  # noqa: E402,F401
    AdvancedSearchEngine,
    advanced_search_engine,
)


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


# Discovery engine — physically extracted to
# ``cli/_market_discovery_engine.py`` (~329 LOC). Re-exported here.
from fluid_build.cli._market_discovery_engine import MarketDiscoveryEngine  # noqa: E402,F401

# Render helpers — physically extracted to ``cli/_market_render.py``.
# Re-exported so callers can use them via the canonical
# ``fluid_build.cli.market.format_*`` namespace.
from fluid_build.cli._market_render import (  # noqa: E402,F401
    format_detailed_output,
    format_json_output,
    format_table_output,
    render_no_catalog_onboarding,
)


def register(subparsers: argparse._SubParsersAction):
    """Register the market command"""
    p = subparsers.add_parser(
        COMMAND,
        help="Discover data products from enterprise catalogs and marketplaces",
        # House style: 4 honest examples, 1 doc link. The previous
        # 70-line epilog described aspirational filters that don't
        # exist on the parser (--owner, --status, --created-after,
        # --tags). Trim aggressively. (UX hardening pass — PR 3.10.)
        epilog=(
            "  fluid market                                   # browse all\n"
            "  fluid market --domain marketing --domain sales\n"
            "  fluid market --layer gold --min-quality 0.9\n"
            "  fluid market --search 'customer analytics' --format json\n\n"
            "Supported catalogs: Google Data Catalog, AWS Glue, Azure Purview,\n"
            "Apache Atlas, DataHub, Collibra, Alation, custom REST.\n\n"
            "Docs: https://github.com/open-data-protocol/fluid/blob/main/docs/market.md"
        ),
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
        "--product-type",
        choices=["SDP", "ADP", "CDP"],
        help=(
            "Filter by Data Mesh productType (SDP=Source-Aligned, "
            "ADP=Aggregated, CDP=Consumption-Aligned). Equivalent to "
            "--layer Bronze/Silver/Gold respectively."
        ),
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
    blueprint_group.add_argument(
        "--params",
        help="Blueprint parameters as a JSON string or a path to a JSON file "
        "(used with --instantiate)",
    )
    blueprint_group.add_argument(
        "--interactive",
        action="store_true",
        help="Fill blueprint parameters via an interactive wizard (used with --instantiate)",
    )
    blueprint_group.add_argument(
        "--show-template",
        action="store_true",
        help="Show a blueprint's Jinja2 contract template (used with --blueprint-id)",
    )

    p.set_defaults(cmd=COMMAND, func=run)


async def run_market_discovery(args, logger: logging.Logger) -> int:
    """Main market discovery execution"""
    try:
        # Load configuration
        config = load_market_config(args, logger)

        # Initialize discovery engine
        engine = MarketDiscoveryEngine(config, logger)

        # In JSON mode stdout is reserved for the JSON document, so keep all
        # progress / status / onboarding chatter off it. Nulling the engine
        # console routes its Rich search-progress to the stderr logger instead.
        json_mode = getattr(args, "format", None) == "json"
        if json_mode:
            engine.console = None

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
            if json_mode:
                # Machine consumers get a valid empty result on stdout; the
                # human onboarding guidance would only corrupt the JSON.
                cprint(format_json_output([]))
            else:
                render_no_catalog_onboarding(
                    catalog_types or config.get("catalogs", []), engine.console
                )
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
                with open(config_path, encoding="utf-8") as f:
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

    if getattr(args, "product_type", None):
        filters.product_type = args.product_type

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
    # JSON mode emits a valid document even when empty (``[]``) — handled before
    # the human "no products" message so stdout stays machine-parseable.
    if args.format == "json":
        output_content = format_json_output(products)
        if args.output:
            try:
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(output_content)
                logger.info(f"📄 Results written to {args.output}")
            except Exception as e:
                logger.error(f"Failed to write output file: {e}")
                return 1
        else:
            cprint(output_content)
        return 0

    if not products:
        if console and RICH_AVAILABLE:
            console.print("[yellow]No data products found matching your criteria.[/yellow]")
        else:
            cprint("No data products found matching your criteria.")
        return 0

    # Generate output based on format
    if args.format == "detailed":
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

            cprint_json(json.dumps(health_status, indent=2))

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

            cprint_json(json.dumps(performance_summary, indent=2))

        return 0
    except Exception as e:
        logger.error(f"Failed to get metrics: {e}")
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
        "mcp": "MCP server (DataHub / OpenMetadata / Data Mesh Manager / any MCP catalog)",
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

# MCP-based discovery — speak Model Context Protocol to any catalog that
# exposes an MCP server (DataHub, OpenMetadata, Data Mesh Manager, ...).
# Catalogs are normally REMOTE/hosted, so the default below connects to a
# hosted MCP endpoint. Add "mcp" to the catalogs list above to enable it.
mcp:
  profile: openmetadata       # datahub | openmetadata | datamesh_manager | auto

  # --- Remote MCP endpoint (default; OpenMetadata exposes one at /mcp) ---
  transport: streamable_http
  url: "https://openmetadata.your-company.com/mcp"
  token_env: "OPENMETADATA_JWT"   # bearer token (Personal Access Token) from this env var

  # --- DataHub: run the official server locally, pointed at your REMOTE
  #     DataHub GMS (the server process is local; the catalog is not) ---
  # profile: datahub
  # transport: stdio
  # command: uvx
  # args: ["mcp-server-datahub"]
  # env:
  #   DATAHUB_GMS_URL: "https://your-company.acryl.io/gms"   # your hosted DataHub
  #   DATAHUB_GMS_TOKEN: "${DATAHUB_GMS_TOKEN}"

  # --- Data Mesh Manager (SaaS): run dataproduct-mcp against the hosted API ---
  # profile: datamesh_manager
  # transport: stdio
  # command: uvx
  # args: ["dataproduct-mcp"]
  # env:
  #   DATAMESH_MANAGER_API_KEY: "${DATAMESH_MANAGER_API_KEY}"

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
    json_mode = getattr(args, "format", None) == "json"
    try:
        # Search for the product across all catalogs
        for catalog_name, connector in engine.connectors.items():
            product = await connector.get_data_product(product_id)
            if product:
                if json_mode:
                    # Honour --format json: emit the product as a single-element
                    # JSON array (same shape/serialiser as the listing view) so
                    # `--product-id … --detailed --format json` is parseable.
                    cprint(format_json_output([product]))
                else:
                    format_detailed_output(product, engine.console)
                return 0

        if json_mode:
            cprint(format_json_output([]))
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
            # Mark the delegated path so marketplace.run() suppresses the
            # "'fluid marketplace' is deprecated" banner — the user IS on the new
            # `fluid market --blueprints` command; that banner only makes sense for
            # a direct (hidden, deprecated) `fluid marketplace` invocation.
            args._from_market = True
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


# ── Connector re-exports for back-compat with `from fluid_build.cli.market import …Connector` ──
# The connector class bodies live in ``cli.market_catalogs.<system>``;
# we re-export them at the bottom of this module so the existing import
# path keeps working. The bottom-of-module placement matters: by the
# time these run, ``BaseCatalogConnector`` (line 553) is fully defined,
# so per-connector modules that subclass it can import from here without
# circular issues.
from fluid_build.cli.market_catalogs.alation import AlationConnector  # noqa: E402,F401
from fluid_build.cli.market_catalogs.apache_atlas import ApacheAtlasConnector  # noqa: E402,F401
from fluid_build.cli.market_catalogs.aws_glue import AWSGlueDataCatalogConnector  # noqa: E402,F401
from fluid_build.cli.market_catalogs.azure_purview import AzurePurviewConnector  # noqa: E402,F401
from fluid_build.cli.market_catalogs.collibra import CollibraConnector  # noqa: E402,F401
from fluid_build.cli.market_catalogs.command_center import CommandCenterConnector  # noqa: E402,F401
from fluid_build.cli.market_catalogs.confluent_schema_reg import (
    ConfluentSchemaRegistryConnector,  # noqa: E402,F401
)
from fluid_build.cli.market_catalogs.datahub import DataHubConnector  # noqa: E402,F401
from fluid_build.cli.market_catalogs.google_data_catalog import (
    GoogleCloudDataCatalogConnector,  # noqa: E402,F401
)
from fluid_build.cli.market_catalogs.rest_api import CustomRestApiConnector  # noqa: E402,F401
