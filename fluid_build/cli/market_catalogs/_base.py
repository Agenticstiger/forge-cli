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
