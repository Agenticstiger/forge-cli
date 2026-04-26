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

"""Inline logical-types and per-dialect physical-type registry.

The tables below mirror the shape of Model AI's
``tmp__data/logical_data_types_master_v1.json`` +
``tmp__data/mappings/<db>.json`` but ship as Python dicts so forge-cli
stays file-less. Each dialect entry covers the logical types the modeler
is most likely to emit (≈25 core types per dialect); unknown logical
types fall through to :func:`DialectMapper.map_type`'s pass-through
behaviour with a warning.

Adding a new dialect is a four-line contribution to :data:`DIALECTS`.
Adding a new logical type means adding a new ``LogicalTypeSpec`` here
**and** one entry per dialect — keeping both sides of that contract is
the whole point of having this registry.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

# ----------------------------------------------------------------------
# Logical types master registry
# ----------------------------------------------------------------------


class LogicalTypeSpec(TypedDict, total=False):
    """One row in the logical-types master — category, qualifiers, defaults.

    Fields align with Model AI's JSON shape so an operator who memorized
    that spec sees the same keys here.
    """

    id: str
    category: str
    description: str
    qualifiers: List[str]
    defaults: Dict[str, Any]
    tags: List[str]


REGISTRY_VERSION = "1.0"
"""Goes into the cache key so a registry bump invalidates mapper results
in the LLM cache without requiring manual ``fluid memory clear``."""


DEFAULTS: Dict[str, Any] = {
    "string_length": 255,
    "decimal_precision": 18,
    "decimal_scale": 4,
    "timestamp_timezone": "UTC",
}


LOGICAL_TYPES: Dict[str, LogicalTypeSpec] = {
    "IDENTIFIER": {
        "id": "IDENTIFIER",
        "category": "identifier",
        "description": "Unique identifier (UUID/sequence/hash).",
        "qualifiers": ["format", "length"],
        "defaults": {"length": 36},
        "tags": ["pk", "id"],
    },
    "REFERENCE": {
        "id": "REFERENCE",
        "category": "relationship",
        "description": "Foreign-key reference to another entity.",
        "qualifiers": ["target_entity", "nullable"],
        "defaults": {"length": 36},
        "tags": ["fk", "link"],
    },
    "STRING": {
        "id": "STRING",
        "category": "textual",
        "description": "Variable-length textual data.",
        "qualifiers": ["length", "collation"],
        "defaults": {"length": 255},
        "tags": ["varchar"],
    },
    "TEXT": {
        "id": "TEXT",
        "category": "textual",
        "description": "Long/unbounded textual content.",
        "qualifiers": [],
        "defaults": {},
        "tags": ["clob", "longtext"],
    },
    "ENUM": {
        "id": "ENUM",
        "category": "textual",
        "description": "Controlled vocabulary of string values.",
        "qualifiers": ["allowed_values", "length"],
        "defaults": {"length": 64},
        "tags": ["domain"],
    },
    "INTEGER": {
        "id": "INTEGER",
        "category": "numeric",
        "description": "Whole numbers (32-bit typical).",
        "qualifiers": [],
        "defaults": {},
        "tags": ["int"],
    },
    "BIGINT": {
        "id": "BIGINT",
        "category": "numeric",
        "description": "Large whole numbers (64-bit).",
        "qualifiers": [],
        "defaults": {},
        "tags": ["long"],
    },
    "DECIMAL": {
        "id": "DECIMAL",
        "category": "numeric",
        "description": "Fixed-point decimal with precision/scale.",
        "qualifiers": ["precision", "scale"],
        "defaults": {"precision": 18, "scale": 4},
        "tags": ["money"],
    },
    "FLOAT": {
        "id": "FLOAT",
        "category": "numeric",
        "description": "Double-precision floating point.",
        "qualifiers": [],
        "defaults": {},
        "tags": ["double"],
    },
    "BOOLEAN": {
        "id": "BOOLEAN",
        "category": "boolean",
        "description": "True/false.",
        "qualifiers": [],
        "defaults": {},
        "tags": ["flag"],
    },
    "DATE": {
        "id": "DATE",
        "category": "temporal",
        "description": "Calendar date, no time.",
        "qualifiers": [],
        "defaults": {},
        "tags": [],
    },
    "TIME": {
        "id": "TIME",
        "category": "temporal",
        "description": "Time-of-day without date.",
        "qualifiers": [],
        "defaults": {},
        "tags": [],
    },
    "DATETIME": {
        "id": "DATETIME",
        "category": "temporal",
        "description": "Date + time without timezone.",
        "qualifiers": [],
        "defaults": {},
        "tags": ["ntz"],
    },
    "TIMESTAMP": {
        "id": "TIMESTAMP",
        "category": "temporal",
        "description": "Date + time with timezone awareness.",
        "qualifiers": ["timezone"],
        "defaults": {"timezone": "UTC"},
        "tags": ["tz"],
    },
    "BINARY": {
        "id": "BINARY",
        "category": "binary",
        "description": "Binary blob.",
        "qualifiers": [],
        "defaults": {},
        "tags": [],
    },
    "JSON": {
        "id": "JSON",
        "category": "semi_structured",
        "description": "Nested/semi-structured payload.",
        "qualifiers": [],
        "defaults": {},
        "tags": ["variant"],
    },
    "ARRAY": {
        "id": "ARRAY",
        "category": "semi_structured",
        "description": "Ordered collection of a single element type.",
        "qualifiers": ["element_type"],
        "defaults": {"element_type": "STRING"},
        "tags": [],
    },
    "MAP": {
        "id": "MAP",
        "category": "semi_structured",
        "description": "Key/value pairs with typed keys and values.",
        "qualifiers": ["key_type", "value_type"],
        "defaults": {"key_type": "STRING", "value_type": "STRING"},
        "tags": [],
    },
    "CURRENCY": {
        "id": "CURRENCY",
        "category": "numeric",
        "description": "Monetary amount — stored as DECIMAL with a paired currency code.",
        "qualifiers": ["precision", "scale"],
        "defaults": {"precision": 18, "scale": 4},
        "tags": ["money"],
    },
    "EMAIL": {
        "id": "EMAIL",
        "category": "textual",
        "description": "RFC-5321 email address.",
        "qualifiers": [],
        "defaults": {"length": 254},
        "tags": ["pii"],
    },
    "PHONE": {
        "id": "PHONE",
        "category": "textual",
        "description": "E.164 phone number string.",
        "qualifiers": [],
        "defaults": {"length": 32},
        "tags": ["pii"],
    },
    "URL": {
        "id": "URL",
        "category": "textual",
        "description": "URL string.",
        "qualifiers": [],
        "defaults": {"length": 2048},
        "tags": [],
    },
    "HASH": {
        "id": "HASH",
        "category": "textual",
        "description": "Hex-encoded hash digest (md5/sha256).",
        "qualifiers": [],
        "defaults": {"length": 128},
        "tags": ["dv2"],
    },
}


# ----------------------------------------------------------------------
# Dialect mappings — each row: logical → (physical, supported, lossy, note, rule_id)
# ----------------------------------------------------------------------


class DialectRule(TypedDict, total=False):
    physical: str
    supported: bool
    lossy: bool
    note: Optional[str]
    rule_id: Optional[str]


def _r(
    physical: str,
    *,
    supported: bool = True,
    lossy: bool = False,
    note: Optional[str] = None,
    rule_id: Optional[str] = None,
) -> DialectRule:
    """Shorthand for building a DialectRule inline — the tables below
    would be twice as long without it."""
    rule: DialectRule = {
        "physical": physical,
        "supported": supported,
        "lossy": lossy,
    }
    if note is not None:
        rule["note"] = note
    if rule_id is not None:
        rule["rule_id"] = rule_id
    return rule


SNOWFLAKE_MAPPINGS: Dict[str, DialectRule] = {
    "IDENTIFIER": _r("VARCHAR({length})", rule_id="sf_identifier"),
    "REFERENCE": _r("VARCHAR({length})", rule_id="sf_reference"),
    "STRING": _r("VARCHAR({length})", rule_id="sf_string"),
    "TEXT": _r("TEXT", rule_id="sf_text"),
    "ENUM": _r("VARCHAR({length})", rule_id="sf_enum"),
    "INTEGER": _r("INTEGER", rule_id="sf_integer"),
    "BIGINT": _r("BIGINT", rule_id="sf_bigint"),
    "DECIMAL": _r("NUMBER({precision},{scale})", note="precision cap 38", rule_id="sf_decimal"),
    "FLOAT": _r("FLOAT", rule_id="sf_float"),
    "BOOLEAN": _r("BOOLEAN", rule_id="sf_boolean"),
    "DATE": _r("DATE", rule_id="sf_date"),
    "TIME": _r("TIME", rule_id="sf_time"),
    "DATETIME": _r("TIMESTAMP_NTZ", rule_id="sf_datetime"),
    "TIMESTAMP": _r("TIMESTAMP_TZ", rule_id="sf_timestamp"),
    "BINARY": _r("BINARY", rule_id="sf_binary"),
    "JSON": _r("VARIANT", rule_id="sf_json"),
    "ARRAY": _r("ARRAY", rule_id="sf_array"),
    "MAP": _r("OBJECT", rule_id="sf_map"),
    "CURRENCY": _r("NUMBER({precision},{scale})", rule_id="sf_currency"),
    "EMAIL": _r("VARCHAR(254)", rule_id="sf_email"),
    "PHONE": _r("VARCHAR(32)", rule_id="sf_phone"),
    "URL": _r("VARCHAR(2048)", rule_id="sf_url"),
    "HASH": _r("VARCHAR(128)", rule_id="sf_hash"),
}

BIGQUERY_MAPPINGS: Dict[str, DialectRule] = {
    "IDENTIFIER": _r("STRING", rule_id="bq_identifier"),
    "REFERENCE": _r("STRING", rule_id="bq_reference"),
    "STRING": _r("STRING", rule_id="bq_string"),
    "TEXT": _r("STRING", rule_id="bq_text"),
    "ENUM": _r("STRING", rule_id="bq_enum"),
    "INTEGER": _r("INT64", rule_id="bq_integer"),
    "BIGINT": _r("INT64", rule_id="bq_bigint"),
    "DECIMAL": _r(
        "NUMERIC({precision},{scale})", note="BIGNUMERIC for precision > 38", rule_id="bq_decimal"
    ),
    "FLOAT": _r("FLOAT64", rule_id="bq_float"),
    "BOOLEAN": _r("BOOL", rule_id="bq_boolean"),
    "DATE": _r("DATE", rule_id="bq_date"),
    "TIME": _r("TIME", rule_id="bq_time"),
    "DATETIME": _r("DATETIME", rule_id="bq_datetime"),
    "TIMESTAMP": _r("TIMESTAMP", rule_id="bq_timestamp"),
    "BINARY": _r("BYTES", rule_id="bq_binary"),
    "JSON": _r("JSON", rule_id="bq_json"),
    "ARRAY": _r("ARRAY<{element_type}>", rule_id="bq_array"),
    "MAP": _r(
        "STRUCT",
        lossy=True,
        note="BigQuery has no native MAP; use REPEATED STRUCT<k,v>",
        rule_id="bq_map",
    ),
    "CURRENCY": _r("NUMERIC({precision},{scale})", rule_id="bq_currency"),
    "EMAIL": _r("STRING", rule_id="bq_email"),
    "PHONE": _r("STRING", rule_id="bq_phone"),
    "URL": _r("STRING", rule_id="bq_url"),
    "HASH": _r("STRING", rule_id="bq_hash"),
}

POSTGRES_MAPPINGS: Dict[str, DialectRule] = {
    "IDENTIFIER": _r(
        "UUID", note="use UUID for ID types; TEXT for hash-keys", rule_id="pg_identifier"
    ),
    "REFERENCE": _r("UUID", rule_id="pg_reference"),
    "STRING": _r("VARCHAR({length})", rule_id="pg_string"),
    "TEXT": _r("TEXT", rule_id="pg_text"),
    "ENUM": _r(
        "VARCHAR({length})", note="native ENUM type available via CREATE TYPE", rule_id="pg_enum"
    ),
    "INTEGER": _r("INTEGER", rule_id="pg_integer"),
    "BIGINT": _r("BIGINT", rule_id="pg_bigint"),
    "DECIMAL": _r("NUMERIC({precision},{scale})", rule_id="pg_decimal"),
    "FLOAT": _r("DOUBLE PRECISION", rule_id="pg_float"),
    "BOOLEAN": _r("BOOLEAN", rule_id="pg_boolean"),
    "DATE": _r("DATE", rule_id="pg_date"),
    "TIME": _r("TIME", rule_id="pg_time"),
    "DATETIME": _r("TIMESTAMP", rule_id="pg_datetime"),
    "TIMESTAMP": _r("TIMESTAMPTZ", rule_id="pg_timestamp"),
    "BINARY": _r("BYTEA", rule_id="pg_binary"),
    "JSON": _r("JSONB", rule_id="pg_json"),
    "ARRAY": _r("{element_type}[]", rule_id="pg_array"),
    "MAP": _r(
        "JSONB", lossy=True, note="Postgres has no MAP; use JSONB or HSTORE", rule_id="pg_map"
    ),
    "CURRENCY": _r("NUMERIC({precision},{scale})", rule_id="pg_currency"),
    "EMAIL": _r("VARCHAR(254)", rule_id="pg_email"),
    "PHONE": _r("VARCHAR(32)", rule_id="pg_phone"),
    "URL": _r("VARCHAR(2048)", rule_id="pg_url"),
    "HASH": _r("VARCHAR(128)", rule_id="pg_hash"),
}

DATABRICKS_MAPPINGS: Dict[str, DialectRule] = {
    "IDENTIFIER": _r("STRING", rule_id="db_identifier"),
    "REFERENCE": _r("STRING", rule_id="db_reference"),
    "STRING": _r("STRING", rule_id="db_string"),
    "TEXT": _r("STRING", rule_id="db_text"),
    "ENUM": _r("STRING", rule_id="db_enum"),
    "INTEGER": _r("INT", rule_id="db_integer"),
    "BIGINT": _r("BIGINT", rule_id="db_bigint"),
    "DECIMAL": _r("DECIMAL({precision},{scale})", rule_id="db_decimal"),
    "FLOAT": _r("DOUBLE", rule_id="db_float"),
    "BOOLEAN": _r("BOOLEAN", rule_id="db_boolean"),
    "DATE": _r("DATE", rule_id="db_date"),
    "TIME": _r(
        "STRING",
        lossy=True,
        note="Databricks has no TIME; store as STRING or cast via TIMESTAMP",
        rule_id="db_time",
    ),
    "DATETIME": _r("TIMESTAMP_NTZ", rule_id="db_datetime"),
    "TIMESTAMP": _r("TIMESTAMP", rule_id="db_timestamp"),
    "BINARY": _r("BINARY", rule_id="db_binary"),
    "JSON": _r(
        "STRING", note="use VARIANT in Unity Catalog 14.1+; STRING is portable", rule_id="db_json"
    ),
    "ARRAY": _r("ARRAY<{element_type}>", rule_id="db_array"),
    "MAP": _r("MAP<{key_type},{value_type}>", rule_id="db_map"),
    "CURRENCY": _r("DECIMAL({precision},{scale})", rule_id="db_currency"),
    "EMAIL": _r("STRING", rule_id="db_email"),
    "PHONE": _r("STRING", rule_id="db_phone"),
    "URL": _r("STRING", rule_id="db_url"),
    "HASH": _r("STRING", rule_id="db_hash"),
}

ANSI_SQL_MAPPINGS: Dict[str, DialectRule] = {
    "IDENTIFIER": _r("VARCHAR({length})", rule_id="ansi_identifier"),
    "REFERENCE": _r("VARCHAR({length})", rule_id="ansi_reference"),
    "STRING": _r("VARCHAR({length})", rule_id="ansi_string"),
    "TEXT": _r("CLOB", rule_id="ansi_text"),
    "ENUM": _r("VARCHAR({length})", rule_id="ansi_enum"),
    "INTEGER": _r("INTEGER", rule_id="ansi_integer"),
    "BIGINT": _r("BIGINT", rule_id="ansi_bigint"),
    "DECIMAL": _r("DECIMAL({precision},{scale})", rule_id="ansi_decimal"),
    "FLOAT": _r("DOUBLE PRECISION", rule_id="ansi_float"),
    "BOOLEAN": _r("BOOLEAN", rule_id="ansi_boolean"),
    "DATE": _r("DATE", rule_id="ansi_date"),
    "TIME": _r("TIME", rule_id="ansi_time"),
    "DATETIME": _r("TIMESTAMP", rule_id="ansi_datetime"),
    "TIMESTAMP": _r("TIMESTAMP WITH TIME ZONE", rule_id="ansi_timestamp"),
    "BINARY": _r("BLOB", rule_id="ansi_binary"),
    "JSON": _r(
        "CLOB",
        lossy=True,
        note="ANSI SQL has no JSON primitive; stored as CLOB",
        rule_id="ansi_json",
    ),
    "ARRAY": _r("{element_type} ARRAY", rule_id="ansi_array"),
    "MAP": _r(
        "CLOB",
        lossy=True,
        note="ANSI SQL has no MAP; stored as serialized CLOB",
        rule_id="ansi_map",
    ),
    "CURRENCY": _r("DECIMAL({precision},{scale})", rule_id="ansi_currency"),
    "EMAIL": _r("VARCHAR(254)", rule_id="ansi_email"),
    "PHONE": _r("VARCHAR(32)", rule_id="ansi_phone"),
    "URL": _r("VARCHAR(2048)", rule_id="ansi_url"),
    "HASH": _r("VARCHAR(128)", rule_id="ansi_hash"),
}


DIALECTS: Dict[str, Dict[str, DialectRule]] = {
    "SNOWFLAKE": SNOWFLAKE_MAPPINGS,
    "BIGQUERY": BIGQUERY_MAPPINGS,
    "POSTGRES": POSTGRES_MAPPINGS,
    "DATABRICKS": DATABRICKS_MAPPINGS,
    "ANSI_SQL": ANSI_SQL_MAPPINGS,
}
"""Every supported target dialect, keyed by the canonical UPPERCASE
label. :func:`DialectMapper._normalize_dialect` accepts a few common
aliases (``postgresql`` → ``POSTGRES``, ``databricks-sql`` →
``DATABRICKS``)."""


__all__ = [
    "ANSI_SQL_MAPPINGS",
    "BIGQUERY_MAPPINGS",
    "DATABRICKS_MAPPINGS",
    "DEFAULTS",
    "DialectRule",
    "DIALECTS",
    "LOGICAL_TYPES",
    "LogicalTypeSpec",
    "POSTGRES_MAPPINGS",
    "REGISTRY_VERSION",
    "SNOWFLAKE_MAPPINGS",
]
