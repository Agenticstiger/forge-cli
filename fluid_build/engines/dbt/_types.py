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

Beyond the canonical scalars the FLUID ``column.type`` schema explicitly
admits *SQL alias* spellings (``decimal``, ``bigint``, ``text``,
``variant`` …) and *parameterized* forms ("a parameterized form such as
``decimal(18,4)``"). Both are handled here rather than by each caller:

- :data:`_ALIASES` folds an alias onto the canonical FLUID key *before*
  the adapter table is consulted, and each adapter table additionally
  carries the native spellings that only make sense on that platform
  (``variant`` / ``timestamp_ltz`` on Snowflake, ``super`` on Redshift).
- ``base(args)`` is split by :func:`split_parameterized`; the base is
  mapped as usual and the argument list is re-attached only when the
  *resolved* SQL type accepts parameters on that adapter
  (:data:`_PARAMETRIC`). Dropping the parameters silently is what made
  ``number(12,2)`` land as ``VARCHAR(16777216)`` in Snowflake.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional, Tuple

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
    # Native spellings with no cross-platform alias.
    "bignumeric": "bignumeric",
    "bytes": "bytes",
    "json": "json",
    "time": "time",
    "geography": "geography",
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
    # Native spellings with no cross-platform alias. ``decimal``/``numeric``
    # stay distinct from ``number`` so a declared ``decimal(18,4)`` round-trips
    # to the same word the author wrote (all three are NUMBER in Snowflake).
    "decimal": "decimal",
    "numeric": "numeric",
    "variant": "variant",
    "time": "time",
    "timestamp_ntz": "timestamp_ntz",
    "timestamp_tz": "timestamp_tz",
    "timestamp_ltz": "timestamp_ltz",
    "binary": "binary",
    "varbinary": "varbinary",
    "geography": "geography",
    "geometry": "geometry",
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
    # Native spellings with no cross-platform alias.
    "decimal": "decimal",
    "numeric": "numeric",
    "bigint": "bigint",
    "time": "time",
    "timestamp_tz": "timestamptz",
    "super": "super",
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
    # Native spellings with no cross-platform alias.
    "decimal": "decimal",
    "numeric": "numeric",
    "bigint": "bigint",
    "time": "time",
    "timestamp_tz": "timestamptz",
    "json": "json",
    "blob": "blob",
    "uuid": "uuid",
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

# SQL alias spellings folded onto a canonical FLUID scalar *before* the
# adapter table is consulted. A table's own key always wins, so an adapter
# that names the alias natively (Snowflake ``decimal``) keeps that spelling
# and only the platforms without it fall back to the canonical scalar.
# Keys are lowercase; the fold is applied to the parameterless base name.
_ALIASES: Dict[str, str] = {
    # integral
    "int": "integer",
    "int2": "integer",
    "int4": "integer",
    "int8": "integer",
    "bigint": "integer",
    "smallint": "integer",
    "tinyint": "integer",
    "byteint": "integer",
    "long": "integer",
    "serial": "integer",
    # fixed-point
    "decimal": "number",
    "dec": "number",
    "numeric": "number",
    "money": "number",
    # floating point
    "double": "float",
    "double precision": "float",
    "float4": "float",
    "float8": "float",
    "real": "float",
    # character
    "varchar": "string",
    "char": "string",
    "character": "string",
    "character varying": "string",
    "text": "string",
    "nvarchar": "string",
    "str": "string",
    "uuid": "string",
    # boolean
    "bool": "boolean",
    # temporal
    "timestamp_ntz": "timestamp",
    "timestamp_tz": "timestamp",
    "timestamp_ltz": "timestamp",
    "timestamptz": "timestamp",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamp",
    # semi-structured
    "variant": "object",
    "json": "object",
    "jsonb": "object",
    "struct": "object",
    "record": "object",
    "map": "object",
    "list": "array",
    # binary
    "binary": "string",
    "varbinary": "string",
    "bytes": "string",
    "blob": "string",
}

# Resolved SQL type names that accept a ``(...)`` argument list on each
# adapter. A declared ``number(12,2)`` keeps its precision/scale only when
# the type it maps to can carry them; otherwise the arguments are dropped
# (``bigint(20)`` → ``number``) rather than emitting SQL the warehouse
# rejects.
_PARAMETRIC: Dict[str, frozenset] = {
    "bigquery": frozenset({"numeric", "bignumeric", "string", "bytes"}),
    "snowflake": frozenset(
        {
            "number",
            "decimal",
            "numeric",
            "varchar",
            "char",
            "character",
            "string",
            "text",
            "binary",
            "varbinary",
            "time",
            "timestamp_ntz",
            "timestamp_tz",
            "timestamp_ltz",
        }
    ),
    "redshift": frozenset({"varchar", "char", "character", "decimal", "numeric"}),
    "duckdb": frozenset({"varchar", "decimal", "numeric"}),
    # Generic (adapter-less) skeleton casts.
    "": frozenset({"varchar", "char", "decimal", "numeric", "timestamp"}),
}

# ``base(args)`` — one level of parentheses, no nesting (no FLUID scalar
# maps to a constructed type such as ``struct<...>``).
_PARAM_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_ ]*?)\s*\(\s*([^()]*)\s*\)$")


def split_parameterized(fluid_type: str) -> Tuple[str, Optional[str]]:
    """Split ``"decimal(18,4)"`` into ``("decimal", "18,4")``.

    Returns ``(fluid_type, None)`` for an unparameterized type. Whitespace
    inside the argument list is normalised so ``number(12, 2)`` and
    ``number(12,2)`` emit the same SQL — generated artifacts must be
    byte-stable for the same contract intent.
    """
    if not isinstance(fluid_type, str):
        return "", None
    match = _PARAM_RE.match(fluid_type.strip())
    if match is None:
        return fluid_type.strip(), None
    base, args = match.group(1).strip(), match.group(2).strip()
    if not args:
        return base, None
    normalized = ",".join(part.strip() for part in args.split(","))
    return base, normalized


def _lookup(table: Mapping[str, str], base: str, fallback: str) -> str:
    """Resolve one base type against a table, then the alias fold."""
    key = base.lower()
    if key in table:
        return table[key]
    alias = _ALIASES.get(key)
    if alias is not None and alias in table:
        return table[alias]
    return fallback


def sql_type(fluid_type: str, adapter: Optional[str] = None) -> str:
    """Map a FLUID column type to a SQL type string.

    ``adapter=None`` uses the platform-agnostic table — the historical
    generic mapping, extended with the alias/parameterized handling
    below. A known adapter name selects the adapter-correct table used
    for model-contract ``data_type`` emission and (since the two halves
    of one generated project must agree) for the skeleton casts in
    ``models.py``; unknown adapters fall back to generic.

    Parameterized declarations keep their arguments whenever the resolved
    type can carry them: ``number(12,2)`` → ``number(12,2)`` on Snowflake,
    ``varchar(25)`` → ``varchar(25)``. When the resolved type takes no
    arguments the declaration degrades to the bare type rather than
    emitting SQL the warehouse rejects — ``array(4)`` → ``array``.
    """
    if not isinstance(fluid_type, str):
        return "varchar"

    base, args = split_parameterized(fluid_type)

    adapter_key = adapter.lower() if adapter else ""
    table = _ADAPTER_TABLES.get(adapter_key)
    if table is None:
        adapter_key = ""
        # Exact-match first so the historical case-sensitive keys
        # (``STRING`` as well as ``string``) resolve byte-identically.
        resolved = _GENERIC.get(base)
        if resolved is None:
            resolved = _lookup(_GENERIC, base, "varchar")
    else:
        resolved = _lookup(table, base, _ADAPTER_FALLBACK[adapter_key])

    if args and resolved in _PARAMETRIC[adapter_key]:
        return f"{resolved}({args})"
    return resolved


# Types whose *unparameterized* spelling silently defaults to scale 0 on an
# adapter, so a monetary value declared with them loses its cents. Keyed by
# adapter → {resolved SQL type: the default the warehouse applies}.
_SCALE_ZERO_DEFAULTS: Dict[str, Dict[str, str]] = {
    # Snowflake: "If a precision is not specified, ... the default is 38.
    # If a scale is not specified, the default is 0." (NUMBER data type docs)
    "snowflake": {
        "number": "NUMBER(38,0)",
        "decimal": "NUMBER(38,0)",
        "numeric": "NUMBER(38,0)",
    },
}


def precision_warnings(contract: Mapping[str, Any], build: Mapping[str, Any]) -> list:
    """Report contract columns whose declared type silently truncates decimals.

    FLUID's ``number`` (and its ``decimal``/``numeric`` aliases) carries no
    precision or scale. On Snowflake that resolves to ``NUMBER(38,0)``, so a
    monetary column declared ``number`` stores ``528803.85`` as ``528804`` —
    dbt raises its own "unspecified precision/scale ... unintended rounding"
    warning at run time, but by then the artifact is already generated.

    Surfaced at generation time (the CLI prints these) because the fix is a
    contract edit: ``type: number(12,2)``, which :func:`sql_type` now honours.
    Returns a list of human-readable strings, empty when nothing is at risk.
    """
    adapter = adapter_for_build(build)
    at_risk = _SCALE_ZERO_DEFAULTS.get(adapter)
    if not at_risk:
        return []

    out: list = []
    for expose in contract.get("exposes", []) or []:
        if not isinstance(expose, Mapping):
            continue
        expose_id = expose.get("exposeId") or expose.get("id") or "?"
        contract_section = expose.get("contract")
        if not isinstance(contract_section, Mapping):
            continue
        for col in contract_section.get("schema", []) or []:
            if not isinstance(col, Mapping) or not col.get("name"):
                continue
            declared = str(col.get("type", "string"))
            base, args = split_parameterized(declared)
            if args is not None:
                continue  # author was explicit — nothing to warn about
            key = base.lower()
            # ``integer`` (and its aliases) legitimately means scale 0 —
            # only the decimal-capable spellings lose information.
            if key == "integer" or _ALIASES.get(key) == "integer":
                continue
            default = at_risk.get(sql_type(declared, adapter))
            if default is None:
                continue
            out.append(
                f"{expose_id}.{col['name']}: type '{declared}' → {default} on {adapter} "
                f"(scale 0 — decimals are truncated). Declare a scale, "
                f"e.g. type: {base.lower()}(12,2)."
            )
    return out


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
