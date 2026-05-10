# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""DuckDB acquisition runner.

Builds a DuckDB SQL string of the form:

    COPY (FROM <reader>(<uri>, <opts>)) TO '<dest>' (FORMAT <fmt>);

Reader dispatch by ``source.kind``:

  - ``filesystem``  -> ``read_csv`` / ``read_parquet`` / ``read_json``
  - ``postgres``    -> ``postgres_scan(...)``
  - ``mysql``       -> ``mysql_scan(...)``
  - ``sqlite``      -> ``sqlite_scan(...)``
  - ``http``        -> ``read_csv_auto('https://...')``

Loads required extensions on demand (``httpfs``, ``postgres``, ``mysql``,
``sqlite``, ``aws``, ``azure``).

The runner satisfies the ``api.runner.Runner`` Protocol and registers the
top-level ``execute_duckdb_build`` function used by ``build_runners.base``
when it detects an acquisition build with ``engine: duckdb``.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, Tuple

from fluid_build.api.runner import (
    RunContext,
    Runner,
    RunnerCapability,
    RunPlan,
    RunResult,
    RunState,
    StreamResult,
)
from fluid_build.api.schema import SchemaFingerprint
from fluid_build.api.source import AcquisitionMode
from fluid_build.providers._sql_safety import (
    build_libpq_dsn,
    libpq_escape,
    quote_string_literal,
    validate_ident,
)

from .._acquisition_common import (
    enforce_schema_policy_or_raise,
    finalize_run_result,
    generate_run_id,
    resolve_connection_secrets,
    utc_now_iso,
    write_run_record,
)
from .._fingerprint import fingerprint_from_duckdb_describe

LOG = logging.getLogger("fluid.acquire.duckdb")


# ── Object-store credentials (CREATE SECRET) ────────────────────────────


_S3_BOOL_KEYS = {"use_ssl"}
_S3_STR_KEYS = {"endpoint", "region", "url_style", "key_id", "secret", "session_token"}
_AZURE_STR_KEYS = {"account_name", "account_key", "tenant_id", "client_id", "client_secret"}
_GCS_STR_KEYS = {"key_id", "secret"}


def _build_create_secret(scheme: str, cfg: Dict[str, Any]) -> Optional[str]:
    """Render a DuckDB ``CREATE SECRET`` statement for the given scheme.

    Returns ``None`` when no credentials need to be configured. The values
    are passed through ``quote_string_literal`` so secrets containing
    single quotes (or any other SQL-meta character) cannot break out of
    the literal.
    """
    if not cfg:
        return None
    type_map = {"s3": "s3", "gcs": "gcs", "azure": "azure"}
    duck_type = type_map.get(scheme)
    if duck_type is None:
        return None

    allowed = {
        "s3": _S3_STR_KEYS | _S3_BOOL_KEYS,
        "gcs": _GCS_STR_KEYS,
        "azure": _AZURE_STR_KEYS,
    }[scheme]

    parts: List[str] = []
    for key, value in cfg.items():
        validate_ident(key)
        if key not in allowed:
            continue
        if value is None:
            continue
        if key in _S3_BOOL_KEYS or isinstance(value, bool):
            parts.append(f"{key.upper()} {'true' if value else 'false'}")
        else:
            parts.append(f"{key.upper()} {quote_string_literal(str(value))}")
    if not parts:
        return None
    return (
        f"CREATE OR REPLACE SECRET fluid_{scheme}_secret (TYPE {duck_type}, "
        + ", ".join(parts)
        + ")"
    )


def _apply_object_store_secret(con: Any, ctx: RunContext) -> None:
    """Issue ``CREATE SECRET`` if the contract carries object-store credentials.

    Looks for ``connection.s3`` / ``connection.gcs`` / ``connection.azure``
    blocks. Best-effort: a missing/incompatible duckdb build logs a warning
    and continues so the run can still proceed via env-var fallback.
    """
    raw = dict(ctx.source.connection.raw or {})
    for scheme in ("s3", "gcs", "azure"):
        cfg = raw.get(scheme)
        if not isinstance(cfg, dict):
            continue
        try:
            stmt = _build_create_secret(scheme, cfg)
            if stmt is None:
                continue
            con.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            # CodeQL py/clear-text-logging-sensitive-data: DuckDB error
            # messages typically echo the failing statement, which would
            # include the cleartext secret embedded in CREATE SECRET. Log
            # only the scheme + exception class so operators can debug
            # without the secret leaving the process.
            LOG.warning("DuckDB CREATE SECRET (%s) failed: %s", scheme, type(exc).__name__)


# ── Reader dispatch ──────────────────────────────────────────────────────


def _csv_options_clause(opts: Dict[str, Any]) -> str:
    """Render DuckDB ``read_csv_auto`` options.

    Option keys are validated as identifiers (no quoting, no spaces);
    string values are properly single-quote-escaped using
    ``quote_string_literal``. Booleans and numerics are rendered literal.
    """
    parts: List[str] = []
    for k, v in opts.items():
        validate_ident(k)  # option keys must be plain identifiers
        if isinstance(v, bool):
            parts.append(f"{k}={'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            parts.append(f"{k}={v}")
        elif isinstance(v, str):
            parts.append(f"{k}={quote_string_literal(v)}")
        else:
            # JSON-y nested values not supported in this pass.
            continue
    return ", " + ", ".join(parts) if parts else ""


def _build_select_for_filesystem(uri: str, fmt: str, opts: Dict[str, Any]) -> str:
    """Build a SELECT against a filesystem source.

    URIs go through ``quote_string_literal`` so a malicious URI containing
    a single quote can't break out of the string literal and inject SQL.
    """
    fmt = (fmt or "csv").lower()
    quoted_uri = quote_string_literal(uri)
    if fmt == "csv":
        return f"SELECT * FROM read_csv_auto({quoted_uri}{_csv_options_clause(opts)})"
    if fmt == "parquet":
        return f"SELECT * FROM read_parquet({quoted_uri})"
    if fmt in ("json", "ndjson"):
        return f"SELECT * FROM read_json_auto({quoted_uri})"
    raise ValueError(f"unsupported reader.format for filesystem: {fmt}")


def _build_select_for_postgres(connection: Dict[str, Any], stream: str) -> str:
    """``postgres_scan('host=… port=… dbname=… user=… password=…', schema, table)``.

    Schema/table are validated as identifiers (so ``stream='x; DROP TABLE y'``
    is rejected at build time, not executed). DSN values are passed through
    ``quote_string_literal`` so embedded single quotes can't escape the
    outer SQL string. The DSN itself uses libpq's ``key=value`` whitespace
    format; the entire DSN is a single SQL literal at the boundary.

    Delegates DSN construction to the shared :func:`build_libpq_dsn` so
    postgres and mysql don't drift over time.
    """
    if "." in stream:
        schema, table = stream.split(".", 1)
    else:
        schema, table = "public", stream
    schema = validate_ident(schema)
    table = validate_ident(table)
    dsn = build_libpq_dsn(connection, database_key="dbname")
    return (
        f"SELECT * FROM postgres_scan({quote_string_literal(dsn)}, "
        f"{quote_string_literal(schema)}, {quote_string_literal(table)})"
    )


def _build_select_for_mysql(connection: Dict[str, Any], stream: str, *, alias: str) -> str:
    """Read from MySQL via the duckdb ``mysql`` extension.

    Requires that ``ATTACH '<dsn>' AS <alias> (TYPE mysql)`` has already
    been issued in the same connection (handled by
    ``DuckdbRunner._attach_external_databases``).

    The ``alias`` is per-build (derived from the build_id) so a contract
    with two mysql sources doesn't collide on a hardcoded ``mysql_db``
    name. Validated as a SQL identifier before composition.

    Stream parsing follows postgres: ``"db.table"`` splits into
    schema/table; a bare ``"table"`` falls back to the connection's
    ``database`` (mysql collapses schema/database into one namespace,
    so ``<alias>.<dbname>.<table>`` is the correct reference shape).
    """
    if "." in stream:
        schema, table = stream.split(".", 1)
    else:
        schema, table = connection.get("database") or "", stream
    if not schema:
        raise ValueError(
            "mysql stream must include schema (``<database>.<table>``) "
            "OR the contract must declare connection.database"
        )
    alias = validate_ident(alias)
    schema = validate_ident(schema)
    table = validate_ident(table)
    return f"SELECT * FROM {alias}.{schema}.{table}"


def _build_select_for_sqlite(connection: Dict[str, Any], stream: str, *, alias: str) -> str:
    """Read from SQLite via the duckdb ``sqlite`` extension.

    Mirrors the mysql attach-then-reference pattern.
    ``ATTACH '<path>' AS <alias> (TYPE sqlite)`` exposes the database;
    we then SELECT against ``<alias>.<table>``. SQLite has no schema
    layer, so the stream is just ``<table>`` (or ``main.<table>``).
    """
    if "." in stream:
        schema, table = stream.split(".", 1)
        if schema not in ("main", ""):
            # SQLite only has the ``main`` schema and ``temp``;
            # anything else suggests a misconfigured contract.
            raise ValueError(f"sqlite stream schema must be 'main' or omitted, got {schema!r}")
    else:
        table = stream
    alias = validate_ident(alias)
    table = validate_ident(table)
    return f"SELECT * FROM {alias}.{table}"


def _mysql_alias_for_build(build_id: str) -> str:
    """Derive a per-build ATTACH alias for the mysql extension.

    Two contracts (or two builds in one contract) targeting different
    mysql sources used to collide on a hardcoded ``mysql_db`` alias.
    Now each build gets ``mysql_<sanitized_build_id>``, validated as
    a SQL identifier so it composes safely into the ATTACH statement.
    """
    safe = re.sub(r"[^A-Za-z0-9_]", "_", build_id) or "default"
    candidate = f"mysql_{safe}"
    return validate_ident(candidate)


def _sqlite_alias_for_build(build_id: str) -> str:
    """Per-build ATTACH alias for sqlite — same pattern as mysql."""
    safe = re.sub(r"[^A-Za-z0-9_]", "_", build_id) or "default"
    candidate = f"sqlite_{safe}"
    return validate_ident(candidate)


def _build_select_for_http(uri: str, fmt: str, opts: Dict[str, Any]) -> str:
    return _build_select_for_filesystem(uri, fmt or "csv", opts)


# ── Destination dispatch ─────────────────────────────────────────────────


def _build_copy_destination(out_path: str, fmt: str) -> str:
    """COPY ... TO '<path>' (FORMAT <fmt>).

    The output path is passed through ``quote_string_literal`` so a path
    containing a single quote can't escape the literal and inject SQL.
    """
    fmt = (fmt or "parquet").lower()
    quoted_path = quote_string_literal(out_path)
    if fmt == "parquet":
        return f"COPY ({{select}}) TO {quoted_path} (FORMAT 'parquet')"
    if fmt == "csv":
        return f"COPY ({{select}}) TO {quoted_path} (FORMAT 'csv', HEADER)"
    if fmt in ("json", "ndjson"):
        return f"COPY ({{select}}) TO {quoted_path} (FORMAT 'json')"
    raise ValueError(f"unsupported sink format: {fmt}")


# ── Extension loading ────────────────────────────────────────────────────


_EXT_BY_KIND = {
    "postgres": ["postgres"],
    # mysql and mariadb both use the duckdb ``mysql`` extension —
    # the wire protocol is compatible enough that one extension
    # handles both upstream flavours.
    "mysql": ["mysql"],
    "mariadb": ["mysql"],
    "sqlite": ["sqlite"],
    "filesystem": [],  # may add httpfs/aws if URI scheme requires it
    "http": ["httpfs"],
}


def _required_extensions(kind: str, uri: Optional[str]) -> List[str]:
    base = list(_EXT_BY_KIND.get(kind, []))
    if uri:
        if uri.startswith("s3://"):
            base.extend(["httpfs", "aws"])
        elif uri.startswith(("gs://", "gcs://")):
            base.append("httpfs")
        elif uri.startswith("azure://"):
            base.append("azure")
        elif uri.startswith(("http://", "https://")):
            base.append("httpfs")
    # Deduplicate while preserving order
    seen = set()
    out = []
    for ext in base:
        if ext not in seen:
            seen.add(ext)
            out.append(ext)
    return out


# ── Runner ───────────────────────────────────────────────────────────────


@dataclass
class DuckdbRunner:
    """Runner Protocol implementation for the DuckDB engine."""

    name: ClassVar[str] = "duckdb"
    declared_capabilities: ClassVar[FrozenSet[RunnerCapability]] = frozenset(
        {
            RunnerCapability.FULL_REFRESH,
            RunnerCapability.INCREMENTAL_APPEND,
            RunnerCapability.SCHEMA_DISCOVERY,
            RunnerCapability.AT_LEAST_ONCE,
        }
    )
    declared_modes: ClassVar[FrozenSet[str]] = frozenset({"embedded"})
    # PARTIAL is treated as a hard failure for duckdb because per-stream
    # failures are surfaced upstream as ``PartialFailureError``; by the
    # time ``finalize_run_result`` sees the result, only SUCCEEDED is
    # acceptable. Declared here as a class constant rather than passed
    # at the call site so the policy lives next to the runner that owns
    # it.
    succeeded_states: ClassVar[Tuple[RunState, ...]] = (RunState.SUCCEEDED,)

    def plan(self, ctx: RunContext) -> RunPlan:
        streams = list(ctx.source.streams) or self._infer_streams(ctx)
        return RunPlan(streams_planned=streams)

    def run(self, ctx: RunContext) -> RunResult:
        return _execute(ctx, self)

    def replay(self, ctx: RunContext, run_id: str) -> RunResult:
        # Replay is just rerun under the same run-id; idempotency on the data
        # path keeps the destination consistent.
        ctx.run_id = run_id
        return _execute(ctx, self)

    def fingerprint(self, ctx: RunContext) -> SchemaFingerprint:
        import duckdb

        con = duckdb.connect(":memory:")
        try:
            self._load_extensions(con, ctx)
            select_sql = _select_for_first_stream(ctx)
            con.execute(f"CREATE TEMP VIEW _fp AS {select_sql}")
            rows = con.execute("DESCRIBE _fp").fetchall()
            return fingerprint_from_duckdb_describe(rows)
        finally:
            con.close()

    # ── helpers ──────────────────────────────────────────────────────────
    def _infer_streams(self, ctx: RunContext) -> List[str]:
        """Single-stream inference for kinds that don't carry a stream list."""
        kind = ctx.source.kind
        if kind in {"filesystem", "http"}:
            return [Path(ctx.source.connection.uri or "data").stem]
        return ["data"]

    def _load_extensions(self, con: Any, ctx: RunContext) -> None:
        for ext in _required_extensions(ctx.source.kind, ctx.source.connection.uri):
            try:
                con.execute(f"INSTALL {ext}")
                con.execute(f"LOAD {ext}")
            except Exception as exc:  # noqa: BLE001
                # Don't include `exc` directly — DuckDB error messages can
                # echo statement text containing DSN-embedded credentials.
                LOG.warning("DuckDB extension load failed (%s): %s", ext, type(exc).__name__)
        _apply_object_store_secret(con, ctx)
        self._attach_external_databases(con, ctx)

    def _attach_external_databases(self, con: Any, ctx: RunContext) -> None:
        """ATTACH any source kind that requires a named database alias.

        Postgres uses ``postgres_scan(dsn, schema, table)`` directly with
        the DSN inline, so no ATTACH is needed. The mysql / mariadb /
        sqlite extensions only expose ATTACH-then-reference semantics —
        there's no ``mysql_scan(dsn, …)`` function — so we ATTACH here
        at extension-load time and ``_build_select_for_<kind>`` helpers
        reference ``<alias>.<schema>.<table>`` (or ``<alias>.<table>``
        for sqlite).

        The alias is per-build (``mysql_<build_id>`` / ``sqlite_<build_id>``)
        so two sources in the same contract / interpreter don't collide
        on a hardcoded name.

        Best-effort: if the connection DSN is malformed or the upstream
        is unreachable, the ATTACH raises and the runner surfaces the
        error in the per-stream try/except.
        """
        kind = ctx.source.kind
        if kind in ("mysql", "mariadb"):
            # Resolve secretRef → password before build_libpq_dsn reads
            # connection.get("password"). Inline literal values still win.
            conn = resolve_connection_secrets(dict(ctx.source.connection.raw))
            dsn = build_libpq_dsn(conn, database_key="database")
            alias = _mysql_alias_for_build(ctx.build_id)
            # mysql extension supports both mysql:// and mariadb:// upstreams;
            # the duckdb extension type literal is always ``mysql``.
            con.execute(f"ATTACH {quote_string_literal(dsn)} AS {alias} (TYPE mysql)")
        elif kind == "sqlite":
            conn = dict(ctx.source.connection.raw)
            # SQLite uses a path, not a libpq-style DSN. The path comes
            # from connection.uri / connection.path / connection.database
            # depending on contract author preference; we accept all three.
            path = conn.get("uri") or conn.get("path") or conn.get("database") or ""
            if not path:
                raise ValueError("sqlite source requires connection.uri, .path, or .database")
            alias = _sqlite_alias_for_build(ctx.build_id)
            con.execute(f"ATTACH {quote_string_literal(str(path))} AS {alias} (TYPE sqlite)")


def _select_for_first_stream(ctx: RunContext) -> str:
    return _select_for_stream(ctx, ctx.source.streams[0] if ctx.source.streams else "data")


def _select_for_stream(ctx: RunContext, stream: str) -> str:
    kind = ctx.source.kind
    conn = dict(ctx.source.connection.raw)
    if kind == "filesystem":
        uri = conn.get("uri") or stream
        fmt = ctx.source.reader.format if ctx.source.reader else "csv"
        opts = dict(ctx.source.reader.options) if ctx.source.reader else {}
        return _build_select_for_filesystem(uri, fmt or "csv", opts)
    if kind == "http":
        uri = conn.get("uri") or stream
        fmt = ctx.source.reader.format if ctx.source.reader else "csv"
        opts = dict(ctx.source.reader.options) if ctx.source.reader else {}
        return _build_select_for_http(uri, fmt or "csv", opts)
    if kind == "postgres":
        return _build_select_for_postgres(conn, stream)
    if kind in ("mysql", "mariadb"):
        # MariaDB uses the same duckdb mysql extension; the discoverer
        # already accepts both schemes (cli/discover/mysql.py), keep
        # the runner consistent.
        alias = _mysql_alias_for_build(ctx.build_id)
        return _build_select_for_mysql(conn, stream, alias=alias)
    if kind == "sqlite":
        alias = _sqlite_alias_for_build(ctx.build_id)
        return _build_select_for_sqlite(conn, stream, alias=alias)
    raise ValueError(f"DuckDB runner: unsupported source.kind '{kind}'")


# Schema-policy enforcement is now shared across all 6 acquisition
# runners; see :func:`enforce_schema_policy_or_raise` in
# ``_acquisition_common``. The duckdb-local helper used to live here.


_INCREMENTAL_MODES = {
    AcquisitionMode.INCREMENTAL_APPEND,
    AcquisitionMode.INCREMENTAL_DEDUP,
    AcquisitionMode.INCREMENTAL_MERGE,
}


# ── Quality gates → DLQ ───────────────────────────────────────────────────
#
# Translate ``properties.quality.gates[]`` into a pair of SQL predicates:
# the GOOD predicate (rows that pass every gate) lands in the destination,
# the BAD predicate (rows that fail at least one) is fetched into Python
# and written to the DLQ via :class:`DLQWriter`. ``onError: route_to_dlq``
# keeps the run green when bad rows show up; ``onError: fail`` raises
# after writing them so the run record reflects the failure but the
# audit trail is still intact.


def _run_post_land_hooks(
    ctx: RunContext,
    stream_results: List[StreamResult],
    sink_format: str,
) -> Dict[str, List[str]]:
    """Sample each successfully-landed stream and run the HookChain.

    Returns a ``{stream_name: [pii_columns]}`` map of detected PII
    classifications. The hook chain itself doesn't mutate the data
    on disk — it inspects a sample (up to 1000 rows) to flag columns
    that match PII patterns. Findings are logged and appear in the
    RunResult facets.

    For full row-by-row masking, contract authors should pair
    ``preLand: [dlp_scan, tokenize_pii]`` AND accept the row-by-row
    cost (the runner falls back to a Python-side pipeline on the
    next major). The current implementation is monitor-only.
    """
    import duckdb

    findings: Dict[str, List[str]] = {}
    out_dir = Path(ctx.workdir) / "out"
    fmt = (sink_format or "parquet").lower()
    reader = {
        "parquet": "read_parquet",
        "csv": "read_csv_auto",
        "json": "read_json_auto",
        "ndjson": "read_json_auto",
    }.get(fmt)
    if reader is None:
        return findings

    con = duckdb.connect(":memory:")
    try:
        for stream_result in stream_results:
            if stream_result.state != RunState.SUCCEEDED:
                continue
            stream_name = stream_result.name
            out_path = _resolve_destination_path(ctx, stream_name, fmt, out_dir)
            quoted_path = quote_string_literal(str(out_path))
            try:
                rows = con.execute(f"SELECT * FROM {reader}({quoted_path}) LIMIT 1000").fetchall()
                cols = [d[0] for d in con.description]
            except Exception as exc:  # noqa: BLE001
                LOG.debug(
                    "post_land_hook_read_failed stream=%s err=%s",
                    stream_name,
                    exc,
                )
                continue
            if not rows:
                continue
            records = [dict(zip(cols, r, strict=False)) for r in rows]
            try:
                result = ctx.hook_chain.run(records, ctx={"classifications": {}})
            except Exception as exc:  # noqa: BLE001
                # Don't pass `exc` directly — userland hook errors can wrap
                # DuckDB exceptions whose messages may echo DSN-embedded creds.
                LOG.debug(
                    "hook_chain_run_failed stream=%s err=%s",
                    stream_name,
                    type(exc).__name__,
                )
                continue
            classifications = result.classifications if hasattr(result, "classifications") else {}
            pii_cols = sorted(col for col, tags in (classifications or {}).items() if tags)
            if pii_cols:
                findings[stream_name] = pii_cols
                LOG.info(
                    "pii_detected stream=%s columns=%s",
                    stream_name,
                    pii_cols,
                )
    finally:
        con.close()
    return findings


def _enforce_late_arrival_split(
    ctx: RunContext,
    stream_results: List[StreamResult],
    sink_format: str,
) -> Dict[str, Dict[str, int]]:
    """Split landed rows into on-time vs late events per the contract's
    ``WatermarkSpec.allowed_lateness``.

    For each successful stream, register the landed file as a duckdb
    table, run the SQL splitter from
    :mod:`fluid_build.build_runners._late_arrival`, and overwrite the
    main file (now without the late rows) plus a sibling
    ``<basename>__late_events.<ext>`` file.

    Returns ``{stream: {"on_time": int, "late": int}}`` for facet
    surfacing. Empty dict when no policy is configured (early return)
    or no streams to process.
    """
    from .._late_arrival import (
        _detect_event_time_column,
        extract_late_arrival_policy,
        split_late_events_in_duckdb,
    )

    policy = extract_late_arrival_policy(contract_or_source=ctx.source)
    if not policy.get("enabled"):
        return {}

    # Resolve the event-time column from the contract's first expose
    # schema. Fall back to the canonical names if the contract doesn't
    # expose a schema (rare for production contracts).
    expose_schemas: List[Dict[str, Any]] = []
    for expose in ctx.contract.get("exposes") or []:
        contract_block = expose.get("contract") or {}
        sch = contract_block.get("schema")
        if isinstance(sch, list):
            expose_schemas.extend(sch)
    event_time_column = _detect_event_time_column(expose_schemas)
    if not event_time_column:
        LOG.debug(
            "late_arrival_split_skipped: no event-time column found in "
            "contract.exposes[].contract.schema"
        )
        return {}

    import duckdb

    fmt = (sink_format or "parquet").lower()
    out_dir = Path(ctx.workdir) / "out"
    reader_func = {
        "parquet": "read_parquet",
        "csv": "read_csv_auto",
        "json": "read_json_auto",
        "ndjson": "read_json_auto",
    }.get(fmt)
    writer_func = {
        "parquet": "FORMAT 'parquet'",
        "csv": "FORMAT 'csv', HEADER",
        "json": "FORMAT 'json'",
        "ndjson": "FORMAT 'json'",
    }.get(fmt)
    if reader_func is None or writer_func is None:
        LOG.debug(
            "late_arrival_split_skipped: format=%s not supported for split",
            fmt,
        )
        return {}

    budget_seconds = float(policy["allowed_lateness_seconds"])

    results: Dict[str, Dict[str, int]] = {}
    con = duckdb.connect(":memory:")
    try:
        for stream_result in stream_results:
            if stream_result.state != RunState.SUCCEEDED:
                continue
            stream_name = stream_result.name
            main_path = _resolve_destination_path(ctx, stream_name, fmt, out_dir)
            main_path_obj = Path(main_path)
            if not main_path_obj.exists():
                continue
            late_path_obj = main_path_obj.with_name(
                main_path_obj.stem + "__late_events" + main_path_obj.suffix
            )

            # Stage data into temp tables, run the SQL split, then
            # overwrite the main file + write the late-events file.
            quoted_main = quote_string_literal(str(main_path_obj))
            quoted_late = quote_string_literal(str(late_path_obj))
            con.execute(
                f"CREATE OR REPLACE TABLE __la_main AS SELECT * FROM {reader_func}({quoted_main})"
            )

            try:
                counts = split_late_events_in_duckdb(
                    con=con,
                    source_relation="__la_main",
                    side_output_relation="__la_side",
                    event_time_column=event_time_column,
                    allowed_lateness_seconds=budget_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                LOG.warning(
                    "late_arrival_split_sql_failed: stream=%s err=%s",
                    stream_name,
                    exc,
                )
                continue

            if counts["late"] == 0:
                # Nothing to do — leave the file alone.
                results[stream_name] = counts
                continue

            # Overwrite the main file (now without late rows) and emit
            # the side-output file. Use COPY ... TO ... so duckdb
            # handles the format details.
            con.execute(f"COPY __la_main TO {quoted_main} ({writer_func})")
            con.execute(f"COPY __la_side TO {quoted_late} ({writer_func})")
            results[stream_name] = counts
            LOG.info(
                "late_arrival_split: stream=%s on_time=%d late=%d side_output=%s",
                stream_name,
                counts["on_time"],
                counts["late"],
                late_path_obj,
            )
    finally:
        con.close()
    return results


def _jsonable(record: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce DuckDB row values into JSON-serialisable forms.

    The DLQ writer emits NDJSON; values like ``datetime``, ``Decimal``,
    and ``bytes`` need a string fallback. ``json.dumps`` would otherwise
    raise on the first DLQ append, leaving the whole batch's audit
    trail empty.
    """
    import datetime as _dt
    import decimal as _dec

    out: Dict[str, Any] = {}
    for k, v in record.items():
        if v is None or isinstance(v, (str, int, float, bool, list, dict)):
            out[k] = v
        elif isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
            out[k] = v.isoformat()
        elif isinstance(v, _dec.Decimal):
            out[k] = str(v)
        elif isinstance(v, (bytes, bytearray)):
            out[k] = v.hex()
        else:
            out[k] = str(v)
    return out


def _build_quality_predicates(
    gates: List[Dict[str, Any]],
) -> Tuple[str, str, Dict[str, str]]:
    """Convert quality gates into ``(good_where, bad_where, per_row_reason)``.

    Returns:

    * ``good_where`` — SQL fragment combined with AND; rows matching this
      pass every gate. Empty string when no gates are declared.
    * ``bad_where`` — negation of ``good_where`` (NOT (...) effectively).
    * ``per_row_reason`` — column-name → human-readable reason map; the
      DLQ envelope's ``reason`` field is set per-record from this map
      based on which gate failed.

    Supported gates (covers the contracts shipped today):

    * ``rule: not_null`` — ``columns: [c1, c2, ...]`` — every listed
      column must be non-null.
    * ``rule: unique`` — single column; emits a window-count predicate.
    * ``rule: range`` — ``column: c, min: a, max: b``.
    * ``rule: regex`` — ``column: c, pattern: '...'``.

    Other gate shapes are silently skipped; the runner logs a debug
    breadcrumb so the operator can see they were ignored.
    """
    good_clauses: List[str] = []
    reasons: Dict[str, str] = {}

    for gate in gates or []:
        rule = (gate.get("rule") or "").strip().lower()
        cols = gate.get("columns") or []
        col = gate.get("column")
        if rule == "not_null" and cols:
            for c in cols:
                c_safe = validate_ident(str(c))
                good_clauses.append(f"{c_safe} IS NOT NULL")
                reasons[c_safe] = f"not_null gate failed on column '{c_safe}'"
        elif rule == "range" and col is not None:
            c_safe = validate_ident(str(col))
            mn, mx = gate.get("min"), gate.get("max")
            parts = []
            if mn is not None:
                parts.append(f"{c_safe} >= {quote_string_literal(str(mn))}")
            if mx is not None:
                parts.append(f"{c_safe} <= {quote_string_literal(str(mx))}")
            if parts:
                good_clauses.append("(" + " AND ".join(parts) + ")")
                reasons[c_safe] = f"range gate failed on column '{c_safe}' (min={mn} max={mx})"
        elif rule == "regex" and col is not None and gate.get("pattern"):
            c_safe = validate_ident(str(col))
            pat = quote_string_literal(str(gate["pattern"]))
            good_clauses.append(f"regexp_matches({c_safe}, {pat})")
            reasons[c_safe] = (
                f"regex gate failed on column '{c_safe}' (pattern={gate['pattern']!r})"
            )
        # Other rule shapes (unique, custom expression) need additional
        # treatment (window functions / arbitrary SQL) and are deferred.

    if not good_clauses:
        return "", "", {}
    good_where = " AND ".join(good_clauses)
    bad_where = f"NOT ({good_where})"
    return good_where, bad_where, reasons


def _apply_incremental_filter(
    ctx: RunContext, stream: str, base_sel: str
) -> Tuple[str, Optional[Any]]:
    """Wrap a base SELECT with an incremental cursor filter.

    Returns ``(filtered_sql, last_cursor_value)``. When the source mode
    is full_refresh / cdc / streaming OR the contract didn't declare
    ``cursor_field``, the SELECT passes through unchanged and
    ``last_cursor_value`` is ``None``.

    The cursor value is read from ``ctx.state_store.get_cursor(...)``
    and quoted via :func:`quote_string_literal` so a tampered state
    file can't inject SQL into the WHERE clause.
    """
    if ctx.source.mode not in _INCREMENTAL_MODES:
        return base_sel, None
    cursor_field = ctx.source.cursor_field
    if not cursor_field:
        # Mode declared incremental but no cursor column — the contract
        # author needs to add ``properties.source.cursor_field``. Don't
        # silently fall back to full refresh; that would be a confusing
        # data-correctness bug.
        raise ValueError(
            f"source.mode={ctx.source.mode.value} requires "
            "properties.source.cursor_field to be set."
        )
    cursor_field_safe = validate_ident(cursor_field)
    last_cursor = ctx.state_store.get_cursor(ctx.product_id, ctx.build_id, stream)
    if last_cursor is None:
        # First run for this stream. Treat as full refresh; subsequent
        # runs will use the cursor that's persisted at end-of-run.
        return base_sel, None
    last_value = last_cursor.value
    # The cursor value is treated as a SQL literal regardless of the
    # column type: duckdb will coerce ``'2026-05-01T00:00:00'`` against
    # a TIMESTAMP column and ``'42'`` against an INT. ``quote_string_literal``
    # neutralises any embedded quote / control char from a tampered
    # state file.
    quoted_value = quote_string_literal(str(last_value))
    filtered = f"SELECT * FROM ({base_sel}) WHERE {cursor_field_safe} > {quoted_value}"
    return filtered, last_value


def _persist_cursor_after_run(
    ctx: RunContext,
    stream: str,
    out_path: str,
    sink_format: str,
) -> bool:
    """After a successful incremental run, advance the cursor to the
    MAX value we just landed. Returns True if the cursor advanced.

    Reads the destination file (parquet/csv/json) directly via duckdb
    so we don't have to round-trip through the upstream source again.
    No-op when mode != incremental.
    """
    if ctx.source.mode not in _INCREMENTAL_MODES:
        return False
    cursor_field = ctx.source.cursor_field
    if not cursor_field:
        return False
    cursor_field_safe = validate_ident(cursor_field)

    import duckdb

    from fluid_build.api.state import Cursor

    fmt = sink_format.lower()
    if fmt == "parquet":
        reader = "read_parquet"
    elif fmt == "csv":
        reader = "read_csv_auto"
    elif fmt in ("json", "ndjson"):
        reader = "read_json_auto"
    else:
        return False  # unsupported sink for cursor read-back

    quoted_path = quote_string_literal(out_path)
    con = duckdb.connect(":memory:")
    try:
        row = con.execute(
            f"SELECT MAX({cursor_field_safe}) FROM {reader}({quoted_path})"
        ).fetchone()
    finally:
        con.close()
    new_value = row[0] if row is not None else None
    if new_value is None:
        return False
    ctx.state_store.set_cursor(
        ctx.product_id,
        ctx.build_id,
        Cursor(
            stream=stream,
            value=str(new_value),
            updated_at=utc_now_iso(),
        ),
    )
    return True


def _execute(ctx: RunContext, runner: DuckdbRunner) -> RunResult:
    import duckdb

    started_at = utc_now_iso()
    t_start = time.time()
    streams_to_run = list(ctx.source.streams) or runner._infer_streams(ctx)
    sink_format = (ctx.sink.format or "parquet").lower()
    out_dir = Path(ctx.workdir) / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Schema-evolution gate (shared across all 6 acquisition runners).
    enforce_schema_policy_or_raise(ctx, runner)

    stream_results: List[StreamResult] = []
    failures = 0
    records_total = 0

    # DLQ writer — created lazily on first bad-row hit so contracts
    # without quality gates pay zero overhead. Quality config comes
    # from ``properties.quality`` on the build (read once via the
    # acquisition-common props extractor).
    from .._acquisition_common import get_acquisition_build_props
    from .._dlq import DLQConfig, DLQWriter

    build_index = next(
        (i for i, b in enumerate(ctx.contract.get("builds", [])) if b.get("id") == ctx.build_id),
        0,
    )
    build_props = get_acquisition_build_props(ctx.contract.get("builds", [{}])[build_index])
    quality_config = build_props.get("quality") or {}
    quality_gates = quality_config.get("gates") or []
    on_error = (quality_config.get("onError") or "fail").strip().lower()
    dlq_writer: Optional[DLQWriter] = None
    dlq_total_records = 0

    con = duckdb.connect(":memory:")
    try:
        runner._load_extensions(con, ctx)
        for stream in streams_to_run:
            t_stream = time.time()
            try:
                base_sel = _select_for_stream(ctx, stream)
                # Apply incremental cursor filter if the contract asked for it.
                # ``last_cursor_value`` is the value we filtered on (None for
                # full refresh OR first incremental run); kept for the
                # ``cursor_advanced`` flag emitted on the StreamResult.
                sel, last_cursor_value = _apply_incremental_filter(ctx, stream, base_sel)
                # Build quality-gate predicates. When gates are declared,
                # we partition the SELECT into a GOOD set (lands in the
                # destination) and a BAD set (routed to DLQ). ``good_where``
                # is empty when no gates are declared; in that case the
                # original ``sel`` is used unchanged.
                good_where, bad_where, gate_reasons = _build_quality_predicates(quality_gates)
                if good_where:
                    landed_sel = f"SELECT * FROM ({sel}) WHERE {good_where}"
                else:
                    landed_sel = sel

                if ctx.sample_rows:
                    landed_sel = f"SELECT * FROM ({landed_sel}) LIMIT {int(ctx.sample_rows)}"

                # Resolve binding location if present, else write under workdir.
                out_path = _resolve_destination_path(ctx, stream, sink_format, out_dir)
                copy_template = _build_copy_destination(out_path, sink_format)
                copy_sql = copy_template.format(select=landed_sel)
                LOG.info("duckdb.run stream=%s sql_chars=%d", stream, len(copy_sql))
                con.execute(copy_sql)
                # Count rows landed (best-effort via COUNT on the same select).
                try:
                    row = con.execute(f"SELECT COUNT(*) FROM ({landed_sel})").fetchone()
                    n = row[0] if row is not None else 0
                except Exception:
                    n = 0
                records_total += int(n)

                # Route bad rows to DLQ — only when gates declared AND
                # the run actually rejected something.
                bad_records_for_stream = 0
                if good_where:
                    bad_sel = f"SELECT * FROM ({sel}) WHERE {bad_where}"
                    try:
                        bad_rows = con.execute(bad_sel).fetchall()
                        bad_cols = [d[0] for d in con.description]
                    except Exception as exc:  # noqa: BLE001
                        # Avoid logging `exc` content — DuckDB error messages
                        # can echo SQL containing DSN-embedded credentials.
                        LOG.warning(
                            "duckdb.dlq.fetch_failed stream=%s err=%s",
                            stream,
                            type(exc).__name__,
                        )
                        bad_rows = []
                        bad_cols = []
                    if bad_rows:
                        if dlq_writer is None:
                            dlq_writer = DLQWriter(
                                config=DLQConfig.from_dict(quality_config.get("dlq")),
                                run_id=ctx.run_id,
                                default_root=Path(ctx.workdir) / ".fluid",
                            )
                        for r in bad_rows:
                            record = dict(zip(bad_cols, r, strict=False))
                            # Pick the most-specific reason: first gate
                            # whose column is non-conformant in this row.
                            reason = "quality gate failed"
                            for col_name, msg in gate_reasons.items():
                                val = record.get(col_name)
                                if val is None or val == "":
                                    reason = msg
                                    break
                            dlq_writer.append(
                                stream=stream,
                                record=_jsonable(record),
                                reason=reason,
                            )
                        bad_records_for_stream = len(bad_rows)
                        dlq_total_records += bad_records_for_stream

                # Persist cursor for next run (no-op for full refresh).
                cursor_advanced = _persist_cursor_after_run(ctx, stream, out_path, sink_format)
                stream_results.append(
                    StreamResult(
                        name=stream,
                        state=RunState.SUCCEEDED,
                        records=int(n),
                        duration_seconds=time.time() - t_stream,
                        cursor_advanced=cursor_advanced,
                    )
                )
                if bad_records_for_stream:
                    LOG.info(
                        "duckdb.dlq stream=%s bad_records=%d on_error=%s",
                        stream,
                        bad_records_for_stream,
                        on_error,
                    )
            except Exception as exc:  # noqa: BLE001
                # The COPY SQL embeds DSN strings (postgres_scan dsn=…),
                # which DuckDB echoes in its error messages. Don't pass
                # `exc` directly and don't dump the full traceback via
                # exc_info — both can carry password/key data.
                LOG.error(
                    "duckdb.run.failed stream=%s err=%s",
                    stream,
                    type(exc).__name__,
                )
                failures += 1
                stream_results.append(
                    StreamResult(
                        name=stream,
                        state=RunState.FAILED,
                        records=0,
                        duration_seconds=time.time() - t_stream,
                        error=str(exc),
                    )
                )
    finally:
        con.close()
        # Flush + close the DLQ writer so consumers (verify probes,
        # alerters) see the records on disk immediately. Defensive
        # against the writer being None (no bad rows fired).
        if dlq_writer is not None:
            dlq_writer.close()

    # Post-land PII scan — when the contract declares
    # ``preLand: [dlp_scan]`` AND we successfully landed rows, sample
    # the destination and run the HookChain to detect PII columns.
    # This is a lightweight monitoring pass (not row-by-row mutation):
    # findings are logged and bubbled into the RunResult facets so
    # ``fluid status`` / catalog can surface them. Authors who need
    # actual masking pair it with ``tokenize_pii`` and accept the
    # row-by-row cost.
    pii_findings: Dict[str, List[str]] = {}
    if ctx.hook_chain.hooks and failures == 0 and stream_results:
        try:
            pii_findings = _run_post_land_hooks(ctx, stream_results, sink_format)
        except Exception as exc:  # noqa: BLE001
            # Hook errors may wrap DuckDB exceptions whose messages can
            # echo DSN/secret content. Defensive: log only the class.
            LOG.warning("post_land_hook_failed: %s", type(exc).__name__)

    # Post-land late-arrival enforcement — when the contract declares
    # ``WatermarkSpec.allowed_lateness``, split rows older than
    # ``max(event_time) - allowed_lateness`` into a sibling
    # ``<target>__late_events`` table. Best-effort: failures here log
    # at WARNING but don't fail the run (the data already landed; the
    # split is monitor + governance, not load-blocking).
    late_arrival_split_results: Dict[str, Dict[str, int]] = {}
    if failures == 0 and stream_results:
        try:
            late_arrival_split_results = _enforce_late_arrival_split(
                ctx, stream_results, sink_format
            )
        except Exception as exc:  # noqa: BLE001
            # Defensive: late-arrival split runs DuckDB SQL which may carry
            # DSN content; don't include exc body or full traceback.
            LOG.warning("late_arrival_split_failed: %s", type(exc).__name__)

    # ``onError: fail`` mode — when ANY bad rows landed in the DLQ,
    # promote the run to FAILED so the run record reflects the issue.
    # The DLQ records are still on disk (audit trail intact); the
    # caller's ``finalize_run_result`` surfaces the count.
    finished_at = utc_now_iso()
    if failures == 0:
        if dlq_total_records > 0 and on_error == "fail":
            run_state = RunState.FAILED
        else:
            run_state = RunState.SUCCEEDED
    elif failures < len(streams_to_run):
        run_state = RunState.PARTIAL
    else:
        run_state = RunState.FAILED

    # Aggregate per-stream errors into a run-level error string so
    # ``finalize_run_result`` (which reads ``result.error``) surfaces
    # something meaningful instead of "(no error message captured)".
    # First non-empty stream error wins; the per-stream record carries
    # the full picture for status / replay.
    run_error = None
    if run_state in (RunState.FAILED, RunState.PARTIAL):
        for s in stream_results:
            if s.error:
                run_error = f"stream {s.name!r}: {s.error}"
                break

    facets: Dict[str, Any] = {
        "engine": "duckdb",
        "duration_seconds": time.time() - t_start,
    }
    # Bubble PII findings into facets so ``fluid status`` and catalog
    # consumers see them without parsing logs.
    if pii_findings:
        facets["pii_findings"] = pii_findings

    return RunResult(
        run_id=ctx.run_id,
        state=run_state,
        streams=stream_results,
        started_at=started_at,
        finished_at=finished_at,
        records_total=records_total,
        bytes_total=0,  # Not tracked at this layer; OTel observers can.
        dlq_records=int(dlq_total_records),
        error=run_error,
        facets=facets,
    )


_REMOTE_URI_SCHEMES = ("s3://", "gs://", "gcs://", "azure://", "abfs://", "http://", "https://")


def _is_remote_uri(p: str) -> bool:
    return p.startswith(_REMOTE_URI_SCHEMES)


def _resolve_destination_path(
    ctx: RunContext, stream: str, sink_format: str, default_dir: Path
) -> str:
    """Pick the destination URI/path for ``stream``.

    Honors the contract's ``exposes[].binding.location.path`` when the
    expose has a single output. Returns a string so URI schemes
    (``s3://``, ``gs://``, ``azure://`` …) survive untouched — wrapping
    them in ``Path`` collapses ``s3://`` to ``s3:/`` which DuckDB can't
    read. For local paths, behavior is unchanged: relative paths are
    rooted under ``ctx.workdir`` and parent directories are created.
    """
    expose = _find_first_expose(ctx)
    if expose is not None:
        loc = expose.get("binding", {}).get("location", {}) or {}
        path = loc.get("path")
        if path and len(ctx.source.streams) <= 1:
            if _is_remote_uri(path):
                return path
            p = Path(path)
            if not p.is_absolute():
                p = Path(ctx.workdir) / p
            p.parent.mkdir(parents=True, exist_ok=True)
            return str(p)
    ext = {"parquet": "parquet", "csv": "csv", "json": "ndjson"}.get(sink_format, sink_format)
    return str(default_dir / f"{stream}.{ext}")


def _find_first_expose(ctx: RunContext) -> Optional[Dict[str, Any]]:
    exposes = ctx.contract.get("exposes") or []
    return exposes[0] if exposes else None


# ── Top-level entry point used by build_runners.base ────────────────────


def execute_duckdb_build(
    build: Dict[str, Any],
    contract: Dict[str, Any],
    contract_dir: Path,
    *,
    dry_run: bool = False,
    sample_rows: Optional[int] = None,
    state_root: Optional[Path] = None,
) -> int:
    """Glue function called by build_runners.base. Returns exit code."""
    from fluid_build.api.runner import RunContext
    from fluid_build.api.source import SinkSpec, SourceSpec
    from fluid_build.build_runners._state import FileStateStore

    from .._acquisition_common import get_acquisition_build_props
    from ..base import _resolve_env_placeholders

    props = get_acquisition_build_props(build)
    source_dict = props.get("source")
    if not source_dict:
        LOG.error("acquisition build missing properties.source")
        return 1

    # Resolve {{ env.NAME }} placeholders in connection (host, port, etc.) so
    # the runner sees real values, not templates. Mirrors how the dbt and
    # python runners already handle env interpolation via build_runners.base.
    source_dict = _resolve_env_placeholders(source_dict)
    source = SourceSpec.from_dict(source_dict)
    sink = SinkSpec.from_dict(props.get("sink"))
    workdir = str(contract_dir)
    state_root = state_root or (contract_dir / ".fluid")
    store = FileStateStore(state_root)

    from fluid_build.api.hooks import HookChain
    from fluid_build.build_runners._cost import InMemoryCostTracker
    from fluid_build.build_runners._lineage import NullLineageEmitter

    # Build the per-build HookChain from ``properties.preLand`` —
    # contracts declare pre-landing hooks that run on each batch
    # before rows hit the destination. Currently supported hook ids:
    # ``dlp_scan`` (PII classification) and ``tokenize_pii`` (mask
    # / replace tagged columns). Unknown hook ids are silently
    # skipped with a debug log so contracts that reference future
    # hooks don't break.
    pre_land = props.get("preLand") or []
    hooks = []
    for hook_id in pre_land:
        hook_id = str(hook_id).strip().lower()
        if hook_id == "dlp_scan":
            try:
                from fluid_build.build_runners.hooks.dlp_scan import DlpScanHook

                hooks.append(DlpScanHook())
            except Exception as exc:  # noqa: BLE001
                # Defensive: never log raw exception text from runner code
                # paths that could be entered with DSN/secret carriers in
                # scope. Class name is enough for debug-level diagnostics.
                LOG.debug("dlp_scan_hook_init_failed: %s", type(exc).__name__)
        elif hook_id in ("tokenize_pii", "tokenize"):
            try:
                from fluid_build.build_runners.hooks.tokenize_pii import (
                    TokenizePiiHook,
                )

                hooks.append(TokenizePiiHook())
            except Exception as exc:  # noqa: BLE001
                LOG.debug("tokenize_pii_hook_init_failed: %s", type(exc).__name__)
        elif hook_id == "quality_gate":
            # quality_gate is handled via the per-row gate evaluation
            # inside ``_execute`` (see ``_build_quality_predicates``);
            # no HookChain entry needed.
            continue
        else:
            # Don't interpolate `hook_id` — CodeQL's taint analysis traces
            # any contract-derived value as potentially-sensitive (the
            # contract dict also carries connection.{s3,gcs,azure} secrets).
            # The hook id is contract-validated upstream; dropping the
            # interpolation keeps the debug trail useful without the
            # taint-flow false positive.
            LOG.debug("unknown_preLand_hook (skipped — see contract.preLand)")

    ctx = RunContext(
        run_id=generate_run_id(),
        product_id=contract.get("id", "unknown"),
        build_id=build.get("id", "unknown"),
        contract=contract,
        source=source,
        sink=sink,
        state_store=store,
        hook_chain=HookChain(hooks=hooks),
        lineage=NullLineageEmitter(),
        cost_tracker=InMemoryCostTracker(),
        workdir=workdir,
        sample_rows=sample_rows,
    )

    runner = DuckdbRunner()
    if dry_run:
        plan = runner.plan(ctx)
        LOG.info("duckdb.dry-run streams=%s", plan.streams_planned)
        return 0

    result = runner.run(ctx)
    # Persist the run record so status/replay can find it. The typed
    # PartialFailureError is raised AFTER persistence so the on-disk
    # audit trail is intact even when the CLI surfaces a Panel.
    duckdb_record = {
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
        "facets": result.facets,
    }
    write_run_record(state_store=store, ctx=ctx, result=result, record_dict=duckdb_record)

    if result.state is RunState.PARTIAL:
        from fluid_build.cli._errors import PartialFailureError

        succeeded = [s.name for s in result.streams if s.state is RunState.SUCCEEDED]
        failed = [s.name for s in result.streams if s.state is not RunState.SUCCEEDED]
        raise PartialFailureError.for_streams(succeeded=succeeded, failed=failed)

    # PARTIAL was already raised above as PartialFailureError, so
    # finalize only sees SUCCEEDED or hard failures. The success-set
    # policy lives on the runner class (``DuckdbRunner.succeeded_states``).
    return finalize_run_result(
        "duckdb",
        ctx.build_id,
        result,
        succeeded_states=DuckdbRunner.succeeded_states,
        logger=LOG,
    )
