# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Shared scaffolding for the per-catalog connectors.

Re-exports the catalog primitives (enums, dataclasses, BaseCatalogConnector)
from :mod:`fluid_build.cli.market`. Per-catalog modules import from
HERE so the import graph is::

    market.py            # owns the shared types
        ↑
    market_catalogs/_base.py
        ↑
    market_catalogs/<system>.py

Future migration: move the shared-types definitions physically into
this module and have ``market.py`` re-export them. Doing so today
would invalidate the test suite\'s imports
(``from fluid_build.cli.market import BaseCatalogConnector``); the
current re-export keeps both shapes working.
"""

from fluid_build.cli.market import (
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

__all__ = [
    "AdvancedSearchEngine",
    "BaseCatalogConnector",
    "CatalogType",
    "CircuitBreaker",
    "DataProductLayer",
    "DataProductMetadata",
    "DataProductStatus",
    "SearchFilters",
    "SearchResult",
]
