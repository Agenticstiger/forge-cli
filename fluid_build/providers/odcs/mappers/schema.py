# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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

Per-property ``quality[]`` is delegated to :mod:`.quality`, and is suppressed
along with every other quality surface when the provider's
``include_quality_checks`` is off.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Sequence

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
    fluid_type_from_odcs,
    logical_type_options,
    physical_type_custom_properties,
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

    # The binding is *not* built here. ``physicalType`` alone says only what
    # kind of object this is — a "table" exists on every warehouse there is —
    # while ``servers[].type`` names the system it actually lives in. Deriving
    # the platform from physicalType made every imported table a BigQuery
    # table, Snowflake sources included. The binding is assembled in
    # :mod:`.normalize`, which sees the servers list and the extras bucket.

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
    # Recover the most specific type the ODCS property carries: physicalType
    # first (the source system's own spelling), then logicalType +
    # logicalTypeOptions. Reading logicalType alone flattened NUMBER(18,4) to
    # a bare ``double``.
    fld: Dict[str, Any] = {
        "name": prop.get("name", "unknown"),
        "type": fluid_type_from_odcs(
            prop.get("logicalType", "string"),
            prop.get("physicalType"),
            prop.get("logicalTypeOptions"),
        ),
    }
    recursive = [
        item.get("value")
        for item in prop.get("customProperties") or []
        if isinstance(item, Mapping) and item.get("property") == "fluid.recursiveType"
    ]
    if recursive:
        if len(recursive) != 1 or not isinstance(recursive[0], Mapping):
            raise ValueError("ODCS fluid.recursiveType must contain exactly one typed object")
        from fluid_build.streaming.types import parse_type

        parse_type(recursive[0], path=str(fld["name"]))
        fld.update(
            copy.deepcopy(
                {
                    key: value
                    for key, value in recursive[0].items()
                    if key in {"type", "fields", "element", "key", "value"}
                }
            )
        )
    if prop.get("description"):
        fld["description"] = prop["description"]
    if prop.get("classification"):
        fld["classification"] = prop["classification"]
    # ``businessName`` is first-class on BOTH sides — ODCS v3.1.0
    # SchemaProperty.businessName and FLUID $defs/column.businessName — so map
    # it directly instead of burying it in the pass-through. Without this the
    # FLUID column never carried one, which is what made the export side's
    # "reproduced by the schema mapper" claim false for hand-written contracts.
    if prop.get("businessName"):
        fld["businessName"] = prop["businessName"]

    # required (v3.1.0); fall back to legacy isNullable. Track whether the
    # source ODCS carried ``required`` *explicitly* so the export side can
    # reproduce an explicit ``required: false`` verbatim (a fresh FLUID export
    # always writes ``required``, so a re-export must too — see to_odcs).
    required_explicit = False
    if "required" in prop:
        fld["required"] = bool(prop["required"])
        required_explicit = True
    elif "isNullable" in prop:
        fld["required"] = not bool(prop["isNullable"])
        required_explicit = True
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
    if required_explicit:
        pt["required_present"] = True
    if prop.get("primaryKey"):
        pt["primary_key"] = True
    for src, dst in (
        ("physicalType", "physical_type"),
        ("physicalName", "physical_name"),
        # ``businessName`` is deliberately absent — it is mapped to the
        # first-class FLUID column field above. Storing it here as well would
        # let the two copies drift, and the pass-through (which export reads
        # first) would win, silently republishing a stale business name after
        # someone edited the contract.
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
        # Property lineage: FLUID derives it from a build's stream mapping on export,
        # and an imported contract carries no build, so it is kept as written.
        ("transformSourceObjects", "transform_source_objects"),
        ("transformLogic", "transform_logic"),
        ("transformDescription", "transform_description"),
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
    ctx.odcs["schema"] = list(
        _extract_schema(ctx.fluid, include_quality=ctx.options.get("include_quality_checks", True))
    )


def _extract_schema(fluid: Mapping[str, Any], include_quality: bool = True) -> List[Dict[str, Any]]:
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
            properties.append(_field_to_property(fld, provider, include_quality))
        if not properties:
            continue

        expose_id = expose.get("exposeId") or expose.get("id") or "dataset"
        grain = _add_stream_lineage(fluid, expose_id, contract_schema, properties)
        pt = get_expose_passthrough(expose)

        physical_type = pt.get("physical_type") or _platform_to_physical_type(provider)
        schema_object: Dict[str, Any] = {
            "name": expose_id,
            "logicalType": "object",
            "physicalType": physical_type,
            "properties": properties,
        }
        # ODCS ``physicalName`` is the real object name in the source system —
        # the piece that, with servers[].{account,database,schema}, lets a
        # consumer address the table. ``name`` is the logical exposeId, which on
        # Snowflake is not interchangeable with the (case-sensitive) object name.
        physical_name = pt.get("physical_name") or _binding_object_name(expose)
        if physical_name:
            schema_object["physicalName"] = physical_name
        if expose.get("description"):
            schema_object["description"] = expose["description"]
        if grain:
            schema_object["dataGranularityDescription"] = grain

        # Pass-through extras
        for src, dst in (
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


def _add_stream_lineage(
    fluid: Mapping[str, Any],
    expose_id: Any,
    columns: List[Mapping[str, Any]],
    properties: List[Dict[str, Any]],
) -> Optional[str]:
    """Mark each property read from a build's ``properties.stream`` with its source.

    ODCS v3's property lineage: the topic as ``transformSourceObjects`` and the path the
    column (or a nested field mapped on its own) is read from as ``transformLogic``.
    Returns the row grain an explode sets, for ``dataGranularityDescription``.
    """
    builds = [
        build
        for build in fluid.get("builds") or []
        if isinstance(build, Mapping)
        and expose_id in (build.get("outputs") or [])
        and isinstance(build.get("properties"), Mapping)
    ]
    if len(builds) != 1:
        return None
    lineage = _stream_lineage(builds[0]["properties"], columns)
    if lineage is None:
        return None
    topic, paths, grain = lineage

    def mark(prefix: str, props: List[Dict[str, Any]]) -> None:
        for prop in props:
            name = f"{prefix}{prop.get('name')}"
            if name in paths:
                prop["transformSourceObjects"] = [topic]
                prop["transformLogic"] = paths[name]
            elif isinstance(prop.get("properties"), list):
                mark(name + ".", prop["properties"])

    mark("", properties)
    return grain


def _stream_lineage(
    properties: Mapping[str, Any], target: Sequence[Any]
) -> tuple[str, dict[str, str], str | None] | None:
    """Where each output column, and each nested field mapped on its own, is read from.

    For catalogs (ODCS ``transformSourceObjects``/``transformLogic``): the stream's topic,
    ``{output path: source path}`` and the row grain an explode sets. It describes the
    contract and does not compile it, so `fluid validate` still reports a mapping that
    cannot be compiled; a build that runs its own SQL has no lineage to report.
    """
    stream = properties.get("stream")
    mapping = properties.get("mapping") or {}
    if properties.get("sql") is not None or not isinstance(stream, Mapping):
        return None
    topic = stream.get("topic")
    if not isinstance(topic, str) or not isinstance(mapping, Mapping):
        return None
    overrides = {
        entry["name"]: entry["from"]
        for entry in mapping.get("columns") or []
        if isinstance(entry, Mapping)
        and isinstance(entry.get("name"), str)
        and isinstance(entry.get("from"), str)
    }
    paths: dict[str, str] = {}

    def walk(prefix: str, fields: Any) -> None:
        for field in fields if isinstance(fields, list) else []:
            if not isinstance(field, Mapping) or not isinstance(field.get("name"), str):
                continue
            name = prefix + field["name"]
            if name in overrides:
                paths[name] = overrides[name]
            elif any(key.startswith(name + ".") for key in overrides):
                walk(name + ".", field.get("fields"))
            else:
                paths[name] = name

    walk("", target)
    explode = mapping.get("explode")
    grain = None
    if isinstance(explode, Mapping) and isinstance(explode.get("path"), str):
        grain = f"One row per element of {explode['path']} in each record of topic {topic}"
    return topic, paths, grain


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


def _binding_object_name(expose: Mapping[str, Any]) -> Optional[str]:
    """The real object name the expose binds to, from ``binding.location``."""
    binding = expose.get("binding")
    if not isinstance(binding, Mapping):
        return None
    location = binding.get("location")
    if not isinstance(location, Mapping):
        return None
    for key in ("table", "topic", "path", "stream"):
        value = location.get(key)
        if value:
            return str(value)
    return None


def _field_to_property(
    fld: Mapping[str, Any], provider: Optional[str], include_quality: bool = True
) -> Dict[str, Any]:
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
        # A physical type whose name cannot carry its own parameters needs them
        # somewhere: an Avro decimal is ``bytes`` + logicalType ``decimal`` +
        # precision + scale, and physicalType is a bare string. Only written on
        # a fresh export — an imported field reproduces the source document's
        # own ``customProperties`` verbatim through the pass-through loop below,
        # which must stay the round-trip fixed point.
        if "custom_properties" not in pt:
            custom = physical_type_custom_properties(fld.get("type", "string"), provider)
            if custom:
                prop["customProperties"] = custom

    # logicalTypeOptions: ODCS keeps the type *parameters* here because
    # logicalType is a nine-value enum with nowhere to put precision/scale/
    # length. Without this a `decimal(18,4)` money column and a `decimal(38,0)`
    # key are indistinguishable in the published contract.
    if "logical_type_options" not in pt:
        options = logical_type_options(fld.get("type", "string"))
        if options:
            prop["logicalTypeOptions"] = options

    if fld.get("description"):
        prop["description"] = fld["description"]

    # required: ODCS default is False. Emit True whenever the field is
    # required. Otherwise emit an explicit ``required: false`` in two cases so
    # export stays a round-trip fixed point:
    #   - fresh FLUID exports always write it (downstream consumers expect it);
    #   - imported fields whose source ODCS carried ``required`` explicitly
    #     (``required_present``) reproduce it verbatim — a FLUID-emitted ODCS
    #     always has ``required``, so its re-export must too.
    if fld.get("required"):
        prop["required"] = True
    elif not is_imported or pt.get("required_present"):
        prop["required"] = False

    if fld.get("classification"):
        prop["classification"] = fld["classification"]
    # First-class on both sides. Written before the pass-through loop below so
    # a legacy ``business_name`` bucket (contracts exported by a build that
    # stored it there) still wins, but a hand-written FLUID column no longer
    # loses its businessName on the way out.
    if fld.get("businessName"):
        prop["businessName"] = fld["businessName"]
    if fld.get("tags"):
        prop["tags"] = list(fld["tags"])

    # Property-level quality: delegated to mappers.quality.to_odcs_property
    # which reads field.quality and synthesises auto-checks when needed.
    #
    # ``include_quality`` is the provider's ``include_quality_checks``
    # (ODCS_INCLUDE_QUALITY / ``fluid generate standard --no-quality``), threaded
    # down from ``to_odcs`` the same way ``provider`` is. The call used to be
    # unconditional, so the switch suppressed only the contract-level block and
    # the declared-quality pass in the quality mapper: a user who turned quality
    # off still got a derived not-null rule on every required column and a
    # uniqueness rule on every primary key. Off now means off everywhere.
    if include_quality:
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
        ("transform_source_objects", "transformSourceObjects"),
        ("transform_logic", "transformLogic"),
        ("transform_description", "transformDescription"),
    ):
        if src in pt:
            prop[dst] = pt[src]

    # primaryKey: prefer the verbatim pass-through, else infer from FLUID tag
    if pt.get("primary_key"):
        prop["primaryKey"] = True
    elif fld.get("tags") and any(t in ("primary-key", "primaryKey") for t in fld["tags"]):
        prop["primaryKey"] = True

    _add_canonical_type(prop, fld, provider, include_quality)
    return prop


def _add_canonical_type(
    prop: Dict[str, Any], fld: Mapping[str, Any], provider: Optional[str], include_quality: bool
) -> None:
    """Add the native ODCS tree and the canonical FLUID type of a nested column.

    A scalar column exports as it always has, whatever its type: ``fluid.recursiveType``
    is for nested types, which ODCS cannot hold as FLUID declares them.
    """
    if not {"fields", "element", "key", "value"} & fld.keys():
        return
    from fluid_build.streaming.types import parse_type

    dtype = parse_type(fld, path=str(fld.get("name")))
    if dtype.kind == "struct":
        prop["properties"] = [
            _field_to_property(child, provider, include_quality) for child in fld["fields"]
        ]
    elif dtype.kind == "array":
        prop["items"] = _field_to_property(
            {"name": "element", **fld["element"]}, provider, include_quality
        )
    # ODCS has native object/array trees, but no typed-map/key nullability
    # model. Preserve the canonical FLUID type alongside that native view.
    extension = {
        "property": "fluid.recursiveType",
        "value": copy.deepcopy(
            {
                key: value
                for key, value in fld.items()
                if key in {"type", "fields", "element", "key", "value"}
            }
        ),
    }
    custom = [
        item
        for item in prop.get("customProperties") or []
        if item.get("property") != "fluid.recursiveType"
    ]
    prop["customProperties"] = [*custom, extension]
