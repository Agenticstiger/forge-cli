# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Per-catalog connector package.

Replaces the connector portion of the monolithic ``cli/market.py``.
Each catalog (Google Data Catalog / AWS Glue / Azure Purview /
DataHub / Atlas / Confluent / Collibra / Alation / generic REST /
Command Center) lives in its own sibling module so adding a new
catalog is a single new file rather than an inline insert.

Existing callers that import from ``fluid_build.cli.market`` still
work — ``market.py`` re-exports every connector from this package
for back-compat with the test suite + downstream tools.
"""

# Re-export the base types so consumers don\'t need to know whether
# they\'re importing the BaseCatalogConnector vs a concrete subclass
# from this single package boundary.
from ._base import (
    AdvancedSearchEngine,
    BaseCatalogConnector,
    CatalogType,
    CircuitBreaker,
    DataProductLayer,
    DataProductMetadata,
    DataProductStatus,
    SearchFilters,
    SearchResult,
)
from .alation import AlationConnector
from .apache_atlas import ApacheAtlasConnector
from .aws_glue import AWSGlueDataCatalogConnector
from .azure_purview import AzurePurviewConnector
from .collibra import CollibraConnector
from .command_center import CommandCenterConnector
from .confluent_schema_reg import ConfluentSchemaRegistryConnector
from .datahub import DataHubConnector
from .google_data_catalog import GoogleCloudDataCatalogConnector
from .rest_api import CustomRestApiConnector

__all__ = [
    "AdvancedSearchEngine",
    "AlationConnector",
    "ApacheAtlasConnector",
    "AWSGlueDataCatalogConnector",
    "AzurePurviewConnector",
    "BaseCatalogConnector",
    "CatalogType",
    "CircuitBreaker",
    "CollibraConnector",
    "CommandCenterConnector",
    "ConfluentSchemaRegistryConnector",
    "CustomRestApiConnector",
    "DataHubConnector",
    "DataProductLayer",
    "DataProductMetadata",
    "DataProductStatus",
    "GoogleCloudDataCatalogConnector",
    "SearchFilters",
    "SearchResult",
]
