# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Product top-level fields: apiVersion, kind, id, name, version, status,
domain, tenant, description, tags, productCreatedTs.

The Bitol ODPS spec requires ``apiVersion``, ``kind``, ``id``, ``status``.
Pass-through values written by :func:`to_fluid` (Phase 3) flow through
``metadata.odps_passthrough.*`` and are replayed verbatim on export.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fluid_build.forge.product_types import (
    LAYER_TO_PRODUCT_TYPE,
    ODPS_TYPE_TO_PRODUCT_TYPE,
    PRODUCT_TYPE_TO_ODPS_TYPE,
)
from fluid_build.providers.base import ProviderError

from .base import (
    ExportCtx,
    ImportCtx,
    fluid_id,
    get_metadata_passthrough,
    metadata_passthrough,
    resolve_status,
)
from .types import bitol_to_fluid_status, fluid_to_bitol_status

API_VERSION = "v1.0.0"
KIND = "DataProduct"


# ----- FLUID → ODPS --------------------------------------------------------


def to_odps(ctx: ExportCtx) -> None:
    fluid = ctx.fluid
    odps = ctx.odps
    metadata = fluid.get("metadata") or {}

    product_id = fluid_id(fluid)
    if not product_id:
        raise ProviderError("Contract missing required 'id' field for ODPS export")

    name = metadata.get("name") or fluid.get("name")
    if not name:
        raise ProviderError(
            "Contract missing required 'name' field. "
            "Set it at the contract root or under metadata.name."
        )

    api_version = str(ctx.options.get("api_version") or API_VERSION)
    odps["apiVersion"] = api_version
    odps["kind"] = KIND
    odps["id"] = product_id
    odps["name"] = name
    odps["version"] = str(metadata.get("version", "1.0.0"))
    # ``lifecycle.state`` is the field the FLUID schema defines and users set;
    # ``metadata.status`` is one the schema forbids, so reading it alone meant
    # every Bitol ODPS export shipped the hard-coded "draft" — an active data
    # product published to a catalog as a draft, a retired one as a draft too.
    odps["status"] = fluid_to_bitol_status(resolve_status(fluid))

    # Top-level ``type`` (approved RFC 0029, first shipped in v1.1.0).
    # v1.0.0 is ``additionalProperties: false``, so the field is emitted
    # only when targeting v1.1.0. Precedence: the CURRENT classification
    # wins whenever it yields a value, so editing metadata.productType
    # after an import re-exports the edited truth rather than a stale
    # imported value. The passthrough copy wins only when it is a custom
    # organisation type (RFC 0029 allows those) that no classification
    # can express.
    if api_version != API_VERSION:
        pt_bucket = get_metadata_passthrough(fluid)
        passthrough_type = str(pt_bucket.get("odps_type") or "")
        derived = _odps_type_from_classification(metadata)
        if derived:
            odps["type"] = derived
        elif passthrough_type and passthrough_type not in ODPS_TYPE_TO_PRODUCT_TYPE:
            odps["type"] = passthrough_type

    # Description: ODPS uses {purpose, limitations, usage}. Accept the
    # description from any of three FLUID locations — explicit
    # odps_passthrough bucket (wins for round-trip fidelity), top-level
    # ``description`` string, or ``metadata.description``.
    pt = get_metadata_passthrough(fluid)
    if "description" in pt:
        odps["description"] = dict(pt["description"])
    elif isinstance(fluid.get("description"), str) and fluid["description"]:
        odps["description"] = {"purpose": fluid["description"]}
    elif metadata.get("description"):
        odps["description"] = {"purpose": metadata["description"]}

    # ``domain`` is a root FLUID field and a root Bitol ODPS field, but only
    # ``metadata.domain`` was read — a key an ODCS import writes and a
    # hand-written contract never has — so `domain: retail` was dropped from
    # every export of every contract authored the documented way.
    business_context = metadata.get("businessContext")
    domain = metadata.get("domain") or fluid.get("domain")
    if not domain and isinstance(business_context, Mapping):
        domain = business_context.get("domain")
    if domain:
        odps["domain"] = domain
    if metadata.get("tenant"):
        odps["tenant"] = metadata["tenant"]

    # tags: the contract root is the canonical list (matching the ODCS
    # exporter); ``metadata.tags`` is the secondary one.
    tags = fluid.get("tags") or metadata.get("tags")
    if tags:
        odps["tags"] = list(tags)

    if "product_created_ts" in pt:
        odps["productCreatedTs"] = pt["product_created_ts"]

    # customProperties: verbatim pass-through wins; otherwise synthesise the
    # legacy "type/domain/fluidVersion" trio so callers that imported the
    # legacy OdpsStandardProvider behaviour keep their expected output.
    if "custom_properties" in pt:
        odps["customProperties"] = list(pt["custom_properties"])
    else:
        custom_props = _legacy_custom_properties(fluid)
        if custom_props:
            odps["customProperties"] = custom_props

    if "authoritative_definitions" in pt:
        odps["authoritativeDefinitions"] = list(pt["authoritative_definitions"])


# ----- ODPS → FLUID (Phase 3) ---------------------------------------------


def to_fluid(ctx: ImportCtx) -> None:
    odps = ctx.odps
    fluid = ctx.fluid

    metadata = fluid.setdefault("metadata", {})
    metadata["version"] = odps.get("version", "1.0.0")
    metadata["name"] = odps.get("name", odps.get("id"))
    metadata["status"] = bitol_to_fluid_status(odps.get("status", "draft"))

    fluid.setdefault("contract", {})["id"] = odps.get("id")
    fluid.setdefault("exposes", [])
    fluid.setdefault("expects", [])

    description = odps.get("description")
    if isinstance(description, Mapping):
        purpose = description.get("purpose")
        if purpose:
            metadata["description"] = purpose
        metadata_passthrough(fluid)["description"] = dict(description)
    elif isinstance(description, str) and description:
        metadata["description"] = description

    for key in ("domain", "tenant"):
        if odps.get(key):
            metadata[key] = odps[key]

    if odps.get("tags"):
        metadata["tags"] = list(odps["tags"])

    # RFC 0029 ``type`` (v1.1.0). A known value maps onto FLUID's Data Mesh
    # classification; ANY value (the spec allows custom organisation types)
    # is kept verbatim in the passthrough bucket so re-export reproduces it.
    odps_type = odps.get("type")
    if isinstance(odps_type, str) and odps_type:
        mapped = ODPS_TYPE_TO_PRODUCT_TYPE.get(odps_type)
        if mapped:
            metadata.setdefault("productType", mapped)
        metadata_passthrough(fluid)["odps_type"] = odps_type

    pt = metadata_passthrough(fluid)
    if odps.get("productCreatedTs"):
        pt["product_created_ts"] = odps["productCreatedTs"]
    if odps.get("customProperties"):
        pt["custom_properties"] = list(odps["customProperties"])
    if odps.get("authoritativeDefinitions"):
        pt["authoritative_definitions"] = list(odps["authoritativeDefinitions"])


def _odps_type_from_classification(metadata: Mapping[str, Any]) -> str:
    """FLUID's Data Mesh classification as the RFC 0029 ``type`` value.

    ``metadata.productType`` (SDP / ADP / CDP) wins; a contract carrying only
    the medallion ``metadata.layer`` derives the productType through the
    canonical Bronze/Silver/Gold mapping first. Platinum and Logical layers
    have no Data Mesh analogue and yield no ``type``.
    """
    product_type = metadata.get("productType")
    if not product_type:
        product_type = LAYER_TO_PRODUCT_TYPE.get(str(metadata.get("layer") or ""))
    return PRODUCT_TYPE_TO_ODPS_TYPE.get(str(product_type or ""), "")


def _legacy_custom_properties(fluid: Mapping[str, Any]) -> list:
    """Synthesise a legacy ``customProperties`` entry for the product ``type``.

    Historically the only home for a product type: the v1.0.0 schema has no
    top-level slot, so the deprecated ``OdpsStandardProvider`` surfaced it as
    a custom property. Since v1.1.0 the REAL slot is the RFC 0029 top-level
    ``type`` (see :func:`_odps_type_from_classification`); this legacy
    surface reads a DIFFERENT field (``metadata.product_type`` /
    ``metadata.type``, e.g. ``analytical`` vs ``operational``) and is kept
    for back-compat only. We skip ``domain`` and ``fluidVersion``: the former
    is already a Bitol top-level field and the latter would pollute the
    round-trip.
    """
    metadata = fluid.get("metadata") or {}
    product_type = (
        metadata.get("product_type")
        or metadata.get("type")
        or fluid.get("product_type")
        or fluid.get("type")
    )
    if not product_type:
        return []
    return [{"property": "type", "value": product_type}]
