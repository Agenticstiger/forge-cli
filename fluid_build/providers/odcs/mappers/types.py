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

"""Pure type-mapping tables: FLUID ↔ ODCS logicalType and provider physical types."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

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


# The FLUID column-type schema is deliberately generous: alongside the bare
# enum it blesses a *parameterized* spelling via a regex branch — ``decimal(18,4)``,
# ``varchar(255)``, ``timestamp_ntz(9)``. Every lookup below therefore has to
# split the parameter suffix off before consulting the tables; a whole-string
# lookup sends every parameterized column to the ``string`` default and throws
# away exactly the precision/scale/length the contract took the trouble to state.
_PARAMETERIZED_TYPE_RE = re.compile(
    r"^\s*(?P<base>[A-Za-z_][A-Za-z0-9_]*(?:\s+[A-Za-z_][A-Za-z0-9_]*)*?)"
    r"\s*(?:\(\s*(?P<params>[^()]*?)\s*\))?\s*$"
)


def split_parameterized(fluid_type: str) -> Tuple[str, Tuple[str, ...]]:
    """Split a FLUID column type into its base name and parameter tuple.

    ``"decimal(18,4)"`` → ``("decimal", ("18", "4"))``;
    ``"VARCHAR( 255 )"`` → ``("varchar", ("255",))``;
    ``"timestamp"``     → ``("timestamp", ())``.

    The base is lower-cased so the tables stay case-insensitive. A type that
    does not match the parameterized grammar is returned lower-cased with no
    parameters, which keeps the callers' behaviour identical to a plain lookup.
    """
    raw = str(fluid_type).strip()
    match = _PARAMETERIZED_TYPE_RE.match(raw)
    if not match:
        return raw.lower(), ()
    base = re.sub(r"\s+", " ", match.group("base")).lower()
    params_raw = match.group("params")
    if not params_raw:
        return base, ()
    params = tuple(p.strip() for p in params_raw.split(",") if p.strip())
    return base, params


def fluid_to_logical(fluid_type: str) -> str:
    """FLUID type → ODCS logicalType.

    Parameter suffixes are stripped before lookup so ``decimal(18,4)`` maps to
    the same ``number`` as bare ``decimal``. An unmapped base type still falls
    back to ``string`` (ODCS has no "unknown"), but it now says so out loud
    rather than degrading in silence.
    """
    base, _ = split_parameterized(fluid_type)
    logical = _FLUID_TYPE_TO_ODCS_LOGICAL.get(base)
    if logical is None:
        logger.warning(
            "No ODCS logicalType mapping for FLUID column type %r — "
            "falling back to 'string'. Add it to _FLUID_TYPE_TO_ODCS_LOGICAL "
            "in fluid_build/providers/odcs/mappers/types.py.",
            fluid_type,
        )
        return "string"
    return logical


def logical_type_options(fluid_type: str) -> Dict[str, Any]:
    """FLUID type parameters → ODCS ``logicalTypeOptions``.

    ``logicalTypeOptions`` is validated per-logicalType by the ODCS v3.1.0
    schema (``additionalProperties: false`` inside each ``if/then`` branch),
    and only the ``string`` branch has a slot for a type parameter —
    ``maxLength``. The ``number``/``integer`` branches deliberately do **not**
    carry precision/scale: their only ``format`` is a Rust-float enum
    (``f32``/``f64``). The spec's home for a parameterized type is
    ``physicalType``, documented as "For example, VARCHAR(2), DOUBLE, INT" —
    which :func:`fluid_to_physical` now fills in with the parameters attached.

    Returns ``{}`` when the type has no parameters, has non-numeric parameters
    (an ``enum`` value list), or has no conformant options slot, so callers can
    emit the key only when it has content.
    """
    base, params = split_parameterized(fluid_type)
    if not params:
        return {}
    numeric: List[int] = []
    for param in params:
        if not param.isdigit():
            return {}
        numeric.append(int(param))
    if not numeric:
        return {}

    if _FLUID_TYPE_TO_ODCS_LOGICAL.get(base, "string") == "string":
        return {"maxLength": numeric[0]}
    return {}


def logical_to_fluid(logical_type: str) -> str:
    """ODCS logicalType → FLUID type (best-effort, defaults to ``string``)."""
    return _LOGICAL_TO_FLUID.get(str(logical_type).lower(), "string")


def fluid_type_from_odcs(
    logical_type: Optional[str],
    physical_type: Optional[str] = None,
    logical_options: Optional[Mapping[str, Any]] = None,
) -> str:
    """ODCS property types → the most faithful FLUID column type.

    ``logicalType`` alone is lossy — ODCS has nine of them and FLUID has
    seventy-odd. Recover the precision the source document actually carried,
    in order of trustworthiness:

    1. ``physicalType`` when its base name is a type FLUID knows
       (``NUMBER(18,4)`` → ``number(18,4)``) — this is the source system's
       own spelling and the most specific signal available;
    2. ``logicalType`` + ``logicalTypeOptions`` (``number`` + ``{precision,
       scale}`` → ``decimal(18,4)``);
    3. bare ``logicalType``.
    """
    if physical_type:
        base, params = split_parameterized(physical_type)
        if base in _FLUID_TYPE_TO_ODCS_LOGICAL:
            return f"{base}({','.join(params)})" if params else base

    fluid_base = logical_to_fluid(logical_type or "string")
    options = logical_options if isinstance(logical_options, Mapping) else {}
    precision = options.get("precision")
    scale = options.get("scale")
    max_length = options.get("maxLength")

    if isinstance(precision, int):
        # ODCS ``number`` imports as FLUID ``double``; a precision/scale pair
        # is fixed-point, so name it as such.
        if fluid_base in ("double", "float", "decimal", "numeric", "number"):
            fluid_base = "decimal"
        if isinstance(scale, int):
            return f"{fluid_base}({precision},{scale})"
        return f"{fluid_base}({precision})"
    if isinstance(max_length, int) and fluid_base == "string":
        return f"varchar({max_length})"
    return fluid_base


# Provider physical-type tables. Both are exhaustive against the FLUID column
# -type enum (the same single source of truth the logicalType map above tracks)
# — see the drift guard in tests/providers/test_odcs_type_mapping.py. A missing
# entry used to emit *no* ``physicalType`` at all, which reads as "FLUID has no
# opinion" when in fact the contract stated one.
_PHYSICAL_BY_PROVIDER: Dict[Tuple[str, ...], Dict[str, str]] = {
    ("gcp", "bigquery"): {
        # string family
        "string": "STRING",
        "text": "STRING",
        "varchar": "STRING",
        "varchar2": "STRING",
        "nvarchar": "STRING",
        "char": "STRING",
        "nchar": "STRING",
        "character": "STRING",
        "clob": "STRING",
        "uuid": "STRING",
        "uniqueidentifier": "STRING",
        "guid": "STRING",
        "enum": "STRING",
        "hll": "BYTES",
        "interval": "INTERVAL",
        # integer family
        "int": "INT64",
        "integer": "INT64",
        "int2": "INT64",
        "int4": "INT64",
        "int8": "INT64",
        "int16": "INT64",
        "int32": "INT64",
        "int64": "INT64",
        "tinyint": "INT64",
        "smallint": "INT64",
        "mediumint": "INT64",
        "bigint": "INT64",
        "long": "INT64",
        "longint": "INT64",
        "serial": "INT64",
        "bigserial": "INT64",
        "year": "INT64",
        # number family
        "float": "FLOAT64",
        "float4": "FLOAT64",
        "float8": "FLOAT64",
        "float32": "FLOAT64",
        "float64": "FLOAT64",
        "double": "FLOAT64",
        "real": "FLOAT64",
        "decimal": "NUMERIC",
        "dec": "NUMERIC",
        "numeric": "NUMERIC",
        "number": "NUMERIC",
        "bignumeric": "BIGNUMERIC",
        "money": "NUMERIC",
        # boolean family
        "bool": "BOOL",
        "boolean": "BOOL",
        "bit": "BOOL",
        # temporal family
        "date": "DATE",
        "time": "TIME",
        "datetime": "DATETIME",
        "datetime2": "DATETIME",
        "smalldatetime": "DATETIME",
        "timestamp": "TIMESTAMP",
        "timestamptz": "TIMESTAMP",
        "timestamp_tz": "TIMESTAMP",
        "timestamp_ntz": "DATETIME",
        "timestamp_ltz": "TIMESTAMP",
        "timestampntz": "DATETIME",
        # structured / semi-structured
        "json": "JSON",
        "jsonb": "JSON",
        "object": "STRUCT",
        "variant": "JSON",
        "super": "JSON",
        "struct": "STRUCT",
        "map": "STRUCT",
        "record": "STRUCT",
        "row": "STRUCT",
        "array": "ARRAY",
        # binary family
        "binary": "BYTES",
        "varbinary": "BYTES",
        "bytes": "BYTES",
        "blob": "BYTES",
        "bytea": "BYTES",
        "raw": "BYTES",
        # geospatial family
        "geography": "GEOGRAPHY",
        "geometry": "GEOGRAPHY",
        "geom": "GEOGRAPHY",
        "point": "GEOGRAPHY",
    },
    ("snowflake",): {
        # string family
        "string": "VARCHAR",
        "text": "TEXT",
        "varchar": "VARCHAR",
        "varchar2": "VARCHAR",
        "nvarchar": "VARCHAR",
        "char": "CHAR",
        "nchar": "CHAR",
        "character": "CHAR",
        "clob": "VARCHAR",
        "uuid": "VARCHAR",
        "uniqueidentifier": "VARCHAR",
        "guid": "VARCHAR",
        "enum": "VARCHAR",
        "hll": "BINARY",
        "interval": "VARCHAR",
        # integer family — Snowflake models every integer as NUMBER(38,0)
        "int": "NUMBER",
        "integer": "NUMBER",
        "int2": "NUMBER",
        "int4": "NUMBER",
        "int8": "NUMBER",
        "int16": "NUMBER",
        "int32": "NUMBER",
        "int64": "NUMBER",
        "tinyint": "NUMBER",
        "smallint": "NUMBER",
        "mediumint": "NUMBER",
        "bigint": "NUMBER",
        "long": "NUMBER",
        "longint": "NUMBER",
        "serial": "NUMBER",
        "bigserial": "NUMBER",
        "year": "NUMBER",
        # number family
        "float": "FLOAT",
        "float4": "FLOAT",
        "float8": "FLOAT",
        "float32": "FLOAT",
        "float64": "FLOAT",
        "double": "DOUBLE",
        "real": "REAL",
        "decimal": "DECIMAL",
        "dec": "DECIMAL",
        "numeric": "DECIMAL",
        "number": "NUMBER",
        "bignumeric": "NUMBER",
        "money": "NUMBER",
        # boolean family
        "bool": "BOOLEAN",
        "boolean": "BOOLEAN",
        "bit": "BOOLEAN",
        # temporal family
        "date": "DATE",
        "time": "TIME",
        "datetime": "TIMESTAMP_NTZ",
        "datetime2": "TIMESTAMP_NTZ",
        "smalldatetime": "TIMESTAMP_NTZ",
        "timestamp": "TIMESTAMP_NTZ",
        "timestamptz": "TIMESTAMP_TZ",
        "timestamp_tz": "TIMESTAMP_TZ",
        "timestamp_ntz": "TIMESTAMP_NTZ",
        "timestamp_ltz": "TIMESTAMP_LTZ",
        "timestampntz": "TIMESTAMP_NTZ",
        # structured / semi-structured
        "json": "VARIANT",
        "jsonb": "VARIANT",
        "object": "OBJECT",
        "variant": "VARIANT",
        "super": "VARIANT",
        "struct": "OBJECT",
        "map": "OBJECT",
        "record": "OBJECT",
        "row": "OBJECT",
        "array": "ARRAY",
        # binary family
        "binary": "BINARY",
        "varbinary": "VARBINARY",
        "bytes": "BINARY",
        "blob": "BINARY",
        "bytea": "BINARY",
        "raw": "BINARY",
        # geospatial family
        "geography": "GEOGRAPHY",
        "geometry": "GEOMETRY",
        "geom": "GEOMETRY",
        "point": "GEOGRAPHY",
    },
}


def fluid_to_physical(fluid_type: str, provider: Optional[str]) -> Optional[str]:
    """FLUID type → provider-specific physical type.

    Any parameter suffix the FLUID type carries is re-attached to the physical
    name, so ``decimal(18,4)`` on Snowflake renders ``DECIMAL(18,4)`` — the
    same thing the IaC compiler puts in the CREATE TABLE. Falls back to the
    logicalType when the provider is unknown.
    """
    base, params = split_parameterized(fluid_type)
    if not provider:
        return fluid_to_logical(fluid_type)
    prov = provider.lower()
    for keys, table in _PHYSICAL_BY_PROVIDER.items():
        if prov in keys:
            physical = table.get(base)
            if physical is None:
                return None
            return f"{physical}({','.join(params)})" if params else physical
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


# ODCS server.type → FLUID ``binding.platform``. This is the *authoritative*
# binding signal on import: ``servers[].type`` names the system the data
# actually lives in. ``physicalType`` (below) only names the object kind.
# Values are constrained to the FLUID binding.platform enum.
_SERVER_TYPE_TO_PLATFORM: Mapping[str, str] = {
    "bigquery": "gcp",
    "snowflake": "snowflake",
    "s3": "aws",
    "redshift": "aws",
    "athena": "aws",
    "glue": "aws",
    "kinesis": "aws",
    "azure": "azure",
    "synapse": "azure",
    "databricks": "databricks",
    "postgres": "postgres",
    "postgresql": "postgres",
    "kafka": "kafka",
    "local": "local",
}


def server_type_to_platform(server_type: str) -> str:
    """ODCS ``servers[].type`` → FLUID ``binding.platform`` (enum-safe)."""
    return _SERVER_TYPE_TO_PLATFORM.get(str(server_type).lower(), "other")


# ODCS SchemaObject.physicalType (table/view/topic/file) → FLUID binding.platform.
# Only ``topic`` carries an unambiguous platform signal; a "table" exists on
# every warehouse there is, so guessing one (this map used to answer "bigquery")
# invents a binding the source document never stated. Everything else resolves
# to the FLUID enum's honest escape hatch and is expected to be overridden by
# the matching ``servers[]`` entry.
_PHYSICAL_TYPE_TO_PLATFORM: Mapping[str, str] = {
    "topic": "kafka",
}


def physical_type_to_platform(physical_type: str) -> str:
    return _PHYSICAL_TYPE_TO_PLATFORM.get(str(physical_type).lower(), "other")


# FLUID ``binding.platform`` + ODCS physicalType → FLUID ``binding.format``
# (required by the FLUID schema, and a closed enum there too).
_PLATFORM_PHYSICAL_TO_FORMAT: Mapping[Tuple[str, str], str] = {
    ("snowflake", "table"): "snowflake_table",
    ("snowflake", "view"): "snowflake_view",
    ("gcp", "table"): "bigquery_table",
    ("gcp", "view"): "bigquery_table",
    ("gcp", "file"): "gcs_file",
    ("aws", "table"): "athena_table",
    ("aws", "view"): "athena_table",
    ("aws", "file"): "s3_file",
    ("postgres", "table"): "postgres_table",
    ("postgres", "view"): "postgres_table",
    ("kafka", "topic"): "kafka_topic",
    ("databricks", "table"): "delta_table",
    ("databricks", "view"): "delta_table",
}


def binding_format(platform: str, physical_type: Optional[str]) -> str:
    """FLUID ``binding.format`` for a (platform, ODCS physicalType) pair."""
    key = (str(platform).lower(), str(physical_type or "table").lower())
    return _PLATFORM_PHYSICAL_TO_FORMAT.get(key, "other")


# ODCS SchemaObject.physicalType → FLUID ``expose.kind`` (required, closed enum).
_PHYSICAL_TYPE_TO_EXPOSE_KIND: Mapping[str, str] = {
    "table": "table",
    "view": "view",
    "topic": "topic",
    "file": "file",
    "stream": "stream",
    "api": "api",
}


def physical_type_to_expose_kind(physical_type: Optional[str]) -> str:
    return _PHYSICAL_TYPE_TO_EXPOSE_KIND.get(str(physical_type or "").lower(), "other")


# Status mapping (shared by both directions).
#
# The FLUID-side vocabulary is ``lifecycle.state``
# (preview/active/deprecated/retired) — see $defs.lifecycleState. ODCS v3.1.0
# documents status as proposed/draft/active/deprecated/retired, so ``preview``
# lands on ``draft`` and every other state maps straight across. Legacy
# ``draft``/``development`` spellings are accepted for contracts that still
# carry a hand-written status string.
_FLUID_TO_ODCS_STATUS: Dict[str, str] = {
    "preview": "draft",
    "proposed": "proposed",
    "draft": "draft",
    "active": "active",
    "deprecated": "deprecated",
    "retired": "retired",
    "development": "draft",
}

_ODCS_TO_FLUID_STATUS: Dict[str, str] = {
    "proposed": "preview",
    "draft": "preview",
    "active": "active",
    "deprecated": "deprecated",
    "retired": "retired",
}


def fluid_to_odcs_status(status: str) -> str:
    return _FLUID_TO_ODCS_STATUS.get(str(status).lower(), "active")


def odcs_to_fluid_status(status: str) -> str:
    """ODCS status → FLUID ``lifecycle.state`` (the enum the schema accepts)."""
    return _ODCS_TO_FLUID_STATUS.get(str(status).lower(), "active")
