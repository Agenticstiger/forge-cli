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
    resolve_status,
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
    # Only carry ``name`` when the source ODCS actually has one. Synthesizing
    # it from ``id`` broke the FLUID-emitted round-trip: a fresh export omits
    # ``name`` when the contract has none, but the importer's ``id`` fallback
    # made re-export re-add a phantom top-level ``name`` (``export`` must be a
    # fixed point). ``name`` is not required on FLUID ``metadata``.
    if odcs.get("name"):
        metadata["name"] = odcs["name"]
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

    # ODCS has one ``tags`` list; FLUID has two homes for it and the exporter
    # reads the contract root first. Landing the import on the root is what
    # keeps the two distinguishable — writing it to ``metadata.tags`` made an
    # imported list indistinguishable from a contract's own secondary list, and
    # the round-trip collapsed them.
    if odcs.get("tags"):
        ctx.fluid["tags"] = list(odcs["tags"])

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

    odcs["version"] = metadata.get("version", "1.0.0")
    odcs["apiVersion"] = ODCS_API_VERSION
    odcs["kind"] = ODCS_KIND
    odcs["id"] = contract_id
    odcs["status"] = fluid_to_odcs_status(resolve_status(fluid))

    # ``name``, ``description`` and ``domain`` are first-class ODCS v3.1.0
    # fields and first-class FLUID root fields. Read the root first — that is
    # where a hand-written contract puts them — then the metadata block, which
    # is where an ODCS import parks them.
    name = metadata.get("name") or fluid.get("name")
    if name:
        odcs["name"] = name

    pt = get_metadata_passthrough(fluid)

    # Description: pass-through original object if present, else build {purpose}
    if "description" in pt:
        odcs["description"] = dict(pt["description"])
    else:
        description = metadata.get("description") or fluid.get("description")
        if description:
            odcs["description"] = {"purpose": description}

    # The contract root is the canonical tag list (``metadata.tags`` is the
    # secondary one). Reading metadata first made the root list unreachable
    # whenever both were set, and the round-trip then collapsed the two.
    tags = fluid.get("tags") or metadata.get("tags")
    if tags:
        odcs["tags"] = list(tags)

    business_context = metadata.get("businessContext")
    domain = metadata.get("domain") or fluid.get("domain")
    if not domain and isinstance(business_context, Mapping):
        domain = business_context.get("domain")
    if domain:
        odcs["domain"] = domain

    for key in ("tenant", "dataProduct"):
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
