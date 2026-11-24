# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Debezium CDC acquisition runner.

Two execution modes:

  - **Kafka Connect** (``bring-your-own`` / ``managed``): default; uses the
    Debezium connector classes via the Kafka Connect REST API.
  - **Debezium Server** (``embedded``): generates an
    ``application.properties`` file and (when the binary is available)
    starts ``debezium-server`` as a subprocess from a fresh directory forge
    owns, never from the project, where the file is
    ``config/application.properties`` (and a ``smallrye.config.locations``
    system property names it too). For tests we stop short of actually running
    the binary; the live proof is
    ``tests/integration/test_debezium_server_runner_live.py``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Dict, FrozenSet, List, Mapping, Optional, Tuple

from fluid_build.api.runner import (
    RunContext,
    RunnerCapability,
    RunPlan,
    RunResult,
    RunState,
    StreamResult,
)
from fluid_build.api.schema import SchemaFingerprint
from fluid_build.build_runners._deployment_host_policy import (
    refuse_unless_host_allowed,
)
from fluid_build.providers._sql_safety import reject_url_syntax

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
    # Debezium builds its JDBC URL (or MongoDB's ``mongodb://host:port`` here)
    # from the host, port and database: refused before the secret is read when
    # one would not stay in its part.
    reject_url_syntax(connection, where="the Debezium connector")
    if extra and connection.get("secretRef"):
        # extra_config is merged last, so a host or credential key in it would
        # move the resolved secret past the destination policy, which only saw
        # the connection block. Refused before the secret is read.
        from fluid_build.secrets.policy import refuse_destination_overrides

        refuse_destination_overrides(extra, where="properties.debezium.extra_config")
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

    from ..kafka_connect.iceberg_sink_validation import executing_build

    # The build this run executes, the SAME one the Iceberg preflight checks.
    # Reading ``builds[0]`` here ran a later build with the first build's
    # Debezium settings while the preflight vetted the later build's.
    build = executing_build(ctx.contract, ctx.build_id)
    if build is None:
        return _failed(
            ctx,
            started_at,
            t_start,
            f"debezium: the run context's build {ctx.build_id!r} is not in the "
            "contract's builds, so its properties cannot be read; run a build the "
            "contract declares",
        )
    props = build.get("properties") or {}
    dbz_props = props.get("debezium", {}) or {}
    deployment = dbz_props.get("deployment", {}) or {}
    mode = deployment.get("mode", "bring-your-own")

    from .._acquisition_common import refuse_contract_expressions

    try:
        # Before any secret is read or config written: Debezium Server and a
        # Kafka Connect worker expand ${...} where they run.
        refuse_contract_expressions(ctx.source, dbz_props, engine="debezium")
    except ValueError as exc:
        return _failed(ctx, started_at, t_start, str(exc))

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
            ctx, build, deployment, dbz_props, snapshot_mode, started_at, t_start
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

    # Before the config is built, because building it resolves the contract's
    # secretRef: the client constructor is the chokepoint, but it runs AFTER
    # that, so a refused deployment would otherwise still have fetched the
    # password from Vault/KMS -- audit entries, API calls, plaintext in memory.
    # The constructor keeps its own check as the backstop for any other caller.
    refuse_unless_host_allowed(server_url, engine="debezium")
    try:
        connector_class = resolve_debezium_class(
            ctx.source.kind, override=dbz_props.get("connector_class")
        )
    except ValueError as exc:
        return _failed(ctx, started_at, t_start, str(exc))

    server_name = dbz_props.get("server_name") or ctx.product_id.replace(".", "_")
    try:
        config = build_connector_config(
            ctx.source.kind,
            connection=ctx.source.connection.raw,
            streams=list(ctx.source.streams),
            server_name=server_name,
            snapshot_mode=snapshot_mode,
            extra=dbz_props.get("extra_config") or {},
        )
    except ValueError as exc:  # reject_url_syntax: a field that would not stay in its part
        return _failed(ctx, started_at, t_start, str(exc))
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
    from .._acquisition_common import refuse_sent_config_expressions, secret_resolved

    try:
        refuse_sent_config_expressions(
            config,
            where="the Debezium connector config",
            secret_keys=(
                ("database.password", "mongodb.password")
                if secret_resolved(ctx.source.connection.raw)
                else ()
            ),
        )
    except ValueError as exc:
        return _failed(ctx, started_at, t_start, str(exc))
    client = KafkaConnectRestClient(server_url, engine="debezium")
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
    """Render one ``key=value`` line for a Java ``.properties`` file, escaped per
    the ``java.util.Properties`` grammar and failing closed on control characters.

    The file is line-based, so a newline (or carriage-return / form-feed / NUL)
    smuggled into a contract-derived key or value would inject arbitrary extra
    Debezium directives — reject those rather than silently write them (the
    Kafka-Connect twin is immune because it serializes to JSON). Backslash is
    ALSO load-bearing: ``Properties.load`` treats ``\\`` as an escape introducer,
    so a literal backslash in a value (a Windows path, a ``destination-regexp``
    replacement) must be doubled or it is silently corrupted on read-back; key
    separators (``=`` / ``:`` / leading whitespace / ``#`` / ``!``) are escaped so
    a contract-derived key round-trips verbatim.
    """
    text_value = str(value)
    for token in (key, text_value):
        if any(ch in token for ch in "\r\n\f\x00"):
            raise ValueError(f"debezium config entry contains a control character: {token!r}")
    if "${" in key or "${" in text_value:
        # SmallRye Config would expand it where Debezium Server runs: the last
        # check, on the text the file gets. forge's own ${FLUID_DBZ_...} lines
        # are not written through here.
        raise ValueError(
            f"debezium config entry {key} holds a ${{...}} expression (the value is not "
            "shown), which Debezium Server would expand from its environment. Write the "
            "value itself; a secret belongs in a secretRef."
        )
    return f"{_escape_properties_key(key)}={_escape_properties_value(text_value)}"


def _escape_properties_key(key: str) -> str:
    """Escape a Java ``.properties`` KEY: backslash + the key/value separators +
    comment markers + spaces, so the key is parsed back exactly as written."""
    out = []
    for ch in key:
        out.append("\\" + ch if ch in "\\=: \t#!" else ch)
    return "".join(out)


def _escape_properties_value(value: str) -> str:
    """Escape a Java ``.properties`` VALUE: double every backslash (an escape
    introducer on load) and a leading space/tab (``Properties.load`` strips
    leading whitespace). ``=`` / ``:`` / ``#`` are literal in the value."""
    escaped = value.replace("\\", "\\\\")
    if escaped[:1] in (" ", "\t"):
        escaped = "\\" + escaped
    return escaped


def _write_private_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path``, readable by this user only (0o600)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        if hasattr(os, "fchmod"):
            os.fchmod(fh.fileno(), 0o600)
        fh.write(text)


#: A property whose VALUE is a credential: Debezium's PASSWORD-typed fields
#: (``database.password``, ``*.ssl.*.password``), the Iceberg / S3 / JDBC /
#: GCS / ADLS secrets (``credential``, ``token``, ``s3.secret-access-key``,
#: ``s3.session-token``, ``jdbc.password``, ``adls.sas-token.*``,
#: ``adls.auth.shared-key.account.key``, ``s3.sse.key``), an HTTP
#: ``header.Authorization``, a Kafka JAAS blob or PEM ``ssl.keystore.key``, a
#: Pulsar ``authParams``, and a Mongo or Event Hubs connection string (it may
#: carry userinfo or a shared access key). Narrower than
#: ``is_sensitive_key_name`` on purpose: ``rest.auth.type`` and
#: ``oauth2-server-uri`` are not secrets, and stay readable in the file. A
#: false positive only moves a value to the environment.
_CREDENTIAL_PROPERTY_RE = re.compile(
    r"(?i)password|secret|token|credential|authorization|auth[._-]?params|jaas\.config"
    r"|connection[._-]?string|api[._-]?key|private[._-]?key|account[._-]?key"
    r"|keystore\.key|sse\.key"
)


def render_application_properties(
    source_config: Mapping[str, Any],
    sink_type: str,
    sink_config: Mapping[str, Any],
) -> Tuple[str, Dict[str, str]]:
    """Render Debezium Server's ``application.properties``, and the environment
    that carries its credentials.

    A credential-valued property is written as a ``${FLUID_DBZ_<PROPERTY>}``
    expression, and its value goes into the returned environment, which the
    caller passes to the server. Quarkus resolves the expression from the
    environment (SmallRye Config: environment variables, ordinal 300, are a
    config source like the file). The line stays in the file because Debezium
    Server only collects the ``debezium.source.*`` properties it finds there
    (DBZ-2886). So no credential is written to disk: the file sits under the
    contract directory, where ``git add -A`` would stage it.

    A sink ``env:<NAME>`` value is written as it is: it is Iceberg's own
    reference to an environment variable (``sink.catalogAuth``), not a secret.
    A source property gets no such reading, so a source password that starts
    with ``env:`` still goes to the environment.

    Raises ``ValueError`` for a control character in a key or value
    (:func:`_properties_line`), for two properties whose names map to the same
    environment variable, or for a ``${`` in the sink's type or config, which
    SmallRye Config would expand (:func:`refuse_config_expressions`). The source
    config is checked as contract text before its secret is resolved.
    """
    from .._acquisition_common import refuse_config_expressions

    refuse_config_expressions(sink_type, where="properties.debezium.server.sink.type")
    refuse_config_expressions(sink_config, where="properties.debezium.server.sink.config")
    env: Dict[str, str] = {}

    def line(key: str, value: object, *, env_refs: bool = False) -> str:
        text = str(value)
        if (
            not text
            or (env_refs and text.startswith("env:"))
            or not _CREDENTIAL_PROPERTY_RE.search(key)
        ):
            return _properties_line(key, value)
        # The value never reaches the file, so only a control character in
        # the KEY could inject a line; the value is still refused (no
        # credential has one, a trailing newline from a secret file is a
        # mistake), but the error names the key, never the value.
        _properties_line(key, "")
        if any(ch in text for ch in "\r\n\f\x00"):
            raise ValueError(f"debezium config entry {key} has a control character in its value")
        if "${" in text:
            # SmallRye Config expands an environment variable's value too, so a
            # credential holding ${ would reach the connector rewritten.
            raise ValueError(
                f"debezium config entry {key} holds ${{ in its value (the value is not shown), "
                "which Debezium Server would expand from its environment. A credential that "
                "holds ${ cannot be handed to Debezium Server: change it."
            )
        name = "FLUID_DBZ_" + re.sub(r"[^A-Za-z0-9]", "_", key.removeprefix("debezium.")).upper()
        if env.get(name, text) != text:
            raise ValueError(f"two debezium properties map to the environment variable {name}")
        env[name] = text
        return f"{_escape_properties_key(key)}=${{{name}}}"

    lines = [line(f"debezium.source.{k}", v) for k, v in source_config.items()]
    lines.append("quarkus.log.console.json=false")
    # The event format, set rather than left to a default: the http sink reads
    # debezium.format.value with no default (Debezium Server 3.7) and fails at
    # startup without it, and the sinks forge derives read JSON.
    lines.append("debezium.format.key=json")
    lines.append("debezium.format.value=json")
    lines.append(_properties_line("debezium.sink.type", sink_type))
    lines += [
        line(f"debezium.sink.{sink_type}.{k}", v, env_refs=True) for k, v in sink_config.items()
    ]
    return "\n".join(lines), env


def _execute_debezium_server(
    ctx: RunContext,
    build: Mapping[str, Any],
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

    try:
        source_config = build_connector_config(
            ctx.source.kind,
            connection=ctx.source.connection.raw,
            streams=list(ctx.source.streams),
            server_name=server_name,
            snapshot_mode=snapshot_mode,
        )
    except ValueError as exc:  # reject_url_syntax: a field that would not stay in its part
        return _failed(ctx, started_at, t_start, str(exc))
    # Absolute, under forge's own work directory: the server's working directory
    # is not the project. They take effect only for the file-based stores
    # (FileOffsetBackingStore, FileSchemaHistory); a server set up with another
    # store, such as Kafka's, ignores them.
    state_dir = workdir.resolve()
    offsets_path = state_dir / _OFFSETS_FILE
    source_config = {
        **source_config,
        "offset.storage.file.filename": str(offsets_path),
        "schema.history.internal.file.filename": str(state_dir / _SCHEMA_HISTORY_FILE),
    }
    sink_block = (dbz_props.get("server") or {}).get("sink") or {}
    sink_type = sink_block.get("type", "iceberg")
    raw_config = sink_block.get("config")
    if raw_config is not None and not isinstance(raw_config, Mapping):
        # dict() would turn a list of pairs into a config that the checks below,
        # which read a mapping (the env: reference scan among them), never saw.
        return _failed(
            ctx,
            started_at,
            t_start,
            "properties.debezium.server.sink.config must be a mapping of sink property names "
            "to values",
        )
    handwritten = dict(raw_config or {})

    # Iceberg sink: derive the bare-key config from the contract's iceberg expose
    # binding so the embedded server lands in the SAME table the static Glue
    # table / KC sink resolve to (RFC zero-drift spine). Gated by
    # ``iceberg_sink_plan``, the one gate `fluid validate` and the KC runner
    # read too: derivation is OFF whenever a hand-written ``config`` block is
    # present, so nothing is derived for it (an explicit empty ``config: {}``
    # counts as present, matching the KC ``sink_connector_config`` default);
    # opt back in with ``server.sink.iceberg_sink_enabled: true``. Derived keys
    # go UNDER the hand-written ones, so an operator key always wins.
    from ..kafka_connect.iceberg_sink_validation import (
        iceberg_sink_plan,
        iceberg_sink_preflight,
    )

    sink_config = handwritten
    sink_plan = iceberg_sink_plan(build)
    if sink_plan is not None:
        # Fail closed BEFORE application.properties is written, for a derived
        # AND a hand-written Iceberg sink: the same checks `fluid validate`
        # runs, so a sink it rejects never boots.
        preflight_error = iceberg_sink_preflight(ctx.contract, ctx.build_id, log=LOG)
        if preflight_error:
            return _failed(ctx, started_at, t_start, preflight_error)
    else:
        # No Iceberg plan (no sink.format: iceberg), yet a hand-written config
        # for the default iceberg sink is written as it is, and Iceberg resolves
        # an env: value in it from the server's environment: refused before
        # application.properties is written, as `fluid validate` refuses it.
        from ..kafka_connect.iceberg_sink_validation import unplanned_sink_reference_errors

        reference_errors = unplanned_sink_reference_errors(build)
        if reference_errors:
            return _failed(
                ctx,
                started_at,
                t_start,
                "iceberg sink preflight failed (see `fluid validate`): "
                + "; ".join(reference_errors),
            )
    if sink_plan is not None and sink_plan.derives:
        from ...providers._iceberg_catalog import resolve_iceberg_catalog
        from ...providers._iceberg_catalog_auth import catalog_auth_of
        from ..kafka_connect.iceberg_sink_validation import iceberg_sink_target
        from .iceberg_sink import emit_debezium_iceberg_sink_config

        # The exposes the build's outputs name, as `fluid validate` resolves
        # them. They share one database and one catalog, so the first one's
        # binding gives the namespace and the catalog block (for Glue, whose
        # warehouse is a per-table prefix, the first one's prefix; a DynamoDB /
        # JDBC warehouse is one value for all of them, or the resolver refuses).
        targets, _error = iceberg_sink_target(ctx.contract, build, sink_plan)
        if targets:
            resolved = resolve_iceberg_catalog(
                targets[0].get("binding") or {},
                contract=ctx.contract,
                sink=ctx.sink,
                account_ref=ctx.env.get("AWS_ACCOUNT_ID", ""),
            )
            derived = emit_debezium_iceberg_sink_config(
                resolved,
                catalog_auth=catalog_auth_of((build.get("properties") or {}).get("sink")),
            )
            sink_config = {**derived, **handwritten}

    # Render the line-based .properties file. Every contract-derived key/value
    # routes through _properties_line, which fail-closes on control characters so
    # a newline in a binding value cannot inject extra Debezium directives.
    # Credentials go to the server's environment, not the file.
    try:
        properties, secret_env = render_application_properties(
            source_config, sink_type, sink_config
        )
    except ValueError as exc:
        return _failed(ctx, started_at, t_start, str(exc))
    # Created 0o600 and tightened to 0o600 when it already exists (a copy an
    # earlier release wrote at 0o644 with the password in it is truncated
    # first): never world-readable, and no chmod whose failure is ignored. A
    # symlink in its place is refused rather than followed. A hand-written
    # sink key the credential rule does not name still lands in this file.
    try:
        _write_private_text(config_path, properties)
    except OSError as exc:
        return _failed(ctx, started_at, t_start, f"could not write {config_path}: {exc}")

    return _launch_server(
        ctx,
        dbz_props,
        config_path,
        offsets_path,
        secret_env,
        server_name,
        snapshot_mode,
        connector_class,
        started_at,
        t_start,
    )


def _launch_server(
    ctx: RunContext,
    dbz_props: Dict[str, Any],
    config_path: Path,
    offsets_path: Path,
    secret_env: Mapping[str, str],
    server_name: str,
    snapshot_mode: str,
    connector_class: str,
    started_at: str,
    t_start: float,
) -> RunResult:
    """Verify the server binary, start it outside the project and report the result."""
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
    # The server is never started in the project: Quarkus reads ``.env`` and
    # ``config/application.properties`` from its working directory and the stock
    # launcher runs ``./jmx/*.sh`` and loads jars from there. A binary inside the
    # project is refused outright, as is a PATH entry or symlink that leads there.
    real_binary, refusal = _locate_server_binary(binary, Path(ctx.workdir))
    if real_binary is None:
        return _failed(ctx, started_at, t_start, refusal or "debezium-server binary rejected")

    # Offsets a server kept at <project>/data/offsets.dat before forge named the file.
    _migrate_project_offsets(Path(ctx.workdir), offsets_path)

    # The working directory is a fresh one forge owns (0700, removed afterwards),
    # never the project: it holds forge's file as config/application.properties
    # and, for the stock launcher, links to the install's own files
    # (_prepare_launch_directory).
    scratch_dir = tempfile.mkdtemp(prefix="fluid-debezium-")
    launch_dir = Path(scratch_dir)
    try:
        try:
            server_config = _prepare_launch_directory(launch_dir, real_binary, config_path)
        except OSError as exc:
            return _failed(ctx, started_at, t_start, f"could not prepare {launch_dir}: {exc}")
        # The server inherits this process's environment, which is where Iceberg
        # resolves the ``env:<NAME>`` catalog properties ``sink.catalogAuth``
        # emits, and where the preflight checked the variable is set, once the
        # operator had paired it with every host it reaches. The
        # ``FLUID_DBZ_*`` credentials the file refers to are added to it, and
        # forge's file is also named by a system property, so its values win over
        # the environment's (the launcher drops arguments, so ``--config`` would
        # never reach the JVM). No secret is written to application.properties.
        try:
            server_env = _server_environment(server_config, secret_env, launch_dir)
        except ValueError as exc:
            return _failed(ctx, started_at, t_start, str(exc))
        try:
            proc = subprocess.run(
                [str(real_binary)],
                env=server_env,
                cwd=str(launch_dir),
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
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)

    if proc.returncode != 0:
        return _failed(ctx, started_at, t_start, _server_failure(proc, secret_env))
    return _success_embedded(ctx, started_at, t_start, server_name, snapshot_mode, connector_class)


#: How much of each stream a failed run reports: the end, where the cause is.
_FAILURE_TAIL_LINES = 40
_FAILURE_TAIL_CHARS = 4000
#: The log lines and exception lines that name the cause.
_FAILURE_CAUSE_RE = re.compile(r"\bERROR\b|Caused by:|\b\w+(?:Exception|Error)\b:")
_FAILURE_CAUSE_LINES = 12
_FAILURE_CAUSE_CHARS = 600


def _server_failure(proc: "subprocess.CompletedProcess[str]", secret_env: Mapping[str, str]) -> str:
    """Why the server stopped: its exit code and the end of what it printed.

    Quarkus logs to standard output, so both streams are read. Every credential
    this run handed the server is masked, and so is anything else that looks
    like a secret (:func:`redact_secret_text`).
    """
    from ...observability.secret_redactor import redact_secret_text

    parts = []
    # The cause is usually far above the end: a Java stack trace prints the
    # exception message first, then its frames. So the ERROR log lines and the
    # exception lines come first, then the end of each stream.
    causes: List[str] = []
    for text in (proc.stderr, proc.stdout):
        for line in (text or "").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("at ") or stripped in causes:
                continue
            if _FAILURE_CAUSE_RE.search(stripped):
                causes.append(stripped[:_FAILURE_CAUSE_CHARS])
            if len(causes) >= _FAILURE_CAUSE_LINES:
                break
    if causes:
        parts.append("--- errors ---\n" + "\n".join(causes))
    for label, text in (("stderr", proc.stderr), ("stdout", proc.stdout)):
        lines = (text or "").strip().splitlines()[-_FAILURE_TAIL_LINES:]
        if lines:
            tail = "\n".join(lines)[-_FAILURE_TAIL_CHARS:]
            parts.append(f"--- {label} (last lines) ---\n{tail}")
    head = f"debezium-server exited with code {proc.returncode}"
    if not parts:
        return f"{head} and printed nothing"
    text = head + "\n" + "\n".join(parts)
    return redact_secret_text(text, extra_secrets=[v for v in secret_env.values() if v])


#: The stock launcher (``run.sh``) finds its runner jar with
#: ``ls debezium-server-*runner.jar`` and sources ``./jmx/*.sh`` and
#: ``./lib_metrics/*.sh``, and its classpath (``config/lib``, ``lib/*``) is
#: relative too: its working directory must hold those entries.
_RUNNER_JAR_GLOB = "debezium-server-*runner.jar"

#: Debezium Server's file-based offset and schema-history stores have no default
#: file name; the documented example (``data/offsets.dat``) is relative to the
#: working directory. Forge names both under its own work directory instead.
_OFFSETS_FILE = "offsets.dat"
_SCHEMA_HISTORY_FILE = "schema_history.dat"


def _path_is_inside(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or below it. Falls back to ``samefile`` for
    each parent, so a case-insensitive filesystem or a bind mount cannot hide it."""
    if path == root or path.is_relative_to(root):
        return True
    for parent in path.parents:
        try:
            if os.path.samefile(parent, root):
                return True
        except OSError:
            continue
    return False


def _locate_server_binary(binary: str, project_dir: Path) -> Tuple[Optional[Path], Optional[str]]:
    """The real path of the verified server binary, or ``(None, message)``.

    A Debezium Server started from the project would read project files
    (Quarkus loads ``.env`` and ``config/application.properties`` from the
    working directory, and the stock ``run.sh`` sources ``./jmx/*.sh`` and
    puts ``config/lib`` and ``lib/*`` on the classpath), so a binary that lives
    in the project is refused, whether it was named by path, found on ``PATH``
    or reached through a symlink. The project is the contract's directory and
    the checkout it sits in (:func:`_checkout_root`), so a binary elsewhere in
    the same repository is refused too.
    """
    try:
        real = Path(binary).resolve()
        project = project_dir.resolve()
    except (OSError, RuntimeError) as exc:
        return None, f"debezium-server binary {binary!r} could not be resolved: {exc}"
    roots = [project]
    checkout = _checkout_root(project)
    if checkout is not None and checkout != project:
        roots.append(checkout)
    for root in roots:
        if _path_is_inside(real, root):
            return None, (
                f"properties.debezium.server_binary resolves to {real}, which is inside the "
                f"project directory {root}: Debezium Server would run files the project "
                "holds. Install Debezium Server outside the project, then put it on PATH or "
                "set properties.debezium.server_binary to its absolute path."
            )
    return real, None


def _checkout_root(project_dir: Path) -> Optional[Path]:
    """The checkout ``project_dir`` sits in: the nearest directory at or above it that
    holds ``.git`` (a clone's directory, a worktree's file), or ``None``. The search
    stops below the home directory, which a dotfiles repository can make a checkout."""
    try:
        home: Optional[Path] = Path.home().resolve()
    except (OSError, RuntimeError):
        home = None
    for candidate in (project_dir, *project_dir.parents):
        if home is not None and candidate == home:
            return None
        if os.path.lexists(candidate / ".git"):
            return candidate
    return None


def _launch_directory(real_binary: Path) -> Optional[Path]:
    """The install directory of the binary when it is the stock launcher (the
    directory holds the runner jar), else ``None``."""
    install_dir = real_binary.parent
    try:
        for candidate in install_dir.glob(_RUNNER_JAR_GLOB):
            if candidate.is_file():
                return install_dir
    except OSError:
        pass
    return None


def _prepare_launch_directory(launch_dir: Path, real_binary: Path, config_path: Path) -> Path:
    """Fill the server's working directory and return forge's config file in it.

    Debezium Server (3.7) learns which connector its engine runs while Quarkus is
    still starting, when only ``config/application.properties`` under the working
    directory is read: a file named only by ``smallrye.config.locations`` configures
    the sink, but no engine starts. So forge's file is copied (0600) to
    ``config/application.properties`` here. For the stock launcher, which finds its
    runner jar, ``./jmx/*.sh``, ``lib/*`` and ``config/lib`` relative to the working
    directory, the install's entries are linked in too, and so are its ``config``
    entries except its own ``application.*`` files. Nothing comes from the project.
    """
    install = _launch_directory(real_binary)
    config_dir = launch_dir / "config"
    config_dir.mkdir(mode=0o700)
    if install is not None:
        for entry in install.iterdir():
            if entry.name != "config":
                os.symlink(entry, launch_dir / entry.name)
        install_config = install / "config"
        if install_config.is_dir():
            for entry in install_config.iterdir():
                if not entry.name.startswith("application."):
                    os.symlink(entry, config_dir / entry.name)
    target = config_dir / "application.properties"
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(config_path.read_bytes())
    return target


def _server_environment(
    config_path: Path, secret_env: Mapping[str, str], cwd: Path
) -> Dict[str, str]:
    """The server's environment: this process's, the credentials the file refers
    to, and forge's file as a system property.

    ``run.sh`` drops its arguments (so ``--config`` never reaches the JVM) and
    passes ``$DEBEZIUM_OPTS $JAVA_OPTS`` to ``java``. A ``smallrye.config.locations``
    system property loads the file at ordinal 400, above ``.env`` (295) and
    ``config/application.properties`` (260). The property goes into both
    variables, last, because the launcher's ``jmx/enable_jmx.sh`` replaces
    ``JAVA_OPTS`` when ``JMX_HOST`` and ``JMX_PORT`` are set. ``quarkus.config.locations``
    is the same setting under Quarkus's name. A last ``-D`` for a key wins,
    so the operator's own value for it is overridden.

    Raises ``ValueError`` when the file's URI holds whitespace or a comma: the
    launcher word-splits the variables and the locations are comma-separated.
    ``Path.as_uri`` percent-encodes both, so this holds only for a value it
    could not encode.
    """
    uri = config_path.resolve().as_uri()
    if re.search(r"[\s,]", uri):
        raise ValueError(
            "the debezium config file's path cannot be passed to the server (it holds "
            "whitespace or a comma after encoding); run from a project path without them"
        )
    prop = f"-Dsmallrye.config.locations={uri} -Dquarkus.config.locations={uri}"
    env = {**os.environ, **secret_env}
    for name in ("JAVA_OPTS", "DEBEZIUM_OPTS"):
        env[name] = " ".join(part for part in (os.environ.get(name, "").strip(), prop) if part)
    env["PWD"] = str(cwd)
    return env


def _migrate_project_offsets(project_dir: Path, offsets_path: Path) -> None:
    """Copy ``<project>/data/offsets.dat`` to forge's offsets file, once.

    Before forge named the offsets file, a server that kept its offsets in a file
    kept them at the documented ``data/offsets.dat`` under the working directory,
    which was the project. The old file stays where it is. Nothing is copied when
    the new file exists, when the old one is not a regular file (a symlink is
    not followed) or when the copy fails (a partial file is removed).
    """
    old = project_dir / "data" / _OFFSETS_FILE
    try:
        # Neither the directory nor the file may be a link: only a file the
        # project holds itself is the server's old store.
        if not stat.S_ISDIR(os.lstat(old.parent).st_mode):
            return
        if not stat.S_ISREG(os.lstat(old).st_mode):
            return
    except OSError:
        return
    try:
        os.lstat(offsets_path)
        return  # the new file exists: it is never overwritten
    except FileNotFoundError:
        pass
    except OSError:
        return
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        src_fd = os.open(old, os.O_RDONLY | nofollow)
        with os.fdopen(src_fd, "rb") as src:
            dst_fd = os.open(offsets_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
            created = True
            with os.fdopen(dst_fd, "wb") as dst:
                shutil.copyfileobj(src, dst)
    except OSError as exc:
        if created:
            try:
                os.unlink(offsets_path)
            except OSError:
                pass
        LOG.warning("debezium.offsets.migrate_failed from=%s to=%s err=%s", old, offsets_path, exc)
        return
    LOG.info("debezium.offsets.migrated from=%s to=%s (old file left in place)", old, offsets_path)


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
