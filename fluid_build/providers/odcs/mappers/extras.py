# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""FLUID-native blocks that ODCS has no field for → ``customProperties``.

ODCS v3.1.0 is a *data contract*: it describes the shape, servers, team, SLA
and quality of one dataset. A FLUID contract carries strictly more — the
``builds`` pipeline, ``governance``, ``lineage``, ``metadata.layer`` /
``productType`` / ``businessContext``, the expose ``title`` and
``binding.properties``, and so on. Those blocks have no ODCS home, so a plain
FLUID → ODCS → FLUID trip used to drop every one of them.

``customProperties`` is the spec's own designated escape hatch ("A list of
key/value pairs for custom properties"), so that is where they go: one
namespaced entry, :data:`FLUID_EXTRAS_PROPERTY`, whose value is the verbatim
subtree. The published contract stays valid ODCS for consumers that ignore it,
and FLUID can reconstruct the original document exactly.

The mapping is deny-list driven — every FLUID key the ODCS mappers do *not*
consume rides along automatically, so a new block added to the FLUID schema
does not silently start leaking.

Set ``ODCS_FLUID_EXTRAS=false`` to publish a lean contract without the blob;
the round-trip is then lossy again, which is the trade the flag names.
"""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from typing import Any, Dict, List

from .base import PASSTHROUGH_KEY, ExportCtx, ImportCtx, metadata_passthrough

FLUID_EXTRAS_PROPERTY = "fluidExtras"

# Root keys the ODCS document reproduces on its own. ``fluidVersion`` and
# ``kind`` are absent: ODCS has no field for either (its own ``kind`` is always
# "DataContract"), so both only survive by riding along.
_ROOT_MAPPED = frozenset(
    {
        "contract",
        "description",
        "domain",
        "exposes",
        "expects",
        "extensions",
        "id",
        "lifecycle",
        "metadata",
        "name",
        "owner",
        "tags",
    }
)

# ``metadata`` keys reproduced elsewhere in the ODCS document. ``owner`` is
# deliberately absent: ODCS ``team`` models {name, members[{username, role}]}
# and has no slot for ``slack`` or ``oncall``, so the FLUID owner block only
# survives verbatim.
_METADATA_MAPPED = frozenset(
    {
        PASSTHROUGH_KEY,
        "dataProduct",
        "description",
        "domain",
        "name",
        "status",
        "tenant",
        "version",
    }
)

# Expose keys reproduced by the schema / servers mappers.
_EXPOSE_MAPPED = frozenset({PASSTHROUGH_KEY, "contract", "exposeId", "id"})

# Column keys reproduced by the schema mapper. ``type`` rides along because the
# ODCS pair (logicalType, physicalType) is a *normalising* projection —
# ``numeric(12,2)`` and ``decimal(12,2)`` both render ``DECIMAL(12,2)`` — so the
# declared FLUID spelling is only recoverable if we keep it.
#
# Every entry here is an assertion that the round-trip reproduces that key
# without help, and a stale entry is a silent data-loss bug: ``businessName``
# sat here while the schema mapper only ever read ``business_name`` out of the
# pass-through (a key that exists solely on an already-imported contract), so a
# hand-written FLUID column's businessName reached neither the ODCS document
# nor this blob and simply vanished. The claim is now true in both directions —
# see schema.py ``_property_to_field`` / ``_field_to_property`` — and
# tests/providers/odcs/test_fluid_roundtrip_fidelity.py asserts it per key
# rather than trusting the list.
_FIELD_MAPPED = frozenset(
    {PASSTHROUGH_KEY, "businessName", "description", "name", "quality", "required", "tags"}
)


def _enabled() -> bool:
    return os.getenv("ODCS_FLUID_EXTRAS", "true").lower() == "true"


# ----- FLUID → ODCS --------------------------------------------------------


def to_odcs(ctx: ExportCtx) -> None:
    existing = ctx.odcs.get("customProperties")
    others: List[Dict[str, Any]] = [
        dict(p)
        for p in (existing or [])
        if isinstance(p, Mapping) and p.get("property") != FLUID_EXTRAS_PROPERTY
    ]

    extras: Dict[str, Any] = {}
    if _enabled():
        # Run the import pipeline over the document we just produced to see
        # exactly what a consumer would get back, then carry only the delta.
        # Deriving the blob rather than declaring it keeps it minimal *and*
        # sufficient by construction: anything the round-trip already
        # reproduces is omitted, anything it does not is preserved.
        baseline = _reimport({k: v for k, v in ctx.odcs.items() if k != "customProperties"}, ctx)
        extras = _collect(ctx.fluid, baseline)
    if extras:
        others.append(
            {
                "property": FLUID_EXTRAS_PROPERTY,
                "value": extras,
                "description": (
                    "FLUID-native contract blocks with no ODCS v3.1.0 field. "
                    "Written by the FLUID exporter so FLUID → ODCS → FLUID is "
                    "lossless; safe for other consumers to ignore."
                ),
            }
        )

    if others:
        ctx.odcs["customProperties"] = others
    else:
        ctx.odcs.pop("customProperties", None)


def _reimport(odcs: Mapping[str, Any], ctx: ExportCtx) -> Mapping[str, Any]:
    """What ``odcs`` imports back to, using the very pipeline a consumer runs."""
    # Local import: the mappers package imports this module at load time.
    from . import IMPORT_PIPELINE, normalize

    baseline: Dict[str, Any] = {}
    import_ctx = ImportCtx(odcs=odcs, fluid=baseline, logger=ctx.logger)
    for mapper in IMPORT_PIPELINE:
        mapper.to_fluid(import_ctx)
    # Rehydrate so the comparison is like-for-like: ``ctx.fluid`` is itself the
    # rehydrated form, and a value the extensions bucket already replays is
    # reproducible — it does not belong in the delta.
    return normalize.rehydrate(normalize.to_document(baseline, odcs))


def _delta(
    source: Mapping[str, Any], baseline: Mapping[str, Any], mapped: frozenset
) -> Dict[str, Any]:
    """Keys of ``source`` the round-trip neither maps nor reproduces."""
    return {
        k: copy.deepcopy(v)
        for k, v in source.items()
        if k not in mapped and not k.startswith("_") and baseline.get(k) != v
    }


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _collect(fluid: Mapping[str, Any], baseline: Mapping[str, Any]) -> Dict[str, Any]:
    extras: Dict[str, Any] = {}

    root = _delta(fluid, baseline, _ROOT_MAPPED)
    if root:
        extras["root"] = root

    metadata = _as_mapping(fluid.get("metadata"))
    md = _delta(metadata, _as_mapping(baseline.get("metadata")), _METADATA_MAPPED)
    if md:
        extras["metadata"] = md

    baseline_exposes = {
        e.get("exposeId") or e.get("id"): e
        for e in (baseline.get("exposes") or [])
        if isinstance(e, Mapping)
    }

    exposes: Dict[str, Any] = {}
    for expose in fluid.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue
        expose_id = expose.get("exposeId") or expose.get("id")
        if not expose_id:
            continue
        base_expose = _as_mapping(baseline_exposes.get(expose_id))
        entry = _delta(expose, base_expose, _EXPOSE_MAPPED)

        contract = _as_mapping(expose.get("contract"))
        base_contract = _as_mapping(base_expose.get("contract"))
        leftover = _delta(contract, base_contract, frozenset({"schema"}))
        if leftover:
            entry["contract"] = leftover

        base_fields = {
            f.get("name"): f for f in (base_contract.get("schema") or []) if isinstance(f, Mapping)
        }
        fields: Dict[str, Any] = {}
        for fld in contract.get("schema") or []:
            if not isinstance(fld, Mapping) or not fld.get("name"):
                continue
            extra = _delta(fld, _as_mapping(base_fields.get(fld["name"])), _FIELD_MAPPED)
            if extra:
                fields[fld["name"]] = extra
        if fields:
            entry["fields"] = fields

        if entry:
            exposes[expose_id] = entry
    if exposes:
        extras["exposes"] = exposes
    return extras


# ----- ODCS → FLUID --------------------------------------------------------


def to_fluid(ctx: ImportCtx) -> None:
    """Splice a previously-exported FLUID subtree back over the import result."""
    extras = _find(ctx.odcs.get("customProperties"))
    # The metadata mapper stored ``customProperties`` verbatim for round-trip;
    # drop our own entry from that copy so re-export regenerates it from the
    # restored FLUID blocks instead of emitting it twice.
    _strip_from_passthrough(ctx.fluid)
    if not isinstance(extras, Mapping):
        return

    root = extras.get("root")
    if isinstance(root, Mapping):
        for key, value in root.items():
            ctx.fluid[key] = copy.deepcopy(value)

    metadata_extra = extras.get("metadata")
    if isinstance(metadata_extra, Mapping):
        metadata = ctx.fluid.setdefault("metadata", {})
        if isinstance(metadata, dict):
            for key, value in metadata_extra.items():
                metadata[key] = copy.deepcopy(value)

    exposes_extra = extras.get("exposes")
    if not isinstance(exposes_extra, Mapping):
        return
    for expose in ctx.fluid.get("exposes") or []:
        if not isinstance(expose, dict):
            continue
        entry = exposes_extra.get(expose.get("exposeId") or expose.get("id"))
        if not isinstance(entry, Mapping):
            continue
        fields = entry.get("fields")
        contract_extra = entry.get("contract")
        for key, value in entry.items():
            if key in ("fields", "contract"):
                continue
            expose[key] = copy.deepcopy(value)
        contract = expose.setdefault("contract", {})
        if isinstance(contract, dict):
            if isinstance(contract_extra, Mapping):
                for key, value in contract_extra.items():
                    contract[key] = copy.deepcopy(value)
            if isinstance(fields, Mapping):
                for fld in contract.get("schema") or []:
                    if not isinstance(fld, dict):
                        continue
                    extra = fields.get(fld.get("name"))
                    if isinstance(extra, Mapping):
                        for key, value in extra.items():
                            fld[key] = copy.deepcopy(value)


def _find(custom_properties: Any) -> Any:
    if not isinstance(custom_properties, list):
        return None
    for prop in custom_properties:
        if isinstance(prop, Mapping) and prop.get("property") == FLUID_EXTRAS_PROPERTY:
            return prop.get("value")
    return None


def _strip_from_passthrough(fluid: Any) -> None:
    pt = metadata_passthrough(fluid)
    raw = pt.get("custom_properties")
    if not isinstance(raw, list):
        return
    kept = [
        p
        for p in raw
        if not (isinstance(p, Mapping) and p.get("property") == FLUID_EXTRAS_PROPERTY)
    ]
    if kept:
        pt["custom_properties"] = kept
    else:
        pt.pop("custom_properties", None)
