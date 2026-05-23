# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Schema mapper.

ODCS v3.1.0 ``schema`` is an array of :class:`SchemaObject` (``logicalType:
"object"``) whose ``properties[]`` carry the actual field definitions. Each
SchemaObject becomes one FLUID expose; each SchemaProperty becomes one FLUID
field.

Object-level extras (``relationships``, object-level ``quality``,
``authoritativeDefinitions``, ``customProperties``, ``dataGranularityDescription``,
``physicalName``, ``businessName``, ``tags``) flow through
``expose.odcs_passthrough.*`` for round-trip.

Field-level extras (``physicalType``, ``physicalName``, ``businessName``,
``unique``, ``partitioned``, ``primaryKeyPosition``, ``logicalTypeOptions``,
``examples``, ``encryptedName``, ``criticalDataElement``, ``authoritativeDefinitions``,
``customProperties``) flow through ``field.odcs_passthrough.*``.

Per-property ``quality[]`` is delegated to :mod:`.quality`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from .base import (
    ExportCtx,
    ImportCtx,
    expose_passthrough,
    field_passthrough,
    get_expose_passthrough,
    get_field_passthrough,
)
from .types import (
    fluid_to_logical,
    fluid_to_physical,
    logical_to_fluid,
    physical_type_to_platform,
)


# ----- ODCS → FLUID --------------------------------------------------------


def to_fluid(ctx: ImportCtx) -> None:
    schema = ctx.odcs.get("schema") or []
    if not isinstance(schema, list):
        return
    exposes = ctx.fluid.setdefault("exposes", [])
    for schema_object in schema:
        if not isinstance(schema_object, Mapping):
            continue
        expose = _schema_object_to_expose(schema_object, ctx.odcs)
        if expose:
            exposes.append(expose)


def _schema_object_to_expose(
    schema_object: Mapping[str, Any], odcs: Mapping[str, Any]
) -> Optional[Dict[str, Any]]:
    expose_id = schema_object.get("name") or odcs.get("id") or "default"

    expose: Dict[str, Any] = {
        "id": expose_id,
        "exposeId": expose_id,
        "version": odcs.get("version", "1.0.0"),
    }
    if schema_object.get("description"):
        expose["description"] = schema_object["description"]

    # Best-effort binding from physicalType
    if schema_object.get("physicalType"):
        binding: Dict[str, Any] = {
            "platform": physical_type_to_platform(schema_object["physicalType"]),
        }
        if schema_object.get("physicalName"):
            binding["physical_name"] = schema_object["physicalName"]
        expose["binding"] = binding

    # Properties → contract.schema (FLUID 0.7.1 layout)
    contract: Dict[str, Any] = {"schema": []}
    for prop in schema_object.get("properties") or []:
        if not isinstance(prop, Mapping):
            continue
        contract["schema"].append(_property_to_field(prop))
    expose["contract"] = contract

    # Object-level pass-throughs
    pt = expose_passthrough(expose)
    for src, dst in (
        ("physicalType", "physical_type"),
        ("physicalName", "physical_name"),
        ("businessName", "business_name"),
        ("dataGranularityDescription", "data_granularity"),
        ("tags", "tags"),
        ("relationships", "relationships"),
        ("quality", "object_quality"),
        ("authoritativeDefinitions", "authoritative_definitions"),
        ("customProperties", "custom_properties"),
    ):
        if src in schema_object:
            pt[dst] = schema_object[src]
    return expose


def _property_to_field(prop: Mapping[str, Any]) -> Dict[str, Any]:
    fld: Dict[str, Any] = {
        "name": prop.get("name", "unknown"),
        "type": logical_to_fluid(prop.get("logicalType", "string")),
    }
    if prop.get("description"):
        fld["description"] = prop["description"]
    if prop.get("classification"):
        fld["classification"] = prop["classification"]

    # required (v3.1.0); fall back to legacy isNullable
    if "required" in prop:
        fld["required"] = bool(prop["required"])
    elif "isNullable" in prop:
        fld["required"] = not bool(prop["isNullable"])
    else:
        fld["required"] = False

    # Preserve original tags verbatim (primaryKey lives in the pass-through, not in tags)
    if prop.get("tags"):
        fld["tags"] = list(prop["tags"])

    # Property-level quality preserved verbatim so export reproduces it exactly.
    # An empty list is meaningful here: it signals "original had no quality field
    # — do NOT auto-generate one on re-export".
    if "quality" in prop and isinstance(prop["quality"], list):
        fld["quality"] = list(prop["quality"])
    else:
        # Pass-through marker: "imported with no quality block".
        fld["quality"] = []

    # Field-level pass-through bucket — also marks the field as ODCS-sourced
    # so the export side knows to stay loss-less (no auto-defaults).
    pt = field_passthrough(fld)
    pt["imported"] = True
    if prop.get("primaryKey"):
        pt["primary_key"] = True
    for src, dst in (
        ("physicalType", "physical_type"),
        ("physicalName", "physical_name"),
        ("businessName", "business_name"),
        ("unique", "unique"),
        ("partitioned", "partitioned"),
        ("partitionKeyPosition", "partition_key_position"),
        ("primaryKeyPosition", "primary_key_position"),
        ("logicalTypeOptions", "logical_type_options"),
        ("examples", "examples"),
        ("encryptedName", "encrypted_name"),
        ("criticalDataElement", "critical_data_element"),
        ("authoritativeDefinitions", "authoritative_definitions"),
        ("customProperties", "custom_properties"),
    ):
        if src in prop:
            pt[dst] = prop[src]
    if not pt:
        fld.pop("odcs_passthrough", None)
    return fld


# ----- FLUID → ODCS --------------------------------------------------------


def to_odcs(ctx: ExportCtx) -> None:
    """Emit ``odcs.schema`` from FLUID exposes.

    Always emits an array — required by the ODCS v3.1.0 schema.
    """
    ctx.odcs["schema"] = list(_extract_schema(ctx.fluid))


def _extract_schema(fluid: Mapping[str, Any]) -> List[Dict[str, Any]]:
    odcs_schema: List[Dict[str, Any]] = []
    for expose in fluid.get("exposes") or []:
        if not isinstance(expose, Mapping):
            continue

        # Resolve the field list — FLUID 0.7.1 puts it at expose.contract.schema,
        # 0.5.7 at expose.schema.fields
        contract_schema: List[Mapping[str, Any]] = []
        contract = expose.get("contract")
        if isinstance(contract, Mapping) and isinstance(contract.get("schema"), list):
            contract_schema = contract["schema"]
        else:
            schema_obj = expose.get("schema")
            if isinstance(schema_obj, Mapping) and isinstance(schema_obj.get("fields"), list):
                contract_schema = schema_obj["fields"]
        if not contract_schema:
            continue

        provider = _expose_provider(expose)
        properties: List[Dict[str, Any]] = []
        for fld in contract_schema:
            if not isinstance(fld, Mapping):
                continue
            properties.append(_field_to_property(fld, provider))
        if not properties:
            continue

        expose_id = expose.get("exposeId") or expose.get("id") or "dataset"
        pt = get_expose_passthrough(expose)

        physical_type = pt.get("physical_type") or _platform_to_physical_type(provider)
        schema_object: Dict[str, Any] = {
            "name": expose_id,
            "logicalType": "object",
            "physicalType": physical_type,
            "properties": properties,
        }
        if expose.get("description"):
            schema_object["description"] = expose["description"]

        # Pass-through extras
        for src, dst in (
            ("physical_name", "physicalName"),
            ("business_name", "businessName"),
            ("data_granularity", "dataGranularityDescription"),
            ("tags", "tags"),
            ("relationships", "relationships"),
            ("object_quality", "quality"),
            ("authoritative_definitions", "authoritativeDefinitions"),
            ("custom_properties", "customProperties"),
        ):
            if src in pt:
                schema_object[dst] = pt[src]

        odcs_schema.append(schema_object)
    return odcs_schema


def _expose_provider(expose: Mapping[str, Any]) -> Optional[str]:
    binding = expose.get("binding")
    if isinstance(binding, Mapping):
        prov = binding.get("platform") or binding.get("provider")
        if prov:
            return prov
    return expose.get("provider")


def _platform_to_physical_type(provider: Optional[str]) -> str:
    if not provider:
        return "table"
    p = provider.lower()
    if p == "kafka":
        return "topic"
    return "table"


def _field_to_property(fld: Mapping[str, Any], provider: Optional[str]) -> Dict[str, Any]:
    prop: Dict[str, Any] = {
        "name": fld.get("name", "unknown"),
        "logicalType": fluid_to_logical(fld.get("type", "string")),
    }

    pt = get_field_passthrough(fld)
    is_imported = bool(pt.get("imported"))

    # physicalType:
    #   - pass-through wins (verbatim round-trip),
    #   - fresh-FLUID export synthesises from provider context,
    #   - imported-without-physicalType skips emission so round-trip is lossless.
    if pt.get("physical_type"):
        prop["physicalType"] = pt["physical_type"]
    elif provider and not is_imported:
        phys = fluid_to_physical(fld.get("type", "string"), provider)
        if phys:
            prop["physicalType"] = phys

    if fld.get("description"):
        prop["description"] = fld["description"]

    # required: ODCS default is False. For round-trip we only emit when the
    # original had it (or when True). For fresh FLUID exports we keep the
    # legacy behaviour of always emitting (downstream consumers expect it).
    if fld.get("required"):
        prop["required"] = True
    elif not is_imported:
        prop["required"] = False

    if fld.get("classification"):
        prop["classification"] = fld["classification"]
    if fld.get("tags"):
        prop["tags"] = list(fld["tags"])

    # Property-level quality: delegated to mappers.quality.to_odcs_property
    # which reads field.quality and synthesises auto-checks when needed.
    from . import quality as quality_mapper  # local import to avoid cycle

    qchecks = quality_mapper.to_odcs_property(fld)
    if qchecks:
        prop["quality"] = qchecks

    # Pass-through extras (excluding physical_type which was applied above)
    for src, dst in (
        ("physical_name", "physicalName"),
        ("business_name", "businessName"),
        ("unique", "unique"),
        ("partitioned", "partitioned"),
        ("partition_key_position", "partitionKeyPosition"),
        ("primary_key_position", "primaryKeyPosition"),
        ("logical_type_options", "logicalTypeOptions"),
        ("examples", "examples"),
        ("encrypted_name", "encryptedName"),
        ("critical_data_element", "criticalDataElement"),
        ("authoritative_definitions", "authoritativeDefinitions"),
        ("custom_properties", "customProperties"),
    ):
        if src in pt:
            prop[dst] = pt[src]

    # primaryKey: prefer the verbatim pass-through, else infer from FLUID tag
    if pt.get("primary_key"):
        prop["primaryKey"] = True
    elif fld.get("tags") and any(t in ("primary-key", "primaryKey") for t in fld["tags"]):
        prop["primaryKey"] = True

    return prop
