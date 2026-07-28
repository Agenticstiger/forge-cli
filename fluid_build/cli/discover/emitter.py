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
                            "type": (c.type or "string").lower(),
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


def _safe_id(name: str) -> str:
    """Convert ``public.orders`` → ``public_orders`` so it satisfies the FLUID
    identifier regex used by exposeId / outputs.
    """
    return name.replace(".", "_").replace("-", "_").lower()
