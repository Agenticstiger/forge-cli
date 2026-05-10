# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Airbyte acquisition runner.

REST mode (default for production deployments): drives an Airbyte OSS / Cloud
server. Embedded mode (PyAirbyte) runs connectors in-process — kept for
parity but exercised via the REST path in tests since PyAirbyte requires a
heavyweight install.

Image-signature verification: when ``properties.airbyte.image_signature`` is
set, the connector image is verified via the configured ``ImageSignatureVerifier``
before any sync triggers.
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
from fluid_build.api.security import ImageSignatureVerifier

from .._acquisition_common import (
    adapt_source_config,
    extract_source_schemas,
    generate_run_id,
    resolve_connection_secrets,
    resolve_secret_ref,
    utc_now_iso,
    write_run_record_and_finalize,
)
from .._fingerprint import fingerprint_from_columns

LOG = logging.getLogger("fluid.acquire.airbyte")


# Per-connector source adapters live in ``fluid_build.build_runners.airbyte.sources``
# and register themselves with the shared ``_acquisition_common`` registry at
# import time (the engine's ``__init__.py`` imports the module for that side
# effect). The runner just calls ``adapt_source_config("airbyte", kind, conn)``
# without knowing about specific connector quirks.


# ── REST client ─────────────────────────────────────────────────────────


class AirbyteRestClient:
    """Minimal REST client for the Airbyte API surface the runner uses.

    Wraps httpx.Client; tolerates Airbyte Cloud auth (Bearer token) when
    ``properties.airbyte.deployment.auth.secretRef`` resolves to a token.
    """

    def __init__(
        self,
        server_url: str,
        *,
        api_token: Optional[str] = None,
        timeout_seconds: int = 30,
    ):
        import httpx

        headers: Dict[str, str] = {}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        self._client = httpx.Client(
            base_url=server_url.rstrip("/"),
            headers=headers,
            timeout=timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def list_sources(self, workspace_id: str) -> List[Dict[str, Any]]:
        r = self._client.post("/api/v1/sources/list", json={"workspaceId": workspace_id})
        r.raise_for_status()
        return r.json().get("sources", [])

    def create_source(self, body: Dict[str, Any]) -> Dict[str, Any]:
        r = self._client.post("/api/v1/sources/create", json=body)
        r.raise_for_status()
        return r.json()

    def create_destination(self, body: Dict[str, Any]) -> Dict[str, Any]:
        r = self._client.post("/api/v1/destinations/create", json=body)
        r.raise_for_status()
        return r.json()

    def create_connection(self, body: Dict[str, Any]) -> Dict[str, Any]:
        r = self._client.post("/api/v1/connections/create", json=body)
        r.raise_for_status()
        return r.json()

    def discover_schema(self, source_id: str, *, timeout_seconds: float = 300.0) -> Dict[str, Any]:
        """Run the connector's discover_schema and return the full catalog.

        Airbyte's ``createConnection`` requires every stream in
        ``syncCatalog`` to carry a ``jsonSchema`` and the connector's
        declared ``supportedSyncModes``. Discover is the only way to get
        that without re-implementing every connector's spec.

        Discovery is slow — Airbyte spawns the connector image, runs
        ``check`` and ``discover`` against the live source, and returns
        the parsed catalog. 5 minutes is a generous default that fits a
        Postgres / S3 / faker connector cold start; pass a smaller
        timeout in tests via the ``timeout_seconds`` kwarg.
        """
        r = self._client.post(
            "/api/v1/sources/discover_schema",
            json={"sourceId": source_id},
            timeout=timeout_seconds,
        )
        r.raise_for_status()
        return r.json()

    def trigger_sync(self, connection_id: str) -> Dict[str, Any]:
        r = self._client.post("/api/v1/connections/sync", json={"connectionId": connection_id})
        r.raise_for_status()
        return r.json()

    def get_job(self, job_id: int) -> Dict[str, Any]:
        r = self._client.post("/api/v1/jobs/get", json={"id": job_id})
        r.raise_for_status()
        return r.json()


# ── Connector kind → image map ─────────────────────────────────────────


KIND_TO_IMAGE: Dict[str, str] = {
    "salesforce": "airbyte/source-salesforce",
    "stripe": "airbyte/source-stripe",
    "github": "airbyte/source-github",
    "postgres": "airbyte/source-postgres",
    "mysql": "airbyte/source-mysql",
    "mongodb": "airbyte/source-mongodb-v2",
    "s3": "airbyte/source-s3",
    "snowflake": "airbyte/source-snowflake",
    "faker": "airbyte/source-faker",
}


def resolve_connector_image(kind: str, override: Optional[str] = None) -> str:
    """Return ``image:tag`` for a given source kind. Override wins."""
    if override:
        return override
    img = KIND_TO_IMAGE.get(kind)
    if not img:
        raise ValueError(f"airbyte: no connector image for kind '{kind}'")
    return img + ":latest"


# ── Mode mapping ───────────────────────────────────────────────────────


def map_mode_to_sync_mode(mode: str) -> str:
    """FLUID acquisition mode → Airbyte syncMode."""
    return {
        "full_refresh": "full_refresh",
        "incremental_append": "incremental",
        "incremental_dedup": "incremental",
        "cdc": "incremental",
    }.get(mode, "full_refresh")


# ── Runner ─────────────────────────────────────────────────────────────


@dataclass
class AirbyteRunner:
    """Runner Protocol implementation for the Airbyte engine."""

    name: ClassVar[str] = "airbyte"
    declared_capabilities: ClassVar[FrozenSet[RunnerCapability]] = frozenset(
        {
            RunnerCapability.FULL_REFRESH,
            RunnerCapability.INCREMENTAL_APPEND,
            RunnerCapability.INCREMENTAL_DEDUP,
            RunnerCapability.CDC,
            RunnerCapability.SCHEMA_DISCOVERY,
            RunnerCapability.AT_LEAST_ONCE,
        }
    )
    declared_modes: ClassVar[FrozenSet[str]] = frozenset({"embedded", "bring-your-own", "managed"})

    image_verifier: Optional[ImageSignatureVerifier] = None

    def plan(self, ctx: RunContext) -> RunPlan:
        return RunPlan(streams_planned=list(ctx.source.streams) or [ctx.source.kind])

    def run(self, ctx: RunContext) -> RunResult:
        return _execute(ctx, self)

    def replay(self, ctx: RunContext, run_id: str) -> RunResult:
        ctx.run_id = run_id
        return _execute(ctx, self)

    def fingerprint(self, ctx: RunContext) -> SchemaFingerprint:
        # Airbyte resolves stream schemas inside ``source.discover`` /
        # ``source.read`` — introspecting at fingerprint() time would mean
        # spinning up a source connector container. Surface a placeholder
        # marked ``is_placeholder=True`` so the schema-evolution gate skips
        # the contract-vs-current comparison; Airbyte's own catalog stream
        # surfaces real schema drift at sync time.
        return SchemaFingerprint.placeholder(
            list(ctx.source.streams or [ctx.source.kind]),
            engine="airbyte",
            captured_at=utc_now_iso(),
        )


def _execute(ctx: RunContext, runner: AirbyteRunner) -> RunResult:
    started_at = utc_now_iso()
    t_start = time.time()

    # Schema-evolution gate (shared across all 6 acquisition runners).
    from .._acquisition_common import enforce_schema_policy_or_raise

    enforce_schema_policy_or_raise(ctx, runner)

    props = ctx.contract.get("builds", [{}])[0].get("properties", {})
    airbyte_props = props.get("airbyte", {}) or {}
    deployment = airbyte_props.get("deployment", {}) or {}
    mode = deployment.get("mode", "bring-your-own")

    try:
        image_ref = resolve_connector_image(
            ctx.source.kind, override=airbyte_props.get("connector_image")
        )
    except ValueError as exc:
        return _failed(ctx, started_at, t_start, str(exc))
    sig_block = airbyte_props.get("image_signature") or {}
    if sig_block and runner.image_verifier is not None:
        sig_result = runner.image_verifier.verify(
            image_ref,
            public_key=sig_block.get("publicKey", ""),
            require_slsa_provenance=(sig_block.get("slsaProvenance") == "required"),
        )
        if not sig_result.signed:
            from fluid_build.cli._errors import SupplyChainViolationError

            # Five-field typed error — the CLI top-level handler renders
            # the Panel. The dispatcher's existing exception path also
            # writes a failed run record, so audit trails on disk are
            # preserved.
            LOG.error(
                "airbyte.signature_failed image=%s err=%s",
                image_ref,
                sig_result.error,
            )
            raise SupplyChainViolationError.for_image(
                image_ref=image_ref,
                reason=str(sig_result.error or "verification failed"),
            )

    if mode in ("bring-your-own", "managed"):
        return _execute_rest_mode(
            ctx, runner, deployment, airbyte_props, image_ref, started_at, t_start
        )
    if mode == "embedded":
        return _execute_embedded_mode(ctx, runner, airbyte_props, image_ref, started_at, t_start)
    return _failed(ctx, started_at, t_start, f"airbyte: unknown deployment.mode '{mode}'")


def _execute_rest_mode(
    ctx: RunContext,
    runner: AirbyteRunner,
    deployment: Dict[str, Any],
    airbyte_props: Dict[str, Any],
    image_ref: str,
    started_at: str,
    t_start: float,
) -> RunResult:
    server_url = deployment.get("server_url")
    if not server_url:
        return _failed(ctx, started_at, t_start, "airbyte REST mode requires deployment.server_url")
    auth = deployment.get("auth", {}) or {}
    # auth.token wins; if absent, resolve auth.secretRef via the central
    # secret backends (env://, vault://, aws://, gcp://, azure://, file://).
    api_token = auth.get("token")
    if not api_token and auth.get("secretRef"):
        try:
            api_token = resolve_secret_ref(auth["secretRef"])
        except (ValueError, NotImplementedError) as exc:
            return _failed(
                ctx, started_at, t_start,
                f"airbyte deployment.auth.secretRef did not resolve: {exc}",
            )

    workspace_id = airbyte_props.get("workspace_id", "default-workspace")
    expose = (ctx.contract.get("exposes") or [{}])[0]
    binding = expose.get("binding", {}) or {}

    client = AirbyteRestClient(server_url, api_token=api_token)
    try:
        # 1. Create source. Airbyte's REST API needs the
        # source-definition UUID (NOT the docker image reference).
        # Prefer ``properties.airbyte.source_definition_id`` from the
        # contract; fall back to ``image_ref`` only as a legacy hint —
        # the legacy path will 400 on Airbyte 1.x but is kept so older
        # contracts surface a clear error rather than a silent miss.
        source_definition_id = airbyte_props.get("source_definition_id") or image_ref
        # Resolve secretRef → password (or other credential field) before
        # POSTing to Airbyte. Inline literal values still win.
        connection_config = resolve_connection_secrets(
            dict(ctx.source.connection.raw)
        )
        # connection.schema / connection.schemas → Airbyte source-postgres
        # (and similar) accept ``schemas`` (list). Pop the generic FLUID
        # fields so unrecognised-setting validators don't fire.
        schemas = extract_source_schemas(connection_config)
        connection_config.pop("schema", None)
        connection_config.pop("schemas", None)
        if schemas:
            connection_config.setdefault("schemas", schemas)
        # Adapt FLUID generic connection → Airbyte source-X spec
        # (renames, type coercions, connector-required defaults).
        connection_config = adapt_source_config(
            "airbyte", ctx.source.kind,connection_config
        )
        source_body = {
            "workspaceId": workspace_id,
            "name": f"forge-{ctx.product_id}",
            "sourceDefinitionId": source_definition_id,
            "connectionConfiguration": connection_config,
        }
        source = client.create_source(source_body)
        source_id = source["sourceId"]

        # 2. Create destination. Same UUID-vs-image issue applies; the
        # contract may pin ``destination_definition_id`` explicitly.
        destination_definition_id = airbyte_props.get(
            "destination_definition_id"
        ) or _destination_image_for_binding(binding)
        # Each Airbyte destination connector has its own JSON-schema
        # spec — Local JSON wants ``destination_path``, S3 wants
        # ``s3_bucket_*``, Postgres wants host/port/etc. The contract's
        # ``binding.location`` is intentionally connector-agnostic, so
        # ``properties.airbyte.destination_config`` is the explicit
        # passthrough escape hatch. When unset, fall back to the smart
        # mapper that handles the local-file shapes (jsonl/csv/parquet
        # → ``destination_path``) — the most common dev path.
        explicit_dest_cfg = airbyte_props.get("destination_config")
        if isinstance(explicit_dest_cfg, dict) and explicit_dest_cfg:
            dest_connection_configuration = dict(explicit_dest_cfg)
        else:
            dest_connection_configuration = _build_destination_config(binding)
        dest_body = {
            "workspaceId": workspace_id,
            "name": f"forge-dst-{ctx.product_id}",
            "destinationDefinitionId": destination_definition_id,
            "connectionConfiguration": dest_connection_configuration,
        }
        destination = client.create_destination(dest_body)
        destination_id = destination["destinationId"]

        # 3. Discover the source's actual schema. Airbyte 1.x's
        # ``createConnection`` validator dereferences
        # ``stream.jsonSchema`` and ``stream.supportedSyncModes`` — both
        # only available from the connector itself via
        # ``/sources/discover_schema``. We can't fabricate this from
        # the FLUID contract because the connector decides.
        sync_mode = map_mode_to_sync_mode(ctx.source.mode.value)
        wanted = set(ctx.source.streams or [])
        discovered = client.discover_schema(source_id)
        catalog = (discovered or {}).get("catalog") or {}
        catalog_streams = catalog.get("streams") or []
        if not catalog_streams:
            return _failed(
                ctx,
                started_at,
                t_start,
                "airbyte: discover_schema returned an empty catalog",
            )
        # Filter + select the requested streams, preserving the connector's
        # declared schema/sync-modes so the validator is satisfied.
        selected_streams = []
        for entry in catalog_streams:
            stream = entry.get("stream") or {}
            sname = stream.get("name") or ""
            if wanted and sname not in wanted:
                continue
            selected_streams.append(
                {
                    "stream": stream,
                    "config": {
                        "selected": True,
                        "syncMode": sync_mode,
                        "destinationSyncMode": "append",
                        "aliasName": sname,
                        # Carry through the discovered primary-key /
                        # cursor when present — Airbyte uses these for
                        # incremental modes; harmless for full_refresh.
                        "cursorField": stream.get("defaultCursorField") or [],
                        "primaryKey": stream.get("sourceDefinedPrimaryKey") or [],
                    },
                }
            )
        if not selected_streams:
            # Fall back to the full discovered catalog so the user gets
            # a working connection rather than a confusing empty error.
            for entry in catalog_streams:
                stream = entry.get("stream") or {}
                sname = stream.get("name") or ""
                selected_streams.append(
                    {
                        "stream": stream,
                        "config": {
                            "selected": True,
                            "syncMode": sync_mode,
                            "destinationSyncMode": "append",
                            "aliasName": sname,
                            "cursorField": stream.get("defaultCursorField") or [],
                            "primaryKey": stream.get("sourceDefinedPrimaryKey") or [],
                        },
                    }
                )

        # 4. Create connection.
        conn_body = {
            "name": f"forge-conn-{ctx.product_id}",
            "sourceId": source_id,
            "destinationId": destination_id,
            "namespaceDefinition": "destination",
            "syncCatalog": {"streams": selected_streams},
            # Airbyte 1.x defaults missing fields server-side, but pinning
            # status=active + scheduleType=manual keeps the smoke
            # deterministic across versions.
            "status": "active",
            "scheduleType": "manual",
        }
        conn = client.create_connection(conn_body)
        connection_id = conn["connectionId"]

        # 4. Trigger sync. Real Airbyte returns ``status="running"`` here
        # (the job is asynchronous); the previous code treated anything
        # other than ``"succeeded"`` as failure, which made the runner
        # report success only against synchronous mocks. We poll
        # ``/jobs/get`` until the job reaches a terminal state or the
        # configured timeout elapses.
        sync = client.trigger_sync(connection_id)
        job_id = sync.get("job", {}).get("id")
        initial_status = sync.get("job", {}).get("status") or "running"

        terminal = {"succeeded", "failed", "cancelled", "incomplete"}
        records_synced = 0
        timeout_seconds = int(
            airbyte_props.get("job_timeout_seconds")
            or deployment.get("job_timeout_seconds")
            or 1800
        )
        poll_interval = float(deployment.get("poll_interval_seconds") or 2.0)
        deadline = time.time() + timeout_seconds

        if initial_status in terminal or job_id is None:
            status = initial_status
        else:
            status = initial_status
            while time.time() < deadline:
                time.sleep(poll_interval)
                try:
                    job_resp = client.get_job(int(job_id))
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("airbyte.poll.transient err=%s", exc)
                    continue
                job = job_resp.get("job", {}) or {}
                status = (job.get("status") or "running").lower()
                attempts = job_resp.get("attempts") or []
                if attempts:
                    last = attempts[-1].get("attempt") or {}
                    records_synced = int(last.get("recordsSynced") or 0)
                if status in terminal:
                    break
            else:
                # Hit the deadline without seeing a terminal state.
                status = "incomplete"

        # 5. Build per-stream results. With the polled status known, we
        # can map to the canonical RunState terminal set.
        per_stream_state = RunState.SUCCEEDED if status == "succeeded" else RunState.FAILED
        # Recover the per-stream names from the discovered catalog so
        # the run record reflects what Airbyte actually synced — this
        # may be a subset of the contract's requested streams or, when
        # the user didn't pin streams, the full discovered set.
        synced_names = [
            (entry.get("stream") or {}).get("name") or "unnamed" for entry in selected_streams
        ]
        stream_results = [
            StreamResult(
                name=s,
                state=per_stream_state,
                records=(records_synced if per_stream_state is RunState.SUCCEEDED else 0),
                cursor_advanced=(per_stream_state is RunState.SUCCEEDED),
            )
            for s in synced_names
        ]

        finished_at = utc_now_iso()
        return RunResult(
            run_id=ctx.run_id,
            state=RunState.SUCCEEDED if status == "succeeded" else RunState.FAILED,
            streams=stream_results,
            started_at=started_at,
            finished_at=finished_at,
            records_total=records_synced,
            bytes_total=0,
            dlq_records=0,
            facets={
                "engine": "airbyte",
                "mode": "rest",
                "duration_seconds": time.time() - t_start,
                "connection_id": connection_id,
                "job_id": job_id,
                "image_ref": image_ref,
                "final_status": status,
            },
        )
    except Exception as exc:  # noqa: BLE001
        LOG.error("airbyte.rest.failed err=%s", exc, exc_info=True)
        return _failed(ctx, started_at, t_start, str(exc))
    finally:
        client.close()


def _execute_embedded_mode(
    ctx: RunContext,
    runner: AirbyteRunner,
    airbyte_props: Dict[str, Any],
    image_ref: str,
    started_at: str,
    t_start: float,
) -> RunResult:
    """PyAirbyte-based in-process mode. Skipped if PyAirbyte is not installed."""
    try:
        import airbyte as ab  # type: ignore[import-untyped]
    except ImportError:
        return _failed(
            ctx,
            started_at,
            t_start,
            "PyAirbyte not installed; install with `pip install airbyte` "
            "or use deployment.mode=bring-your-own with a server URL.",
        )

    try:
        # Resolve secretRef → password before passing to PyAirbyte.
        embedded_config = resolve_connection_secrets(
            dict(ctx.source.connection.raw)
        )
        # Same schema-list translation as the REST mode above.
        embedded_schemas = extract_source_schemas(embedded_config)
        embedded_config.pop("schema", None)
        embedded_config.pop("schemas", None)
        if embedded_schemas:
            embedded_config.setdefault("schemas", embedded_schemas)
        # Adapt FLUID generic connection → Airbyte source-X spec
        # (renames, type coercions, connector-required defaults).
        embedded_config = adapt_source_config(
            "airbyte", ctx.source.kind,embedded_config
        )
        source = ab.get_source(
            f"source-{ctx.source.kind}",
            config=embedded_config,
        )
        if ctx.source.streams:
            source.select_streams(list(ctx.source.streams))
        else:
            source.select_all_streams()

        # Pick a PyAirbyte cache via the introspector in airbyte/destinations.py
        # (one ~40-line introspector that walks ``<X>Cache.__init__`` signature
        # — no per-destination factories). Credentials are resolved through the
        # pydantic-settings layer in _credentials.py. Falls back to local
        # DuckDB cache + warning when no cache class matches the platform.
        from .._credentials import make_destination

        binding = (ctx.contract.get("exposes") or [{}])[0].get("binding") or {}
        platform = binding.get("platform", "local")
        cache = make_destination(
            "airbyte", platform,
            binding=binding,
            contract=ctx.contract,
            product_id=ctx.product_id,
        )
        if cache is None:
            LOG.warning(
                "airbyte: no destination factory registered for platform=%r; "
                "falling back to local DuckDB cache (data lands in "
                "~/.cache/airbyte/, NOT the contract's declared destination). "
                "Register a factory in airbyte/destinations.py to wire it.",
                platform,
            )
            safe_id = ctx.product_id.replace(".", "_").replace("-", "_")
            cache = ab.new_local_cache(cache_name=f"forge_{safe_id}")
        # Honour the contract's acquisition mode (per FLUID 0.7.3
        # ``properties.source.mode``). PyAirbyte defaults to incremental
        # which trips on relational sources that don't have a cursor field
        # configured (postgres source falls into ``getCursorBasedSyncStatus``
        # and emits ``column "null" does not exist``). Forcing the read
        # mode to match the contract is the right thing.
        mode = (ctx.source.mode.value if hasattr(ctx.source.mode, "value") else str(ctx.source.mode)).lower()
        force_full_refresh = mode == "full_refresh"
        try:
            result = source.read(cache=cache, force_full_refresh=force_full_refresh)
        except TypeError:
            # Older PyAirbyte versions don't expose force_full_refresh.
            # Fall back to the default; the contract author can configure
            # cursor_field if they hit this path.
            result = source.read(cache=cache)
        records_total = (
            sum(len(stream) for stream in result.streams.values())
            if hasattr(result, "streams")
            else 0
        )

        return RunResult(
            run_id=ctx.run_id,
            state=RunState.SUCCEEDED,
            streams=[
                StreamResult(name=s, state=RunState.SUCCEEDED, records=0)
                for s in (ctx.source.streams or [ctx.source.kind])
            ],
            started_at=started_at,
            finished_at=utc_now_iso(),
            records_total=records_total,
            bytes_total=0,
            dlq_records=0,
            facets={
                "engine": "airbyte",
                "mode": "embedded",
                "duration_seconds": time.time() - t_start,
                "image_ref": image_ref,
            },
        )
    except Exception as exc:  # noqa: BLE001
        return _failed(ctx, started_at, t_start, f"PyAirbyte run failed: {exc}")


def _destination_image_for_binding(binding: Dict[str, Any]) -> str:
    """Map binding.platform/format → Airbyte destination image."""
    fmt = binding.get("format", "")
    platform = binding.get("platform", "")
    if "snowflake" in fmt or platform == "snowflake":
        return "airbyte/destination-snowflake"
    if "bigquery" in fmt or platform == "gcp":
        return "airbyte/destination-bigquery"
    if "postgres" in fmt:
        return "airbyte/destination-postgres"
    return "airbyte/destination-jsonl"


def _build_destination_config(binding: Dict[str, Any]) -> Dict[str, Any]:
    """Translate ``binding.location`` into a destination connector config.

    Airbyte destinations each have their own JSON-schema spec — Local
    JSON wants ``destination_path``, Postgres wants
    ``host``/``port``/``database``/``username``/``password``, S3 wants
    ``s3_bucket_*``, etc. The contract's ``binding.location`` is
    deliberately connector-agnostic, so this function picks a sensible
    default for the most common cases. Anything unusual should set
    ``properties.airbyte.destination_config`` explicitly to bypass this
    mapper entirely.

    For the file destinations (Local JSON / Local CSV) the destination
    container's filesystem root is ``/local`` (mounted from
    ``LOCAL_ROOT`` in the deployment). The contract path is appended
    under ``/local`` so downstream readers can find the files.
    """
    location = dict(binding.get("location") or {})
    fmt = (binding.get("format") or "").lower()
    platform = (binding.get("platform") or "").lower()

    # Postgres destination — contract.connection-shaped config wins.
    if "postgres" in fmt or platform == "postgres":
        # Accept both contract conventions (host/port/database/user/password)
        # and pass-through if already in Airbyte shape.
        return {
            "host": location.get("host") or location.get("server"),
            "port": int(location.get("port", 5432)),
            "database": location.get("database") or location.get("schema"),
            "username": location.get("username") or location.get("user"),
            "password": location.get("password"),
            "schema": location.get("schema") or "public",
            "ssl_mode": location.get("ssl_mode") or {"mode": "disable"},
            "tunnel_method": location.get("tunnel_method") or {"tunnel_method": "NO_TUNNEL"},
        }

    # S3 — pass through s3_* keys when present.
    if platform in ("s3", "aws") or fmt.startswith("s3"):
        return {k: v for k, v in location.items() if k}

    # Local-file destinations (Local JSON / Local CSV / "jsonl"). The
    # contract carries a workspace-relative path; Airbyte's local
    # destination requires a path relative to ``/local``. We strip a
    # leading ``./`` and prepend ``/local`` so a contract path of
    # ``./out`` lands at ``/local/out`` inside the destination
    # container.
    raw_path = str(location.get("path") or location.get("uri") or "/data")
    if raw_path.startswith("./"):
        raw_path = raw_path[2:]
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path
    if not raw_path.startswith("/local"):
        raw_path = "/local" + raw_path
    return {"destination_path": raw_path}


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
        facets={"engine": "airbyte", "duration_seconds": time.time() - t_start},
    )


# ── Top-level entry point ──────────────────────────────────────────────


def execute_airbyte_build(
    build: Dict[str, Any],
    contract: Dict[str, Any],
    contract_dir: Path,
    *,
    dry_run: bool = False,
    sample_rows: Optional[int] = None,
    state_root: Optional[Path] = None,
    image_verifier: Optional[ImageSignatureVerifier] = None,
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
    runner = AirbyteRunner(image_verifier=image_verifier)
    if dry_run:
        plan = runner.plan(ctx)
        LOG.info("airbyte.dry-run streams=%s", plan.streams_planned)
        return 0

    try:
        result = runner.run(ctx)
    except Exception as exc:
        # Image-signature / supply-chain failures raise typed errors
        # from inside ``_execute``; the operator's contract is "non-zero
        # exit when the build aborts", not "trace-back surfaced". The
        # CLI top-level handler still renders a Panel for typed
        # ``SupplyChainViolationError`` etc. when the call is from
        # ``fluid apply --mode amend-and-build``; standalone callers of
        # ``execute_airbyte_build`` (tests, other runners) just see a
        # non-zero return code with the error logged at ERROR level.
        from fluid_build.cli._errors import SupplyChainViolationError

        LOG.error(
            "airbyte.run_aborted err=%s exc_type=%s",
            exc,
            type(exc).__name__,
            exc_info=True,
        )
        # Persist a FAILED run record so the audit trail captures the
        # abort even though no streams ran.
        from fluid_build.api.runner import RunResult, RunState

        from .._acquisition_common import utc_now_iso

        ts = utc_now_iso()
        result = RunResult(
            run_id=ctx.run_id,
            state=RunState.FAILED,
            streams=[],
            started_at=ts,
            finished_at=ts,
            records_total=0,
            bytes_total=0,
            dlq_records=0,
            facets={
                "engine": "airbyte",
                "abort_reason": str(exc),
                "abort_type": type(exc).__name__,
            },
        )
        # Re-raise typed supply-chain errors when called from a
        # ``fluid apply`` context (the apply path's exception handler
        # converts them to typed ``CLIError`` events). Standalone
        # callers (tests) with ``raise_supply_chain=False`` get the
        # rc-based contract instead. Default: surface as rc only,
        # matching ``execute_airbyte_build``'s int-returning contract.
        write_run_record_and_finalize(engine="airbyte", ctx=ctx, result=result, state_store=store)
        if isinstance(exc, SupplyChainViolationError):
            return 3  # supply-chain abort exit code
        return 1

    return write_run_record_and_finalize(
        engine="airbyte", ctx=ctx, result=result, state_store=store
    )
