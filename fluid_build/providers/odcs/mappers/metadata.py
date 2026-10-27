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

A per-port export (``render(..., expose_id=...)``) names the document after
that port and keeps the product name in a ``customProperties`` entry — see
:func:`to_odcs`.

All non-FLUID-native fields flow through ``metadata.odcs_passthrough.*`` so
the round-trip is lossless.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any, List

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

# ``customProperties`` key carrying the name of the FLUID data product a
# per-port contract was split out of. ``fluid``-prefixed camelCase, following
# the one other property this provider writes (``extras.py``'s
# ``fluidExtras``) and ODCS's own rule for the key ("Names should be in camel
# case — the same as if they were permanent properties in the contract").
FLUID_PRODUCT_NAME_PROPERTY = "fluidProductName"


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
    product_name = metadata.get("name") or fluid.get("name")

    # A per-port export is named after its port; a whole-contract export after
    # the product.
    #
    # Every per-port ODCS used to take the product's name, so a product with
    # two exposes published two contracts both called "Orders Product": the
    # ``id`` is per-port (``<productId>.<exposeId>``, or a pinned
    # ``exposes[].contract.id``) but the ``name`` was not, so a tool that keys
    # files, registry subjects or display names on the name cannot tell the
    # two apart — DataHub's ODCS ingestion, for one, displays a dataset as
    # ``"<contract.name> — <schema.name>"``. The port's ``title`` is the human
    # name of the thing the document actually describes, and ``exposeId`` is
    # the fallback for a title-less port — which is also what the document's
    # own ``schema[0].name`` already carries.
    #
    # Deliberately *no* special case for a single-port product: making the
    # exported name depend on how many siblings a port has would change that
    # name the day a second port is added, which is this very collision one
    # release later. A single-port product's exported name therefore changes
    # too whenever its port's title (or exposeId) differs from the product's
    # name.
    expose_id = ctx.options.get("expose_id")
    name = _port_contract_name(fluid, expose_id) if expose_id else product_name
    if name:
        odcs["name"] = name

    pt = get_metadata_passthrough(fluid)

    # Description: pass-through the original object if we have it, else build
    # one from whatever FLUID holds.
    #
    # The two directions used to disagree about what `description` can be. The
    # importer above type-checks it (Mapping -> unwrap `purpose`, str -> use as
    # is); this side wrapped UNCONDITIONALLY, so handing it a Mapping produced
    # `{"purpose": {"purpose": ..., "limitations": ..., "usage": ...}}` — an
    # object where ODCS declares a string, and therefore invalid ODCS emitted
    # with no error. Mirror the importer instead of assuming a str.
    if "description" in pt:
        odcs["description"] = dict(pt["description"])
    else:
        description = metadata.get("description") or fluid.get("description")
        if isinstance(description, Mapping) and description:
            odcs["description"] = dict(description)
        elif isinstance(description, str) and description:
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

    # The product name is no longer in ``name`` on the per-port path, so keep
    # it somewhere it is not lost. Runs after the pass-through loop above,
    # which owns ``customProperties`` wholesale.
    #
    # *Not* ODCS ``dataProduct``, tempting as the field looks ("The name of
    # the data product"): Bitol deprecated it in v3.1.0 — the official
    # ``open-data-contract-standard`` 3.2.0 schema marks it "DEPRECATED since
    # v3.1.0" — specifically to keep products in ODPS and contracts in ODCS.
    # ``customProperties`` is the spec's own escape hatch, which is where
    # ``extras.py`` already puts FLUID-native data. ``ODCS_FLUID_EXTRAS`` does
    # not gate this one: it is provenance a consumer reads, not round-trip
    # plumbing, and it is what makes the renamed contract traceable at all.
    if expose_id and product_name:
        _stamp_product_name(odcs, product_name)


def _port_contract_name(fluid: Mapping[str, Any], expose_id: str) -> str:
    """Name for a per-port ODCS: the port's ``title``, else its ``exposeId``.

    ``title`` is FLUID's human name for an output port, so it is what a person
    reading a catalog wants to see. A port that declares none (or whose title
    is blank, or that is not in this contract at all — the exporter scopes the
    document to one port before calling us, so that last case means a caller
    passed a stray ``expose_id``) falls back to the id, which is always
    present and always unique within the product.
    """
    for expose in fluid.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue
        if expose_id in (expose.get("exposeId"), expose.get("id")):
            title = expose.get("title")
            if isinstance(title, str) and title.strip():
                return title.strip()
            break
    return expose_id


def _stamp_product_name(odcs: MutableMapping[str, Any], product_name: str) -> None:
    """Record the owning data product in ``customProperties``, once."""
    existing = odcs.get("customProperties")
    properties: List[Any] = list(existing) if isinstance(existing, list) else []
    if any(
        isinstance(prop, Mapping) and prop.get("property") == FLUID_PRODUCT_NAME_PROPERTY
        for prop in properties
    ):
        # An entry already here came off ``metadata.odcs_passthrough`` — the
        # source document's own statement of its product, which outranks
        # anything we would derive. Same precedence ``description`` follows
        # above, and it matters for a contract imported from ODCS and
        # re-exported: there, ``metadata.name`` is only the port title the
        # importer reconstructed, not the product the producer named.
        return
    properties.append(
        {
            "property": FLUID_PRODUCT_NAME_PROPERTY,
            "value": product_name,
            "description": (
                "Name of the FLUID data product this contract is one output "
                "port of. Written here because the contract's own 'name' is "
                "the port's, and ODCS 'dataProduct' is deprecated since "
                "v3.1.0; the product itself is published as ODPS."
            ),
        }
    )
    odcs["customProperties"] = properties
