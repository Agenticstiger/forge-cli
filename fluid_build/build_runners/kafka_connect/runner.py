# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Kafka Connect acquisition runner.

REST-driven: creates / updates / deletes connectors against a Kafka Connect
cluster (bring-your-own or Strimzi-managed). For tests, the
``kafka_connect_mock`` respx fixture stands in for a real cluster.

The runner manages the connector lifecycle: it idempotently posts the
configuration, waits for the connector to reach RUNNING state, and reports
per-task status. Records flow continuously through Kafka — there is no
stream-of-records to consume here; the runner's job is connector
orchestration.
"""

from __future__ import annotations

import logging
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
    extract_source_schemas,
    generate_run_id,
    resolve_connection_secrets,
    utc_now_iso,
    write_run_record_and_finalize,
)
from .._fingerprint import fingerprint_from_columns

LOG = logging.getLogger("fluid.acquire.kafka_connect")


# ── Connector class resolution ─────────────────────────────────────────


SOURCE_CONNECTOR_CLASS: Dict[str, str] = {
    "jdbc": "io.confluent.connect.jdbc.JdbcSourceConnector",
    "postgres": "io.confluent.connect.jdbc.JdbcSourceConnector",
    "mysql": "io.confluent.connect.jdbc.JdbcSourceConnector",
    "sqlserver": "io.confluent.connect.jdbc.JdbcSourceConnector",
    "oracle": "io.confluent.connect.jdbc.JdbcSourceConnector",
    "s3": "io.confluent.connect.s3.S3SourceConnector",
    "salesforce": "io.confluent.salesforce.SalesforceCdcSourceConnector",
    "mongodb": "com.mongodb.kafka.connect.MongoSourceConnector",
}

SINK_CONNECTOR_CLASS: Dict[str, str] = {
    "jdbc": "io.confluent.connect.jdbc.JdbcSinkConnector",
    "s3": "io.confluent.connect.s3.S3SinkConnector",
    "snowflake": "com.snowflake.kafka.connector.SnowflakeSinkConnector",
    "iceberg": "org.apache.iceberg.connect.IcebergSinkConnector",
    "bigquery": "com.wepay.kafka.connect.bigquery.BigQuerySinkConnector",
}


def resolve_source_connector(kind: str, override: Optional[str] = None) -> str:
    if override:
        return override
    cls = SOURCE_CONNECTOR_CLASS.get(kind.lower())
    if not cls:
        raise ValueError(f"kafka-connect: no source connector class for kind '{kind}'")
    return cls


def resolve_sink_connector(format_or_platform: str, override: Optional[str] = None) -> str:
    if override:
        return override
    key = (format_or_platform or "").lower()
    for k, cls in SINK_CONNECTOR_CLASS.items():
        if k in key:
            return cls
    return SINK_CONNECTOR_CLASS["s3"]  # safe default


# ── REST client ────────────────────────────────────────────────────────


class KafkaConnectRestClient:
    """Minimal REST client for the Kafka Connect API."""

    def __init__(self, base_url: str, *, timeout_seconds: int = 30):
        # SSRF guard — Kafka Connect ``server_url`` comes from
        # contract.fluid.yaml, which may be operator-supplied. Route
        # through safe_httpx_client so a poisoned URL pointing at IMDS
        # / RFC1918 cannot exfil connector configs (which carry DB
        # creds). allow_private=True because Kafka Connect endpoints
        # commonly live on private cluster networks.
        from fluid_build.util.safe_http import safe_httpx_client

        self._client = safe_httpx_client(
            base_url=base_url,
            timeout=float(timeout_seconds),
            allow_private=True,
        )

    def close(self) -> None:
        self._client.close()

    def list_connectors(self) -> List[str]:
        r = self._client.get("/connectors")
        r.raise_for_status()
        return r.json()

    def create_connector(self, name: str, config: Dict[str, Any]) -> Dict[str, Any]:
        r = self._client.post("/connectors", json={"name": name, "config": config})
        if r.status_code in (200, 201):
            return r.json()
        r.raise_for_status()
        return r.json()

    def update_config(self, name: str, config: Dict[str, Any]) -> Dict[str, Any]:
        r = self._client.put(f"/connectors/{name}/config", json=config)
        r.raise_for_status()
        return r.json()

    def get_connector(self, name: str) -> Optional[Dict[str, Any]]:
        r = self._client.get(f"/connectors/{name}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def delete_connector(self, name: str) -> bool:
        r = self._client.delete(f"/connectors/{name}")
        return r.status_code in (204, 404)

    def get_status(self, name: str) -> Dict[str, Any]:
        r = self._client.get(f"/connectors/{name}/status")
        r.raise_for_status()
        return r.json()


# ── Runner ─────────────────────────────────────────────────────────────


@dataclass
class KafkaConnectRunner:
    name: ClassVar[str] = "kafka-connect"
    declared_capabilities: ClassVar[FrozenSet[RunnerCapability]] = frozenset(
        {
            RunnerCapability.STREAMING,
            RunnerCapability.AT_LEAST_ONCE,
            RunnerCapability.EXACTLY_ONCE,
            RunnerCapability.CDC,
            RunnerCapability.SCHEMA_DISCOVERY,
        }
    )
    declared_modes: ClassVar[FrozenSet[str]] = frozenset({"bring-your-own", "managed"})

    def plan(self, ctx: RunContext) -> RunPlan:
        return RunPlan(streams_planned=list(ctx.source.streams) or [ctx.source.kind])

    def run(self, ctx: RunContext) -> RunResult:
        return _execute(ctx, self)

    def replay(self, ctx: RunContext, run_id: str) -> RunResult:
        ctx.run_id = run_id
        return _execute(ctx, self)

    def fingerprint(self, ctx: RunContext) -> SchemaFingerprint:
        # Kafka Connect connectors emit schema info via the schema registry
        # (Avro/JSON-Schema) at run time; introspecting at fingerprint() time
        # would require deploying the connector + querying the registry.
        # Surface a placeholder marked ``is_placeholder=True`` so the schema-
        # evolution gate skips comparison — the Connect REST API + schema
        # registry handle real drift after deploy.
        return SchemaFingerprint.placeholder(
            list(ctx.source.streams or [ctx.source.kind]),
            engine="kafka-connect",
            captured_at=utc_now_iso(),
        )


def _execute(ctx: RunContext, runner: KafkaConnectRunner) -> RunResult:
    started_at = utc_now_iso()

    # Schema-evolution gate (shared across all 6 acquisition runners).
    from .._acquisition_common import enforce_schema_policy_or_raise

    enforce_schema_policy_or_raise(ctx, runner)
    t_start = time.time()

    props = ctx.contract.get("builds", [{}])[0].get("properties", {})
    kc_props = props.get("kafka-connect", {}) or {}
    deployment = kc_props.get("deployment", {}) or {}
    server_url = deployment.get("server_url")
    if not server_url:
        return _failed(ctx, started_at, t_start, "kafka-connect requires deployment.server_url")

    try:
        connector_class = resolve_source_connector(
            ctx.source.kind, override=kc_props.get("connector_class")
        )
    except ValueError as exc:
        return _failed(ctx, started_at, t_start, str(exc))

    # Build connector config from source connection. Debezium connectors
    # use a different config shape than the Confluent JDBC source — the
    # former needs ``database.hostname``/``database.port``/etc. plus a
    # ``topic.prefix`` while the latter needs ``connection.url`` and
    # ``mode``. Route by connector class.
    is_debezium = connector_class.startswith("io.debezium.")
    base_config: Dict[str, Any] = {
        "connector.class": connector_class,
        "tasks.max": str(kc_props.get("tasks_max", 1)),
    }
    connector_name_for_topic = (
        kc_props.get("connector_name") or f"forge-{ctx.product_id.replace('.', '-')}"
    )
    if is_debezium:
        base_config.update(
            _debezium_config(
                ctx.source.connection.raw,
                ctx.source.kind,
                topic_prefix=kc_props.get("topic_prefix") or connector_name_for_topic,
                streams=list(ctx.source.streams or []),
            )
        )
    else:
        base_config.update(_jdbc_config(ctx.source.connection.raw, ctx.source.kind))
        # Confluent JDBC source needs a topic prefix or it 400s. Default
        # to the connector name with dashes-as-underscores so the topic
        # is a valid Kafka name.
        base_config.setdefault(
            "topic.prefix",
            (kc_props.get("topic_prefix") or connector_name_for_topic).replace("-", "_") + "_",
        )
        if ctx.source.streams:
            base_config["table.whitelist"] = ",".join(ctx.source.streams)
        # Stream / mode wiring.
        base_config["mode"] = _map_acquisition_mode_to_kc(ctx.source.mode.value)
        if ctx.source.cursor_field:
            base_config["incrementing.column.name"] = ctx.source.cursor_field

    # Avro / Schema-Registry wiring (optional). When the contract declares
    # ``properties.kafka-connect.schema_registry.url`` we emit the AvroConverter
    # properties so produced records are serialized as Avro under the
    # registered subjects. Without it we leave Connect's default JSON converter.
    sr_cfg = kc_props.get("schema_registry") or {}
    sr_url = sr_cfg.get("url")
    if sr_url:
        from .schema_registry import avro_converter_config

        base_config.update(avro_converter_config(sr_url))

    # Late-arrival policy (Phase-3 #15). Read the contract's
    # ``WatermarkSpec.allowed_lateness`` and surface as connector
    # config under ``fluid.late_arrival.*`` keys so a downstream SMT
    # (or sink-side enforcer) can route over-budget events to the
    # canonical ``<target>__late_events`` side-output table.
    from .._late_arrival import extract_late_arrival_policy

    late_arrival_policy = extract_late_arrival_policy(
        contract_or_source=ctx.source,
        target_table=connector_name_for_topic,
    )
    if late_arrival_policy.get("enabled"):
        base_config.update(late_arrival_policy["connector_config"])

    # Optional sink-side connector (companion).
    sink_config = kc_props.get("sink_connector_config")

    connector_name = kc_props.get("connector_name") or f"forge-{ctx.product_id.replace('.', '-')}"
    client = KafkaConnectRestClient(server_url)
    try:
        existing = client.get_connector(connector_name)
        if existing is None:
            client.create_connector(connector_name, base_config)
        else:
            client.update_config(connector_name, base_config)

        sink_name = None
        if sink_config:
            sink_name = kc_props.get("sink_connector_name") or f"{connector_name}-sink"
            existing_sink = client.get_connector(sink_name)
            if existing_sink is None:
                client.create_connector(sink_name, sink_config)
            else:
                client.update_config(sink_name, sink_config)

        # Connect's REST API is eventually consistent: a 201 from
        # /connectors doesn't mean /status is queryable yet. Poll until
        # the connector reports a terminal state or the timeout lapses.
        status_timeout = float(kc_props.get("status_timeout_seconds") or 15.0)
        poll_interval = float(kc_props.get("poll_interval_seconds") or 0.5)
        deadline = time.time() + status_timeout
        connector_state = "UNKNOWN"
        while time.time() < deadline:
            try:
                status = client.get_status(connector_name)
                connector_state = (status.get("connector", {}) or {}).get("state", "UNKNOWN")
                if connector_state in ("RUNNING", "FAILED", "PAUSED"):
                    break
            except Exception as exc:  # noqa: BLE001 — eventual consistency
                LOG.debug("kafka_connect.status_poll.transient err=%s", exc)
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
        finished_at = utc_now_iso()
        return RunResult(
            run_id=ctx.run_id,
            state=RunState.SUCCEEDED if ok else RunState.FAILED,
            streams=stream_results,
            started_at=started_at,
            finished_at=finished_at,
            records_total=0,
            bytes_total=0,
            dlq_records=0,
            facets={
                "engine": "kafka-connect",
                "duration_seconds": time.time() - t_start,
                "connector_name": connector_name,
                "connector_class": connector_class,
                "sink_connector_name": sink_name,
                "connector_state": connector_state,
                "late_arrival_enabled": bool(late_arrival_policy.get("enabled")),
                "late_arrival_budget_seconds": late_arrival_policy.get("allowed_lateness_seconds"),
                "late_arrival_side_output_table": late_arrival_policy.get("side_output_table"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        LOG.error("kafka_connect.failed err=%s", exc, exc_info=True)
        return _failed(ctx, started_at, t_start, str(exc))
    finally:
        client.close()


def _debezium_config(
    connection: Dict[str, Any],
    kind: str,
    *,
    topic_prefix: str,
    streams: List[str],
) -> Dict[str, Any]:
    """Translate a connection into Debezium connector config keys.

    The Debezium connectors expect ``database.hostname``/``database.port``/etc.
    plus a ``topic.prefix`` (was ``database.server.name`` pre-2.0). For
    Postgres we also default ``plugin.name=pgoutput``. Per-source-stream
    filtering uses ``table.include.list`` (Postgres / MySQL / SQL Server).
    """
    # Resolve secretRef → password before any connection.get("password") read.
    connection = resolve_connection_secrets(dict(connection))
    host = connection.get("host", "localhost")
    port = connection.get("port") or {"postgres": 5432, "mysql": 3306}.get(kind, 5432)
    db = connection.get("database", "")
    user = connection.get("user", "")
    password = connection.get("password", "")
    cfg: Dict[str, Any] = {
        "database.hostname": host,
        "database.port": str(port),
        "database.user": user,
        "database.password": password,
        "database.dbname": db,
        # Debezium 2.x uses topic.prefix; pre-2.x used database.server.name.
        # The replacement table-include list keeps the same topic shape.
        "topic.prefix": topic_prefix.replace(".", "_").replace("-", "_"),
    }
    if kind == "postgres":
        cfg.setdefault("plugin.name", "pgoutput")
    if streams:
        cfg["table.include.list"] = ",".join(streams)
    return cfg


def _jdbc_config(connection: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """Translate a connection dict into Kafka Connect JDBC config keys."""
    if kind in ("jdbc", "postgres", "mysql", "sqlserver", "oracle"):
        # Resolve secretRef → password ONLY when we're about to consume it.
        # Non-JDBC kinds (s3, gcs, http, …) fall through to the passthrough
        # branch below and don't need credential resolution; eagerly resolving
        # there would force the secret backend to be available even when the
        # secret isn't used.
        connection = resolve_connection_secrets(dict(connection))
        host = connection.get("host", "localhost")
        port = connection.get("port", "")
        db = connection.get("database", "")
        user = connection.get("user", "")
        password = connection.get("password", "")
        protocol = {
            "postgres": "postgresql",
            "mysql": "mysql",
            "sqlserver": "sqlserver",
            "oracle": "oracle",
        }.get(kind, "postgresql")
        url = f"jdbc:{protocol}://{host}{':' + str(port) if port else ''}/{db}"
        cfg = {
            "connection.url": url,
            "connection.user": user,
            "connection.password": password,
        }
        # connection.schema / connection.schemas → Confluent JDBC source
        # ``schema.pattern`` (regex). Single-schema only; multi-schema needs
        # one connector per schema.
        schemas = extract_source_schemas(connection)
        if schemas:
            cfg["schema.pattern"] = schemas[0]
        return cfg
    # Passthrough for non-JDBC kinds: strip secretRef so a downstream client
    # never sees a stray field. The original strip preserved here intentionally.
    return {k: v for k, v in connection.items() if k != "secretRef"}


def _map_acquisition_mode_to_kc(mode: str) -> str:
    """FLUID acquisition mode → Confluent JDBC source `mode`."""
    return {
        "full_refresh": "bulk",
        "incremental_append": "incrementing",
        "incremental_dedup": "timestamp+incrementing",
        "cdc": "timestamp",
        "streaming": "incrementing",
    }.get(mode, "bulk")


def _failed(ctx: RunContext, started_at: str, t_start: float, err: str) -> RunResult:
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
        facets={"engine": "kafka-connect", "duration_seconds": time.time() - t_start},
    )


# ── Top-level entry point ──────────────────────────────────────────────


def execute_kafka_connect_build(
    build: Dict[str, Any],
    contract: Dict[str, Any],
    contract_dir: Path,
    *,
    dry_run: bool = False,
    sample_rows: Optional[int] = None,
    state_root: Optional[Path] = None,
) -> int:
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
    runner = KafkaConnectRunner()
    if dry_run:
        plan = runner.plan(ctx)
        LOG.info("kafka_connect.dry-run streams=%s", plan.streams_planned)
        return 0

    result = runner.run(ctx)
    return write_run_record_and_finalize(
        engine="kafka-connect", ctx=ctx, result=result, state_store=store
    )
