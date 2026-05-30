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
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build.cli.console import cprint
from fluid_build.util.contract import slugify_identifier

# Canonical FLUID contract-id pattern (lowercased form — ``product_id`` is
# lowercased before the check). Mirrors the JSON-schema id constraint.
_FLUID_ID_RE = re.compile(r"^[a-z0-9_][a-z0-9_.-]*[a-z0-9_]$|^[a-z0-9_]$")


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
        # authentication failed", etc.). Console output is OK for the
        # interactive run; structured logs (which may flow to log
        # aggregators) get only the exception class — JDBC error
        # messages can echo the JDBC URL including embedded password.
        cprint(f"[red]JDBC introspection failed:[/red] {exc}")
        logger.debug("jdbc_introspect_failed: %s", type(exc).__name__)
        return 1

    if not db.tables:
        cprint(
            f"[yellow]No tables found in {args.source} database "
            f"{db.database!r}[/yellow] (schema_filter="
            f"{getattr(args, 'schema_name', None)!r})."
        )
        return 1

    # Synthesise a minimal valid v0.7.3 contract from the introspected
    # tables. Each table becomes one ``exposes[]`` entry with its column
    # list. Required top-level fields: fluidVersion, kind, id, name,
    # metadata (with owner), exposes (with exposeId, kind, binding,
    # contract.schema using "type" only). The operator can then refine
    # via ``fluid forge --refine``.
    # ``db.database`` is a real database name for postgres/mysql but a FILE
    # PATH for sqlite (ATTACH takes a path) — the raw path contains ``/`` and
    # ``.`` and is not a valid FLUID id, so the whole emitted contract was
    # rejected by the validator. Use the sqlite file *stem* as the base name.
    if not args.name and db.source_kind == "sqlite":
        base_name = Path(db.database).stem or "sqlite_db"
    else:
        base_name = args.name or db.database
    # Preserve the historical id shape (lowercase, spaces→underscores) for the
    # common case; only fall back to the shared slugifier when the base still
    # carries characters the FLUID id pattern rejects, so the id is ALWAYS valid.
    product_id = base_name.lower().replace(" ", "_")
    if not _FLUID_ID_RE.match(product_id):
        product_id = slugify_identifier(base_name, fallback="source_model")
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": product_id,
        "name": base_name,
        "description": (f"Forged from {args.source}. {len(db.tables)} tables enumerated."),
        "metadata": {
            "layer": "Bronze",
            "productType": "SDP",
            "owner": {
                "team": "data-platform",
            },
        },
        "exposes": [],
    }

    # Collect FKs into the top-level extensions bucket. The FLUID
    # schema doesn't natively carry a per-column FK target slot; the
    # extensions block is open ("additionalProperties: true") so we
    # use the ``jdbcIntrospection`` vendor namespace. Downstream
    # consumers (composition / lineage) can read this directly until
    # the schema gains first-class relationship support — see
    # /tmp/fluid-ux-findings/05-from-source-postgres.md fix #1.
    extensions_block: Dict[str, Any] = {
        "source_kind": db.source_kind,
        "database": db.database,
        "schemas": [],
        "foreignKeys": [],
        "primaryKeys": [],
        "checkConstraints": [],
    }
    seen_schemas: set = set()

    for table in db.tables:
        if table.schema not in seen_schemas:
            extensions_block["schemas"].append(table.schema)
            seen_schemas.add(table.schema)

        pk_set = set(table.primary_key_columns)
        # Build a column → list[CheckConstraint] index so we can attach
        # CHECK exprs to the column they bind to (via constraint_column_usage).
        per_column_checks: Dict[str, List[Dict[str, str]]] = {}
        for chk in table.checks:
            entry = {
                "type": "custom",
                "constraint": _normalize_check_expr(chk.check_clause),
            }
            if chk.constraint_name:
                entry["message"] = f"violates check constraint {chk.constraint_name!r}"
            for col_name in chk.columns or []:
                per_column_checks.setdefault(col_name, []).append(entry)

        # Build a column → fk-target index so we can label the column
        # with its FK target.
        per_column_fk: Dict[str, str] = {}
        for fk in table.foreign_keys:
            for src, dst in zip(fk.from_columns, fk.to_columns, strict=False):
                target_table = f"{fk.to_schema}.{fk.to_table}" if fk.to_schema else fk.to_table
                per_column_fk[src] = f"{target_table}.{dst}"

        schema_block: List[Dict[str, Any]] = []
        for col in table.columns:
            col_entry: Dict[str, Any] = {
                "name": col.name,
                "type": _map_jdbc_type_to_logical(
                    col.type_name,
                    numeric_precision=col.numeric_precision,
                    numeric_scale=col.numeric_scale,
                    character_max_length=col.character_max_length,
                ),
            }
            # PK columns are not nullable + tagged.
            if col.name in pk_set:
                col_entry["required"] = True
                col_entry.setdefault("tags", []).append("primary-key")
            elif not col.nullable:
                col_entry["required"] = True

            # Attach CHECK constraints as validationRules entries.
            checks_for_col = per_column_checks.get(col.name) or []
            if checks_for_col:
                col_entry["validationRules"] = checks_for_col

            # FK target → emit as a "foreign-key" tag + labels for the
            # downstream lineage / relationship-graph extractor.
            fk_target = per_column_fk.get(col.name)
            if fk_target:
                col_entry.setdefault("tags", []).append("foreign-key")
                col_entry.setdefault("labels", {})["jdbc.fk.target"] = fk_target

            # Dedupe tags while preserving order.
            if "tags" in col_entry:
                seen: set = set()
                col_entry["tags"] = [t for t in col_entry["tags"] if not (t in seen or seen.add(t))]

            schema_block.append(col_entry)

        # H6 fix — name-based PII pre-classifier. Tags columns like
        # ``c_email``, ``ssn``, ``date_of_birth``, ``phone_number`` with
        # ``tags`` / ``sensitivity`` / ``semanticType`` so the resulting
        # contract carries the signal the Judge ``security`` axis
        # needs. Pure-Python regex, no value scanning, no extra deps.
        # Kill-switch: FLUID_COPILOT_PII_CLASSIFIER=0. Runs after the
        # PK / FK tagging so PII tags merge in cleanly via the
        # apply_pii_tags de-dupe logic. See
        # fluid_build/copilot/pii.py for the OSS-borrowed vocabulary.
        from fluid_build.copilot.pii import apply_pii_tags

        apply_pii_tags(schema_block)

        # Capture PK + FK + CHECK at the extension bucket too — single
        # canonical surface a consumer can read without scanning every
        # column's tags / labels / validationRules.
        if table.primary_key_columns:
            extensions_block["primaryKeys"].append(
                {
                    "table": table.name,
                    "columns": table.primary_key_columns,
                }
            )
        for fk in table.foreign_keys:
            extensions_block["foreignKeys"].append(
                {
                    "from_table": table.name,
                    "from_columns": fk.from_columns,
                    "to_schema": fk.to_schema,
                    "to_table": fk.to_table,
                    "to_columns": fk.to_columns,
                    "constraint_name": fk.constraint_name,
                    "update_rule": fk.update_rule,
                    "delete_rule": fk.delete_rule,
                    "match_option": fk.match_option,
                }
            )
        for chk in table.checks:
            extensions_block["checkConstraints"].append(
                {
                    "table": table.name,
                    "constraint_name": chk.constraint_name,
                    "expression": chk.check_clause,
                    "columns": chk.columns,
                }
            )

        contract["exposes"].append(
            {
                "exposeId": table.name,
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "other",
                    "location": {
                        "path": f"./out/{table.name}",
                    },
                },
                "contract": {
                    "schema": schema_block,
                },
            }
        )

    # Only attach the extensions block if there's something to surface.
    has_constraints = (
        extensions_block["primaryKeys"]
        or extensions_block["foreignKeys"]
        or extensions_block["checkConstraints"]
    )
    if has_constraints:
        contract["extensions"] = {"jdbcIntrospection": extensions_block}

    # Write the contract.
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    import yaml as _yaml

    output_path.write_text(_yaml.safe_dump(contract, sort_keys=False))

    # Validate the emitted contract in-process before reporting success.
    # A contract that fails schema validation must never exit 0 — the
    # operator would silently get a broken artifact.
    from fluid_build.schema_manager import validate_contract_file

    vr = validate_contract_file(str(output_path), offline_only=True, logger=logger)
    if not vr.is_valid:
        error_lines = "\n  ".join(str(e) for e in vr.errors)
        cprint(
            f"[red]✗ Emitted contract failed schema validation "
            f"({len(vr.errors)} error(s)):[/red]\n  {error_lines}\n"
            f"[yellow]Hint:[/yellow] The file was written to "
            f"[cyan]{output_path}[/cyan] for inspection."
        )
        logger.error("jdbc_emitter_invalid_contract: %d error(s)", len(vr.errors))
        return 1

    cprint(
        f"[green]✓[/green] Forged {len(db.tables)}-table SDP contract from "
        f"{args.source} into [cyan]{output_path}[/cyan]"
    )
    return 0


# Bare/canonical logical-type names accepted by the schema's column-type
# enum. We keep this list small — the column-type schema also accepts a
# parameterised form via regex (``decimal(15,2)``, ``varchar(80)``,
# ``char(1)``), so we can compose precision/scale into the logical-type
# string directly.
_BARE_DECIMAL_FAMILY = {
    "numeric",
    "decimal",
    "dec",
    "real",
    "double precision",
    "double",
    "float",
    "float4",
    "float8",
    "money",
}
_BARE_CHAR_FAMILY = {
    "character",
    "character varying",
    "varchar",
    "varchar2",
    "nvarchar",
    "char",
    "nchar",
    "text",
    "clob",
}


def _map_jdbc_type_to_logical(
    type_name: str,
    *,
    numeric_precision: Optional[int] = None,
    numeric_scale: Optional[int] = None,
    character_max_length: Optional[int] = None,
) -> str:
    """JDBC → logical-type mapping with precision/scale pass-through.

    The bucket-only behaviour (kept for callers that don't pass the
    extras) collapses an entire family to one of seven names. The
    enriched path lifts ``numeric(15,2)``-style precision into the
    output type string so downstream stages (validators, the
    physical-layout emitter) can reconstruct the original shape.

    Format follows the FLUID column-type pattern: bare canonical name
    optionally followed by ``(p)`` or ``(p,s)``.
    """
    t = type_name.lower().strip()

    # Decimal-family precision/scale parameterisation.
    is_decimal = (
        any(s in t for s in ("numeric", "decimal", "real", "double", "float"))
        or t in _BARE_DECIMAL_FAMILY
    )
    if is_decimal:
        if numeric_precision is not None and numeric_scale is not None:
            return f"decimal({numeric_precision},{numeric_scale})"
        if numeric_precision is not None:
            return f"decimal({numeric_precision})"
        return "decimal"

    # Integer-family — precision (e.g. INT64 has precision=64) is the
    # bit width, not parameter-meaningful in FLUID. Emit bare "integer".
    if any(s in t for s in ("int", "serial")):
        return "integer"

    # Character / text family — preserve character_maximum_length as
    # ``varchar(N)`` / ``char(N)`` when available.
    is_char = any(s in t for s in ("char", "text", "uuid", "json")) or t in _BARE_CHAR_FAMILY
    if is_char:
        # uuid / json don't take a parameter — emit bare "string".
        if any(s in t for s in ("uuid", "json")):
            return "string"
        if character_max_length is not None:
            # Pick varchar vs char based on the source-side hint.
            if "char" in t and "varchar" not in t and "varying" not in t:
                return f"char({character_max_length})"
            return f"varchar({character_max_length})"
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


def _normalize_check_expr(raw: str) -> str:
    """Normalise a Postgres-flavoured ``check_clause`` for the
    contract's ``validationRules.constraint`` field.

    Postgres emits ``check_clause`` strings like
    ``((o_orderstatus = ANY (ARRAY['O'::bpchar, 'F'::bpchar])))``.
    We trim the outer parenthesis-pairs (cosmetic) and strip
    PostgreSQL-specific cast suffixes (``::bpchar``, ``::numeric``)
    so the expression reads cleaner downstream. The transform is
    lossless w.r.t. semantics — the casts are inferable from the
    column type.
    """
    s = raw.strip()
    # Strip outer balanced ()()  layers.
    while s.startswith("(") and s.endswith(")"):
        depth = 0
        ok = True
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i < len(s) - 1:
                    ok = False
                    break
        if not ok:
            break
        s = s[1:-1].strip()
    # Drop ``::typename`` casts (lossless; types are in the column block).
    import re as _re

    s = _re.sub(r"::[a-zA-Z_][a-zA-Z0-9_ ]*", "", s)
    return s.strip()
