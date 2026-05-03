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

"""``fluid forge data-model from-source --source <jdbc>`` path.

Lifted from ``cli/forge_data_model.py`` (host file was 1825 LOC).
~127 LOC of JDBC-introspection plumbing for the postgres / mysql /
sqlite source kinds. Keeps the host file's catalog-adapter dispatch
clean and lets future JDBC connectors (oracle, mssql) land here
without touching the catalog code path.

``forge_data_model.py`` re-imports both functions at module top so
existing call sites and test patches keep resolving.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from fluid_build.cli.console import cprint


def _run_from_jdbc_source(args: Any, logger: logging.Logger) -> int:
    """Forge a logical model from a JDBC database via duckdb extensions.

    Branch entered from ``run_from_source_command`` when
    ``--source <postgres|postgresql|mysql|sqlite>``. Reads ``--uri``,
    enumerates tables + columns via the shared
    :mod:`fluid_build.cli.discover._jdbc_introspect` helper, then emits
    a contract under ``args.output`` using the same ``--name`` and
    sidecar logic as the catalog path.

    Implementation note: we don't reuse the catalog adapter
    abstraction here because there's no credential resolver — the URI
    carries everything. A thin synthesise-then-write pass is enough
    to plug the JDBC source into the world-class plan v1.5.
    """
    from fluid_build.cli.discover._jdbc_introspect import introspect_jdbc

    if not getattr(args, "uri", None):
        cprint(
            "[red]from-source --source {src}[/red] requires ``--uri``. "
            "Example: ``--uri "
            "postgresql://user:pass@host:5432/db``.".format(src=args.source)
        )
        return 2

    try:
        db = introspect_jdbc(
            source=args.source,
            uri=args.uri,
            schema_filter=getattr(args, "schema_name", None),
            table_filter=getattr(args, "tables", None),
        )
    except (ImportError, ValueError) as exc:
        cprint(f"[red]JDBC introspection failed:[/red] {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 — surface clean
        # Connect / SQL errors carry detail the operator needs to see
        # verbatim ("could not connect to server", "FATAL: password
        # authentication failed", etc.).
        cprint(f"[red]JDBC introspection failed:[/red] {exc}")
        logger.debug("jdbc_introspect_traceback", exc_info=True)
        return 1

    if not db.tables:
        cprint(
            f"[yellow]No tables found in {args.source} database "
            f"{db.database!r}[/yellow] (schema_filter="
            f"{getattr(args, 'schema_name', None)!r})."
        )
        return 1

    # Synthesise a minimal contract from the introspected tables. Each
    # table becomes one ``exposes[]`` entry with its column list. The
    # operator can then refine via ``fluid forge --refine``.
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.3",
        "id": (args.name or db.database).lower().replace(" ", "_"),
        "name": args.name or db.database,
        "description": (
            f"Forged from {args.source} via duckdb introspection. "
            f"{len(db.tables)} tables enumerated."
        ),
        "metadata": {
            "layer": "Bronze",
            "productType": "SDP",
        },
        "exposes": [],
    }
    for table in db.tables:
        schema_block = []
        for col in table.columns:
            schema_block.append(
                {
                    "name": col.name,
                    "logicalType": _map_jdbc_type_to_logical(col.type_name),
                    "physicalType": col.type_name,
                    "nullable": col.nullable,
                }
            )
        contract["exposes"].append(
            {
                "id": table.name,
                "name": table.name,
                "contract": {
                    "schema": schema_block,
                },
            }
        )

    # Write the contract.
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    import yaml as _yaml

    output_path.write_text(_yaml.safe_dump(contract, sort_keys=False))
    cprint(
        f"[green]✓[/green] Forged {len(db.tables)}-table SDP contract from "
        f"{args.source} into [cyan]{output_path}[/cyan]"
    )
    return 0


def _map_jdbc_type_to_logical(type_name: str) -> str:
    """Coarse JDBC → logical-type mapping. Sufficient for the v1
    from-source SDP contract; ``fluid forge --refine`` upgrades to
    domain-specific types as needed."""
    t = type_name.lower()
    if any(s in t for s in ("int", "serial")):
        return "integer"
    if any(s in t for s in ("numeric", "decimal", "real", "double", "float")):
        return "decimal"
    if any(s in t for s in ("char", "text", "uuid", "json")):
        return "string"
    if "bool" in t:
        return "boolean"
    if "timestamp" in t or "datetime" in t:
        return "timestamp"
    if t == "date":
        return "date"
    if "time" in t:
        return "time"
    if "bytea" in t or "blob" in t or "binary" in t:
        return "binary"
    return "string"
