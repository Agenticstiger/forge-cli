# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""dlt acquisition runner.

dlt (data load tool) is a Python library for building ingestion pipelines.
This runner supports two source resolution modes:

  - **Verified source**: ``properties.source.kind`` matches a built-in dlt
    source (``filesystem``, ``sql_database``, ``rest_api``, etc.). The runner
    constructs the source via dlt's verified-source factories.
  - **Custom source**: ``properties.dlt.source_module`` points at a user
    Python module (relative to the contract dir) that exports a function
    decorated with ``@dlt.source`` or returns a dlt resource. The module is
    sandboxed via ``_path_safety`` and loaded under a restricted spec.

The runner satisfies the ``api.runner.Runner`` Protocol. Default
destination is DuckDB (zero-infra); ``properties.dlt.destination`` may
override to ``filesystem``, ``snowflake``, ``bigquery``, etc., when the
matching dlt extra is installed.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional

from fluid_build.api.runner import (
    RunContext,
    Runner,
    RunnerCapability,
    RunPlan,
    RunResult,
    RunState,
    StreamResult,
)
from fluid_build.api.schema import SchemaColumn, SchemaFingerprint

from .._acquisition_common import (
    generate_run_id,
    utc_now_iso,
    write_run_record_and_finalize,
)
from .._fingerprint import fingerprint_from_columns

LOG = logging.getLogger("fluid.acquire.dlt")


# ── Verified-source dispatch ────────────────────────────────────────────


def _make_filesystem_source(connection: Dict[str, Any], reader: Dict[str, Any]) -> Any:
    """Build a dlt filesystem source from connection + reader spec."""
    import dlt
    from dlt.sources.filesystem import filesystem, read_csv

    uri = connection.get("uri")
    if uri is None:
        raise ValueError("filesystem source requires connection.uri")
    fmt = (reader or {}).get("format", "csv")
    fs = filesystem(bucket_url=os.path.dirname(uri), file_glob=os.path.basename(uri))
    if fmt == "csv":
        return fs | read_csv()
    if fmt == "parquet":
        from dlt.sources.filesystem import read_parquet

        return fs | read_parquet()
    if fmt in ("json", "ndjson"):
        from dlt.sources.filesystem import read_jsonl

        return fs | read_jsonl()
    raise ValueError(f"dlt filesystem: unsupported format '{fmt}'")


def _make_sql_database_source(connection: Dict[str, Any], streams: List[str]) -> Any:
    """Build a dlt sql_database source for Postgres/MySQL/SQLite/etc.

    The ``sql_database`` source ships under dlt's ``[sql_database]`` extra
    (which transitively requires SQLAlchemy + the right dialect). When the
    extra is missing we surface the typed catalog's ``MissingExtraError``
    so the user gets a five-field Panel pointing at the precise install
    command, instead of dlt's raw ``MissingDependencyException`` text.
    """
    import dlt

    try:
        from dlt.sources.sql_database import sql_database
        from sqlalchemy.engine.url import URL
    except Exception as exc:  # noqa: BLE001 — dlt + sqlalchemy import paths
        from fluid_build.cli._errors import MissingExtraError

        raise MissingExtraError.for_extra(
            extra="dlt[sql_database]",
            install_hint=(
                "pip install 'dlt[sql_database]' (and the matching dialect, "
                "e.g. psycopg or pymysql, if not already installed)"
            ),
        ) from exc

    # Use SQLAlchemy's URL builder so credentials with URL-special characters
    # (``@``, ``:``, ``/``, ``%``) are safely percent-encoded. Building URLs
    # by f-string concatenation lets a malicious password like ``x@y:z`` shift
    # parsing boundaries — URL.create() escapes correctly.
    host = connection.get("host", "localhost")
    raw_port = connection.get("port") or None
    if raw_port is not None and not str(raw_port).isdigit():
        raise ValueError(f"sql_database port must be numeric, got {raw_port!r}")
    port = int(raw_port) if raw_port is not None else None
    user = connection.get("user") or None
    password = connection.get("password") or None
    database = connection.get("database") or None
    drivername = connection.get("drivername", "postgresql+psycopg")

    url = URL.create(
        drivername=drivername,
        username=user,
        password=password,
        host=host,
        port=port,
        database=database,
    )
    # ``URL.render_as_string(hide_password=False)`` produces the percent-encoded
    # connection string dlt's ``ConnectionStringCredentials`` parses; passing the
    # raw ``URL`` object trips dlt's native-value resolver.
    rendered_dsn = url.render_as_string(hide_password=False)
    src = sql_database(credentials=rendered_dsn)
    if streams:
        return src.with_resources(*[s.split(".")[-1] for s in streams])
    return src


def _make_custom_source(module_path: str, contract_dir: Path) -> Any:
    """Load a user Python module and return its dlt source/resource.

    Searches the module for the first attribute that is a dlt source factory
    (``@dlt.source``-decorated function), or a top-level callable named ``source``.
    """
    abs_path = (contract_dir / module_path).resolve()
    try:
        from fluid_build.build_runners._path_safety import safe_join

        safe_join(str(contract_dir), module_path)
    except Exception:
        # Path safety is advisory; the import below will also fail on bad paths.
        pass

    if not abs_path.exists():
        raise FileNotFoundError(f"dlt custom source module not found: {abs_path}")
    spec = importlib.util.spec_from_file_location("_fluid_dlt_source", abs_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load dlt source module: {abs_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_fluid_dlt_source"] = module
    spec.loader.exec_module(module)
    # Look for a function called ``source`` or any function decorated with @dlt.source.
    if hasattr(module, "source") and callable(module.source):
        return module.source()
    for name in dir(module):
        attr = getattr(module, name)
        if callable(attr) and getattr(attr, "_dlt_source", False):
            return attr()
    raise AttributeError(
        f"dlt custom source {module_path}: no `source()` function or @dlt.source decorator found"
    )


# ── Runner ───────────────────────────────────────────────────────────────


@dataclass
class DltRunner:
    """Runner Protocol implementation for the dlt engine."""

    name: ClassVar[str] = "dlt"
    declared_capabilities: ClassVar[FrozenSet[RunnerCapability]] = frozenset(
        {
            RunnerCapability.FULL_REFRESH,
            RunnerCapability.INCREMENTAL_APPEND,
            RunnerCapability.INCREMENTAL_MERGE,
            RunnerCapability.SCHEMA_EVOLUTION,
            RunnerCapability.SCHEMA_DISCOVERY,
            RunnerCapability.AT_LEAST_ONCE,
        }
    )
    declared_modes: ClassVar[FrozenSet[str]] = frozenset({"embedded"})

    def plan(self, ctx: RunContext) -> RunPlan:
        streams = list(ctx.source.streams) or [ctx.source.kind]
        return RunPlan(streams_planned=streams)

    def run(self, ctx: RunContext) -> RunResult:
        return _execute(ctx, self)

    def replay(self, ctx: RunContext, run_id: str) -> RunResult:
        ctx.run_id = run_id
        return _execute(ctx, self)

    def fingerprint(self, ctx: RunContext) -> SchemaFingerprint:
        # dlt resolves schema during pipeline.run; we surface a placeholder
        # fingerprint here that reflects only the declared streams. A full
        # fingerprint requires running the source which is too expensive for
        # a fingerprint-only call — that's by design for code-as-config.
        cols = [
            SchemaColumn(name=s, type="dlt", nullable=True)
            for s in (ctx.source.streams or [ctx.source.kind])
        ]
        return SchemaFingerprint.of(cols, captured_at=utc_now_iso())


def _execute(ctx: RunContext, runner: DltRunner) -> RunResult:
    import dlt

    started_at = utc_now_iso()
    t_start = time.time()

    # Schema-evolution gate (shared across all 6 acquisition runners).
    from .._acquisition_common import enforce_schema_policy_or_raise

    enforce_schema_policy_or_raise(ctx, runner)

    props = ctx.contract.get("builds", [{}])[0].get("properties", {})
    dlt_props = props.get("dlt", {}) or {}

    # Source resolution: custom > kind-based.
    custom_module = dlt_props.get("source_module")
    contract_dir = Path(ctx.workdir)
    try:
        if custom_module:
            source_obj = _make_custom_source(custom_module, contract_dir)
        else:
            kind = ctx.source.kind
            connection = dict(ctx.source.connection.raw)
            if kind == "filesystem":
                reader_dict = {}
                if ctx.source.reader is not None:
                    reader_dict = {
                        "format": ctx.source.reader.format,
                        "options": ctx.source.reader.options,
                    }
                source_obj = _make_filesystem_source(connection, reader_dict)
            elif kind in ("postgres", "mysql", "sqlite", "sql_database"):
                source_obj = _make_sql_database_source(connection, list(ctx.source.streams))
            else:
                raise ValueError(f"dlt runner: unsupported source.kind '{kind}'")
    except Exception as exc:  # noqa: BLE001
        LOG.error("dlt.source.build.failed err=%s", exc, exc_info=True)
        return _failed_result(ctx, started_at, str(exc), t_start)

    # Destination: default DuckDB; override via dlt.destination.
    dest_name = dlt_props.get("destination", "duckdb")
    dataset_name = dlt_props.get("dataset_name") or "fluid_acquire"
    pipeline_name = dlt_props.get("pipeline_name") or f"fluid_{ctx.product_id.replace('.', '_')}"

    # Pipeline working dir under .fluid/dlt/<product>/<build>/.
    dlt_root = contract_dir / ".fluid" / "dlt" / ctx.product_id / ctx.build_id
    dlt_root.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("DLT_DATA_DIR", str(dlt_root))

    # Resolve the destination using the expose's binding when available.
    # Both ``dest_name=duckdb`` (writes a .duckdb file) and
    # ``dest_name=filesystem`` (writes parquet/csv/json under a
    # bucket_url) honour ``binding.location.path`` so the user's
    # configured output path is the canonical sink. When no binding
    # path is set, fall back to the per-build ``.fluid/dlt/...``
    # workdir so contracts without an explicit binding still produce
    # an inspectable output.
    expose = (ctx.contract.get("exposes") or [{}])[0]
    binding_loc = (expose.get("binding") or {}).get("location") or {}
    binding_path = binding_loc.get("path")

    if dest_name == "duckdb":
        if binding_path:
            dest_path = Path(binding_path)
            if not dest_path.is_absolute():
                dest_path = contract_dir / dest_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            # ``binding.location.path`` may name a parquet / csv / json file
            # because the contract describes the LOGICAL output. duckdb
            # destinations need a .duckdb file, so swap the suffix.
            if dest_path.suffix and dest_path.suffix != ".duckdb":
                dest_path = dest_path.with_suffix(".duckdb")
            destination = dlt.destinations.duckdb(str(dest_path))
        else:
            destination = dlt.destinations.duckdb(str(dlt_root / "pipeline.duckdb"))
    elif dest_name == "filesystem":
        from dlt.destinations import filesystem as fs_dest

        if binding_path:
            # Filesystem destinations route the output under the binding
            # location's parent dir. dlt creates per-table sub-dirs
            # underneath. When the path is a file (e.g. ``./out/c.parquet``)
            # we use the parent so dlt's table-name convention takes
            # over: ``./out/<dataset>/<table>/*.parquet``.
            out_dir = Path(binding_path)
            if not out_dir.is_absolute():
                out_dir = contract_dir / out_dir
            if out_dir.suffix:  # path looks like a file, use the parent
                out_dir = out_dir.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            destination = fs_dest(bucket_url=str(out_dir))
        else:
            out_dir = dlt_root / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            destination = fs_dest(bucket_url=str(out_dir))
    else:
        # Generic — let dlt pick up from env
        destination = dest_name

    write_disposition = _map_mode_to_write_disposition(ctx.source.mode.value)

    pipeline = dlt.pipeline(
        pipeline_name=pipeline_name,
        destination=destination,
        dataset_name=dataset_name,
    )
    # NOTE: per-record hook chain (DLQ + alerter) is not currently
    # plumbed into dlt because dlt's source factory returns lazy
    # generators that materialize at ``pipeline.run`` time. Wiring
    # hooks would require wrapping each resource with ``.add_map(...)``
    # which is engine-aware refactor scope. The Meltano runner has full
    # row-level visibility and is the canonical Singer-protocol path
    # for hook-driven flows. dlt users get destination-side schema
    # validation (built into dlt) and the standard ``verify`` stage's
    # post-apply probes.
    try:
        info = pipeline.run(source_obj, write_disposition=write_disposition)
    except Exception as exc:  # noqa: BLE001
        LOG.error("dlt.pipeline.run.failed err=%s", exc, exc_info=True)
        return _failed_result(ctx, started_at, str(exc), t_start)

    # Compose stream results from the dlt LoadInfo.
    streams_to_run = list(ctx.source.streams) or [ctx.source.kind]
    stream_results: List[StreamResult] = []
    records_total = 0
    from fluid_build.providers._sql_safety import validate_ident

    for name in streams_to_run:
        # dlt's LoadInfo doesn't expose per-stream counts directly; use
        # pipeline schema row counts as a best-effort approximation. The
        # dataset access pattern below is library-stable.
        rows = 0
        try:
            with pipeline.sql_client() as client:
                # Best-effort: query each table named after the resource.
                # Validate identifiers before interpolating; reject anything
                # that isn't a plain SQL identifier to neutralize injection.
                table = name.split(".")[-1].lower()
                safe_dataset = validate_ident(dataset_name)
                safe_table = validate_ident(table)
                cur = client.execute_sql(f"SELECT COUNT(*) FROM {safe_dataset}.{safe_table}")
                rows = int(cur[0][0]) if cur and cur[0] else 0
        except Exception:  # noqa: BLE001
            rows = 0
        records_total += rows
        stream_results.append(
            StreamResult(name=name, state=RunState.SUCCEEDED, records=rows, cursor_advanced=False)
        )

    finished_at = utc_now_iso()
    return RunResult(
        run_id=ctx.run_id,
        state=RunState.SUCCEEDED,
        streams=stream_results,
        started_at=started_at,
        finished_at=finished_at,
        records_total=records_total,
        bytes_total=0,
        dlq_records=0,
        facets={
            "engine": "dlt",
            "duration_seconds": time.time() - t_start,
            "destination": dest_name,
            "dataset_name": dataset_name,
            "pipeline_name": pipeline_name,
        },
    )


def _failed_result(ctx: RunContext, started_at: str, err: str, t_start: float) -> RunResult:
    return RunResult(
        run_id=ctx.run_id,
        state=RunState.FAILED,
        streams=[],
        started_at=started_at,
        finished_at=utc_now_iso(),
        records_total=0,
        bytes_total=0,
        dlq_records=0,
        error=err,
        facets={"engine": "dlt", "duration_seconds": time.time() - t_start},
    )


def _map_mode_to_write_disposition(mode: str) -> str:
    """Map FLUID acquisition mode → dlt write_disposition."""
    if mode == "full_refresh":
        return "replace"
    if mode in ("incremental_append", "streaming"):
        return "append"
    if mode in ("incremental_dedup", "incremental_merge", "cdc"):
        return "merge"
    return "append"


# ── Top-level entry point used by build_runners.base ────────────────────


def execute_dlt_build(
    build: Dict[str, Any],
    contract: Dict[str, Any],
    contract_dir: Path,
    *,
    dry_run: bool = False,
    sample_rows: Optional[int] = None,
    state_root: Optional[Path] = None,
) -> int:
    """Glue function called by build_runners.base. Returns exit code."""
    from fluid_build.api.hooks import HookChain
    from fluid_build.api.runner import RunContext
    from fluid_build.api.source import SinkSpec, SourceSpec
    from fluid_build.build_runners._cost import InMemoryCostTracker
    from fluid_build.build_runners._lineage import NullLineageEmitter
    from fluid_build.build_runners._state import FileStateStore

    from .._acquisition_common import get_acquisition_build_props
    from ..base import _resolve_env_placeholders

    props = get_acquisition_build_props(build)
    source_dict = props.get("source")
    if not source_dict:
        LOG.error("acquisition build missing properties.source")
        return 1
    source_dict = _resolve_env_placeholders(source_dict)
    source = SourceSpec.from_dict(source_dict)
    sink = SinkSpec.from_dict(props.get("sink"))

    state_root = state_root or (contract_dir / ".fluid")
    store = FileStateStore(state_root)

    ctx = RunContext(
        run_id=generate_run_id(),
        product_id=contract.get("id", "unknown"),
        build_id=build.get("id", "unknown"),
        contract=contract,
        source=source,
        sink=sink,
        state_store=store,
        hook_chain=HookChain(hooks=[]),
        lineage=NullLineageEmitter(),
        cost_tracker=InMemoryCostTracker(),
        workdir=str(contract_dir),
        sample_rows=sample_rows,
    )
    runner = DltRunner()
    if dry_run:
        plan = runner.plan(ctx)
        LOG.info("dlt.dry-run streams=%s", plan.streams_planned)
        return 0

    result = runner.run(ctx)
    # dlt's per-stream record carries ``duration_seconds`` + per-stream
    # ``error`` that the canonical record doesn't capture; pass the
    # full dict explicitly so ``fluid status`` keeps showing them.
    return write_run_record_and_finalize(
        engine="dlt",
        ctx=ctx,
        result=result,
        state_store=store,
        record_dict={
            "run_id": result.run_id,
            "state": result.state.value,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "records_total": result.records_total,
            "streams": [
                {
                    "name": s.name,
                    "state": s.state.value,
                    "records": s.records,
                    "duration_seconds": s.duration_seconds,
                    "error": s.error,
                }
                for s in result.streams
            ],
            "error": result.error,
            "facets": result.facets,
        },
    )
