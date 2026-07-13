# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Meltano (Singer protocol) acquisition runner.

Two execution modes:

  - **Embedded Singer** (default): invokes a Singer tap binary directly via
    ``subprocess`` and consumes its stdout protocol (``SCHEMA``, ``RECORD``,
    ``STATE`` messages). The records are routed to a built-in target that
    writes to local Parquet (or DuckDB), or to a user-supplied target binary.
    No Meltano installation required for this mode.
  - **Meltano project** (when ``properties.meltano.project_dir`` is set): shells
    out to ``meltano elt <tap> <target>`` for users who already operate a
    Meltano project. Honors that project's `meltano.yml`.

Singer state messages are round-tripped through the FLUID ``StateStore`` so
incremental runs resume from the cursor written by the previous run.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Dict, FrozenSet, Iterator, List, Optional

from fluid_build.api.runner import (
    RunContext,
    RunnerCapability,
    RunPlan,
    RunResult,
    RunState,
    StreamResult,
)
from fluid_build.api.schema import SchemaFingerprint
from fluid_build.api.state import Cursor
from fluid_build.providers._sql_safety import quote_string_literal, validate_ident

from .._acquisition_common import (
    extract_source_schemas,
    resolve_connection_secrets,
    utc_now_iso,
    write_run_record_and_finalize,
)

LOG = logging.getLogger("fluid.acquire.meltano")


# ── Singer protocol ─────────────────────────────────────────────────────


def stream_singer_messages(stdout: Iterator[str]) -> Iterator[Dict[str, Any]]:
    """Yield parsed Singer protocol messages from a tap's stdout."""
    for line in stdout:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            LOG.warning("singer.bad_line line=%r", line[:200])


def collect_singer_output(
    raw_messages: Iterator[Dict[str, Any]],
) -> Dict[str, Any]:
    """Drain Singer messages into a structured result.

    Returns ``{"schemas": {stream: schema_msg}, "records": {stream: [rec, ...]},
    "state": last_state_dict}``.
    """
    schemas: Dict[str, Dict[str, Any]] = {}
    records: Dict[str, List[Dict[str, Any]]] = {}
    last_state: Dict[str, Any] = {}

    for msg in raw_messages:
        msg_type = msg.get("type")
        if msg_type == "SCHEMA":
            stream = msg.get("stream", "default")
            schemas[stream] = msg
            records.setdefault(stream, [])
        elif msg_type == "RECORD":
            stream = msg.get("stream", "default")
            records.setdefault(stream, []).append(msg.get("record") or {})
        elif msg_type == "STATE":
            last_state = msg.get("value") or msg
        # ACTIVATE_VERSION and other types pass through silently.
    return {"schemas": schemas, "records": records, "state": last_state}


# ── Tap invocation ──────────────────────────────────────────────────────


_TAP_NAME_RE = re.compile(r"^tap-[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
_TARGET_NAME_RE = re.compile(r"^target-[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")


def _resolve_tap_binary(tap_name: str, *, project_dir: Optional[Path] = None) -> Optional[str]:
    """Locate a Singer tap binary on PATH, in the active Python's venv, or
    in the Meltano project venv.

    Search order (each step short-circuits on first hit):

    1. ``shutil.which(name)`` — operator pinned the binary on PATH
       (preferred for production / containerised use).
    2. ``Path(sys.executable).parent / name`` — sibling of the running
       Python interpreter. Catches the common dev-time pattern of
       ``pip install meltanolabs-tap-postgres`` into the same venv that
       hosts forge-cli, where ``shutil.which`` won't find the binary
       because the venv isn't activated when ``fluid`` is invoked
       directly via its bin path.
    3. ``project_dir/.meltano/extractors/<name>/venv/bin/<name>`` —
       Meltano-style per-extractor isolated venv (legacy / production
       Meltano-managed install).

    The tap name is validated against ``_TAP_NAME_RE`` (lowercase + alnum +
    ``._-`` only, must start with ``tap-`` and start/end alnum) so a
    malicious value like ``tap-../../etc/passwd`` is rejected before it
    can be used to construct a venv path. The resolved venv path is also
    confined to ``project_dir/.meltano/extractors/`` via ``Path.resolve()``
    + a ``relative_to()`` prefix check, so even an unforeseen regex escape
    can't leave the extractors tree.
    """
    candidate = tap_name if tap_name.startswith("tap-") else f"tap-{tap_name}"
    if not _TAP_NAME_RE.match(candidate):
        LOG.warning("singer.invalid_tap_name name=%r", tap_name)
        return None
    on_path = shutil.which(candidate)
    if on_path:
        return on_path
    # Sibling-of-Python lookup: catches the common dev-time install pattern
    # of putting Singer taps into the same venv as forge-cli, where the
    # venv isn't on PATH because ``fluid`` is invoked via its absolute bin
    # path (no activation).
    #
    # Use ``sys.prefix`` not ``Path(sys.executable).resolve().parent`` —
    # ``.resolve()`` follows the venv's python symlink back to the system
    # interpreter (e.g. /opt/homebrew/.../python), escaping the venv. Each
    # venv sets ``sys.prefix`` to its own root directory, regardless of
    # interpreter symlinks.
    venv_bin = Path(sys.prefix) / "bin"
    sibling_candidate = venv_bin / candidate
    if sibling_candidate.exists():
        return str(sibling_candidate)
    if project_dir is not None:
        extractors_root = (project_dir / ".meltano" / "extractors").resolve()
        candidate_path = (extractors_root / candidate / "venv" / "bin" / candidate).resolve()
        try:
            candidate_path.relative_to(extractors_root)
        except ValueError:
            LOG.warning(
                "singer.tap_path_escape candidate=%s root=%s",
                candidate_path,
                extractors_root,
            )
            return None
        if candidate_path.exists():
            return str(candidate_path)
    return None


def invoke_tap(
    binary: str,
    *,
    config: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
    catalog: Optional[Dict[str, Any]] = None,
    workdir: Path,
    timeout_seconds: int = 300,
) -> Dict[str, Any]:
    """Invoke a Singer tap as a subprocess and return its parsed output.

    Writes config / state / catalog to JSON files, then runs::

        <binary> --config <conf> [--state <state>] [--catalog <catalog>]

    Captures stdout (Singer messages), stderr (logs), and the exit code.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    # Resolve to absolute paths so the subprocess can find them
    # regardless of its cwd. Using relative paths here would have the
    # tap look for ``<cwd>/<workdir>/tap_config.json`` after subprocess
    # cd's into ``workdir`` — that's a double-prefix bug we hit.
    config_path = (workdir / "tap_config.json").resolve()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    cmd = [binary, "--config", str(config_path)]
    if state is not None:
        state_path = (workdir / "tap_state.json").resolve()
        state_path.write_text(json.dumps(state), encoding="utf-8")
        cmd += ["--state", str(state_path)]
    if catalog is not None:
        catalog_path = (workdir / "tap_catalog.json").resolve()
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        cmd += ["--catalog", str(catalog_path)]

    LOG.info("singer.invoke binary=%s cwd=%s", binary, workdir)
    proc = subprocess.run(
        cmd,
        cwd=str(workdir.resolve()),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return collect_singer_output(stream_singer_messages(iter(proc.stdout.splitlines()))) | {
        "exit_code": proc.returncode,
        "stderr": proc.stderr,
    }


def discover_tap_catalog(
    binary: str,
    *,
    config: Dict[str, Any],
    workdir: Path,
    timeout_seconds: int = 120,
) -> Optional[Dict[str, Any]]:
    """Run ``<binary> --config <c> --discover`` and parse the catalog.

    Singer SDK taps (and most legacy Singer taps) require a discover
    pass before sync: the user picks streams from the discovered catalog
    by setting ``metadata[].metadata.selected = true``. Returns the raw
    catalog dict, or ``None`` when the tap doesn't support ``--discover``
    (older "everything in --config" taps — caller falls back to a plain
    ``invoke_tap`` call).
    """
    workdir.mkdir(parents=True, exist_ok=True)
    # See note in invoke_tap — paths must be absolute so the tap finds
    # them after subprocess.run cd's into workdir.
    config_path = (workdir / "tap_config.json").resolve()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    cmd = [binary, "--config", str(config_path), "--discover"]
    LOG.info("singer.discover binary=%s", binary)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workdir.resolve()),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        # Tap doesn't support --discover (or failed) — caller decides.
        # We dump the full stderr to a sibling file so the user can
        # inspect it without fighting structured-logging truncation.
        try:
            (workdir / "tap_discover_stderr.log").write_text(proc.stderr or "", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        LOG.warning(
            "singer.discover.exit code=%d (full stderr → %s/tap_discover_stderr.log)",
            proc.returncode,
            workdir,
        )
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        catalog = json.loads(out)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(catalog, dict) and "streams" in catalog:
        return catalog
    return None


def _stream_matches_request(stream_name: str, requested: set) -> bool:
    """Return True if the catalog ``stream_name`` matches any requested name.

    Singer taps name streams differently:

    - **bare table**: ``usage_event`` (some legacy taps)
    - **schema-prefixed (dash)**: ``telco-usage_event`` (tap-postgres,
      tap-mysql — they join schema + table with ``-``)
    - **schema-prefixed (dot)**: ``telco.usage_event`` (some custom taps)
    - **database-prefixed**: ``mydb-telco-usage_event``

    We accept any of these so the contract author can write the natural
    table name and not have to know which separator the tap chose. Order:

    1. exact match on ``stream`` or ``tap_stream_id``
    2. dot ↔ dash swaps (``telco.usage_event`` ↔ ``telco-usage_event``)
    3. **suffix match** after splitting on ``-`` or ``.`` — catches
       ``telco-usage_event`` matching request ``usage_event``
    """
    if stream_name in requested:
        return True
    if stream_name.replace(".", "-") in requested:
        return True
    if stream_name.replace("-", ".") in requested:
        return True
    # Suffix match: split on either separator and check if the last
    # segment is requested (e.g. "telco-usage_event" -> "usage_event").
    for sep in ("-", "."):
        if sep in stream_name:
            tail = stream_name.rsplit(sep, 1)[-1]
            if tail in requested:
                return True
    return False


def _select_streams_in_catalog(catalog: Dict[str, Any], streams: List[str]) -> Dict[str, Any]:
    """Mark the requested streams as ``selected: true`` in catalog metadata.

    Singer + Singer-SDK both honour the ``selected`` metadata flag on the
    root metadata entry of each stream. Without it the tap emits SCHEMA
    messages but no RECORDs. Stream matching is via
    :func:`_stream_matches_request` (exact, dot↔dash, and suffix match) so
    a contract requesting ``usage_event`` matches a tap-postgres catalog
    entry called ``telco-usage_event``.
    """
    if not streams:
        return catalog
    wanted = set(streams)
    for s in catalog.get("streams") or []:
        sname = s.get("stream") or s.get("tap_stream_id") or ""
        if not _stream_matches_request(sname, wanted):
            continue
        md_list = s.get("metadata") or []
        seen_root = False
        for md in md_list:
            bp = md.get("breadcrumb") or []
            if not bp:
                seen_root = True
                md.setdefault("metadata", {})["selected"] = True
        if not seen_root:
            md_list.append({"breadcrumb": [], "metadata": {"selected": True}})
        s["metadata"] = md_list
    return catalog


# ── Singer target invocation (out-of-process, stdin Singer pipe) ────────


def _resolve_target_binary(
    target_name: str, *, project_dir: Optional[Path] = None
) -> Optional[str]:
    """Locate a Singer target binary using the same precedence as taps.

    1. ``shutil.which`` (PATH, preferred for production).
    2. ``Path(sys.prefix) / "bin"`` — sibling of the running Python (catches
       ``pip install meltanolabs-target-snowflake`` into the same venv).
    3. ``project_dir/.meltano/loaders/<name>/venv/bin/<name>`` — Meltano
       per-loader venv (mirrors the tap-side ``extractors`` layout).

    Validation parallels ``_resolve_tap_binary``: the name must match
    ``_TARGET_NAME_RE`` so a malicious ``target-../../etc/passwd`` can't
    construct a path-escape.
    """
    candidate = target_name if target_name.startswith("target-") else f"target-{target_name}"
    if not _TARGET_NAME_RE.match(candidate):
        LOG.warning("singer.invalid_target_name name=%r", target_name)
        return None
    on_path = shutil.which(candidate)
    if on_path:
        return on_path
    venv_bin = Path(sys.prefix) / "bin"
    sibling_candidate = venv_bin / candidate
    if sibling_candidate.exists():
        return str(sibling_candidate)
    if project_dir is not None:
        loaders_root = (project_dir / ".meltano" / "loaders").resolve()
        candidate_path = (loaders_root / candidate / "venv" / "bin" / candidate).resolve()
        try:
            candidate_path.relative_to(loaders_root)
        except ValueError:
            LOG.warning(
                "singer.target_path_escape candidate=%s root=%s",
                candidate_path,
                loaders_root,
            )
            return None
        if candidate_path.exists():
            return str(candidate_path)
    return None


def invoke_target(
    binary: str,
    *,
    config: Dict[str, Any],
    schemas: Dict[str, Dict[str, Any]],
    records: Dict[str, List[Dict[str, Any]]],
    state: Optional[Dict[str, Any]] = None,
    workdir: Path,
    timeout_seconds: int = 600,
) -> Dict[str, Any]:
    """Pipe Singer messages into ``<binary> --config <conf>`` over stdin.

    Replays the captured ``schemas`` (one SCHEMA per stream) followed by
    all ``records`` for that stream as RECORD messages, then a final STATE
    message. The target reads from stdin, writes to its destination, and
    exits.

    This is the canonical Singer pattern (stdin/stdout JSONL pipe). We
    reuse the in-memory tap result rather than re-piping tap → target
    directly so the FLUID hook chain (PII tokenization, DLQ, quality
    gates) can mutate / drop records before they land in the warehouse.

    Returns ``{"exit_code": int, "stderr": str}``. Caller checks exit
    code and surfaces stderr to the operator.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    config_path = (workdir / "target_config.json").resolve()
    config_path.write_text(json.dumps(config), encoding="utf-8")
    cmd = [binary, "--config", str(config_path)]

    LOG.info("singer.target.invoke binary=%s cwd=%s", binary, workdir)
    # Pre-render the Singer message stream once; Popen.communicate(input=...)
    # handles the write+wait+drain in one call so we don't have to manage
    # the stdin pipe manually (avoids the "I/O operation on closed file"
    # race when the target exits early during config validation).
    lines: List[str] = []
    for stream, schema_msg in schemas.items():
        lines.append(json.dumps(schema_msg))
        for record in records.get(stream, []):
            lines.append(json.dumps({"type": "RECORD", "stream": stream, "record": record}))
    if state is not None:
        lines.append(json.dumps({"type": "STATE", "value": state}))
    singer_input = "\n".join(lines) + ("\n" if lines else "")

    proc = subprocess.Popen(
        cmd,
        cwd=str(workdir.resolve()),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _stdout, stderr = proc.communicate(input=singer_input, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        _stdout, stderr = proc.communicate()
        return {"exit_code": -1, "stderr": f"target timeout after {timeout_seconds}s\n{stderr}"}

    # Persist stderr to disk so an operator can re-read after the run
    # without re-piping. Mirrors the tap-side ``tap_discover_stderr.log``
    # convention so support paths look the same for both halves.
    if stderr:
        (workdir / "target_stderr.log").write_text(stderr, encoding="utf-8")

    return {"exit_code": proc.returncode, "stderr": stderr}


# ── Built-in target: Parquet / DuckDB ───────────────────────────────────


def write_records_to_duckdb(
    records: Dict[str, List[Dict[str, Any]]],
    *,
    duckdb_path: Path,
    dataset: str = "bronze",
) -> Dict[str, int]:
    """Write per-stream records to a DuckDB file under the given dataset.

    Returns a per-stream record-count dict. Each stream becomes a table
    named ``<dataset>.<stream>`` (DuckDB schemas).

    All identifiers (``dataset``, table name derived from stream, column
    names) are validated via ``validate_ident`` so a malicious stream name
    like ``"orders; DROP TABLE secrets; --"`` is rejected at the boundary
    rather than executed.
    """
    import duckdb

    dataset = validate_ident(dataset)
    duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(duckdb_path))
    try:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {dataset}")
        counts: Dict[str, int] = {}
        for stream, rows in records.items():
            # Stream names like ``public.orders`` or ``orders-v2`` get
            # normalized to a safe identifier; the result is then validated
            # so even after normalization any non-conforming value is rejected.
            table_raw = stream.replace(".", "_").replace("-", "_").lower()
            table = validate_ident(table_raw)
            if not rows:
                con.execute(f"CREATE TABLE IF NOT EXISTS {dataset}.{table} (id BIGINT)")
                counts[stream] = 0
                continue
            con.execute(f"DROP TABLE IF EXISTS {dataset}.{table}")
            # Validate every column name as an identifier; double-quoted
            # column literals are emitted verbatim only after validation.
            cols = list(rows[0].keys())
            for c in cols:
                validate_ident(c)
            col_list = ", ".join(f'"{c}"' for c in cols)
            values_sql = ", ".join(
                "(" + ", ".join(_sql_literal(r.get(c)) for c in cols) + ")" for r in rows
            )
            con.execute(
                f"CREATE TABLE {dataset}.{table} AS SELECT * FROM (VALUES {values_sql}) t({col_list})"
            )
            counts[stream] = len(rows)
        return counts
    finally:
        con.close()


def _sql_literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    # Untrusted Singer record values feed a CREATE TABLE AS SELECT VALUES; route
    # the string branch through the central _sql_safety chokepoint.
    return quote_string_literal(str(v))


# ── Runner ───────────────────────────────────────────────────────────────


@dataclass
class MeltanoRunner:
    """Runner Protocol implementation for the Meltano / Singer engine."""

    name: ClassVar[str] = "meltano"
    declared_capabilities: ClassVar[FrozenSet[RunnerCapability]] = frozenset(
        {
            RunnerCapability.FULL_REFRESH,
            RunnerCapability.INCREMENTAL_APPEND,
            RunnerCapability.INCREMENTAL_DEDUP,
            RunnerCapability.SCHEMA_DISCOVERY,
            RunnerCapability.AT_LEAST_ONCE,
        }
    )
    declared_modes: ClassVar[FrozenSet[str]] = frozenset({"embedded", "bring-your-own"})

    def plan(self, ctx: RunContext) -> RunPlan:
        streams = list(ctx.source.streams) or [ctx.source.kind]
        return RunPlan(streams_planned=streams)

    def run(self, ctx: RunContext) -> RunResult:
        return _execute(ctx, self)

    def replay(self, ctx: RunContext, run_id: str) -> RunResult:
        ctx.run_id = run_id
        return _execute(ctx, self)

    def fingerprint(self, ctx: RunContext) -> SchemaFingerprint:
        # Singer taps emit their own SCHEMA messages mid-stream;
        # introspecting at fingerprint() time would require running the tap.
        # Surface a placeholder marked ``is_placeholder=True`` so the schema-
        # evolution gate skips comparison — actual drift is surfaced at run-
        # time by the tap's SCHEMA messages and meltano's catalog diff.
        return SchemaFingerprint.placeholder(
            list(ctx.source.streams or [ctx.source.kind]),
            engine="singer",
            captured_at=utc_now_iso(),
        )


def _execute(ctx: RunContext, runner: MeltanoRunner) -> RunResult:
    from .._acquisition_common import begin_acquisition_run

    started_at, t_start = begin_acquisition_run(ctx, runner)

    props = ctx.contract.get("builds", [{}])[0].get("properties", {})
    meltano_props = props.get("meltano", {}) or {}

    tap = meltano_props.get("tap") or f"tap-{ctx.source.kind}"
    project_dir_str = meltano_props.get("project_dir")
    project_dir = Path(project_dir_str).expanduser().resolve() if project_dir_str else None
    binary = _resolve_tap_binary(tap, project_dir=project_dir)
    if binary is None:
        return _failed(ctx, started_at, t_start, f"Singer tap binary not found: {tap}")

    # Build tap config from the source connection block. Resolve secretRef →
    # password (or other credential field) before the dict reaches the Singer
    # tap. Inline literal values still win.
    # Then run the per-source-kind adapter (registered in meltano/sources.py)
    # to coerce FLUID-canonical fields into the shape the specific tap
    # expects — e.g. tap-postgres requires ``port: integer`` but FLUID's
    # ``{{ env.X }}`` template substitution always yields strings.
    from .._acquisition_common import adapt_source_config

    tap_config = resolve_connection_secrets(dict(ctx.source.connection.raw))
    tap_config = adapt_source_config("meltano", ctx.source.kind, tap_config)
    # connection.schema / connection.schemas → Singer convention
    # ``filter_schemas`` (a list). Pop the generic FLUID fields so the tap
    # doesn't see them as unrecognised settings.
    schemas = extract_source_schemas(tap_config)
    tap_config.pop("schema", None)
    tap_config.pop("schemas", None)
    if schemas:
        tap_config.setdefault("filter_schemas", schemas)
    if ctx.source.streams:
        # Many taps accept ``selected_streams``; harmless for ones that don't.
        tap_config["selected_streams"] = list(ctx.source.streams)

    # Restore state for incremental modes.
    state: Optional[Dict[str, Any]] = None
    if ctx.source.mode.value in ("incremental_append", "incremental_dedup", "cdc"):
        cursor = ctx.state_store.get_cursor(ctx.product_id, ctx.build_id, "_singer")
        if cursor is not None:
            state = dict(cursor.value or {})

    workdir = Path(ctx.workdir) / ".fluid" / "meltano" / ctx.product_id / ctx.build_id
    workdir.mkdir(parents=True, exist_ok=True)

    # Singer SDK taps (and most modern Singer taps) require the
    # discover→catalog round trip: --discover emits the catalog, the
    # caller marks streams as ``selected``, then re-invokes with
    # --catalog. Legacy taps that work with --config alone just return
    # ``None`` from ``discover_tap_catalog`` and we fall through to the
    # original code path.
    catalog: Optional[Dict[str, Any]] = None
    try:
        catalog = discover_tap_catalog(binary, config=tap_config, workdir=workdir)
    except Exception as exc:  # noqa: BLE001 — discover is best-effort
        LOG.debug("singer.discover.failed err=%s", exc)
        catalog = None
    if catalog and ctx.source.streams:
        catalog = _select_streams_in_catalog(catalog, list(ctx.source.streams))

    try:
        result = invoke_tap(
            binary, config=tap_config, state=state, catalog=catalog, workdir=workdir
        )
    except subprocess.TimeoutExpired as exc:
        return _failed(ctx, started_at, t_start, f"tap timeout: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _failed(ctx, started_at, t_start, f"tap invocation failed: {exc}")

    if result["exit_code"] != 0:
        return _failed(
            ctx,
            started_at,
            t_start,
            f"tap exited {result['exit_code']}: {result['stderr'][:500]}",
        )

    # Cap at sample_rows when requested.
    if ctx.sample_rows:
        for stream, rows in result["records"].items():
            result["records"][stream] = rows[: ctx.sample_rows]

    # ── Pre-land hook chain + DLQ + alerter ─────────────────────────────
    # Singer is row-by-row, so we have the full record visibility the
    # batch hooks need. Feed each stream through the configured hook
    # chain (PII tokenization + quality gates + lineage), route any
    # rejected records to the DLQ, and fire alerts per the contract's
    # ``delivery.dlq.alertOn`` list.
    try:
        from fluid_build.build_runners._alerter import (
            Alerter,
            channels_from_config,
        )
        from fluid_build.build_runners._dlq import (
            DLQConfig,
            DLQOverflowError,
            DLQWriter,
            process_batch_with_dlq,
        )

        delivery_cfg = (
            ctx.contract.get("builds", [{}])[0].get("properties", {}).get("delivery", {}) or {}
        )
        dlq_cfg = DLQConfig.from_dict(delivery_cfg.get("dlq"))
        alert_obs = (ctx.contract.get("observability") or {}).get("alert") or {}
        alerter = Alerter(channels=channels_from_config(alert_obs)) if alert_obs else None
        dlq_writer = DLQWriter(
            dlq_cfg,
            run_id=ctx.run_id,
            default_root=Path(ctx.workdir) / ".fluid",
        )
        quality_gates = (
            ctx.contract.get("builds", [{}])[0]
            .get("properties", {})
            .get("quality", {})
            .get("gates", [])
        )
        for stream, rows in result["records"].items():
            try:
                cleaned = process_batch_with_dlq(
                    records=rows,
                    hook_chain=ctx.hook_chain,
                    dlq_writer=dlq_writer,
                    alerter=alerter,
                    stream=stream,
                    run_id=ctx.run_id,
                    product_id=ctx.product_id,
                    build_id=ctx.build_id,
                    ctx={
                        "quality_gates": quality_gates,
                        "alert_on": dlq_cfg.alert_on or [],
                    },
                )
                result["records"][stream] = cleaned
            except DLQOverflowError as exc:
                return _failed(ctx, started_at, t_start, f"DLQ overflow on stream {stream}: {exc}")
    except Exception as exc:  # noqa: BLE001 — hook chain is best-effort
        LOG.warning("meltano hook chain failed: %s", exc)

    # ── Destination dispatch ────────────────────────────────────────────
    # Two paths based on the contract's binding:
    #  1. ``platform: snowflake`` (or any registered meltano destination
    #     introspector) → invoke target-<platform> via Singer stdin pipe.
    #  2. anything else → fall back to the built-in DuckDB writer (matches
    #     the historical default; useful for local dev / tests).
    expose = (ctx.contract.get("exposes") or [{}])[0]
    binding = expose.get("binding") or {}
    binding_loc = binding.get("location") or {}
    sink_platform = (binding.get("platform") or "").lower()
    sink_format = (binding.get("format") or "").lower()
    dataset = meltano_props.get("dataset_name") or "bronze"

    use_singer_target = sink_platform in (
        "snowflake",
        "bigquery",
        "redshift",
        "postgres",
    ) or sink_format.startswith(tuple(["snowflake_", "bigquery_", "redshift_", "postgres_"]))

    if use_singer_target:
        # Resolve target binary (operator may pin via properties.meltano.target,
        # otherwise default to ``target-<platform>``).
        target_name = meltano_props.get("target") or f"target-{sink_platform}"
        target_binary = _resolve_target_binary(target_name, project_dir=project_dir)
        if target_binary is None:
            return _failed(
                ctx,
                started_at,
                t_start,
                f"Singer target binary not found: {target_name}",
            )

        # Build the target config via the FLUID-canonical credentials layer.
        # Side-effect import: the meltano destinations module registers the
        # introspector with the unified registry.
        from .._credentials import make_destination
        from . import destinations  # noqa: F401  (registration side-effect)

        target_config = (
            make_destination(
                "meltano",
                sink_platform,
                binding=binding,
                contract=ctx.contract,
                product_id=ctx.product_id,
            )
            or {}
        )
        # Merge any contract-author-specified extra config (rare; lets a
        # contract pin ``add_record_metadata: true`` etc. without forcing
        # an env var).
        for k, v in (meltano_props.get("target_config") or {}).items():
            target_config.setdefault(k, v)

        try:
            tgt_result = invoke_target(
                target_binary,
                config=target_config,
                schemas=result["schemas"],
                records=result["records"],
                state=result["state"],
                workdir=workdir,
            )
        except Exception as exc:  # noqa: BLE001
            return _failed(ctx, started_at, t_start, f"target invocation failed: {exc}")

        if tgt_result["exit_code"] != 0:
            return _failed(
                ctx,
                started_at,
                t_start,
                f"target exited {tgt_result['exit_code']}: {tgt_result['stderr'][:500]}",
            )

        counts = {stream: len(rows) for stream, rows in result["records"].items()}
        destination_label = sink_platform
    else:
        # DuckDB fallback path (historical default).
        duckdb_path_str = binding_loc.get("path") or str(workdir / "out.duckdb")
        duckdb_path = Path(duckdb_path_str)
        if not duckdb_path.is_absolute():
            duckdb_path = Path(ctx.workdir) / duckdb_path
        if duckdb_path.suffix and duckdb_path.suffix != ".duckdb":
            duckdb_path = duckdb_path.with_suffix(".duckdb")
        try:
            counts = write_records_to_duckdb(
                result["records"], duckdb_path=duckdb_path, dataset=dataset
            )
        except Exception as exc:  # noqa: BLE001
            return _failed(ctx, started_at, t_start, f"duckdb write failed: {exc}")
        destination_label = "duckdb"

    # Persist new state.
    if result["state"]:
        ctx.state_store.set_cursor(
            ctx.product_id,
            ctx.build_id,
            Cursor(stream="_singer", value=result["state"], updated_at=utc_now_iso()),
        )

    stream_results: List[StreamResult] = []
    for stream, n in counts.items():
        stream_results.append(
            StreamResult(
                name=stream,
                state=RunState.SUCCEEDED,
                records=n,
                cursor_advanced=bool(result["state"]),
            )
        )
    records_total = sum(counts.values())
    finished_at = utc_now_iso()
    return RunResult(
        run_id=ctx.run_id,
        state=RunState.SUCCEEDED if records_total >= 0 else RunState.FAILED,
        streams=stream_results,
        started_at=started_at,
        finished_at=finished_at,
        records_total=records_total,
        bytes_total=0,
        dlq_records=0,
        facets={
            "engine": "meltano",
            "duration_seconds": time.time() - t_start,
            "tap": tap,
            "dataset_name": dataset,
            "destination": destination_label,
        },
    )


def _failed(ctx: RunContext, started_at: str, t_start: float, err: str) -> RunResult:
    from .._acquisition_common import failed_run_result

    return failed_run_result(ctx, engine="meltano", started_at=started_at, t_start=t_start, err=err)


# ── Top-level entry point used by build_runners.base ────────────────────


def execute_meltano_build(
    build: Dict[str, Any],
    contract: Dict[str, Any],
    contract_dir: Path,
    *,
    dry_run: bool = False,
    sample_rows: Optional[int] = None,
    state_root: Optional[Path] = None,
) -> int:
    from .._acquisition_common import build_acquisition_run_context

    ctx = build_acquisition_run_context(
        build, contract, contract_dir, sample_rows=sample_rows, state_root=state_root
    )
    if ctx is None:
        return 1
    store = ctx.state_store
    runner = MeltanoRunner()
    if dry_run:
        plan = runner.plan(ctx)
        LOG.info("meltano.dry-run streams=%s", plan.streams_planned)
        return 0

    result = runner.run(ctx)
    # Meltano carries per-stream ``cursor_advanced`` (incremental
    # tap-supports-cursor signal); pass the explicit dict so it persists
    # in the run record alongside the canonical fields.
    return write_run_record_and_finalize(
        engine="meltano",
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
                    "cursor_advanced": s.cursor_advanced,
                }
                for s in result.streams
            ],
            "error": result.error,
            "facets": result.facets,
        },
    )
