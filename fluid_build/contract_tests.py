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

"""
Local Provider for FLUID Build
-----------------------------

Executes plan actions locally using DuckDB for SQL, with CSV/Parquet inputs/outputs.

Action schema (expected keys used here):
- op:          "add" | "update" | "delete" (we act on "add"/"update"; "delete" is no-op for SQL)
- resource_type: "sql" | (others ignored here)
- id:          resource identifier for logs
- engine:      optional; if "duckdb", forces duckdb. Default: duckdb.
- inputs:      dict[str, {path: str|list[str], format?: "csv"|"parquet", name?: str}]
               - The key is an alias; referenced as table/view name in SQL.
               - `path` can be a string (supports globs) or list of strings.
               - If `name` is set, it overrides the alias for the table/view name.
- sql:         the SQL string to execute (must be a SELECT or CREATE TABLE AS ... style)
- outputs:     dict with one or more outputs. Supported:
               {
                 "path": "runtime/out/high_value_churn_risk.csv",
                 "format": "csv" | "parquet",
                 "mode": "overwrite" | "append" (csv overwrite only; parquet append allowed),
               }

Provider context (from CLI):
- ctx.env:         environment name (dev/prod/etc)
- ctx.dry_run:     if True, we load inputs + parse SQL but DO NOT write outputs
- ctx.extras:      any extra fields passed by CLI

Environment variables:
- FLUID_LOCAL_DUCKDB_PATH: optional path to a persistent DuckDB db file; default: in-memory

"""

from __future__ import annotations

import glob
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

LOGGER = logging.getLogger("fluid.provider.local")

# -------------------------
# Helpers
# -------------------------


class LocalProviderError(RuntimeError):
    pass


def _as_list(v: Union[str, List[str]]) -> List[str]:
    if isinstance(v, list):
        return v
    return [v]


def _glob_all(paths: List[str]) -> List[str]:
    out: List[str] = []
    for p in paths:
        expanded = glob.glob(os.path.expanduser(os.path.expandvars(p)))
        out.extend(expanded)
    # Remove duplicates while preserving order
    seen = set()
    uniq = []
    for x in out:
        if x not in seen:
            uniq.append(x)
            seen.add(x)
    return uniq


def _require_duckdb():
    try:
        import duckdb  # noqa: F401
    except Exception as e:
        raise LocalProviderError(
            "duckdb not installed. Install it with: pip install duckdb\n"
            "Alternatively pin a compatible Python (3.9+) and DuckDB version."
        ) from e


def _connect_duckdb():
    # Allow persistent db file for debugging/local exploration:
    # FLUID_LOCAL_DUCKDB_PATH=/tmp/fluid_local.duckdb
    _require_duckdb()
    import duckdb  # type: ignore

    db_path = os.environ.get("FLUID_LOCAL_DUCKDB_PATH", ":memory:")
    try:
        return duckdb.connect(db_path, read_only=False)
    except Exception as e:
        raise LocalProviderError(f"Failed to connect to duckdb at '{db_path}': {e}") from e


def _register_input(con, alias: str, cfg: Dict[str, Any]) -> str:
    """
    Register an input as a DuckDB view. Supports CSV/Parquet.
    Returns the final table/view name to be used in SQL (alias or overridden name).
    """
    fmt = (cfg.get("format") or "").lower()
    name = cfg.get("name") or alias
    raw_paths = cfg.get("path")
    if not raw_paths:
        raise LocalProviderError(f"Input '{alias}' missing 'path'")

    paths = _as_list(raw_paths)
    files = _glob_all(paths)
    if not files:
        raise LocalProviderError(
            f"No files found for input '{alias}'. " f"Checked patterns: {paths}"
        )

    # Normalize to absolute paths for robust COPY/scan
    files = [str(Path(p).resolve()) for p in files]

    # If format unspecified, infer from extension of first file
    if not fmt:
        ext = Path(files[0]).suffix.lower().lstrip(".")
        fmt = ext if ext in ("csv", "parquet") else "csv"

    try:
        if fmt == "csv":
            # Use read_csv_auto for robust inference, unify schema across many files
            # Create a view name bound to a UNION ALL of all files
            if len(files) == 1:
                con.execute(
                    f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_csv_auto('{files[0]}');"
                )
            else:
                union = " UNION ALL ".join([f"SELECT * FROM read_csv_auto('{f}')" for f in files])
                con.execute(f"CREATE OR REPLACE VIEW {name} AS {union};")
        elif fmt == "parquet":
            # parquet_scan() can accept a glob. Join files to a single glob if possible.
            # If not, make a union.
            if len(files) == 1:
                con.execute(
                    f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM parquet_scan('{files[0]}');"
                )
            else:
                union = " UNION ALL ".join([f"SELECT * FROM parquet_scan('{f}')" for f in files])
                con.execute(f"CREATE OR REPLACE VIEW {name} AS {union};")
        else:
            raise LocalProviderError(f"Unsupported input format '{fmt}' for '{alias}'")
    except Exception as e:
        raise LocalProviderError(f"DuckDB failed to register input '{alias}': {e}") from e

    return name


def _execute_sql(con, sql: str):
    """Execute SQL and return DuckDB relation."""
    sql_stripped = (sql or "").strip().rstrip(";")
    if not sql_stripped:
        raise LocalProviderError("No SQL provided for action")
    try:
        # DuckDB returns a Relation for SELECT; for CREATE TABLE AS, we can
        # run an additional SELECT if needed. We assume a SELECT for portability.
        rel = con.sql(sql_stripped)
        return rel
    except Exception as e:
        # Try to be helpful if the error hints at missing tables
        msg = str(e)
        if "Binder Error" in msg or "Catalog Error" in msg:
            raise LocalProviderError(
                "SQL failed to bind. Ensure your inputs are registered and "
                "the SQL references the correct table/view names. Error: " + msg
            ) from e
        raise LocalProviderError("SQL execution failed: " + msg) from e


def _write_output(rel, outputs: Dict[str, Any], dry_run: bool) -> Tuple[int, Optional[str]]:
    """
    Write the relation to output path.
    Returns (row_count, path_written)
    """
    path = outputs.get("path")
    fmt = (outputs.get("format") or "").lower()
    mode = (outputs.get("mode") or "overwrite").lower()

    if not path:
        raise LocalProviderError("outputs.path is required for local sql action")
    if fmt not in ("csv", "parquet"):
        raise LocalProviderError("outputs.format must be 'csv' or 'parquet'")

    abs_path = Path(path).resolve()
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    if dry_run:
        # Estimate count without writing
        try:
            cnt = rel.count("*").fetchone()[0]
        except Exception as e:
            LOGGER.warning(f"Failed to count rows in dry run: {e}")
            cnt = -1
        return cnt, None

    try:
        if fmt == "csv":
            # DuckDB COPY writes header by default
            if mode not in ("overwrite",):
                # Append for CSV is messy; stick to overwrite for determinism
                raise LocalProviderError("CSV output supports mode=overwrite only")
            rel.to_df().to_csv(abs_path, index=False)
        else:
            # parquet: DuckDB supports parquet writing via COPY as well,
            # but using to_parquet keeps pandas out for large volumes.
            # Here we prefer DuckDB's COPY for speed when available.
            # Use COPY (SELECT * FROM rel) TO 'path' (FORMAT PARQUET);
            # However duckdb.Relation doesn't expose COPY directly; use SQL.
            import duckdb  # noqa

            # Create a temp view and then COPY from it.
            tmp_view = "__fluid_tmp_out"
            rel.create_view(tmp_view, replace=True)
            if mode == "append":
                # Append parquet: create if not exists then append is not trivial.
                # DuckDB supports COPY with APPEND (v0.10+). Try; else fallback.
                try:
                    rel._cursor.execute(
                        f"COPY (SELECT * FROM {tmp_view}) TO '{str(abs_path)}' (FORMAT PARQUET, APPEND 1);"
                    )
                except Exception as e:
                    # Fallback: read existing, union, rewrite (costly)
                    LOGGER.warning(f"COPY APPEND not supported, using fallback merge: {e}")
                    import pandas as pd

                    new_df = rel.to_df()
                    if abs_path.exists():
                        import pyarrow as pa
                        import pyarrow.parquet as pq

                        old_tbl = pq.read_table(abs_path)
                        old_df = old_tbl.to_pandas()
                        all_df = pd.concat([old_df, new_df], ignore_index=True)
                        table = pa.Table.from_pandas(all_df)
                        pq.write_table(table, abs_path)
                    else:
                        new_df.to_parquet(abs_path, index=False)
            else:
                # overwrite
                try:
                    rel._cursor.execute(
                        f"COPY (SELECT * FROM {tmp_view}) TO '{str(abs_path)}' (FORMAT PARQUET, FORCE 1);"
                    )
                except Exception as e:
                    # Fallback: pandas path
                    LOGGER.warning(f"COPY failed, using pandas fallback: {e}")
                    rel.to_df().to_parquet(abs_path, index=False)
            # Drop the temp view
            try:
                rel._cursor.execute(f"DROP VIEW IF EXISTS {tmp_view};")
            except Exception as e:
                LOGGER.debug(f"Failed to drop temp view {tmp_view}: {e}")

        # Count rows
        try:
            cnt = rel.count("*").fetchone()[0]
        except Exception as e:
            LOGGER.warning(f"Failed to count output rows: {e}")
            cnt = -1
        return cnt, str(abs_path)
    except LocalProviderError:
        raise
    except Exception as e:
        raise LocalProviderError(f"Failed to write output '{abs_path}': {e}") from e


# -------------------------
# Public API (used by CLI)
# -------------------------


def apply_plan(plan: List[Dict[str, Any]], ctx) -> Dict[str, int]:
    """
    Apply a list of actions with fail-fast = False semantics.
    Returns summary dict: {applied, failed, skipped}
    """
    applied = failed = skipped = 0
    for idx, action in enumerate(plan):
        try:
            apply_action(action, ctx)
            applied += 1
            LOGGER.info(json_log("apply_action_ok", idx=idx, resource=action.get("id")))
        except LocalProviderError as e:
            failed += 1
            LOGGER.error(
                json_log("apply_action_failed", idx=idx, resource=action.get("id"), error=str(e))
            )
        except Exception as e:
            failed += 1
            LOGGER.exception(
                json_log(
                    "apply_action_unexpected", idx=idx, resource=action.get("id"), error=str(e)
                )
            )
    return {"applied": applied, "failed": failed, "skipped": skipped}


def apply_action(action: Dict[str, Any], ctx) -> None:
    """
    Apply a single action. Supports resource_type == 'sql'.
    """
    op = (action.get("op") or "add").lower()
    rtype = (action.get("resource_type") or action.get("type") or "").lower()
    rid = action.get("id") or "<unnamed>"

    LOGGER.info(json_log("apply_action_begin", op=op, resource_type=rtype, resource_id=rid))

    if rtype != "sql":
        # No-op for other resources in local provider, but keep it explicit.
        LOGGER.info(
            json_log(
                "apply_action_skip",
                reason="unsupported_resource_type",
                resource_type=rtype,
                resource_id=rid,
            )
        )
        return

    # For "delete" on sql resource in local mode, do nothing (idempotent local semantics)
    if op == "delete":
        LOGGER.info(
            json_log(
                "apply_action_skip",
                reason="delete_noop_local",
                resource_type=rtype,
                resource_id=rid,
            )
        )
        return

    # Validate action payload
    sql = action.get("sql")
    if not sql:
        raise LocalProviderError(f"Action '{rid}' missing SQL")

    inputs = action.get("inputs") or {}
    outputs = action.get("outputs") or {}
    if not outputs:
        raise LocalProviderError(f"Action '{rid}' missing outputs block")

    # Connect to DuckDB
    con = _connect_duckdb()

    # Register inputs
    for alias, cfg in inputs.items():
        _register_input(con, alias, cfg)

    # Execute SQL
    rel = _execute_sql(con, sql)

    # Write output(s)
    # We support a single output dict or a dict with 'path' etc.
    if isinstance(outputs, dict) and "path" in outputs:
        _rowcnt, path = _write_output(rel, outputs, dry_run=bool(getattr(ctx, "dry_run", False)))
        LOGGER.info(json_log("apply_action_output", rows=_rowcnt, path=path or "(dry-run)"))
    else:
        # Support outputs = {"targets": [ {path,format}, ... ]}
        targets = outputs.get("targets", [])
        if not isinstance(targets, list) or not targets:
            raise LocalProviderError(
                f"Action '{rid}' outputs invalid (expected dict with path or targets[])"
            )
        total = 0
        for out in targets:
            rc, path = _write_output(rel, out, dry_run=bool(getattr(ctx, "dry_run", False)))
            total += max(rc, 0)
            LOGGER.info(json_log("apply_action_output", rows=rc, path=path or "(dry-run)"))
        LOGGER.info(json_log("apply_action_outputs_total", rows=total))

    try:
        con.close()
    except Exception:
        pass


# -------------------------
# Logging helper
# -------------------------


def json_log(message: str, **kwargs) -> str:
    payload = {"message": message}
    payload.update(kwargs)
    # Keep it compact; CLI wraps with its own JSON formatter too
    return str(payload)
