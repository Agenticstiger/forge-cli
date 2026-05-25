# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""DataHub structured-property definitions for FLUID metadata.

DataHub's ``structuredProperties`` aspect is the right home for
typed, queryable, governable metadata that has a closed allowed-value
set — exactly what ``fluid.layer`` (Bronze / Silver / Gold) and
``fluid.productType`` (SDP / ADP / CDP) are. They render as facets in
the DataHub search UI, allow governance to enforce allowed values,
and survive entity migrations cleanly.

Two-step lifecycle:

1. **Bootstrap** the property *definition* (``propertyDefinition``
   aspect on ``urn:li:structuredProperty:<id>``) once per DataHub
   instance. Idempotent — re-PUTting the same definition just
   refreshes the metadata.
2. **Assign** values to entities (``structuredProperties`` aspect on
   the Dataset / DataProduct URN) with each publish.

Capability detection: older DataHub OSS releases (pre-v0.13) don't
have the structuredProperty entity model. The bootstrap PUT returns
4xx on those; callers should catch and fall back to the legacy
``customProperties`` path. We never crash a publish over a missing
structured-property feature.

Shapes match the canonical example at
https://docs.datahub.com/docs/api/tutorials/structured-properties —
``valueType: urn:li:dataType:datahub.string``, cardinality SINGLE for
both, allowedValues list with ``{value: {string: "X"}, description:
"..."}`` entries.
"""

from __future__ import annotations

from typing import Any, Dict, List

# ── Property definitions ──────────────────────────────────────────────
#
# Frozen-by-design: changing an existing definition's allowedValues
# could orphan stored values on entities. New layers or product types
# get appended; existing entries don't move.

FLUID_LAYER_PROPERTY_ID = "fluid.layer"
FLUID_PRODUCT_TYPE_PROPERTY_ID = "fluid.productType"

_ENTITY_TYPES_DATA_ASSETS = [
    "urn:li:entityType:datahub.dataset",
    "urn:li:entityType:datahub.dataProduct",
]


def _string_property_definition(
    qualified_name: str,
    display_name: str,
    description: str,
    allowed_values: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Build the ``propertyDefinition`` aspect body for a string-typed,
    closed-allowed-values, single-cardinality FLUID property.

    The shape mirrors the official docs example verbatim — `valueType:
    urn:li:dataType:datahub.string`, allowedValues as
    `[{value: {string: "<v>"}, description: "<desc>"}]`.
    """
    return {
        "qualifiedName": qualified_name,
        "displayName": display_name,
        "description": description,
        "valueType": "urn:li:dataType:datahub.string",
        "cardinality": "SINGLE",
        "entityTypes": list(_ENTITY_TYPES_DATA_ASSETS),
        "allowedValues": [
            {"value": {"string": av["value"]}, "description": av["description"]}
            for av in allowed_values
        ],
    }


def fluid_layer_definition() -> Dict[str, Any]:
    """``fluid.layer`` — medallion classification (Bronze/Silver/Gold)."""
    return _string_property_definition(
        qualified_name=FLUID_LAYER_PROPERTY_ID,
        display_name="FLUID Layer",
        description=(
            "Medallion architecture layer this asset belongs to. "
            "Bronze = raw / source-aligned landing, Silver = cleaned + "
            "joined / aggregated, Gold = consumer-aligned marts."
        ),
        allowed_values=[
            {
                "value": "Bronze",
                "description": "Raw, source-aligned, lossless landing.",
            },
            {
                "value": "Silver",
                "description": "Cleaned, conformed, joined / aggregated.",
            },
            {
                "value": "Gold",
                "description": "Consumer-aligned data marts and product views.",
            },
        ],
    )


def fluid_product_type_definition() -> Dict[str, Any]:
    """``fluid.productType`` — Data Mesh archetype (SDP/ADP/CDP)."""
    return _string_property_definition(
        qualified_name=FLUID_PRODUCT_TYPE_PROPERTY_ID,
        display_name="FLUID Product Type",
        description=(
            "Data Mesh-aligned product archetype: SDP = Source-aligned "
            "Data Product, ADP = Aggregate Data Product (joins SDPs), "
            "CDP = Consumer-aligned Data Product."
        ),
        allowed_values=[
            {
                "value": "SDP",
                "description": (
                    "Source-aligned Data Product. Owns the lossless "
                    "ingestion path from one upstream system; typically "
                    "Bronze."
                ),
            },
            {
                "value": "ADP",
                "description": (
                    "Aggregate Data Product. Joins / conforms multiple "
                    "SDPs into shared facts and dimensions; typically "
                    "Silver."
                ),
            },
            {
                "value": "CDP",
                "description": (
                    "Consumer-aligned Data Product. Curated for a "
                    "specific consumer / use-case; typically Gold."
                ),
            },
        ],
    )


# ── URN helpers ───────────────────────────────────────────────────────


def structured_property_urn(qualified_name: str) -> str:
    """Stable URN for a structured property. ``qualifiedName`` is the
    canonical id DataHub uses for both the URN path segment and the
    body field; we keep them in lockstep so re-bootstrapping is a true
    upsert and not a duplicate-with-different-id."""
    return f"urn:li:structuredProperty:{qualified_name}"


# ── Assignment shape ──────────────────────────────────────────────────


def assignment_for(layer: str | None, product_type: str | None) -> Dict[str, Any]:
    """Build the ``structuredProperties`` aspect body assigning values
    to a target entity. Omits any property whose value is empty — DataHub
    treats an absent entry as "no value" (cleaner than emitting nulls).

    The shape mirrors the docs example: each entry is
    ``{propertyUrn: "urn:li:structuredProperty:...", values: [{string: "..."}]}``
    where ``values`` is always a list (cardinality SINGLE just means
    the list has at most one entry).
    """
    properties: List[Dict[str, Any]] = []
    if layer:
        properties.append(
            {
                "propertyUrn": structured_property_urn(FLUID_LAYER_PROPERTY_ID),
                "values": [{"string": layer}],
            }
        )
    if product_type:
        properties.append(
            {
                "propertyUrn": structured_property_urn(FLUID_PRODUCT_TYPE_PROPERTY_ID),
                "values": [{"string": product_type}],
            }
        )
    return {"properties": properties}


# ── Bootstrap-order list ──────────────────────────────────────────────
#
# Callers iterate this list to bootstrap both definitions in a single
# pass. Order is irrelevant (definitions are independent) but stable
# for log readability.

ALL_DEFINITIONS: List[Dict[str, Any]] = [
    fluid_layer_definition(),
    fluid_product_type_definition(),
]
