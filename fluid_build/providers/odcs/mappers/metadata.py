# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Top-level metadata: id, version, status, name, description, tags, domain,
tenant, dataProduct, customProperties, authoritativeDefinitions, support,
price, roles, slaDefaultElement, contractCreatedTs.

All non-FLUID-native fields flow through ``metadata.odcs_passthrough.*`` so
the round-trip is lossless.
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
from .types import fluid_to_odcs_status, odcs_to_fluid_status


ODCS_API_VERSION = "v3.1.0"
ODCS_KIND = "DataContract"


# ----- ODCS → FLUID --------------------------------------------------------


def to_fluid(ctx: ImportCtx) -> None:
    odcs = ctx.odcs
    fluid = ctx.fluid

    metadata = fluid.setdefault("metadata", {})
    metadata["version"] = odcs.get("version", "1.0.0")
    metadata["name"] = odcs.get("name", odcs.get("id"))
    metadata["status"] = odcs_to_fluid_status(odcs.get("status", "active"))

    fluid.setdefault("contract", {})["id"] = odcs.get("id")
    fluid.setdefault("exposes", [])
    fluid.setdefault("expects", [])

    # Description: ODCS uses an object {purpose, limitations, usage}; unwrap
    # the human-readable purpose for FLUID and preserve the rest for round-trip.
    description = odcs.get("description")
    if isinstance(description, Mapping):
        purpose = description.get("purpose")
        if purpose:
            metadata["description"] = purpose
        metadata_passthrough(fluid)["description"] = dict(description)
    elif isinstance(description, str) and description:
        metadata["description"] = description

    if odcs.get("tags"):
        metadata["tags"] = list(odcs["tags"])

    for key in ("domain", "tenant", "dataProduct"):
        if odcs.get(key):
            metadata[key] = odcs[key]

    pt = metadata_passthrough(fluid)
    for src, dst in (
        ("customProperties", "custom_properties"),
        ("authoritativeDefinitions", "authoritative_definitions"),
        ("support", "support"),
        ("price", "price"),
        ("roles", "roles"),
        ("slaDefaultElement", "sla_default_element"),
        ("contractCreatedTs", "contract_created_ts"),
    ):
        if odcs.get(src) is not None:
            pt[dst] = odcs[src]


# ----- FLUID → ODCS --------------------------------------------------------


def to_odcs(ctx: ExportCtx) -> None:
    fluid = ctx.fluid
    odcs = ctx.odcs
    metadata = fluid.get("metadata") or {}

    contract_id = fluid.get("_scoped_id") or fluid_id(fluid)
    if not contract_id:
        raise ProviderError(
            "Contract missing required 'id' field. "
            "Expected one of: fluid['id'], fluid['contract']['id'], or fluid['metadata']['id']"
        )

    raw_status = fluid.get("_scoped_status") or metadata.get("status", "active")

    odcs["version"] = metadata.get("version", "1.0.0")
    odcs["apiVersion"] = ODCS_API_VERSION
    odcs["kind"] = ODCS_KIND
    odcs["id"] = contract_id
    odcs["status"] = fluid_to_odcs_status(raw_status)

    if metadata.get("name"):
        odcs["name"] = metadata["name"]

    pt = get_metadata_passthrough(fluid)

    # Description: pass-through original object if present, else build {purpose}
    if "description" in pt:
        odcs["description"] = dict(pt["description"])
    elif metadata.get("description"):
        odcs["description"] = {"purpose": metadata["description"]}

    if metadata.get("tags"):
        odcs["tags"] = list(metadata["tags"])

    for key in ("domain", "tenant", "dataProduct"):
        if metadata.get(key):
            odcs[key] = metadata[key]

    for src, dst in (
        ("custom_properties", "customProperties"),
        ("authoritative_definitions", "authoritativeDefinitions"),
        ("support", "support"),
        ("price", "price"),
        ("roles", "roles"),
        ("sla_default_element", "slaDefaultElement"),
        ("contract_created_ts", "contractCreatedTs"),
    ):
        if src in pt:
            odcs[dst] = pt[src]
