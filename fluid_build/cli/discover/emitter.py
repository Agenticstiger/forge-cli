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

"""Discovered-stream → Bronze acquisition contract emitter.

Pure function (deterministic). Given a list of ``DiscoveredStream``s and
some metadata about the source, emit a contract dict ready to be written
as YAML.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .registry import DiscoveredStream


def emit_contract(
    *,
    product_id: str,
    name: str,
    domain: str,
    owner_team: str,
    owner_email: str,
    engine: str,
    source_kind: str,
    connection: Dict[str, Any],
    streams: List[DiscoveredStream],
    sink_format: str = "parquet",
    description: Optional[str] = None,
    schema_policy: str = "discover_and_freeze",
) -> Dict[str, Any]:
    """Emit a deterministic acquisition contract from discovered streams.

    All streams land in a single build (named ``ingest_<product>``); the
    ``streams`` field carries the per-table list. This matches how all six
    runners interpret a multi-stream source.
    """
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": product_id,
        "name": name,
        "domain": domain,
        "description": description or f"Source-aligned Bronze ingestion for {name}.",
        "metadata": {
            # Bronze (medallion) and SDP (Data Mesh) are equivalent —
            # Source-Aligned Data Product. Emitting both keeps the
            # contract readable to consumers regardless of which
            # vocabulary they use.
            "layer": "Bronze",
            "productType": "SDP",
            "owner": {"team": owner_team, "email": owner_email},
        },
        "builds": [
            {
                "id": f"ingest_{product_id.split('.')[-1]}",
                "description": f"Discovered-source ingest of {len(streams)} stream(s).",
                "pattern": "acquisition",
                "engine": engine,
                "capabilities": ["full_refresh", "schema_discovery"],
                "properties": {
                    "source": {
                        "kind": source_kind,
                        "connection": connection,
                        "mode": "full_refresh",
                        "streams": [s.name for s in streams],
                    },
                    "sink": {"format": sink_format},
                },
                "outputs": [_safe_id(s.name) for s in streams],
            }
        ],
        "exposes": [
            {
                "exposeId": _safe_id(s.name),
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": sink_format,
                    "location": {"path": f"./out/{_safe_id(s.name)}.{sink_format}"},
                },
                "contract": {
                    "schema": [
                        {
                            "name": c.name,
                            "type": canonical_column_type(c.type),
                            "required": not c.nullable,
                        }
                        for c in s.columns
                    ],
                    "schemaPolicy": schema_policy,
                },
            }
            for s in streams
        ],
    }


#: Source type names the bundled contract schema does not accept, mapped to the
#: nearest name it does. Built by validating every name in DuckDB's own
#: ``duckdb_types()`` against ``$defs.column.properties.type`` on duckdb 1.5.6
#: and 2.0.0: 35 of them are rejected, not the two the bug report named.
#:
#: Without this the emitter wrote DuckDB's spelling straight through, so a
#: discover on DuckDB 2.0 produced ``type: hugeint`` and ``type: '"null"'`` and
#: the tool's own ``fluid validate`` then rejected the contract it had just
#: written. Two of these (``uinteger``, ``integer[]``) were rejected on 1.5 too,
#: so this is not only a 2.0 problem.
#:
#: An unsigned or 128-bit type maps to a name WIDE ENOUGH to hold its range
#: rather than to the same-sounding one: ``uinteger`` (0..2^32-1) does not fit
#: ``integer``, and ``hugeint`` does not fit ``bigint``, so they become
#: ``bigint`` and ``numeric``. The schema's own enum keeps ``bigint`` and
#: ``integer`` distinct, so the choice is visible in the emitted contract.
#:
#: Parameterised and spaced forms are deliberately absent: the schema's pattern
#: branch already accepts ``decimal(18,2)``, ``varchar(255)``, ``struct(...)``,
#: ``map(...)`` and ``timestamp with time zone``. Only ``<type>[]`` and the
#: quoted ``"NULL"`` need shape handling, below.
_SCHEMA_TYPE_ALIASES = {
    # integers: width that holds the range, not the similar-sounding name
    "int1": "tinyint",
    "uint8": "smallint",
    "utinyint": "smallint",
    "short": "smallint",
    "uint16": "integer",
    "usmallint": "integer",
    "integral": "integer",
    "signed": "integer",
    "uint32": "bigint",
    "uinteger": "bigint",
    "oid": "bigint",
    # 128-bit and unsigned 64-bit exceed bigint
    "hugeint": "numeric",
    "int128": "numeric",
    "uhugeint": "numeric",
    "uint128": "numeric",
    "uint64": "numeric",
    "ubigint": "numeric",
    "varint": "numeric",
    "bignum": "numeric",
    # text, binary, boolean
    "bpchar": "varchar",
    "bitstring": "bit",
    "logical": "boolean",
    # date and time: the precision suffix is not a different type
    "timestamp_s": "timestamp",
    "timestamp_ms": "timestamp",
    "timestamp_us": "timestamp",
    "timestamp_ns": "timestamp",
    "timestamptz_ns": "timestamptz",
    "time_ns": "time",
    "timetz": "time",
    "time with time zone": "time",
    # composites
    "list": "array",
    "tuple": "struct",
    "union": "variant",
    # a column of only NULLs, and DuckDB's own meta type
    "null": "string",
    "type": "string",
}


def canonical_column_type(raw: Optional[str]) -> str:
    """A source column type the bundled contract schema accepts.

    The one place a discovered type becomes a contract field type, so every
    discover backend inherits it.

    A name the table does not list passes through lower-cased, exactly as
    before. That is deliberate rather than a gap: the schema accepts a
    cross-dialect superset through its pattern branch, so the JDBC backends'
    own dialect names (and ``decimal(18,2)``, ``varchar(255)``,
    ``struct(...)``) are already valid and must not be squashed. The table
    therefore carries only names measured to be REJECTED. A source type that is
    neither valid nor listed still reaches the contract unchanged and still
    fails ``fluid validate`` -- the same as today, and not something this
    change claims to fix.
    """
    text = (raw or "string").strip()
    if not text:
        return "string"
    lowered = text.lower()
    # ``INTEGER[]``, ``VARCHAR[][]`` -- a list of anything is an array.
    if lowered.endswith("[]"):
        return "array"
    # ``typeof(NULL)`` is the quoted string ``"NULL"``.
    if lowered.strip('"') == "null":
        return "string"
    if lowered in _SCHEMA_TYPE_ALIASES:
        return _SCHEMA_TYPE_ALIASES[lowered]
    base = lowered.split("(", 1)[0].strip()
    if base in _SCHEMA_TYPE_ALIASES:
        return _SCHEMA_TYPE_ALIASES[base]
    return lowered


def _safe_id(name: str) -> str:
    """Convert ``public.orders`` → ``public_orders`` so it satisfies the FLUID
    identifier regex used by exposeId / outputs.
    """
    return name.replace(".", "_").replace("-", "_").lower()
