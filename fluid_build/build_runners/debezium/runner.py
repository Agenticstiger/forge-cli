# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Debezium CDC acquisition runner.

Two execution modes:

  - **Kafka Connect** (``bring-your-own`` / ``managed``): default; uses the
    Debezium connector classes via the Kafka Connect REST API.
  - **Debezium Server** (``embedded``): generates an
    ``application.properties`` file and (when the binary is available)
    starts ``debezium-server`` as a subprocess. For tests we stop short of
    actually running the binary; that path lives behind the
    ``FLUID_TEST_DEBEZIUM_SERVER`` env gate.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
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
    extract_source_schemas,
    resolve_connection_secrets,
    utc_now_iso,
    write_run_record_and_finalize,
)
from ..kafka_connect.runner import KafkaConnectRestClient

LOG = logging.getLogger("fluid.acquire.debezium")


# ── Connector class resolution ─────────────────────────────────────────


SOURCE_CLASS_BY_KIND: Dict[str, str] = {
    "postgres": "io.debezium.connector.postgresql.PostgresConnector",
    "postgres-cdc": "io.debezium.connector.postgresql.PostgresConnector",
    "mysql": "io.debezium.connector.mysql.MySqlConnector",
    "mysql-cdc": "io.debezium.connector.mysql.MySqlConnector",
    "mongodb": "io.debezium.connector.mongodb.MongoDbConnector",
    "mongodb-cdc": "io.debezium.connector.mongodb.MongoDbConnector",
    "sqlserver": "io.debezium.connector.sqlserver.SqlServerConnector",
    "sqlserver-cdc": "io.debezium.connector.sqlserver.SqlServerConnector",
    "oracle": "io.debezium.connector.oracle.OracleConnector",
    "oracle-cdc": "io.debezium.connector.oracle.OracleConnector",
}


def resolve_debezium_class(kind: str, override: Optional[str] = None) -> str:
    if override:
        return override
    cls = SOURCE_CLASS_BY_KIND.get(kind.lower())
    if not cls:
        raise ValueError(f"debezium: no connector class for kind '{kind}'")
    return cls


SUPPORTED_SNAPSHOT_MODES = {"initial", "schema_only", "never", "when_needed", "always"}


def resolve_snapshot_mode(mode: Optional[str]) -> str:
    if not mode:
        return "initial"
    if mode in SUPPORTED_SNAPSHOT_MODES:
        return mode
    raise ValueError(
        f"debezium: invalid snapshot.mode '{mode}'; expected one of {sorted(SUPPORTED_SNAPSHOT_MODES)}"
    )


def resolve_server_binary(server_binary: Optional[str]) -> Optional[str]:
    """Resolve the Debezium Server executable, validating any contract override.

    ``properties.debezium.server_binary`` is a contract-controlled field that
    becomes ``argv[0]`` of a subprocess, so a hostile value would be an
    arbitrary-binary-execution vector. We validate it the same way the
    meltano runner validates its tap/target binaries and ``dbt``'s
    ``_resolve_dbt_executable`` validates ``DBT_EXECUTABLE``:

    * **Bare program name** (no path separator) — must resolve on ``PATH``
      via :func:`shutil.which`. This is the preferred / production shape and
      confines the binary to the operator-controlled ``PATH``.
    * **Explicit path** (contains a path separator, or starts with ``.``/``~``)
      — must point at an existing, executable file (``is_file()`` rejects a
      directory; ``os.X_OK`` rejects a non-executable).

    Anything that resolves to neither is rejected (warn-log + ``None``) so the
    caller fails closed rather than ``exec``-ing an unverifiable program. When
    no override is set we fall back to ``debezium-server`` on ``PATH``.
    """
    if not server_binary:
        return shutil.which("debezium-server")
    candidate = str(server_binary)
    if (
        os.path.sep in candidate
        or (os.path.altsep and os.path.altsep in candidate)
        or candidate.startswith((".", "~"))
    ):
        # Explicit path form — must be an existing, executable file.
        resolved = Path(candidate).expanduser()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return str(resolved)
        LOG.warning(
            "debezium.server_binary.rejected reason=path-not-an-executable-file value=%r",
            candidate,
        )
        return None
    # Bare program name — must resolve on PATH.
    found = shutil.which(candidate)
    if found:
        return found
    LOG.warning(
        "debezium.server_binary.rejected reason=not-on-path value=%r",
        candidate,
    )
    return None


# ── Connector config builder ───────────────────────────────────────────


def build_connector_config(
    kind: str,
    *,
    connection: Dict[str, Any],
    streams: List[str],
    server_name: str,
    snapshot_mode: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a Debezium connector config from the contract source block.

    The ``database.*`` keys are normalized for each connector class. Only the
    most common dialects (Postgres / MySQL / Mongo / SQL Server / Oracle) are
    typed here; other dialects fall through with the connection dict's keys
    passed verbatim under the ``database.*`` namespace.
    """
    connector_class = resolve_debezium_class(kind)
    # Resolve secretRef → password (or other credential field) before any
    # ``connection.get("password")`` lookup. Inline literal values still win.
    connection = resolve_connection_secrets(dict(connection))
    cfg: Dict[str, Any] = {
        "connector.class": connector_class,
        "database.server.name": server_name,
        "topic.prefix": server_name,
        "snapshot.mode": snapshot_mode,
        "tasks.max": "1",
    }
    if "postgres" in kind:
        cfg.update(
            {
                "database.hostname": connection.get("host", "localhost"),
                "database.port": str(connection.get("port", 5432)),
                "database.user": connection.get("user", ""),
                "database.password": connection.get("password", ""),
                "database.dbname": connection.get("database", ""),
                "plugin.name": connection.get("plugin_name", "pgoutput"),
                "slot.name": connection.get("slot_name", "fluid_slot"),
                "publication.name": connection.get("publication_name", "fluid_pub"),
            }
        )
    elif "mysql" in kind:
        cfg.update(
            {
                "database.hostname": connection.get("host", "localhost"),
                "database.port": str(connection.get("port", 3306)),
                "database.user": connection.get("user", ""),
                "database.password": connection.get("password", ""),
                "database.include.list": connection.get("database", ""),
                "database.server.id": str(connection.get("server_id", 184054)),
                "schema.history.internal.kafka.topic": f"{server_name}.history",
                "schema.history.internal.kafka.bootstrap.servers": connection.get(
                    "kafka_bootstrap_servers", "kafka:9092"
                ),
            }
        )
    elif "mongodb" in kind:
        cfg.update(
            {
                "mongodb.connection.string": connection.get(
                    "connection_string",
                    f"mongodb://{connection.get('host', 'localhost')}:{connection.get('port', 27017)}",
                ),
                "mongodb.user": connection.get("user", ""),
                "mongodb.password": connection.get("password", ""),
            }
        )
    elif "sqlserver" in kind:
        cfg.update(
            {
                "database.hostname": connection.get("host", "localhost"),
                "database.port": str(connection.get("port", 1433)),
                "database.user": connection.get("user", ""),
                "database.password": connection.get("password", ""),
                "database.names": connection.get("database", ""),
                "schema.history.internal.kafka.topic": f"{server_name}.history",
                "schema.history.internal.kafka.bootstrap.servers": connection.get(
                    "kafka_bootstrap_servers", "kafka:9092"
                ),
            }
        )
    elif "oracle" in kind:
        cfg.update(
            {
                "database.hostname": connection.get("host", "localhost"),
                "database.port": str(connection.get("port", 1521)),
                "database.user": connection.get("user", ""),
                "database.password": connection.get("password", ""),
                "database.dbname": connection.get("database", ""),
            }
        )

    if streams:
        cfg["table.include.list"] = ",".join(streams)
    # connection.schema / connection.schemas → Debezium ``schema.include.list``
    # (Postgres + SQL Server use schemas; MySQL uses databases natively so we
    # write to ``database.include.list`` instead). Comma-separated list of
    # regex patterns per Debezium docs.
    schemas = extract_source_schemas(connection)
    if schemas:
        if "postgres" in kind or "sqlserver" in kind:
            cfg.setdefault("schema.include.list", ",".join(schemas))
        elif "mysql" in kind:
            cfg.setdefault("database.include.list", ",".join(schemas))
        # mongo / oracle: no equivalent concept — skip silently.
    if extra:
        cfg.update(extra)
    return cfg


# ── Runner ─────────────────────────────────────────────────────────────


@dataclass
class DebeziumRunner:
    name: ClassVar[str] = "debezium"
    declared_capabilities: ClassVar[FrozenSet[RunnerCapability]] = frozenset(
        {
            RunnerCapability.CDC,
            RunnerCapability.STREAMING,
            RunnerCapability.AT_LEAST_ONCE,
            RunnerCapability.SCHEMA_DISCOVERY,
        }
    )
    declared_modes: ClassVar[FrozenSet[str]] = frozenset({"embedded", "bring-your-own", "managed"})

    def plan(self, ctx: RunContext) -> RunPlan:
        return RunPlan(streams_planned=list(ctx.source.streams) or [ctx.source.kind])

    def run(self, ctx: RunContext) -> RunResult:
        return _execute(ctx, self)

    def replay(self, ctx: RunContext, run_id: str) -> RunResult:
        ctx.run_id = run_id
        return _execute(ctx, self)

    def fingerprint(self, ctx: RunContext) -> SchemaFingerprint:
        # Debezium reads the actual source schema inside the connector
        # process; introspecting at fingerprint() time would require booting
        # a connector. Surface a placeholder marked ``is_placeholder=True``
        # so the schema-evolution gate skips comparison; CDC-side drift is
        # surfaced by Debezium's own schema-history topic at run-time.
        return SchemaFingerprint.placeholder(
            list(ctx.source.streams or [ctx.source.kind]),
            engine="debezium",
            captured_at=utc_now_iso(),
        )


def _execute(ctx: RunContext, runner: DebeziumRunner) -> RunResult:
    from .._acquisition_common import begin_acquisition_run

    started_at, t_start = begin_acquisition_run(ctx, runner)

    props = ctx.contract.get("builds", [{}])[0].get("properties", {})
    dbz_props = props.get("debezium", {}) or {}
    deployment = dbz_props.get("deployment", {}) or {}
    mode = deployment.get("mode", "bring-your-own")

    try:
        snapshot_mode = resolve_snapshot_mode(dbz_props.get("snapshot_mode"))
    except ValueError as exc:
        return _failed(ctx, started_at, t_start, str(exc))

    if mode in ("bring-your-own", "managed"):
        return _execute_kafka_connect(
            ctx, deployment, dbz_props, snapshot_mode, started_at, t_start
        )
    if mode == "embedded":
        return _execute_debezium_server(
            ctx, deployment, dbz_props, snapshot_mode, started_at, t_start
        )
    return _failed(ctx, started_at, t_start, f"debezium: unknown deployment.mode '{mode}'")


def _execute_kafka_connect(
    ctx: RunContext,
    deployment: Dict[str, Any],
    dbz_props: Dict[str, Any],
    snapshot_mode: str,
    started_at: str,
    t_start: float,
) -> RunResult:
    server_url = deployment.get("server_url")
    if not server_url:
        return _failed(
            ctx,
            started_at,
            t_start,
            "debezium kafka-connect mode requires deployment.server_url",
        )
    try:
        connector_class = resolve_debezium_class(
            ctx.source.kind, override=dbz_props.get("connector_class")
        )
    except ValueError as exc:
        return _failed(ctx, started_at, t_start, str(exc))

    server_name = dbz_props.get("server_name") or ctx.product_id.replace(".", "_")
    config = build_connector_config(
        ctx.source.kind,
        connection=ctx.source.connection.raw,
        streams=list(ctx.source.streams),
        server_name=server_name,
        snapshot_mode=snapshot_mode,
        extra=dbz_props.get("extra_config") or {},
    )
    config["connector.class"] = connector_class

    connector_name = (
        dbz_props.get("connector_name") or f"forge-debezium-{ctx.product_id.replace('.', '-')}"
    )

    # Late-arrival policy (Phase-3 #15). Same pattern as the kafka-
    # connect runner — surface ``allowed_lateness`` as connector
    # config so a downstream SMT can route over-budget events to the
    # canonical side-output table.
    from .._late_arrival import extract_late_arrival_policy

    late_arrival_policy = extract_late_arrival_policy(
        contract_or_source=ctx.source,
        target_table=connector_name,
    )
    if late_arrival_policy.get("enabled"):
        config.update(late_arrival_policy["connector_config"])
    client = KafkaConnectRestClient(server_url)
    try:
        existing = client.get_connector(connector_name)
        if existing is None:
            client.create_connector(connector_name, config)
        else:
            client.update_config(connector_name, config)

        # Connect is eventually consistent — /status returns 404 for the
        # first 1–2s after a fresh /connectors POST. Poll until terminal.
        status_timeout = float(dbz_props.get("status_timeout_seconds") or 30.0)
        poll_interval = float(dbz_props.get("poll_interval_seconds") or 0.5)
        deadline = time.time() + status_timeout
        connector_state = "UNKNOWN"
        while time.time() < deadline:
            try:
                status = client.get_status(connector_name)
                connector_state = (status.get("connector") or {}).get("state", "UNKNOWN")
                if connector_state in ("RUNNING", "FAILED", "PAUSED"):
                    break
            except Exception as exc:  # noqa: BLE001 — eventual consistency
                LOG.debug("debezium.status_poll.transient err=%s", exc)
            time.sleep(poll_interval)
        ok = connector_state == "RUNNING"

        stream_results = [
            StreamResult(
                name=s,
                state=RunState.SUCCEEDED if ok else RunState.FAILED,
                records=0,
                cursor_advanced=False,
            )
            for s in (ctx.source.streams or [ctx.source.kind])
        ]
        return RunResult(
            run_id=ctx.run_id,
            state=RunState.SUCCEEDED if ok else RunState.FAILED,
            streams=stream_results,
            started_at=started_at,
            finished_at=utc_now_iso(),
            records_total=0,
            bytes_total=0,
            dlq_records=0,
            facets={
                "engine": "debezium",
                "mode": "kafka-connect",
                "duration_seconds": time.time() - t_start,
                "connector_name": connector_name,
                "connector_class": connector_class,
                "snapshot_mode": snapshot_mode,
                "connector_state": connector_state,
                "server_name": server_name,
                "late_arrival_enabled": bool(late_arrival_policy.get("enabled")),
                "late_arrival_budget_seconds": late_arrival_policy.get("allowed_lateness_seconds"),
                "late_arrival_side_output_table": late_arrival_policy.get("side_output_table"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        LOG.error("debezium.kc.failed err=%s", exc, exc_info=True)
        return _failed(ctx, started_at, t_start, str(exc))
    finally:
        client.close()


def _properties_line(key: str, value: object) -> str:
    """Render one ``key=value`` line for a Java ``.properties`` file, failing
    closed on control characters.

    The file is line-based, so a newline (or carriage-return / form-feed / NUL)
    smuggled into a contract-derived key or value would inject arbitrary extra
    Debezium directives. Reject rather than silently write it — the Kafka-Connect
    twin is immune because it serializes to JSON, but this path writes raw lines.
    """
    text_value = str(value)
    for token in (key, text_value):
        if any(ch in token for ch in "\r\n\f\x00"):
            raise ValueError(f"debezium config entry contains a control character: {token!r}")
    return f"{key}={text_value}"


def _execute_debezium_server(
    ctx: RunContext,
    deployment: Dict[str, Any],
    dbz_props: Dict[str, Any],
    snapshot_mode: str,
    started_at: str,
    t_start: float,
) -> RunResult:
    """Generate Debezium Server config and (optionally) start the binary."""
    workdir = Path(ctx.workdir) / ".fluid" / "debezium" / ctx.product_id / ctx.build_id
    workdir.mkdir(parents=True, exist_ok=True)
    config_path = workdir / "application.properties"

    server_name = dbz_props.get("server_name") or ctx.product_id.replace(".", "_")
    try:
        connector_class = resolve_debezium_class(
            ctx.source.kind, override=dbz_props.get("connector_class")
        )
    except ValueError as exc:
        return _failed(ctx, started_at, t_start, str(exc))

    source_config = build_connector_config(
        ctx.source.kind,
        connection=ctx.source.connection.raw,
        streams=list(ctx.source.streams),
        server_name=server_name,
        snapshot_mode=snapshot_mode,
    )
    sink_block = (dbz_props.get("server") or {}).get("sink") or {}
    sink_type = sink_block.get("type", "iceberg")
    handwritten = dict(sink_block.get("config") or {})

    # Iceberg sink: derive the bare-key config from the contract's iceberg expose
    # binding so the embedded server lands in the SAME table the static Glue
    # table / KC sink resolve to (RFC zero-drift spine). Mirrors the KC gate:
    # derivation is OFF whenever a hand-written ``config`` block is present, so
    # those contracts stay byte-for-byte identical (an explicit empty ``config:
    # {}`` counts as present, matching the KC ``sink_connector_config`` default);
    # opt back in with ``server.sink.iceberg_sink_enabled: true``. Derived keys
    # go UNDER the hand-written ones, so an operator key always wins.
    sink_config = handwritten
    iceberg_on = sink_block.get("iceberg_sink_enabled", "config" not in sink_block)
    if sink_type == "iceberg" and iceberg_on:
        from ...providers._iceberg_catalog import (
            find_iceberg_expose_binding,
            resolve_iceberg_catalog,
        )
        from .iceberg_sink import emit_debezium_iceberg_sink_config

        binding = find_iceberg_expose_binding(ctx.contract)
        if binding is not None:
            resolved = resolve_iceberg_catalog(
                binding,
                contract=ctx.contract,
                sink=ctx.sink,
                account_ref=ctx.env.get("AWS_ACCOUNT_ID", ""),
            )
            sink_config = {**emit_debezium_iceberg_sink_config(resolved), **handwritten}

    # Render the line-based .properties file. Every contract-derived key/value
    # routes through _properties_line, which fail-closes on control characters so
    # a newline in a binding value cannot inject extra Debezium directives.
    try:
        lines = [_properties_line(f"debezium.source.{k}", v) for k, v in source_config.items()]
        lines.append("quarkus.log.console.json=false")
        lines.append(_properties_line("debezium.sink.type", sink_type))
        for k, v in sink_config.items():
            lines.append(_properties_line(f"debezium.sink.{sink_type}.{k}", v))
    except ValueError as exc:
        return _failed(ctx, started_at, t_start, str(exc))
    config_path.write_text("\n".join(lines), encoding="utf-8")
    # The file holds the source DB password and any operator-forwarded sink
    # credentials (s3.secret-access-key / jdbc.password); it inherits the umask
    # (0o644) otherwise. Force 0o600, mirroring dbt/profiles.py.
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass

    # Validate any contract-supplied ``server_binary`` before it becomes
    # ``argv[0]`` (arbitrary-binary-execution guard). A value with a path
    # separator must resolve to an existing executable file; a bare name must
    # resolve on PATH. An unverifiable value yields ``None`` → fail closed.
    binary = resolve_server_binary(dbz_props.get("server_binary"))
    if binary is None:
        # Config is generated; without a verified binary we can't run it.
        return _failed(
            ctx,
            started_at,
            t_start,
            "debezium-server binary not found / rejected; install it on PATH, "
            "or set properties.debezium.server_binary to a valid executable "
            "(bare name on PATH, or an absolute path to an executable file)",
        )

    try:
        proc = subprocess.run(
            [binary, "--config", str(config_path)],
            capture_output=True,
            text=True,
            timeout=int(dbz_props.get("timeout_seconds", 60)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        # Debezium Server is long-running; a timeout is the expected exit when
        # we want to verify the binary boots successfully.
        return _success_embedded(
            ctx, started_at, t_start, server_name, snapshot_mode, connector_class
        )

    if proc.returncode != 0:
        return _failed(ctx, started_at, t_start, proc.stderr[:500])
    return _success_embedded(ctx, started_at, t_start, server_name, snapshot_mode, connector_class)


def _success_embedded(
    ctx: RunContext,
    started_at: str,
    t_start: float,
    server_name: str,
    snapshot_mode: str,
    connector_class: str,
) -> RunResult:
    return RunResult(
        run_id=ctx.run_id,
        state=RunState.SUCCEEDED,
        streams=[
            StreamResult(name=s, state=RunState.SUCCEEDED, records=0)
            for s in (ctx.source.streams or [ctx.source.kind])
        ],
        started_at=started_at,
        finished_at=utc_now_iso(),
        records_total=0,
        bytes_total=0,
        dlq_records=0,
        facets={
            "engine": "debezium",
            "mode": "embedded",
            "duration_seconds": time.time() - t_start,
            "connector_class": connector_class,
            "snapshot_mode": snapshot_mode,
            "server_name": server_name,
        },
    )


def _failed(ctx: RunContext, started_at: str, t_start: float, err: str) -> RunResult:
    from .._acquisition_common import failed_run_result

    return failed_run_result(
        ctx, engine="debezium", started_at=started_at, t_start=t_start, err=err
    )


# ── Top-level entry point ──────────────────────────────────────────────


def execute_debezium_build(
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
    runner = DebeziumRunner()
    if dry_run:
        plan = runner.plan(ctx)
        LOG.info("debezium.dry-run streams=%s", plan.streams_planned)
        return 0

    result = runner.run(ctx)
    return write_run_record_and_finalize(
        engine="debezium", ctx=ctx, result=result, state_store=store
    )
