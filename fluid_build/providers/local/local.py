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

# fluid_build/providers/local.py
# Production-grade Local provider for FLUID Build
#
# What’s new vs. previous version:
# - If the incoming plan has ONLY unsupported ops (e.g., ensure_dataset/table),
#   we now fall back to a runnable SQL action derived from the FLUID contract.
#   The original infra ops are kept as "noop" so the report remains complete.
# - Added explicit "noop" handler (skipped: true).
# - Clear logs for the fallback decision and model lookup.
#
# Requirements:
#   pip install duckdb pandas pyarrow  (pyarrow optional, improves parquet)
#
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from fluid_build.providers._sql_safety import validate_ident
from fluid_build.providers.base import ApplyResult, BaseProvider, ProviderMetadata

from .util.logging import (
    duration_ms,
    redact_dict,
    redact_sql,
)

# Import retry and logging utilities
from .util.retry import with_retry

JSONLike = Dict[str, Any]
PathLike = Union[str, Path]

# ------------------------------ Utilities ------------------------------ #

RESERVED_LOG_KEYS = {
    "name",
    "msg",
    "message",
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
}


def _safe_extra(extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Avoid clobbering LogRecord fields by nesting custom keys under ctx."""
    extra = extra or {}
    if any(k in RESERVED_LOG_KEYS for k in extra.keys()):
        return {"ctx": extra}
    if "ctx" not in extra:
        return {"ctx": extra}
    return extra


def _now_iso() -> str:
    import datetime as _dt

    return (
        _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def _validate_ident(name: str) -> str:
    """Compatibility wrapper around the shared SQL identifier validator."""
    return validate_ident(name)


def _mkdir(p: PathLike) -> Path:
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ext(p: Path) -> str:
    return p.suffix.lower().lstrip(".")


def _has_glob(path: Path) -> bool:
    s = path.as_posix()
    return any(ch in s for ch in ("*", "?", "["))


def _guess_table_name_from_path(p: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9_]+", "_", p.stem)
    return stem or "t"


# --------------------------- DuckDB adaptor ---------------------------- #


class _Duck:
    _duck = None

    @classmethod
    def get(cls):
        if cls._duck is None:
            try:
                import duckdb  # type: ignore
            except Exception as e:
                raise RuntimeError(
                    "duckdb not installed. Install it with: pip install duckdb"
                ) from e
            cls._duck = duckdb
        return cls._duck


# ----------------------------- Provider -------------------------------- #


class LocalProvider(BaseProvider):
    """Local development provider using DuckDB.

    Runs FLUID contracts locally for rapid iteration and testing.
    Supports SQL execution, file loading, and CSV/Parquet output.
    """

    name = "local"

    @classmethod
    def get_provider_info(cls) -> ProviderMetadata:
        return ProviderMetadata(
            name="local",
            display_name="Local (DuckDB)",
            description="Local development provider — runs FLUID contracts via DuckDB for rapid iteration",
            version="0.7.1",
            author="Agentics AI / DustLabs",
            supported_platforms=["local", "duckdb"],
            tags=["local", "development", "duckdb", "sql"],
        )

    SUPPORTED_OPS = {"sql", "query", "copy", "materialize", "noop", "load_data", "execute_sql"}

    def __init__(
        self,
        *,
        project: Optional[str] = None,
        region: Optional[str] = None,
        logger: Optional[Any] = None,
        persist: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(project=project, region=region, logger=logger, **kwargs)
        self.persist = persist  # Enable persistent DuckDB at ~/.fluid/local.db

    def _get_db_path(self) -> str:
        """Get database path - persistent, session-scoped, or in-memory."""
        if self.persist:
            db_dir = Path.home() / ".fluid"
            db_dir.mkdir(parents=True, exist_ok=True)
            return str(db_dir / "local.db")
        # During an apply run, use a session-scoped file DB so tables
        # created by one action are visible to later actions.
        if hasattr(self, "_session_db"):
            return self._session_db
        return ":memory:"

    # ---------------------------- Public API ---------------------------- #

    def capabilities(self) -> Dict[str, bool]:
        """
        Advertise Local Provider capabilities.

        Updated to match GCP provider feature set for consistency.
        """
        return {
            "planning": True,  # Full planning engine with planner.py
            "apply": True,  # Execution via DuckDB
            "render": True,  # OPDS export support
            "graph": True,  # Dependency graphing
            "auth": False,  # No auth needed for local
        }

    def plan(self, contract: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Generate local execution plan from FLUID contract.

        Uses full planning engine to create properly ordered actions:
        - load_data: Import consumes[] files
        - execute_sql: Run transformations
        - materialize: Write exposes[] outputs

        Args:
            contract: FLUID contract (0.4.0 or 0.5.7 format)

        Returns:
            List of actions ready for apply()
        """
        self._log_info("local_plan_start", {"contract_id": contract.get("id")})

        # Import planner (lazy to avoid circular import)
        from .planner import plan_actions, validate_plan

        try:
            actions = plan_actions(contract, self.project, self.region, self.logger)

            # Validate plan before returning
            is_valid, errors = validate_plan(actions, self.logger)

            if not is_valid:
                self._log_error("local_plan_validation_failed", {"errors": errors})
                # Still return actions - let apply() handle the errors

            self._log_info(
                "local_plan_complete",
                {"contract_id": contract.get("id"), "actions_count": len(actions)},
            )

            return actions

        except Exception as e:
            self._log_error("local_plan_error", {"error": str(e)})
            raise

    def apply(
        self,
        actions: Optional[List[Dict[str, Any]]] = None,
        plan: Optional[Dict[str, Any]] = None,
        out: Optional[str] = None,
        **kwargs: Any,
    ) -> ApplyResult:
        """
        Execute a plan locally. Accepts either `actions` or `plan` (or both).
        - If only `plan` passed, derive actions = plan["actions"] or from embedded contract.
        - If only `actions` passed, execute them.
        - If both passed, prefer explicit `actions`.
        Fallback:
        - If the plan contains ONLY unsupported ops (e.g., ensure_dataset/table),
          we keep those as 'noop' (skipped) AND append a derived SQL action from the contract
          so the run still produces tangible artifacts.
        """
        start_ts = time.time()
        self._log_info("local_apply_start", {"project": self.project, "region": self.region})

        # Use a session-scoped DB file so tables persist across actions
        session_dir = tempfile.mkdtemp(prefix="fluid_")
        self._session_db = str(Path(session_dir) / "session.duckdb")

        # ---- Normalize actions from inputs ----
        norm_actions: Optional[List[Dict[str, Any]]] = None
        if isinstance(actions, list):
            norm_actions = actions
        elif plan is not None:
            if isinstance(plan, dict):
                if isinstance(plan.get("actions"), list):
                    norm_actions = plan["actions"]
                elif isinstance(plan.get("contract"), dict):
                    norm_actions = self._derive_actions_from_contract(plan["contract"])
                else:
                    norm_actions = self._derive_actions_from_contract(
                        plan
                    )  # treat plan itself as contract
            else:
                norm_actions = [self._demo_action()]
        else:
            norm_actions = [self._demo_action()]

        if not isinstance(norm_actions, list):
            raise TypeError("LocalProvider.apply requires a list of action dicts (plan/actions)")

        # ---- If all ops unsupported, fall back to contract-derived SQL ----
        ops = [self._op_name(a) for a in norm_actions]
        has_supported = any(op in self.SUPPORTED_OPS for op in ops)
        if not has_supported:
            contract = None
            if isinstance(plan, dict):
                contract = plan.get("contract")
            if contract is None:
                contract = kwargs.get("contract")
            if isinstance(contract, dict):
                self._log_info(
                    "local_fallback_contract_actions", {"reason": "only_unsupported_ops"}
                )
                derived = self._derive_actions_from_contract(contract)
                # Preserve original infra ops as explicit NOOPs for auditability
                norm_actions = [self._infra_as_noop(a) for a in norm_actions] + derived
            else:
                self._log_info(
                    "local_fallback_demo", {"reason": "only_unsupported_ops_no_contract"}
                )
                norm_actions = [self._infra_as_noop(a) for a in norm_actions] + [
                    self._demo_action()
                ]

        # ---- Execute ----
        results: List[Dict[str, Any]] = []
        error_count = 0
        for idx, action in enumerate(norm_actions):
            try:
                res = self._execute_action(idx, action)
                results.append({"i": idx, "status": "ok", **res})
            except Exception as e:
                error_count += 1
                self._log_error(
                    "local_apply_action_error", {"i": idx, "error": str(e), "action": action}
                )
                results.append({"i": idx, "status": "error", "error": str(e)})

        summary = ApplyResult(
            provider="local",
            applied=len(norm_actions) - error_count,
            failed=error_count,
            duration_sec=round(time.time() - start_ts, 3),
            timestamp=_now_iso(),
            results=results,
        )

        if out:
            self._write_text_or_stdout(out, summary.to_json() + "\n")

        self._append_jsonl("runtime/out/local_apply_log.jsonl", results)
        self._log_info(
            "local_apply_end", {"applied": summary["applied"], "failed": summary["failed"]}
        )

        # Clean up session DB
        if hasattr(self, "_session_db"):
            try:
                session_dir = str(Path(self._session_db).parent)
                shutil.rmtree(session_dir, ignore_errors=True)
            except OSError:
                pass
            del self._session_db

        return summary

    def render(
        self,
        src: Any = None,
        *,
        out: Optional[str] = None,
        fmt: Optional[str] = None,
        plan: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Delegate to apply() so exporter-oriented flows still produce artifacts locally.

        Signature aligned with SDK ``BaseProvider.render(src, *, out, fmt)`` while
        keeping backward-compat ``plan=`` keyword.
        """
        effective_plan = plan if plan is not None else (src if isinstance(src, dict) else None)
        return self.apply(actions=None, plan=effective_plan, out=out, **kwargs)

    # ------------------- Plan → Actions (from contract) ------------------ #

    def _derive_actions_from_contract(self, contract: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Build a single runnable SQL action for local execution:
          - Load SQL from build (supports both 0.4.0 and 0.5.7 formats).
          - Register consumes[*] files or parameters.inputs as DuckDB views.
          - Write to exposes[0] path if provided, else default under runtime/out/.
        """
        # Import contract utilities for version-agnostic access
        from fluid_build.util.contract import get_primary_build

        build = get_primary_build(contract)
        sql_text: Optional[str] = None
        inputs_spec: List[Any] = []

        if build:
            # Check for inline SQL in properties.sql (0.5.7) or properties.model file (0.4.0)
            props = build.get("properties") or {}

            # Try inline SQL first (0.5.7 embedded-logic pattern)
            inline_sql = props.get("sql")
            if isinstance(inline_sql, str) and inline_sql.strip():
                sql_text = inline_sql
                self._log_info("local_sql_inline", {"length": len(sql_text)})

                # Get inputs from parameters.inputs (0.5.7)
                params = props.get("parameters") or {}
                param_inputs = params.get("inputs") or []
                for inp in param_inputs:
                    if isinstance(inp, dict):
                        path = inp.get("path")
                        name = (
                            inp.get("name") or _guess_table_name_from_path(Path(path))
                            if path
                            else None
                        )
                        if path and name:
                            inputs_spec.append({"path": path, "table": name})
            else:
                # Try model file path (0.4.0)
                trans = build.get("transformation") or {}
                trans_props = trans.get("properties") or {}
                model_path = trans_props.get("model")

                if isinstance(model_path, str) and model_path.strip():
                    mp = Path(model_path)
                    if mp.exists():
                        self._log_info("local_model_found", {"path": str(mp)})
                        sql_text = mp.read_text(encoding="utf-8")
                    else:
                        self._log_warn("local_model_missing", {"model": model_path})

        # If no inputs from build, collect from consumes (fallback)
        if not inputs_spec:
            for c in contract.get("consumes") or []:
                p = c.get("path") or (c.get("location") or {}).get("path")
                if p:
                    inputs_spec.append(
                        {"path": p, "table": c.get("id") or _guess_table_name_from_path(Path(p))}
                    )

        # Decide output
        output_paths: List[str] = []
        exposes = contract.get("exposes") or []
        if exposes:
            loc = exposes[0].get("location") or {}
            out_path = loc.get("path")
            if out_path:
                output_paths = [out_path]
            else:
                cid = contract.get("id") or "product"
                output_paths = [f"runtime/out/{cid.replace('.', '_')}.csv"]
        else:
            output_paths = ["runtime/out/output.csv"]

        # Fallback SQL if none found
        if not sql_text:
            if inputs_spec:
                tbl = validate_ident(inputs_spec[0].get("table") or "t")
                sql_text = f"SELECT * FROM {tbl}"
            else:
                sql_text = "SELECT 1 AS demo_col"

        return [{"op": "sql", "sql": sql_text, "inputs": inputs_spec, "outputs": output_paths}]

    def _demo_action(self) -> Dict[str, Any]:
        return {"op": "copy", "out": "runtime/out/demo_artifact.csv"}

    def _infra_as_noop(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Turn infra-only action (e.g., ensure_dataset) into a harmless NOOP report entry."""
        op = self._op_name(action)
        return {"op": "noop", "original_op": op, "skipped": True}

    # ------------------------- Action execution ------------------------- #

    def _op_name(self, action: Dict[str, Any]) -> str:
        return (action.get("op") or action.get("type") or "").lower().strip()

    def _execute_action(self, idx: int, action: Dict[str, Any]) -> Dict[str, Any]:
        # Flatten payload into the action so executors can use top-level keys.
        # The planner wraps params in action["payload"], but executors expect
        # keys like "sql", "dst", "path" at the top level.
        flat = dict(action)
        payload = flat.pop("payload", None)
        if isinstance(payload, dict):
            for k, v in payload.items():
                flat.setdefault(k, v)
        op = self._op_name(flat)

        if op in {"sql", "query", "execute_sql"}:
            return self._run_sql_action(idx, flat)
        if op in {"load_data"}:
            return self._run_load_data_action(idx, flat)
        if op in {"copy", "materialize"}:
            return self._run_copy_action(idx, flat)
        if op == "noop":
            self._log_info(
                "local_noop", {"i": idx, "original_op": flat.get("original_op", "<unknown>")}
            )
            return {"op": "noop", "skipped": True, "original_op": flat.get("original_op")}

        # Unknown op => treat as NOOP; don't fail the run.
        self._log_warn("local_unknown_action", {"i": idx, "op": op or "<missing>"})
        return {"op": op or "<missing>", "skipped": True}

    # ----------------------- Load Data Action (NEW) --------------------- #

    def _run_load_data_action(self, idx: int, action: Dict[str, Any]) -> Dict[str, Any]:
        """
        Load data from file into DuckDB table with retry logic.

        Supports: CSV, TSV, Parquet, JSON, JSONL
        Handles: Globs, schemas, custom options, retries on transient errors
        """
        duckdb = _Duck.get()

        path = action.get("path")
        table_name = action.get("table_name") or action.get("resource_id")
        fmt = action.get("format", "csv")
        options = action.get("options", {})

        if not path:
            raise ValueError(f"load_data action {idx} missing path")
        if not table_name:
            raise ValueError(f"load_data action {idx} missing table_name")

        path_obj = Path(path)

        # Check if file exists (unless glob pattern)
        if not _has_glob(path_obj) and not path_obj.exists():
            raise FileNotFoundError(f"Input file not found: {path}")

        db_path = self._get_db_path()
        con = duckdb.connect(database=db_path)

        try:
            # Use retry logic for table registration (can fail with I/O errors)
            def _register_with_retry():
                self._register_one(con, table_name, path_obj, fmt, options)

            with_retry(_register_with_retry, logger=self.logger, max_attempts=3)

            # Get row count for reporting
            rowcount = con.execute(f"SELECT COUNT(*) FROM {validate_ident(table_name)}").fetchone()[
                0
            ]

            self._log_info(
                "local_load_data_complete",
                {
                    "i": idx,
                    "table": table_name,
                    "path": str(path),
                    "rows": rowcount,
                    "persistent": self.persist,
                },
            )

            return {
                "op": "load_data",
                "table": table_name,
                "path": str(path),
                "rows": rowcount,
                "format": fmt,
            }

        except Exception as e:
            self._log_error(
                "local_load_data_error",
                {"i": idx, "error": str(e), "path": str(path), "table": table_name},
            )
            raise

    # ----------------------- SQL (DuckDB) execution --------------------- #

    def _run_sql_action(self, idx: int, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute SQL with retry logic, persistent DB, and enhanced logging."""
        duckdb = _Duck.get()
        start_time = time.time()

        sql = action.get("sql") or action.get("query")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("SQL action missing 'sql'/'query' string")

        _mkdir("runtime/out")

        db_path = self._get_db_path()
        con = duckdb.connect(database=db_path)
        con.execute("PRAGMA threads=4;")

        inputs = action.get("inputs") or action.get("tables") or []
        reg_info = self._register_inputs(con, inputs)

        # Log with redacted SQL (in case it contains sensitive data)
        redacted_sql = redact_sql(sql)
        self._log_info(
            "local_sql_begin",
            {
                "i": idx,
                "sql_len": len(sql),
                "sql_preview": redacted_sql[:200],
                "persistent": self.persist,
            },
        )

        try:
            # Execute SQL with retry logic (handles transient DuckDB errors)
            def _execute_sql():
                return con.sql(sql)

            rel = with_retry(_execute_sql, logger=self.logger, max_attempts=3)

        except Exception as e:
            self._log_error(
                "local_sql_error",
                {
                    "i": idx,
                    "error": str(e),
                    "sql_preview": redacted_sql[:500],
                    "duration_ms": duration_ms(start_time),
                },
            )
            raise

        # If an output_table is specified, persist the result as a DuckDB table
        # so downstream materialize/copy steps can reference it.
        output_table = action.get("output_table") or action.get("table_name")
        if output_table and rel is not None:
            try:
                con.execute(
                    f"CREATE OR REPLACE TABLE {validate_ident(output_table)} AS SELECT * FROM ({sql})"
                )
            except Exception as e:
                self._log_warn(
                    "local_sql_create_table_warn", {"table": output_table, "error": str(e)}
                )

        outputs = action.get("outputs") or action.get("out") or []
        if isinstance(outputs, (str, Path)):
            outputs = [outputs]

        written: List[str] = []
        if outputs:
            for out_spec in outputs:
                p, fmt = self._normalize_output(out_spec)

                # Retry write operations (can fail with I/O errors)
                def _write_with_retry():
                    self._write_relation(rel, p, fmt)

                with_retry(_write_with_retry, logger=self.logger, max_attempts=3)
                written.append(str(p))
        else:
            p = Path(f"runtime/out/preview_{idx}.csv")

            def _write_preview():
                rel.limit(50).write_csv(str(p))

            with_retry(_write_preview, logger=self.logger, max_attempts=3)
            written.append(str(p))

        rowcount = None
        try:
            rowcount = rel.aggregate("count(*)").fetchone()[0]
        except Exception:
            pass

        self._log_info(
            "local_sql_end",
            {
                "i": idx,
                "written": written,
                "rows": rowcount,
                "duration_ms": duration_ms(start_time),
            },
        )
        return {"op": "sql", "written": written, "rows": rowcount, "inputs": reg_info}

    def _register_inputs(self, con: Any, inputs: Iterable[Any]) -> List[Dict[str, Any]]:
        info: List[Dict[str, Any]] = []
        for item in inputs or []:
            if isinstance(item, (str, Path)):
                path = Path(str(item))
                table = _guess_table_name_from_path(path)
                fmt = _ext(path)
                self._register_one(con, table, path, fmt, options=None)
                info.append({"table": table, "path": str(path), "format": fmt or "auto"})
            elif isinstance(item, dict):
                path = Path(str(item.get("path", "")))
                table = str(item.get("table") or _guess_table_name_from_path(path))
                fmt = str(item.get("format") or _ext(path) or "csv").lower()
                options = item.get("options") or {}
                self._register_one(con, table, path, fmt, options)
                info.append({"table": table, "path": str(path), "format": fmt, "options": options})
            else:
                raise TypeError(f"Unsupported input spec: {item!r}")
        return info

    def _register_one(
        self, con: Any, table: str, path: Path, fmt: str, options: Optional[Dict[str, Any]]
    ) -> None:
        is_glob = _has_glob(path)
        if not is_glob and not path.exists():
            raise FileNotFoundError(f"Input file not found: {path}")

        if fmt in {"csv", "tsv"} or path.suffix.lower() in {".csv", ".tsv"}:
            delim = "," if (fmt != "tsv" and path.suffix.lower() != ".tsv") else "\t"
            opt = {"AUTO_DETECT": True, "DELIM": delim}
            opt.update({k.upper(): v for k, v in (options or {}).items()})
            opt_sql = ", ".join(
                f"{k}:={json.dumps(v) if not isinstance(v, str) else repr(v)}"
                for k, v in opt.items()
            )
            con.execute(
                f"CREATE OR REPLACE VIEW {validate_ident(table)} AS SELECT * FROM read_csv_auto({repr(str(path))}, {opt_sql});"
            )
        elif fmt in {"parquet", "pq"} or path.suffix.lower() == ".parquet":
            con.execute(
                f"CREATE OR REPLACE VIEW {validate_ident(table)} AS SELECT * FROM read_parquet({repr(str(path))});"
            )
        else:
            # try csv auto as fallback
            con.execute(
                f"CREATE OR REPLACE VIEW {validate_ident(table)} AS SELECT * FROM read_csv_auto({repr(str(path))});"
            )

    # ------------------ COPY / materialize (helper) --------------------- #

    def _run_copy_action(self, idx: int, action: Dict[str, Any]) -> Dict[str, Any]:
        src = action.get("src") or action.get("source_table")
        dst = action.get("dst") or action.get("out") or action.get("path")
        if not dst:
            raise ValueError("copy/materialize action requires 'dst', 'out', or 'path'")

        dst = Path(str(dst))
        _mkdir(dst.parent)

        # If we have a source_table, try to materialize from DuckDB
        source_table = action.get("source_table")
        fmt = (action.get("format") or _ext(dst) or "csv").lower()
        if source_table:
            try:
                duckdb = _Duck.get()
                db_path = self._get_db_path()
                con = duckdb.connect(database=db_path)
                rel = con.sql(f"SELECT * FROM {validate_ident(source_table)}")
                self._write_relation(rel, dst, fmt)
                rowcount = rel.count("*").fetchone()[0] if hasattr(rel, "count") else -1
                self._log_info(
                    "local_materialize_done",
                    {"i": idx, "dst": str(dst), "source": source_table, "rows": rowcount},
                )
                return {
                    "op": "materialize",
                    "dst": str(dst),
                    "source_table": source_table,
                    "format": fmt,
                }
            except Exception as e:
                self._log_warn(
                    "local_materialize_fallback",
                    {"i": idx, "source_table": source_table, "error": str(e)},
                )

        if src and not source_table:
            src = Path(str(src))
            if not src.exists():
                raise FileNotFoundError(f"copy src not found: {src}")
            data = src.read_bytes()
            dst.write_bytes(data)
        else:
            dst.write_text("id,value\n1,materialized\n", encoding="utf-8")

        self._log_info("local_copy_done", {"i": idx, "dst": str(dst)})
        return {"op": "copy", "dst": str(dst)}

    # -------------------------- Output writing -------------------------- #

    def _normalize_output(self, out_spec: Union[str, Path, Dict[str, Any]]) -> Tuple[Path, str]:
        if isinstance(out_spec, (str, Path)):
            p = Path(str(out_spec))
            e = _ext(p)
            fmt = "csv" if e in {"", "csv"} else ("parquet" if e in {"parquet", "pq"} else "csv")
            return p, fmt
        if isinstance(out_spec, dict):
            p = Path(str(out_spec.get("path")))
            fmt = (out_spec.get("format") or _ext(p) or "csv").lower()
            if fmt not in {"csv", "parquet"}:
                fmt = "csv"
            return p, fmt
        raise TypeError(f"Unsupported output spec: {out_spec!r}")

    def _write_relation(self, rel: Any, path: Path, fmt: str) -> None:
        _mkdir(path.parent)
        if fmt == "parquet":
            rel.write_parquet(str(path), compression="snappy")
        else:
            rel.write_csv(str(path))

    # ---------------------------- I/O helpers --------------------------- #

    def _write_text_or_stdout(self, out: str, text: str) -> None:
        if out.strip() == "-":
            sys.stdout.write(text)
            sys.stdout.flush()
        else:
            p = Path(out)
            _mkdir(p.parent)
            p.write_text(text, encoding="utf-8")

    def _append_jsonl(self, path: PathLike, items: List[Dict[str, Any]]) -> None:
        p = Path(path)
        _mkdir(p.parent)
        with p.open("a", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item) + "\n")

    # ---------------------------- Logging -------------------------------- #

    def _log_info(self, msg: str, extra: Optional[Dict[str, Any]] = None) -> None:
        """Log info with automatic secret redaction."""
        if self.logger:
            redacted_extra = redact_dict(extra) if extra else None
            self.logger.info(msg, extra=_safe_extra(redacted_extra))

    def _log_warn(self, msg: str, extra: Optional[Dict[str, Any]] = None) -> None:
        """Log warning with automatic secret redaction."""
        if self.logger:
            redacted_extra = redact_dict(extra) if extra else None
            self.logger.warning(msg, extra=_safe_extra(redacted_extra))

    def _log_error(self, msg: str, extra: Optional[Dict[str, Any]] = None) -> None:
        """Log error with automatic secret redaction."""
        if self.logger:
            redacted_extra = redact_dict(extra) if extra else None
            self.logger.error(msg, extra=_safe_extra(redacted_extra))
