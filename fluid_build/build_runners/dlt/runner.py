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
from typing import (
    Any,
    ClassVar,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)
from urllib.parse import urlsplit

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
    from .destinations import undo_bridge_writes

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
    # The environment before the bridge copies anything into dlt's names: a
    # dlt variable set there is the operator's or the project's, never the
    # bridge's copy of a FLUID one (_project_endpoints_for_destination). An
    # earlier build in this process (fluid apply runs its builds in a loop and
    # goes on after a failed one) left its copies in place, for dlt to read
    # until that build was done; they go back first, so every build starts
    # from the environment as the operator left it.
    undo_bridge_writes()
    environ_before_bridge = dict(os.environ)
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
        endpoints = _project_endpoints_for_destination(
            dest_name, pipeline_name, binding, environ_before_bridge
        )
        if endpoints:
            try:
                destination = _destination_with_project_credentials(
                    dest_name, binding, pipeline_name, endpoints
                )
            except Exception as exc:  # noqa: BLE001 — any failure here must stop the run
                LOG.error("dlt.destination.pinned.failed err=%s", exc)
                return _failed_result(ctx, started_at, str(exc), t_start)
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


# The FLUID services whose endpoint variables the dlt bridge (destinations.py,
# through _credentials.py) copies into a destination's credentials:
# SNOWFLAKE_ACCOUNT, POSTGRES_HOST / PG_HOST, REDSHIFT_HOST.
_BRIDGED_SERVICES = {
    "snowflake": "Snowflake",
    "postgres": "PostgreSQL",
    "redshift": "Amazon Redshift",
}

# The dlt destination types that connect through libpq (psycopg2), and the
# connection parameters of the connection string dlt hands it: its credentials
# require a host, a user name, a password and a database, and have a port and a
# connect timeout by default (destinations/impl/postgres/sql_client.py,
# ``Psycopg2SqlClient.open_connection``, which the redshift client inherits;
# dlt 1.31.0, checked against dlt and psycopg2 in
# tests/test_dotenv_dlt_destination.py).
_LIBPQ_DESTINATIONS = frozenset({"postgres", "redshift"})
_DLT_LIBPQ_PARAMETERS = frozenset({"host", "port", "user", "password", "dbname", "connect_timeout"})

# libpq's variables that choose where it connects, with the parameter each
# gives. libpq takes a parameter from its variable only where the connection
# string leaves it out
# (https://www.postgresql.org/docs/current/libpq-envars.html), and given
# ``hostaddr`` it connects to that address whatever ``host`` says
# (https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-CONNECT-HOSTADDR).
# So on dlt's connection a project's PGHOST and PGPORT change nothing and a
# project's PGHOSTADDR would move the connection, the operator's password with
# it. No project file can set PGHOSTADDR (protected_env.PROTECTED_ENV_KEYS);
# should one ever load, the runner still pins the destination to the project's
# values. libpq's other variables that could change where the password goes or
# what is sent are set by dlt's connection string (PGUSER, PGPASSWORD,
# PGDATABASE), read only without a password in it (PGPASSFILE), or refused in a
# project file outright (PGSERVICE, PGSERVICEFILE, PGSYSCONFDIR, PGSSLMODE,
# PGSSLROOTCERT ...).
_LIBPQ_ENDPOINTS = {"PGHOST": "host", "PGHOSTADDR": "hostaddr", "PGPORT": "port"}

# Default-deny for a dlt destination's own credentials. dlt layers every field
# of a destination's credentials it finds in the environment on top of the
# operator's (dlt/common/configuration/resolve.py: the environment provider
# comes first, and each field resolves on its own), so a project that set one
# could move or reshape the operator's connection with the operator's password
# still on it: the postgres / redshift ``query`` map gives libpq ``hostaddr``
# (https://www.postgresql.org/docs/current/libpq-connect.html), the ducklake
# ``catalog`` connection is a libpq URL of its own, an ``endpoint_url`` takes
# the operator's object-store keys elsewhere, a ``password`` or ``username``
# chooses the credential. Enumerating the fields that can do that missed nested
# configurations (round 16), so every name dlt reads under a destination's
# credentials — every field, at any depth, and every lookup spelling of it
# (:func:`_dlt_credentials_names`) — is an endpoint: a project dotenv file
# that sets one pins the destination to the project's own credentials, or the
# run fails closed. When dlt's spec cannot be introspected the destination's
# credentials sections pin whole.
#
# The exceptions: the fields below, per dlt credentials class (an exact class,
# a subclass inherits nothing), each a top-level field holding one value. They
# select inside the operator's own account or tune its session, and cannot move
# the connection or choose a credential; each was checked against dlt 1.31.0
# (tests/test_dotenv_dlt_default_deny.py):
#   PostgreSQL, Redshift  database, connect_timeout, client_encoding,
#                         session_timezone. dlt hands libpq a URL
#                         (``to_native_representation``) plus ``options``;
#                         libpq's own parser (psycopg2 ``parse_dsn``) shows a
#                         value in one of them either stays in its own
#                         parameter or makes libpq refuse the string, never
#                         sets the host, address, port, user, password, TLS
#                         files or service.
#   Snowflake             database, warehouse, role. dlt passes each to the
#                         connector as its own keyword argument
#                         (``SnowflakeCredentials.to_connector_params``), never
#                         into the account or host the connector derives its
#                         address from.
# Every other field of every destination pins, ``database`` included wherever
# it was not checked or is not plain data: Fabric joins it into its ODBC
# connection string unescaped (MSSQL / Synapse escape it), DuckDB reads it as a
# file path, MotherDuck as an ``md:`` URL, SQLAlchemy as its dialect does.
_DLT_IN_ACCOUNT_FIELDS: Dict[str, FrozenSet[str]] = {
    "dlt.destinations.impl.postgres.configuration.PostgresCredentials": frozenset(
        {"database", "connect_timeout", "client_encoding", "session_timezone"}
    ),
    "dlt.destinations.impl.redshift.configuration.RedshiftCredentials": frozenset(
        {"database", "connect_timeout", "client_encoding", "session_timezone"}
    ),
    "dlt.destinations.impl.snowflake.configuration.SnowflakeCredentials": frozenset(
        {"database", "warehouse", "role"}
    ),
}

# How deep the spec walk follows configurations nested in the credentials; a
# deeper one pins whole (dlt 1.31 nests at most three: ducklake's storage
# credentials).
_DLT_MAX_NESTING = 6

# The sections dlt's environment provider reads a destination's credentials
# under, most specific first, each tried first behind the pipeline name
# (dlt/common/configuration/resolve.py, _build_section_lookup_paths and
# _resolve_single_value; providers/environ.py joins the pipeline name, the
# sections and the field with ``__`` and upper-cases the result).
_DLT_CREDENTIAL_SECTIONS = (
    "DESTINATION__{name}__CREDENTIALS",
    "DESTINATION__CREDENTIALS",
    "CREDENTIALS",
)


def _dlt_destination(destination: str) -> Tuple[str, str]:
    """The section dlt reads the contract's destination ``destination``'s
    configuration under, and the short name of its type, as ``dlt.pipeline``
    resolves it (``Destination.from_reference``): ``snowflake`` and
    ``dlt.destinations.snowflake`` are both ``("snowflake", "snowflake")``, and
    a named destination ``my_wh`` whose ``DESTINATION__MY_WH__DESTINATION_TYPE``
    is ``snowflake`` is ``("my_wh", "snowflake")``
    (https://dlthub.com/docs/general-usage/destination#configure-multiple-destinations-of-the-same-type).
    A reference dlt cannot resolve is returned as it is: ``dlt.pipeline`` then
    fails on it before it reads any credentials."""
    try:
        from dlt.common.destination import Destination

        reference = Destination.from_reference(destination)
    except Exception:  # noqa: BLE001 — dlt missing, or a reference it cannot resolve
        return destination, destination
    return reference.destination_name, reference.destination_type.rsplit(".", 1)[-1]


def _dlt_names(destination: str, pipeline_name: str, field: Optional[str]) -> List[str]:
    """The variables dlt reads ``field`` of ``destination``'s credentials from,
    in its order; with ``field`` None, the whole credentials (a connection
    string)."""
    sections = [section.format(name=destination.upper()) for section in _DLT_CREDENTIAL_SECTIONS]
    prefixes = [f"{pipeline_name.upper()}__", ""] if pipeline_name else [""]
    suffix = f"__{field.upper()}" if field else ""
    return [prefix + section + suffix for prefix in prefixes for section in sections]


def _dlt_config_classes(hint: Any) -> List[Any]:
    """The dlt configuration classes ``hint`` names: itself, or each member of
    a union (``Optional``, ``Final`` and ``Annotated`` unwrapped), as
    ``typing.get_args`` gives them."""
    import typing

    from dlt.common.configuration.specs.base_configuration import BaseConfiguration

    found: List[Any] = []
    pending = [hint]
    while pending:
        current = pending.pop()
        if isinstance(current, type) and issubclass(current, BaseConfiguration):
            if current not in found:
                found.append(current)
        else:
            pending.extend(typing.get_args(current))
    return found


def _dlt_credentials_spec(
    kind: str,
) -> Tuple[List[Tuple[Tuple[str, ...], bool]], List[Tuple[str, ...]], List[Tuple[str, ...]]]:
    """dlt's credentials of the destination type ``kind``, walked:
    ``(fields, configurations, opaque)``.

    ``fields`` are the resolvable fields at any depth, each as its path from
    the credentials and whether :data:`_DLT_IN_ACCOUNT_FIELDS` allows it;
    ``configurations`` the nested configurations' paths; ``opaque`` those
    (``()`` for the credentials themselves) whose fields cannot be listed in
    advance: dlt picks the class at run time (a hint resolver, such as lance's
    and the filesystem's credentials), or it has none of its own. Every class
    the credentials can be (``credentials_type()`` and each member of a union
    hint) is walked. Raises when dlt cannot introspect ``kind``.
    """
    import dlt

    from .destinations import _credentials_class

    root = _credentials_class(kind)
    if root is None:
        raise LookupError(f"dlt has no credentials class for destination type {kind!r}")
    spec = getattr(dlt.destinations, kind.lower())().spec()
    classes = _dlt_config_classes(spec.get_resolvable_fields().get("credentials"))
    fields: List[Tuple[Tuple[str, ...], bool]] = []
    configurations: List[Tuple[str, ...]] = []
    opaque: List[Tuple[str, ...]] = []
    if "credentials" in (getattr(type(spec), "__hint_resolvers__", None) or {}):
        opaque.append(())

    def walk(cls: Any, path: Tuple[str, ...], seen: Tuple[Any, ...]) -> None:
        allowed = _DLT_IN_ACCOUNT_FIELDS.get(f"{cls.__module__}.{cls.__qualname__}", frozenset())
        resolvers = getattr(cls, "__hint_resolvers__", None) or {}
        hints = cls.get_resolvable_fields()
        if not hints and path:
            opaque.append(path)
        for field, hint in hints.items():
            here = (*path, field)
            nested = _dlt_config_classes(hint)
            if not nested:
                fields.append((here, not path and field in allowed and field not in resolvers))
                continue
            configurations.append(here)
            if field in resolvers or field.startswith("_") or len(here) >= _DLT_MAX_NESTING:
                opaque.append(here)
            for member in nested:
                if member in seen:
                    opaque.append(here)
                else:
                    walk(member, here, (*seen, member))

    for cls in dict.fromkeys([root, *classes]):
        walk(cls, (), (cls,))
    return fields, configurations, opaque


def _dlt_credentials_names(
    section: str, pipeline_name: str, kind: str
) -> Tuple[FrozenSet[str], Tuple[str, ...]]:
    """The names dlt reads the credentials of the destination under
    ``section`` (type ``kind``) from in the pipeline ``pipeline_name``, every
    field at any depth in every spelling, upper-cased, but those of
    :data:`_DLT_IN_ACCOUNT_FIELDS`: ``(names, prefixes)``; a name under one of
    ``prefixes`` is one of them too.

    dlt looks a field of a configuration up under that configuration's
    section path, then under each shorter path made by dropping sections
    before the configuration's own name, each also behind the pipeline name:
    the credentials' ``host`` under ``DESTINATION__<NAME>__CREDENTIALS``,
    ``DESTINATION__CREDENTIALS`` and ``CREDENTIALS``; ducklake's
    ``credentials.catalog.host`` under
    ``DESTINATION__DUCKLAKE__CREDENTIALS__CATALOG``,
    ``DESTINATION__DUCKLAKE__CATALOG``, ``DESTINATION__CATALOG`` and
    ``CATALOG`` (dlt/common/configuration/resolve.py,
    ``_build_section_lookup_paths``, and ``_resolve_config_field`` for a
    nested configuration, dlt 1.31.0). A configuration's whole value (a
    connection string) is read under its own section path the same way. The
    names below a field (a single key of a ``query`` map) count with it, and
    every name below an opaque configuration (:func:`_dlt_credentials_spec`).
    When dlt cannot introspect ``kind`` the credentials are opaque, and every
    name under ``DESTINATION__<NAME>__`` counts too: fail closed.
    """
    heads = (f"{pipeline_name.upper()}__", "") if pipeline_name else ("",)

    def spellings(path: Sequence[str], tail: Sequence[str] = ()) -> List[str]:
        full = ["DESTINATION", section.upper(), "CREDENTIALS", *(part.upper() for part in path)]
        sections, own = full[:-1], full[-1]
        return [
            head + "__".join((*sections[:kept], own, *tail))
            for head in heads
            for kept in range(len(sections), -1, -1)
        ]

    names: Set[str] = set()
    prefixes: Set[str] = set()
    try:
        fields, configurations, opaque = _dlt_credentials_spec(kind)
    except Exception as exc:  # noqa: BLE001 — dlt missing, or a spec it cannot build: pin
        LOG.debug("dlt.destination.spec.unknown kind=%s err=%s", kind, exc)
        fields, configurations, opaque = [], [], [()]
        prefixes.update(f"{head}DESTINATION__{section.upper()}__" for head in heads)
    for path, allowed in fields:
        if not allowed:
            read = spellings(path[:-1], (path[-1].upper(),))
            names.update(read)
            prefixes.update(f"{name}__" for name in read)
    for path in [(), *configurations]:
        names.update(spellings(path))
    for path in opaque:
        prefixes.update(f"{name}__" for name in spellings(path))
    return frozenset(names), tuple(sorted(prefixes))


def _dlt_endpoint_fields(kind: str) -> FrozenSet[str]:
    """The top-level fields of ``kind``'s dlt credentials that pin it: all of
    them (nested configurations included) but :data:`_DLT_IN_ACCOUNT_FIELDS`.
    The empty set when dlt cannot introspect ``kind``; the names under its
    credentials sections then pin whole (:func:`_dlt_credentials_names`)."""
    try:
        fields, configurations, _ = _dlt_credentials_spec(kind)
    except Exception:  # noqa: BLE001 — dlt missing, or a spec it cannot build
        return frozenset()
    return frozenset(
        {path[0] for path, allowed in fields if not allowed} | {path[0] for path in configurations}
    )


def _project_endpoint_field_names(
    section: str, pipeline_name: str, kind: str, names: Sequence[str]
) -> List[str]:
    """The variables in ``names`` dlt reads a field of the credentials of the
    destination under ``section`` (type ``kind``) from in the pipeline
    ``pipeline_name``, at any depth and in any spelling it looks up, other than
    the in-account fields (:func:`_dlt_credentials_names`). Compared
    upper-cased and stripped, as everywhere here; never parsed. Over-matching
    only pins more, never less.
    """
    read, below = _dlt_credentials_names(section, pipeline_name, kind)
    return [
        name
        for name in names
        if name.strip().upper() in read or name.strip().upper().startswith(below)
    ]


def _reaches_destination(name: str, destination: str, pipeline_name: str) -> bool:
    """Whether dlt reads the variable ``name`` for ``destination``'s credentials
    in the pipeline ``pipeline_name``: one of the names :func:`_dlt_names`
    gives for the whole credentials (a connection string), or one of them
    followed by a field (``__HOST``, ``___SNOWFLAKE_HOST``).

    Matched against those exact names, never parsed: the pipeline name is the
    contract's and is the first section dlt tries, so one that looks like a
    section (``sources``, ``a__destination__postgres``) is read as the
    destination's credentials all the same. A name behind another pipeline's
    name is one dlt never reads in this run.
    """
    upper = name.strip().upper()
    for credentials in _dlt_names(destination, pipeline_name, None):
        if upper == credentials:
            return True
        field = upper[len(credentials) + 2 :]
        if upper.startswith(credentials + "__") and field and "__" not in field:
            return True
    return False


def _operator_destination_endpoint(
    section: str, pipeline_name: str, environ: Mapping[str, str]
) -> Optional[str]:
    """The operator's variable in ``environ`` that gives dlt the host of the
    destination under ``section`` ahead of the bridge's copy, or ``None``.

    The bridge writes ``DESTINATION__<SECTION>__CREDENTIALS__HOST`` only where
    that is unset. dlt takes a field from the first of :func:`_dlt_names` that
    is set, so a host the operator exported under that name or one dlt reads
    before it is the one dlt uses. A connection string comes before every field
    variable: dlt parses it first and keeps what it gives as explicit values
    that no provider overrides, filling in only what it leaves out
    (dlt/common/configuration/resolve.py, ``_maybe_parse_native_value`` and
    ``_resolve_config_field``, the same from dlt 0.5.4 to 1.31.0). One the
    operator exported that names a host wins the same way. A value is the
    operator's when it is not blank, no project dotenv file supplied it and
    the bridge did not write it (``destinations.bridge_writes``: a copy it has
    not put back yet is the bridge's, whatever it copied).
    """
    from fluid_build.credentials.dotenv_store import project_value_in_effect

    from .destinations import bridge_writes

    copies = bridge_writes()

    def operators(name: str) -> str:
        value = environ.get(name) or ""
        if not value.strip() or project_value_in_effect(name, environ):
            return ""
        if copies.get(name) == value:
            return ""
        return value

    hosts = _dlt_names(section, pipeline_name, "host")
    copied = f"DESTINATION__{section.upper()}__CREDENTIALS__HOST"
    for name in hosts[: hosts.index(copied) + 1]:
        if operators(name):
            return name
    for name in _dlt_names(section, pipeline_name, None):
        value = operators(name)
        try:
            host = urlsplit(value.strip()).hostname if value else None
        except ValueError:
            host = None
        if host:
            return name
    return None


def _project_endpoints_for_destination(
    destination: str,
    pipeline_name: str,
    binding: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> List[str]:
    """The project's endpoint variables in effect that reach the contract's
    destination ``destination``'s credentials in the pipeline
    ``pipeline_name``: dlt's own, and those of the FLUID service the bridge
    copies into them. Names compare in any case (``dotenv_store``).

    The bridge (``destinations.py``) runs only for ``binding.platform``, writes
    under that platform's section, reads only the variables of its credential
    class (``_credentials.env_names_read_for``) and keeps a dlt variable that
    is already set. So a FLUID endpoint reaches the destination only when
    ``binding.platform`` is the section dlt reads it under, only under a name
    the bridge reads (not libpq's ``PGHOST`` or the Snowflake resolver's
    ``SF_ACCOUNT``), and only while ``environ`` (the environment before the
    bridge ran in this build; default ``os.environ``) holds no host of the
    operator's that dlt uses ahead of the bridge's copy
    (:func:`_operator_destination_endpoint`): the destination is then the
    operator's. ``binding`` None means not known, and counts as if the bridge
    reached the destination: that pins more, never less.

    Every field of the destination's own dlt credentials a project sets, at
    any depth and in any spelling dlt looks it up under, is an endpoint too,
    for every destination type, bridged or not (default-deny): dlt layers it
    on top of the operator's credentials, so the connector would read the
    project's address, options or credential from it while dlt still sends
    the operator's password. The exceptions are the in-account fields of
    :data:`_DLT_IN_ACCOUNT_FIELDS` (:func:`_project_endpoint_field_names`).

    libpq's ``PGHOSTADDR`` would reach every destination dlt connects to
    through libpq, whatever the binding or the operator exported: libpq
    connects to that address with the password dlt passes
    (:data:`_LIBPQ_ENDPOINTS`). No project file can set it; one that loaded
    all the same is found here by its name, not through ``protected_env``'s
    endpoint groups, which leave a protected name out.
    """
    from fluid_build.credentials.dotenv_store import (
        project_endpoints_in_effect,
        project_values_in_effect,
    )

    from .._credentials import env_names_read_for

    section, kind = _dlt_destination(destination)
    found = [
        name
        for name in project_endpoints_in_effect("dlt")
        if _reaches_destination(name, section, pipeline_name)
    ]
    if kind.lower() in _LIBPQ_DESTINATIONS:
        for name in project_values_in_effect():
            parameter = _LIBPQ_ENDPOINTS.get(name.strip().upper())
            if parameter is not None and parameter not in _DLT_LIBPQ_PARAMETERS:
                found.append(name)
    # Every field of the destination's own dlt credentials a project sets is
    # an endpoint too, but the in-account ones (default-deny,
    # :data:`_DLT_IN_ACCOUNT_FIELDS`): dlt layers it on top of the operator's
    # credentials, so libpq / the connector reads the project's ``hostaddr``,
    # catalog host or password from it while dlt still sends the operator's.
    # This holds for every destination, bridged or not, so it comes before the
    # FLUID-service branch below. A name already found is listed once.
    found += [
        name
        for name in _project_endpoint_field_names(
            section, pipeline_name, kind, list(project_values_in_effect())
        )
        if name not in found
    ]
    service = _BRIDGED_SERVICES.get(section.lower())
    if not service:
        return found
    if binding is not None and str(binding.get("platform") or "").lower() != section.lower():
        return found
    operator = _operator_destination_endpoint(
        section, pipeline_name, os.environ if environ is None else environ
    )
    if operator:
        LOG.debug("dlt.destination.operator section=%s name=%s", section, operator)
        return found
    read = env_names_read_for(section)
    return found + [
        name for name in project_endpoints_in_effect(service) if name.strip().upper() in read
    ]


def _destination_with_project_credentials(
    destination: str,
    binding: Dict[str, Any],
    pipeline_name: str,
    endpoints: Optional[Sequence[str]] = None,
) -> Any:
    """dlt's ``destination`` with credentials from the project's own values only.

    dlt resolves each credential field on its own, from the environment and
    then from its ``secrets.toml`` and ``config.toml`` (the operator's global
    ``~/.dlt`` ones included) and its other config providers, so a host the
    project chose would be completed with the operator's key, authenticator,
    user, database, TLS client key or password. The bridge also copies the
    operator's FLUID variables (``SNOWFLAKE_PRIVATE_KEY_PATH`` ...) into the
    environment dlt reads. So the credentials are built here from the
    project's values in effect (``dotenv_store.project_values_in_effect``):
    first the variables dlt reads, in its order, the connection string before
    the fields, then FLUID's variables the bridge reads for the field, and the
    contract's ``binding.location``. They are handed to the destination
    factory resolved, which dlt then uses as they are
    (https://dlthub.com/docs/general-usage/destination, passing credentials
    to a destination factory).

    ``endpoints`` are the project's endpoints that pinned the destination,
    named in an error (by default :func:`_project_endpoints_for_destination`).
    Raises ``ValueError`` for a destination FLUID cannot build credentials
    for, and when the project's values leave out a field dlt requires.
    """
    import dlt
    from dlt.common.configuration.specs.base_configuration import extract_inner_hint
    from dlt.common.configuration.utils import deserialize_value

    from fluid_build.credentials.dotenv_store import project_values_in_effect

    from .._credentials import env_names_for, get_credentials_for
    from .destinations import _credentials_class, _value_for_dlt_field

    if endpoints is None:
        endpoints = _project_endpoints_for_destination(destination, pipeline_name, binding)
    named = ", ".join(endpoints)
    section, kind = _dlt_destination(destination)
    factory = getattr(dlt.destinations, kind.lower(), None)
    credentials_class = _credentials_class(kind) if factory is not None else None
    if credentials_class is None:
        raise ValueError(
            f"dlt destination {destination!r}: the project's dotenv files set its endpoint "
            f"({named}), and FLUID cannot build its credentials from the project's "
            "values alone. Export the endpoint in your shell or CI environment to use it."
        )
    project = project_values_in_effect()
    by_name: Dict[str, List[str]] = {}
    for name, value in project.items():
        by_name.setdefault(name.upper(), []).append(value)
    location = binding.get("location") or {}
    bridged: Dict[str, Any] = {}
    for field, value in get_credentials_for(section, binding=binding).items():
        names = env_names_for(section, field)
        if location.get(field) == value or any(str(value) in by_name.get(n, ()) for n in names):
            bridged[field] = value
        elif not str(value).strip():
            # behind the operator's blank variable the project's value is the
            # one in effect, as the credential resolver reads it
            # (dotenv_store.project_value_in_effect)
            theirs = next((by_name[n][0] for n in names if by_name.get(n)), None)
            if theirs is not None:
                bridged[field] = theirs

    def first(names: List[str]) -> Optional[str]:
        return next((project[name] for name in names if project.get(name)), None)

    credentials = credentials_class()
    native = first(_dlt_names(section, pipeline_name, None))
    if native:
        credentials.parse_native_representation(native)
    hints = credentials.get_resolvable_fields()
    for field, hint in hints.items():
        if field == "drivername":
            continue
        value: Any = first(_dlt_names(section, pipeline_name, field))
        if value is None:
            value = _value_for_dlt_field(section, field, bridged)
        if value is None or value == "":
            continue
        if isinstance(value, str):
            value = deserialize_value(field, value, extract_inner_hint(hint))
        setattr(credentials, field, value)
    missing = [
        field
        for field, hint in hints.items()
        if not credentials.is_field_resolved(getattr(credentials, field, None), hint)
    ]
    if missing:
        raise ValueError(
            f"dlt destination {destination!r}: the project's dotenv files set its endpoint "
            f"({named}), so its credentials come only from those files and the "
            f"contract, which leave out {', '.join(missing)}. Add them there, or export "
            "the endpoint in your shell or CI environment to use your own dlt credentials."
        )
    credentials.resolve()
    return factory(credentials=credentials, destination_name=section)


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
