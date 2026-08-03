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
    RunnerCapability,
    RunPlan,
    RunResult,
    RunState,
    StreamResult,
)
from fluid_build.api.schema import SchemaFingerprint

from .._acquisition_common import (
    utc_now_iso,
    write_run_record_and_finalize,
)
from .._path_safety import confine_to_workspace

LOG = logging.getLogger("fluid.acquire.dlt")


# ── Verified-source dispatch ────────────────────────────────────────────


def _make_filesystem_source(connection: Dict[str, Any], reader: Dict[str, Any]) -> Any:
    """Build a dlt filesystem source from connection + reader spec."""
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

    Container-runtime loopback override fires here so dlt's
    SQLAlchemy URL is built with ``host.docker.internal`` (or whatever
    ``FLUID_RUNNER_HOST_OVERRIDE`` is set to) when the contract author
    wrote ``host: localhost`` and FLUID is running inside a container
    whose localhost differs from the operator's. Mutates the input
    dict in place — same pattern the airbyte / meltano source adapters
    use. Defensive: it's a no-op when the env var isn't set or the
    host isn't a loopback address.
    """

    try:
        from dlt.sources.sql_database import sql_database
        from sqlalchemy.engine.url import URL
    except Exception as exc:  # noqa: BLE001 — dlt + sqlalchemy import paths
        from fluid_build._errors import MissingExtraError

        raise MissingExtraError.for_extra(
            extra="dlt[sql_database]",
            install_hint=(
                "pip install 'dlt[sql_database]' (and the matching dialect, "
                "e.g. psycopg or pymysql, if not already installed)"
            ),
        ) from exc

    # NOTE: do NOT apply ``apply_loopback_host_override`` here. dlt runs
    # in-process (the dlt SQL source talks to Postgres directly from
    # fluid's Python process via SQLAlchemy + psycopg), so the operator's
    # shell-level ``localhost`` is correct. The Airbyte runner DOES need
    # the override because PyAirbyte runs each source as a Docker
    # container with its own loopback. Calling the override here would
    # mistranslate ``host: localhost`` into ``host.docker.internal`` and
    # break host-side dlt runs on macOS where that name is unresolvable.

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

    # connection.schema / connection.schemas → dlt sql_database(schema=...).
    # dlt's sql_database accepts a single schema (Postgres/MySQL/Oracle
    # convention). When the contract declares multiple schemas we use the
    # first and warn — multi-schema reads need a per-schema source instance.
    from .._acquisition_common import extract_source_schemas

    schemas = extract_source_schemas(connection)
    sql_db_kwargs: Dict[str, Any] = {"credentials": rendered_dsn}
    if schemas:
        sql_db_kwargs["schema"] = schemas[0]
        if len(schemas) > 1:
            LOG.warning(
                "dlt.sql_database accepts a single schema; using %r and ignoring "
                "the rest: %r. Author one acquisition build per schema if you "
                "need multi-schema ingestion.",
                schemas[0],
                schemas[1:],
            )
    src = sql_database(**sql_db_kwargs)
    if streams:
        return src.with_resources(*[s.split(".")[-1] for s in streams])
    return src


def _make_custom_source(module_path: str, contract_dir: Path) -> Any:
    """Load a user Python module and return its dlt source/resource.

    Searches the module for the first attribute that is a dlt source factory
    (``@dlt.source``-decorated function), or a top-level callable named ``source``.

    ``module_path`` is operator-controlled contract input
    (``builds[].properties.dlt.source_module``). Before the module is
    ``exec_module``'d — which runs arbitrary code at import time — the
    resolved path is confined to the contract's workspace via
    :func:`confine_to_workspace`. A path that escapes the workspace
    (``../../../../tmp/evil.py``) or an absolute path raises ``ValueError``
    and is **never** executed. This mirrors the python / dbt runners, which
    treat an out-of-workspace path as fail-closed.
    """
    # Reject absolute paths outright — they can name a file anywhere on the
    # host regardless of the workspace boundary. ``Path("/etc/x").is_absolute()``
    # catches POSIX; ``ntpath`` semantics (drive letters / UNC) are caught by
    # the same check on Windows.
    if Path(module_path).is_absolute():
        raise ValueError(
            f"dlt custom source module path must be relative to the contract "
            f"directory; refusing absolute path: {module_path!r}"
        )

    abs_path = (contract_dir / module_path).resolve()

    # Fail CLOSED: confine the resolved module path to the contract's
    # workspace before any import/exec. ``confine_to_workspace`` returns
    # ``None`` when the path escapes the workspace (after symlink
    # resolution); we refuse to load it.
    confined = confine_to_workspace(
        abs_path, contract_dir, build_id="dlt", kind="source_module", logger=LOG
    )
    if confined is None:
        raise ValueError(
            f"dlt custom source module escapes the contract workspace and "
            f"will not be loaded: {module_path!r}"
        )

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
        # dlt resolves the actual source schema inside ``pipeline.run`` —
        # introspecting it here would require constructing the source AND
        # making a metadata round-trip to the upstream system. That's too
        # expensive for fingerprint() (which gets called from the schema-
        # evolution gate before any side-effecting work). Instead we surface
        # a placeholder marked ``is_placeholder=True`` so the gate skips the
        # contract-vs-current comparison; drift gets surfaced at run-time by
        # dlt's own schema-discovery hooks once the connector is live.
        return SchemaFingerprint.placeholder(
            list(ctx.source.streams or [ctx.source.kind]),
            engine="dlt",
            captured_at=utc_now_iso(),
        )


def _execute(ctx: RunContext, runner: DltRunner) -> RunResult:
    import dlt

    from .._acquisition_common import begin_acquisition_run, resolve_connection_secrets
    from .._credentials import make_destination

    started_at, t_start = begin_acquisition_run(ctx, runner)

    # Bridge FLUID's canonical destination env vars (SNOWFLAKE_*, BIGQUERY_*,
    # …) to dlt's DESTINATION__<NAME>__CREDENTIALS__* convention via the
    # introspector in dlt/destinations.py — that walks dlt's OWN spec to
    # discover the field names rather than hardcoding per-destination
    # factories. binding.platform is the canonical signal of where data
    # is being written.
    expose = (ctx.contract.get("exposes") or [{}])[0]
    binding = expose.get("binding") or {}
    dest_platform = binding.get("platform")
    if dest_platform:
        make_destination(
            "dlt",
            dest_platform,
            binding=binding,
            contract=ctx.contract,
            product_id=ctx.product_id,
        )

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
            # Resolve secretRef → password (or other credential field) before
            # the connection dict reaches dlt's source factories. Inline literal
            # values in the connection always win over secretRef.
            connection = resolve_connection_secrets(dict(ctx.source.connection.raw))
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

    # Resolve {{ env.X }} placeholders in dataset_name / pipeline_name before
    # passing to dlt.pipeline().  An unresolved placeholder (env var absent)
    # must be caught here: dlt's identifier normaliser silently mangles it into
    # a bogus schema name (e.g. ``env_snowflake_stage_schema__``) and writes
    # data there, causing silent data misdirection.  We use
    # ``resolve_env_templates`` (leaves the placeholder intact on miss) rather
    # than the base runner's ``_resolve_env_placeholders`` (replaces with "")
    # so the error message clearly identifies the missing variable name.
    from fluid_build.providers.snowflake.util.config import (
        ENV_TEMPLATE_RE,
        resolve_env_templates,
    )

    _raw_dataset = dlt_props.get("dataset_name") or "fluid_acquire"
    dataset_name = resolve_env_templates(_raw_dataset)
    if ENV_TEMPLATE_RE.search(dataset_name):
        # Still contains a {{ env.X }} token → env var was absent.
        unresolved = ENV_TEMPLATE_RE.findall(dataset_name)
        raise ValueError(
            f"dlt runner: dataset_name contains unresolved env-template placeholders "
            f"({', '.join(unresolved)}). Set the missing environment variable(s) before "
            f"running the pipeline."
        )

    _raw_pipeline = dlt_props.get("pipeline_name")
    pipeline_name_raw = _raw_pipeline or f"fluid_{ctx.product_id.replace('.', '_')}"
    pipeline_name = resolve_env_templates(pipeline_name_raw)
    if ENV_TEMPLATE_RE.search(pipeline_name):
        unresolved = ENV_TEMPLATE_RE.findall(pipeline_name)
        raise ValueError(
            f"dlt runner: pipeline_name contains unresolved env-template placeholders "
            f"({', '.join(unresolved)}). Set the missing environment variable(s) before "
            f"running the pipeline."
        )

    # Pipeline working dir under .fluid/dlt/<product>/<build>/.
    dlt_root = contract_dir / ".fluid" / "dlt" / ctx.product_id / ctx.build_id
    dlt_root.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("DLT_DATA_DIR", str(dlt_root))

    # Defensive env-namespace cleanup: dlt's Snowflake destination has a
    # ``stage_name`` config field which dlt's configspec resolves from
    # env in this priority: explicit DESTINATION__SNOWFLAKE__STAGE_NAME
    # → namespaced fallbacks → bare ``STAGE_NAME``. Jenkins Pipeline DSL
    # automatically exports ``STAGE_NAME=<the stage groovy name>`` inside
    # any ``stage('foo') { ... }`` block — so a generated Jenkinsfile
    # stage like ``stage('7 - apply')`` ends up overriding dlt's stage
    # name to ``7 - apply``, which produces invalid Snowflake SQL like
    # ``COPY INTO ... FROM @7 - apply/...``. Strip it before invoking
    # dlt so the destination falls back to its default per-table stage.
    # Same threat: GitHub Actions exports ``GITHUB_JOB`` / ``GITHUB_ACTION``
    # which don't currently collide but the principle holds.
    for _shadowed in ("STAGE_NAME",):
        os.environ.pop(_shadowed, None)

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
    from .._acquisition_common import failed_run_result

    return failed_run_result(ctx, engine="dlt", started_at=started_at, t_start=t_start, err=err)


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
    from .._acquisition_common import build_acquisition_run_context

    ctx = build_acquisition_run_context(
        build, contract, contract_dir, sample_rows=sample_rows, state_root=state_root
    )
    if ctx is None:
        return 1
    store = ctx.state_store
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
