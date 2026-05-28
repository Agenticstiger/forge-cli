# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Pure type-mapping tables: FLUID ↔ ODCS logicalType and provider physical types."""

from __future__ import annotations

from typing import Dict, Mapping, Optional, Tuple

# FLUID source type → ODCS v3.1.0 logicalType.
# ODCS logicalType enum: string, date, time, timestamp, integer, number,
# object, array, boolean.
#
# The map is exhaustive against the FLUID 0.7.3 column-type enum
# (fluid_build/schemas/fluid-schema-0.7.3.json $defs.column.properties.type)
# so a new column type can never silently degrade to the "string" default
# and lose type fidelity in published ODCS contracts. The drift guard in
# tests/providers/test_odcs_type_mapping.py fails if any FLUID schema type
# is missing here.
_FLUID_TYPE_TO_ODCS_LOGICAL: Dict[str, str] = {
    # string family
    "string": "string",
    "text": "string",
    "varchar": "string",
    "varchar2": "string",
    "nvarchar": "string",
    "char": "string",
    "nchar": "string",
    "character": "string",
    "clob": "string",
    "uuid": "string",
    "uniqueidentifier": "string",
    "guid": "string",
    "enum": "string",
    # binary family — ODCS has no binary logicalType; binary columns
    # serialize to text/base64, so "string" is the honest mapping.
    "binary": "string",
    "varbinary": "string",
    "bytes": "string",
    "blob": "string",
    "bytea": "string",
    "raw": "string",
    "hll": "string",
    # geospatial family — ODCS has no geo logicalType; geo values
    # serialize to WKT / GeoJSON text.
    "geography": "string",
    "geometry": "string",
    "geom": "string",
    "point": "string",
    # integer family
    "int": "integer",
    "integer": "integer",
    "int2": "integer",
    "int4": "integer",
    "int8": "integer",
    "int16": "integer",
    "int32": "integer",
    "int64": "integer",
    "tinyint": "integer",
    "smallint": "integer",
    "mediumint": "integer",
    "bigint": "integer",
    "long": "integer",
    "longint": "integer",
    "serial": "integer",
    "bigserial": "integer",
    "year": "integer",
    # number family (floating-point + fixed-point)
    "float": "number",
    "float4": "number",
    "float8": "number",
    "float32": "number",
    "float64": "number",
    "double": "number",
    "real": "number",
    "decimal": "number",
    "dec": "number",
    "numeric": "number",
    "number": "number",
    "bignumeric": "number",
    "money": "number",
    # boolean family
    "bool": "boolean",
    "boolean": "boolean",
    "bit": "boolean",
    # temporal family
    "date": "date",
    "time": "time",
    "datetime": "timestamp",
    "datetime2": "timestamp",
    "smalldatetime": "timestamp",
    "timestamp": "timestamp",
    "timestamptz": "timestamp",
    "timestamp_tz": "timestamp",
    "timestamp_ntz": "timestamp",
    "timestamp_ltz": "timestamp",
    "timestampntz": "timestamp",
    "interval": "string",  # ODCS has no interval/duration logicalType
    # structured / semi-structured family
    "json": "object",
    "jsonb": "object",
    "object": "object",
    "variant": "object",
    "super": "object",
    "struct": "object",
    "map": "object",
    "record": "object",
    "row": "object",
    "array": "array",
}


# ODCS logicalType → FLUID type (lossy; physicalType pass-through preserves
# the original source-system flavour)
_LOGICAL_TO_FLUID: Dict[str, str] = {
    "string": "string",
    "integer": "int",
    "long": "bigint",
    "float": "float",
    "double": "double",
    "decimal": "decimal",
    "number": "double",
    "boolean": "bool",
    "date": "date",
    "timestamp": "timestamp",
    "time": "time",
    "object": "object",
    "array": "array",
    "binary": "binary",
}


def fluid_to_logical(fluid_type: str) -> str:
    """FLUID type → ODCS logicalType (best-effort, defaults to ``string``)."""
    return _FLUID_TYPE_TO_ODCS_LOGICAL.get(str(fluid_type).lower(), "string")


def logical_to_fluid(logical_type: str) -> str:
    """ODCS logicalType → FLUID type (best-effort, defaults to ``string``)."""
    return _LOGICAL_TO_FLUID.get(str(logical_type).lower(), "string")


_PHYSICAL_BY_PROVIDER: Dict[Tuple[str, ...], Dict[str, str]] = {
    ("gcp", "bigquery"): {
        "string": "STRING",
        "text": "STRING",
        "int": "INT64",
        "integer": "INT64",
        "bigint": "INT64",
        "long": "INT64",
        "float": "FLOAT64",
        "double": "FLOAT64",
        "decimal": "NUMERIC",
        "numeric": "NUMERIC",
        "bool": "BOOL",
        "boolean": "BOOL",
        "date": "DATE",
        "datetime": "DATETIME",
        "timestamp": "TIMESTAMP",
        "time": "TIME",
        "json": "JSON",
        "object": "STRUCT",
        "array": "ARRAY",
        "binary": "BYTES",
        "bytes": "BYTES",
    },
    ("snowflake",): {
        "string": "VARCHAR",
        "text": "TEXT",
        "int": "NUMBER",
        "integer": "NUMBER",
        "bigint": "NUMBER",
        "long": "NUMBER",
        "float": "FLOAT",
        "double": "DOUBLE",
        "decimal": "DECIMAL",
        "numeric": "DECIMAL",
        "bool": "BOOLEAN",
        "boolean": "BOOLEAN",
        "date": "DATE",
        "datetime": "TIMESTAMP_NTZ",
        "timestamp": "TIMESTAMP_NTZ",
        "time": "TIME",
        "json": "VARIANT",
        "object": "OBJECT",
        "array": "ARRAY",
        "binary": "BINARY",
        "bytes": "BINARY",
    },
}


def fluid_to_physical(fluid_type: str, provider: Optional[str]) -> Optional[str]:
    """FLUID type → provider-specific physical type. Falls back to logicalType."""
    if not provider:
        return fluid_to_logical(fluid_type)
    prov = provider.lower()
    for keys, table in _PHYSICAL_BY_PROVIDER.items():
        if prov in keys:
            return table.get(str(fluid_type).lower())
    return fluid_to_logical(fluid_type)


# ODCS server.type ↔ FLUID provider
_PROVIDER_TO_SERVER_TYPE: Dict[str, str] = {
    "gcp": "bigquery",
    "bigquery": "bigquery",
    "snowflake": "snowflake",
    "aws": "s3",
    "s3": "s3",
    "redshift": "redshift",
    "athena": "athena",
    "azure": "azure",
    "databricks": "databricks",
    "postgres": "postgres",
    "postgresql": "postgres",
    "mysql": "mysql",
    "kafka": "kafka",
    "mongodb": "mongodb",
    "elasticsearch": "elasticsearch",
    "local": "local",
}

_SERVER_TYPE_TO_PROVIDER: Dict[str, str] = {
    "bigquery": "gcp",
    "snowflake": "snowflake",
    "s3": "aws",
    "redshift": "aws",
    "athena": "aws",
    "azure": "azure",
    "databricks": "databricks",
    "postgres": "postgres",
    "mysql": "mysql",
    "kafka": "kafka",
    "mongodb": "mongodb",
    "elasticsearch": "elasticsearch",
    "local": "local",
}


def provider_to_server_type(provider: str) -> str:
    return _PROVIDER_TO_SERVER_TYPE.get(provider.lower(), "custom")


def server_type_to_provider(server_type: str) -> str:
    return _SERVER_TYPE_TO_PROVIDER.get(server_type.lower(), "custom")


# ODCS SchemaObject.physicalType (table/view/topic/file) → FLUID binding.platform
_PHYSICAL_TYPE_TO_PLATFORM: Mapping[str, str] = {
    "topic": "kafka",
    "table": "bigquery",
    "view": "bigquery",
    "file": "s3",
}


def physical_type_to_platform(physical_type: str) -> str:
    return _PHYSICAL_TYPE_TO_PLATFORM.get(str(physical_type).lower(), "custom")


# Status mapping (shared by both directions)
_FLUID_TO_ODCS_STATUS: Dict[str, str] = {
    "draft": "draft",
    "active": "active",
    "deprecated": "deprecated",
    "retired": "retired",
    "development": "draft",
}

_ODCS_TO_FLUID_STATUS: Dict[str, str] = {
    "draft": "draft",
    "active": "active",
    "deprecated": "deprecated",
    "retired": "retired",
}


def fluid_to_odcs_status(status: str) -> str:
    return _FLUID_TO_ODCS_STATUS.get(str(status).lower(), "active")


def odcs_to_fluid_status(status: str) -> str:
    return _ODCS_TO_FLUID_STATUS.get(str(status).lower(), "active")
