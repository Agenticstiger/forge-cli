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

from fluid_build.observability.secret_redactor import redact_secret_text, redact_value
from fluid_build.providers._duckdb_read import build_register_view_sql
from fluid_build.providers._duckdb_sandbox import (
    DuckDBAllowlist,
    is_sandbox_refusal,
    sandbox_refusal_hint,
    secure_duckdb_connect,
    unaliased_dir,
)
from fluid_build.providers._sql_safety import quote_ansi_string_literal, validate_ident
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


def _is_s3_uri(path: str) -> bool:
    return path[:5].lower() == "s3://"


def _s3_bucket_regions(specs: Iterable[Any]) -> Dict[str, Optional[str]]:
    """``{bucket: region}`` for every ``s3://`` input or output spec.

    A spec's ``region`` is the binding's ``location.region``. The first spec
    that names a region for a bucket sets it: one bucket lives in one region.
    """
    buckets: Dict[str, Optional[str]] = {}
    for spec in specs:
        raw = spec.get("path") if isinstance(spec, dict) else spec
        if not isinstance(raw, str) or not _is_s3_uri(raw):
            continue
        bucket = raw.split("://", 1)[1].split("/", 1)[0]
        region = spec.get("region") if isinstance(spec, dict) else None
        if bucket and not buckets.get(bucket):
            buckets[bucket] = str(region) if region else None
    return buckets


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
        anchor_dir: Optional[PathLike] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(project=project, region=region, logger=logger, **kwargs)
        self.persist = persist  # Enable persistent DuckDB at ~/.fluid/local.db
        # Directory a relative ``exposes[].binding.location.path`` resolves
        # against: the SOURCE contract's directory (``fluid_build.util.
        # binding_paths``). ``None`` keeps the historical working-directory
        # semantics for callers that have no source contract to anchor to.
        self.anchor_dir: Optional[Path] = Path(anchor_dir) if anchor_dir else None

    def _anchored(self, contract: Dict[str, Any]) -> Dict[str, Any]:
        """``contract`` with its relative expose paths anchored at :attr:`anchor_dir`."""
        if self.anchor_dir is None or not isinstance(contract, dict):
            return contract
        from fluid_build.util.binding_paths import anchor_binding_paths

        return anchor_binding_paths(contract, self.anchor_dir)

    # ------------------------- Sandboxed DuckDB ------------------------- #

    @staticmethod
    def _declared_io(action: Dict[str, Any]) -> List[Any]:
        """Every location ``action`` declares it reads or writes through DuckDB."""
        op = (action.get("op") or action.get("type") or "").lower().strip()
        found: List[Any] = []
        if op in {"sql", "query", "execute_sql"}:
            for key in ("inputs", "tables", "outputs", "out"):
                value = action.get(key) or []
                found.extend(value if isinstance(value, list) else [value])
        elif op == "load_data":
            found.append(action.get("path"))
        elif op in {"copy", "materialize"}:
            found.append(action.get("dst") or action.get("out") or action.get("path"))
        return [spec for spec in found if spec]

    def _declare_run_io(self, actions: Iterable[Dict[str, Any]]) -> None:
        """Record what every action of this run declares, before any SQL runs.

        One apply shares one session database, so a view one action registers
        over a file is read again by a later action's SQL: every connection of
        the run gets the run's whole declared allowlist, not just its own.
        """
        specs: List[Any] = []
        for action in actions:
            flat = dict(action)
            payload = flat.pop("payload", None)
            if isinstance(payload, dict):
                for key, value in payload.items():
                    flat.setdefault(key, value)
            specs.extend(self._declared_io(flat))
        self._run_io = specs

    def _allowlist(self, specs: Iterable[Any]) -> DuckDBAllowlist:
        """What this provider's SQL may touch, and nothing else.

        The source contract's directory (``anchor_dir``) and the FLUID
        workspace it sits in, the run's session scratch directory,
        ``./runtime`` (where previews and default outputs land), and each
        location the actions declare: an input file, an output file, an
        ``s3://`` prefix. A contract's SQL that names any other path,
        ``/etc/passwd`` or ``~/.aws/credentials``, is refused by DuckDB.
        ``./runtime`` is left out when it is a symlink that leads outside the
        contract's directory and workspace (:func:`unaliased_dir`).

        A declared location is granted only inside those directories (plus
        the upstream roots in ``FLUID_UPSTREAM_CONTRACTS`` and the operator's
        ``FLUID_DUCKDB_ALLOWED_DIRS``): the contract's author writes the
        declaration, so it must not be a way to grant the host
        (``DuckDBAllowlist.with_declared``). A relative declared path is
        resolved where DuckDB opens it, the working directory, and is confined
        all the same.
        """
        from fluid_build.util.upstream_discovery import collect_search_roots
        from fluid_build.util.workspace_root import find_workspace_root

        session = getattr(self, "_session_db", None)
        scratch = Path(session).parent if session else None
        # Without a contract directory (a bare ``apply`` of actions), the
        # working directory stands in for it, as it does for relative paths.
        anchor = self.anchor_dir if self.anchor_dir is not None else Path.cwd()
        # A contract inside a FLUID workspace (``fluid.workspace.yaml``) may
        # read its sibling products' files by path, as consumes[] does.
        workspace = find_workspace_root(self.anchor_dir) if self.anchor_dir is not None else None
        # ``./runtime`` is in the working directory, usually the contract's
        # own, so the contract's repository can ship it as a symlink
        # (``runtime -> ../../..``). Granted only where its name says it is, or
        # inside the contract's directory or workspace; otherwise not at all.
        runtime = unaliased_dir("runtime", within=[anchor, workspace])
        if runtime is None:
            self._log_warn(
                "local_runtime_not_granted",
                {"runtime": str(Path("runtime").absolute()), "reason": "symlink_leads_out"},
            )
        allow = DuckDBAllowlist.none().with_dirs(self.anchor_dir, workspace, runtime, scratch)
        within = [anchor, runtime, scratch, *collect_search_roots(workspace)]
        for spec in specs:
            raw = spec.get("path") if isinstance(spec, dict) else spec
            if not raw:
                continue
            raw = str(raw)
            # Only s3:// is remote here (``_register_mapping_input``): any other
            # string is a local path, as ``Path`` reads it.
            allow = allow.with_declared(raw if _is_s3_uri(raw) else str(Path(raw)), within=within)
        return allow

    def _connect(self, specs: Iterable[Any] = (), *, config: Optional[Dict[str, Any]] = None):
        """The provider's one way to DuckDB: sandboxed to :meth:`_allowlist`.

        ``specs`` are the calling action's own declared locations, added to the
        run's (:meth:`_declare_run_io`). The S3 buckets among them get their
        extensions and credential secret before the configuration is locked.
        """
        _Duck.get()
        all_specs = [*getattr(self, "_run_io", []), *specs]
        allow = self._allowlist(all_specs)
        self._last_allow = allow
        return secure_duckdb_connect(
            self._get_db_path(),
            allow=allow,
            config=config,
            before_lock=lambda con: self._attach_object_stores(con, all_specs),
        )

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

    def plan(
        self,
        contract: Dict[str, Any],
        *,
        mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Generate local execution plan from FLUID contract.

        Uses full planning engine to create properly ordered actions:
        - load_data: Import consumes[] files
        - execute_sql: Run transformations
        - materialize: Write exposes[] outputs

        The optional ``mode`` argument carries the apply-time mode
        (``replace`` / ``replace-and-build`` etc.). Forwarded to the
        planner so destructive modes can adjust materialisation
        strategy (e.g. CREATE OR REPLACE TABLE for SQL transforms,
        ``COPY ... TO`` overwriting for parquet sinks). Local SDP via
        DuckDB acquisition already overwrites parquet implicitly via
        ``COPY ... TO`` so this is mostly a no-op for SDP — but SQL
        transforms (engine: sql, pattern: embedded-logic) honour it.

        Args:
            contract: FLUID contract (0.7.x format)
            mode: apply-time mode; None for additive default

        Returns:
            List of actions ready for apply()
        """
        self._log_info("local_plan_start", {"contract_id": contract.get("id"), "mode": mode})
        contract = self._anchored(contract)

        # Import planner (lazy to avoid circular import)
        from .planner import plan_actions, validate_plan

        try:
            try:
                actions = plan_actions(contract, self.project, self.region, self.logger, mode=mode)
            except TypeError:
                # Older planner signature without mode kwarg.
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
        self._declare_run_io(a for a in norm_actions if isinstance(a, dict))
        results: List[Dict[str, Any]] = []
        error_count = 0
        for idx, action in enumerate(norm_actions):
            try:
                res = self._execute_action(idx, action)
                results.append({"i": idx, "status": "ok", **res})
            except Exception as e:
                error_count += 1
                # The error text can carry a value the SQL was templated with
                # (``CAST('{{ env.X }}' AS INTEGER)`` echoes X's value), and the
                # result is printed by the build runner and appended to
                # ``runtime/out/local_apply_log.jsonl``: both get it redacted.
                error_text = redact_secret_text(str(e))
                self._log_error(
                    "local_apply_action_error",
                    {"i": idx, "error": error_text, "action": redact_value(action)},
                )
                results.append({"i": idx, "status": "error", "error": error_text})

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
          - Load SQL from build (v0.7.x canonical only formats).
          - Register consumes[*] files or parameters.inputs as DuckDB views.
          - Write to exposes[0] path if provided, else default under runtime/out/.
        """
        # Import contract utilities for version-agnostic access
        from fluid_build.util.contract import get_primary_build

        contract = self._anchored(contract)
        build = get_primary_build(contract)
        sql_text: Optional[str] = None
        inputs_spec: List[Any] = []

        if build:
            # Check for inline SQL in properties.sql (v0.7.x) or properties.model file (legacy, dropped)
            props = build.get("properties") or {}

            # Try inline SQL first (v0.7.x embedded-logic pattern)
            inline_sql = props.get("sql")
            if isinstance(inline_sql, str) and inline_sql.strip():
                sql_text = inline_sql
                self._log_info("local_sql_inline", {"length": len(sql_text)})

                # Get inputs from parameters.inputs (v0.7.x)
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
                # Try model file path (legacy, dropped)
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

        # consumes[] cannot be a fallback source of inputs, and saying so is
        # more useful than pretending otherwise. This block used to read
        # ``c["path"]`` / ``c["location"]["path"]`` / ``c["id"]``; the
        # consumeRef schema is ``additionalProperties: false`` and permits
        # none of them (only productId, exposeId, versionConstraint,
        # qosExpectations, requiredPolicies, purpose, tags, labels,
        # upstreamWorkspace, upstreamDigest), so it could never fire for a
        # contract that validates. A consumeRef carries a LOGICAL address
        # only; turning it into a physical one needs the upstream contract,
        # which is tracked separately as the consumes[]-wiring work.
        unbound = len(contract.get("consumes") or []) if not inputs_spec else 0
        if unbound:
            self._log_warn(
                "local_consumes_not_bound",
                {
                    "count": unbound,
                    "hint": (
                        "consumes[] declares upstream products but carries no "
                        "physical address. Declare the reader explicitly under "
                        "builds[].properties.parameters.inputs to bind it."
                    ),
                },
            )

        # Decide output — honour the declared path AND format from the first expose.
        # A contract that declares ``format: parquet`` at an ``output/*.parquet``
        # path previously had apply silently write a CSV to the wrong (default)
        # path. Fix: read format from ``binding.format`` / ``format`` fields and
        # use the binding ``location.path`` or ``path`` field as declared.
        output_paths: List[str] = []
        exposes = contract.get("exposes") or []
        if exposes:
            first_expose = exposes[0]
            # Resolve location from binding (v0.7.x) or direct location key (legacy).
            binding = first_expose.get("binding") or {}
            loc = binding.get("location") or first_expose.get("location") or {}
            out_path = loc.get("path")

            # Resolve declared output format so parquet contracts write parquet.
            fmt_raw = (binding.get("format") or first_expose.get("format") or "").lower()
            # Normalise aliases.
            if fmt_raw in {"parquet", "pq"}:
                declared_fmt = "parquet"
            else:
                declared_fmt = "csv"

            if out_path:
                # The file name and format come from the one helper the
                # consumes[] reader also uses (``local_provider_landing``), so
                # a downstream build reads this file where, and as what, it was
                # written: a parquet binding whose path lacks the suffix gets
                # ``.parquet``, and any non-parquet format is written as CSV.
                from fluid_build.util.binding_paths import local_provider_landing

                out_path, declared_fmt = local_provider_landing(str(out_path), fmt_raw)
                output_paths = [{"path": out_path, "format": declared_fmt}]
            else:
                cid = contract.get("id") or "product"
                stem = cid.replace(".", "_")
                ext = ".parquet" if declared_fmt == "parquet" else ".csv"
                output_paths = [{"path": f"runtime/out/{stem}{ext}", "format": declared_fmt}]
        else:
            output_paths = [{"path": "runtime/out/output.csv", "format": "csv"}]

        # Fallback SQL if none found
        if not sql_text:
            if inputs_spec:
                tbl = validate_ident(inputs_spec[0].get("table") or "t")
                sql_text = f"SELECT * FROM {tbl}"
            else:
                existing = self._existing_outputs(output_paths)
                if existing:
                    # No SQL and no inputs: the only thing left to write is a
                    # one-row ``demo_col`` placeholder, and the declared output
                    # already holds data (an acquisition build lands it). Now
                    # that a relative path resolves at the contract's directory,
                    # that is the file the build wrote; never replace it with a
                    # placeholder.
                    self._log_warn(
                        "local_placeholder_refused",
                        {"outputs": existing, "reason": "declared output exists; no SQL"},
                    )
                    return [{"op": "noop", "skipped": True, "reason": "placeholder_refused"}]
                sql_text = "SELECT 1 AS demo_col"

        return [{"op": "sql", "sql": sql_text, "inputs": inputs_spec, "outputs": output_paths}]

    @staticmethod
    def _existing_outputs(output_paths: List[Any]) -> List[str]:
        """The output paths in ``output_paths`` that already exist on disk."""
        found: List[str] = []
        for spec in output_paths:
            raw = spec.get("path") if isinstance(spec, dict) else spec
            if raw and Path(str(raw)).exists():
                found.append(str(raw))
        return found

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

        con = self._connect([path])

        # Closed on every path: every action of the run shares one session
        # database, and a connection left open (an exception's traceback keeps
        # it alive) holds that file's locked instance, which refuses the next
        # action's sandboxed connection.
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
        finally:
            con.close()

    # ----------------------- SQL (DuckDB) execution --------------------- #

    def _run_sql_action(self, idx: int, action: Dict[str, Any]) -> Dict[str, Any]:
        """Execute SQL with retry logic, persistent DB, and enhanced logging."""
        start_time = time.time()

        sql = action.get("sql") or action.get("query")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("SQL action missing 'sql'/'query' string")

        _mkdir("runtime/out")

        inputs = action.get("inputs") or action.get("tables") or []
        outputs = action.get("outputs") or action.get("out") or []
        if isinstance(outputs, (str, Path)):
            outputs = [outputs]
        # Threads set at connect: the sandbox locks the configuration, so a
        # ``PRAGMA threads`` afterwards is refused.
        con = self._connect([*inputs, *outputs], config={"threads": 4})
        # Closed on every path, a failure included (see _run_load_data_action):
        # otherwise one failing SQL action fails every later action of the run.
        try:
            return self._run_sql_on(con, idx, action, sql, inputs, outputs, start_time)
        finally:
            con.close()

    def _run_sql_on(
        self,
        con: Any,
        idx: int,
        action: Dict[str, Any],
        sql: str,
        inputs: List[Any],
        outputs: List[Any],
        start_time: float,
    ) -> Dict[str, Any]:
        """The body of :meth:`_run_sql_action`, on a connection it closes."""
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
            if not is_sandbox_refusal(e):
                raise
            # A path outside the sandbox: say what the SQL may read instead.
            # Raised from a helper, so this frame keeps no reference to the new
            # error (an error -> traceback -> frame -> error cycle would keep
            # the connection alive until the cyclic GC runs).
            raise self._sandbox_refusal(e) from e

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

        written: List[str] = []
        if outputs:
            for out_spec in outputs:
                written.append(self._write_output(con, rel, out_spec))
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

    def _sandbox_refusal(self, refused: BaseException) -> PermissionError:
        """``refused`` as a PermissionError that names what the SQL may read instead."""
        return PermissionError(f"{refused} {sandbox_refusal_hint(self._last_allow, refused)}")

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
                info.append(self._register_mapping_input(con, item))
            else:
                raise TypeError(f"Unsupported input spec: {item!r}")
        return info

    def _register_mapping_input(self, con: Any, item: Dict[str, Any]) -> Dict[str, Any]:
        """Register one ``{path, table?, format?, options?, quoted?}`` input spec."""
        raw = str(item.get("path", ""))
        # An ``s3://`` URI stays a string: ``Path`` folds ``s3://`` into
        # ``s3:/``, which DuckDB cannot read. Only S3, whose access
        # ``_attach_object_stores`` sets up: any other scheme (``http://``,
        # ``gs://``) still takes the local-file path and its existence check,
        # rather than a fetch from a URL a contract names.
        path: Union[str, Path] = raw if _is_s3_uri(raw) else Path(raw)
        table = str(item.get("table") or _guess_table_name_from_path(Path(raw)))
        fmt = str(item.get("format") or _ext(Path(raw)) or "csv").lower()
        options = item.get("options") or {}
        self._register_one(
            con, table, path, fmt, options, quote_identifier=bool(item.get("quoted"))
        )
        entry: Dict[str, Any] = {
            "table": table,
            "path": str(path),
            "format": fmt,
            "options": options,
        }
        # A consumes[]-resolved input says which upstream it is, so the apply
        # log records the lineage that ran, not just a path.
        if item.get("productId"):
            entry.update(productId=item["productId"], exposeId=item.get("exposeId"), uri=str(path))
        return entry

    def _register_one(
        self,
        con: Any,
        table: str,
        path: Union[str, Path],
        fmt: str,
        options: Optional[Dict[str, Any]],
        *,
        quote_identifier: bool = False,
    ) -> None:
        # Existence is checked for a local file only. An object-store URI is
        # checked by DuckDB when the view is created, which fails on a glob
        # that matches nothing, so an empty upstream prefix still fails here.
        if isinstance(path, Path) and not _has_glob(path) and not path.exists():
            raise FileNotFoundError(f"Input file not found: {path}")

        # The statement itself lives in ``providers/_duckdb_read`` because the
        # sql engine writes the same one into its generated script. One copy:
        # otherwise ``fluid apply`` and the emitted script drift, which is the
        # split that left six shipped examples generating SQL that will not run.
        con.execute(
            build_register_view_sql(table, path, fmt, options, quote_identifier=quote_identifier)
        )

    def _attach_object_stores(self, con: Any, specs: Iterable[Any]) -> None:
        """Load httpfs and authenticate each bucket an input or output names.

        Reuses the duckdb acquisition runner's own helper, so a build reads an
        upstream's objects, and lands its own, with the same extensions and the
        same ambient-credential-chain secret that runner writes them with. One
        secret per bucket, scoped to it, carrying that spec's ``region``.
        """
        buckets = _s3_bucket_regions(specs)
        if not buckets:
            return
        from fluid_build.build_runners.duckdb.runner import attach_object_store

        for n, (bucket, region) in enumerate(sorted(buckets.items())):
            attach_object_store(
                con,
                f"s3://{bucket}/",
                region=region,
                scope=f"s3://{bucket}",
                name=f"__fluid_s3_{n}",
            )

    def _write_output(self, con: Any, rel: Any, out_spec: Any) -> str:
        """Write the result to one output spec, with retry; return where it went.

        An ``s3://`` spec is ``COPY``-ed to that object (``_copy_relation_to_uri``);
        anything else is a local file, as it always was.
        """
        remote = self._remote_output(out_spec)
        if remote is not None:
            uri, remote_fmt = remote

            def _copy_with_retry():
                self._copy_relation_to_uri(con, rel, uri, remote_fmt)

            with_retry(_copy_with_retry, logger=self.logger, max_attempts=3)
            return uri
        p, fmt = self._normalize_output(out_spec)

        # Retry write operations (can fail with I/O errors)
        def _write_with_retry():
            self._write_relation(rel, p, fmt)

        with_retry(_write_with_retry, logger=self.logger, max_attempts=3)
        return str(p)

    @staticmethod
    def _remote_output(out_spec: Any) -> Optional[Tuple[str, str]]:
        """``(uri, format)`` for an ``s3://`` output spec, else ``None``."""
        raw = out_spec.get("path") if isinstance(out_spec, dict) else out_spec
        if not isinstance(raw, str) or not _is_s3_uri(raw):
            return None
        fmt = str((out_spec.get("format") if isinstance(out_spec, dict) else "") or "parquet")
        return raw, fmt.lower()

    def _copy_relation_to_uri(self, con: Any, rel: Any, uri: str, fmt: str) -> None:
        """``COPY`` the result to an object-store URI, as the duckdb runner lands one.

        The statement is the runner's own (``_build_copy_destination``), so an
        embedded-SQL build writes the same bytes, format options included, as
        an acquisition build landing the same binding.
        """
        from fluid_build.build_runners.duckdb.runner import _build_copy_destination

        view = "__fluid_embedded_result"
        rel.create_view(view, replace=True)
        # ``replace`` on the first ``{select}``, not ``str.format``: the quoted
        # URI follows it in the template and may itself hold braces.
        template = _build_copy_destination(uri, fmt)
        con.execute(template.replace("{select}", f"SELECT * FROM {view}", 1))

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
                con = self._connect([dst])
                try:
                    rel = con.sql(f"SELECT * FROM {validate_ident(source_table)}")
                    self._write_relation(rel, dst, fmt)
                    rowcount = rel.count("*").fetchone()[0] if hasattr(rel, "count") else -1
                    del rel
                finally:
                    # Closed on every path (see _run_load_data_action).
                    con.close()
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
        elif dst.exists():
            # Nothing to copy or materialize from, and the destination
            # already holds data (a build wrote it): writing the
            # ``id,value`` placeholder would destroy it. Report and skip.
            self._log_warn(
                "local_placeholder_refused",
                {"i": idx, "dst": str(dst), "reason": "destination exists; no source"},
            )
            return {"op": "noop", "dst": str(dst), "skipped": True}
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
        """Log info with automatic secret redaction.

        Emitted at DEBUG level so observability breadcrumbs
        (``local_plan_start``, ``local_apply_end``, ``local_sql_begin``,
        etc.) don't bleed into user-facing CLI output. Operators who
        want them surface them with ``--debug`` / ``FLUID_LOG_LEVEL=DEBUG``
        or by routing to a ``--log-file`` JSON sink.

        UX hardening pass — the ``info`` level here was creating ~6
        ``local_*_*`` event lines on every ``apply`` invocation, which
        operators consistently flagged as noise.
        """
        if self.logger:
            redacted_extra = redact_dict(extra) if extra else None
            self.logger.debug(msg, extra=_safe_extra(redacted_extra))

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
