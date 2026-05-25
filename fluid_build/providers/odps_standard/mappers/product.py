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

from fluid_build.providers.base import ProviderError

from .base import (
    ExportCtx,
    ImportCtx,
    fluid_id,
    get_metadata_passthrough,
    metadata_passthrough,
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

    odps["apiVersion"] = API_VERSION
    odps["kind"] = KIND
    odps["id"] = product_id
    odps["name"] = name
    odps["version"] = str(metadata.get("version", "1.0.0"))
    odps["status"] = fluid_to_bitol_status(metadata.get("status", "draft"))

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

    for key in ("domain", "tenant"):
        if metadata.get(key):
            odps[key] = metadata[key]

    # tags: prefer metadata.tags; fall back to top-level fluid.tags (legacy)
    tags = metadata.get("tags") or fluid.get("tags")
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

    pt = metadata_passthrough(fluid)
    if odps.get("productCreatedTs"):
        pt["product_created_ts"] = odps["productCreatedTs"]
    if odps.get("customProperties"):
        pt["custom_properties"] = list(odps["customProperties"])
    if odps.get("authoritativeDefinitions"):
        pt["authoritative_definitions"] = list(odps["authoritativeDefinitions"])


def _legacy_custom_properties(fluid: Mapping[str, Any]) -> list:
    """Synthesise a legacy ``customProperties`` entry for the product ``type``.

    The Bitol ODPS v1.0.0 schema has no top-level slot for product type
    (e.g. ``analytical`` vs ``operational``), so callers that previously
    relied on the deprecated ``OdpsStandardProvider`` got it as a custom
    property. We preserve that surface for back-compat, but skip ``domain``
    and ``fluidVersion`` — the former is already a Bitol top-level field
    and the latter would pollute the round-trip.
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
