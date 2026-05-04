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

"""``fluid market`` semantic search engine — physical extraction.

Lifted from ``cli/market.py`` (host file was 2228 LOC). ~265 LOC of
the :class:`AdvancedSearchEngine` ranking + filtering logic.
``cli/market.py`` re-imports the class so existing call sites keep
resolving.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from fluid_build.cli.market import (
        DataProductMetadata,
        DataProductStatus,
        SearchFilters,
    )


def _market_classes():
    """Resolve dataclasses (DataProductMetadata, SearchFilters,
    DataProductStatus) lazily from the host module — they're defined
    above this class in the original file but extraction order
    inverts that."""
    from fluid_build.cli import market as _market

    return _market


def __getattr__(name: str):
    if name in ("DataProductMetadata", "DataProductStatus", "SearchFilters"):
        return getattr(_market_classes(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _bind_classes_from_host() -> None:
    host = _market_classes()
    g = globals()
    for name in ("DataProductMetadata", "DataProductStatus", "SearchFilters"):
        if hasattr(host, name):
            g[name] = getattr(host, name)


_bind_classes_from_host()


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
        facets = {
            "domain": {},
            "owner": {},
            "layer": {},
            "productType": {},  # NEW in v0.7.3: Data Mesh vocabulary
            "status": {},
            "tags": {},
        }

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

            # productType facets (Data Mesh vocabulary). Falls back to
            # the canonical layer↔productType mapping so a Bronze contract
            # still surfaces under SDP. Routes through the single registry
            # at fluid_build.forge.product_types so a future ProductType
            # row picks this up without an edit here.
            from fluid_build.forge.product_types import LAYER_TO_PRODUCT_TYPE

            pt = product.product_type or LAYER_TO_PRODUCT_TYPE.get(layer.capitalize())
            if pt:
                facets["productType"][pt] = facets["productType"].get(pt, 0) + 1

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
