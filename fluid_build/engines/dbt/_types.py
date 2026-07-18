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

"""FLUID type → SQL type mapping for dbt artifacts, adapter-aware.

Extracted from :func:`fluid_build.engines.dbt.models._sql_type` so the
mapping has one home shared by the SQL skeleton casts (``models.py``)
and the model-contract ``data_type`` emission (``schema_yml.py``).

Two layers:

- :data:`_GENERIC` — the pre-existing platform-agnostic mapping, kept
  byte-for-byte identical to the old ``models._sql_type`` table. It is
  the default (``adapter=None``) so skeleton casts are unchanged.
- Per-adapter overrides for the four profile adapters this engine can
  emit (``profiles.py``): ``bigquery`` / ``snowflake`` / ``redshift`` /
  ``duckdb``. Generic types alone fail on some adapters — BigQuery has
  no ``varchar`` — so contract ``data_type`` must be adapter-correct.

The per-adapter tables adapt the mapping shape of ``datacontract-cli``'s
``export/sql_type_converter.py`` (MIT), trimmed to the FLUID scalar
types this engine emits. Reference the functions via module attribute
access from call sites (``from . import _types as _types;
_types.sql_type(...)``) so test patches flow through — the repo's
extraction convention.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

# The pre-existing generic mapping — must stay byte-for-byte identical to the
# historical ``models._sql_type`` table (skeleton-cast behavior is pinned).
_GENERIC: Dict[str, str] = {
    "string": "varchar",
    "STRING": "varchar",
    "integer": "integer",
    "INTEGER": "integer",
    "number": "numeric",
    "NUMBER": "numeric",
    "float": "numeric",
    "FLOAT": "numeric",
    "boolean": "boolean",
    "BOOLEAN": "boolean",
    "date": "date",
    "DATE": "date",
    "timestamp": "timestamp",
    "TIMESTAMP": "timestamp",
    "datetime": "timestamp",
    "array": "varchar",  # fallback
    "object": "varchar",  # fallback
}

# Per-adapter tables are keyed on the *lowercased* FLUID type. Values follow
# datacontract-cli's sql_type_converter per-adapter choices where FLUID has an
# equivalent type; semi-structured fallbacks use each platform's native
# JSON-ish type instead of the generic ``varchar``.
_BIGQUERY: Dict[str, str] = {
    "string": "string",  # BigQuery rejects varchar
    "integer": "int64",
    "number": "numeric",
    "float": "float64",
    "boolean": "bool",
    "date": "date",
    "timestamp": "timestamp",
    "datetime": "datetime",
    "array": "json",
    "object": "json",
}

_SNOWFLAKE: Dict[str, str] = {
    "string": "varchar",
    "integer": "number",
    "number": "number",
    "float": "float",
    "boolean": "boolean",
    "date": "date",
    # Snowflake's bare TIMESTAMP defaults to TIMESTAMP_NTZ; name it
    # explicitly so the contract does not depend on account settings.
    "timestamp": "timestamp_ntz",
    "datetime": "timestamp_ntz",
    "array": "array",
    "object": "object",
}

_REDSHIFT: Dict[str, str] = {
    "string": "varchar",
    "integer": "integer",
    "number": "numeric",
    "float": "double precision",
    "boolean": "boolean",
    "date": "date",
    "timestamp": "timestamp",
    "datetime": "timestamp",
    "array": "super",  # Redshift's semi-structured type
    "object": "super",
}

_DUCKDB: Dict[str, str] = {
    "string": "varchar",
    "integer": "integer",
    "number": "numeric",
    "float": "numeric",  # matches the generic skeleton cast for consistency
    "boolean": "boolean",
    "date": "date",
    "timestamp": "timestamp",
    "datetime": "timestamp",
    "array": "varchar",  # matches the generic fallback cast
    "object": "varchar",
}

_ADAPTER_TABLES: Dict[str, Dict[str, str]] = {
    "bigquery": _BIGQUERY,
    "snowflake": _SNOWFLAKE,
    "redshift": _REDSHIFT,
    "duckdb": _DUCKDB,
}

# Fallback for FLUID types unknown to an adapter table (mirrors the generic
# table's ``varchar`` fallback, adjusted where the adapter rejects varchar).
_ADAPTER_FALLBACK: Dict[str, str] = {
    "bigquery": "string",
    "snowflake": "varchar",
    "redshift": "varchar",
    "duckdb": "varchar",
}


def sql_type(fluid_type: str, adapter: Optional[str] = None) -> str:
    """Map a FLUID column type to a SQL type string.

    ``adapter=None`` reproduces the historical generic mapping exactly
    (including its case-sensitive keys and ``varchar`` fallback) — the
    skeleton-cast path in ``models.py`` uses this. A known adapter name
    selects the adapter-correct table used for model-contract
    ``data_type`` emission; unknown adapters fall back to generic.
    """
    if adapter:
        table = _ADAPTER_TABLES.get(adapter.lower())
        if table is not None:
            key = fluid_type.lower() if isinstance(fluid_type, str) else ""
            return table.get(key, _ADAPTER_FALLBACK[adapter.lower()])
    return _GENERIC.get(fluid_type, "varchar")


def adapter_for_build(build: Mapping[str, Any]) -> str:
    """Resolve the dbt profile adapter for a contract build.

    Reads ``builds[].execution.runtime.platform`` with the same platform
    dispatch as ``profiles.py::_profile_for_platform`` so the contract
    ``data_type`` always matches the adapter the generated
    ``profiles.yml`` targets.
    """
    execution = build.get("execution", {}) if isinstance(build, Mapping) else {}
    runtime = execution.get("runtime", {}) if isinstance(execution, Mapping) else {}
    platform = str(runtime.get("platform", "local") or "local").lower()

    if platform in ("gcp", "bigquery"):
        return "bigquery"
    if platform == "snowflake":
        return "snowflake"
    if platform in ("aws", "redshift"):
        return "redshift"
    return "duckdb"
