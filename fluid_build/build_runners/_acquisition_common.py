# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for acquisition runners.

- ``RunIdGenerator`` — ULID-style monotonic ids that survive replay.
- ``build_acquisition_run_context`` — assemble the ``RunContext`` shared by
  every acquisition runner (source/sink resolution + state store).
- ``utc_now_iso`` — single source of truth for timestamps.
- ``finalize_run_result`` — convert a ``RunResult`` to an exit code AND
  surface failures to the user (single point that handles redaction +
  ANSI stripping + log emission).
- ``write_run_record_and_finalize`` — combined helper that writes the
  state-store run record AND finalizes; one call replaces the 15-line
  per-runner boilerplate.
- ``DEFAULT_SUCCEEDED_STATES`` — ``(SUCCEEDED, PARTIAL)`` for the runners
  that treat partial as success; the duckdb runner overrides on its
  side.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from fluid_build._net import is_loopback_host as _is_loopback_host

LOG = logging.getLogger("fluid.build_runners.acquisition")

# Strips ANSI CSI sequences and ASCII control chars from error strings
# before they reach the user's terminal. Compiled once at import.
_CONTROL_CHAR_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_run_id() -> str:
    """Lightweight monotonic id without external deps. Format: HHMMM-XXXXXX
    where the prefix is a millisecond timestamp (base32) and the suffix is
    6 random chars. Sortable and unique per process.
    """
    ts_ms = int(time.time() * 1000)
    ts_b32 = _to_base32(ts_ms, width=10)
    rand = _rand_b32(6)
    return f"01{ts_b32}{rand}"


_BASE32_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _to_base32(n: int, width: int) -> str:
    out = []
    while n > 0:
        out.append(_BASE32_ALPHABET[n & 31])
        n >>= 5
    while len(out) < width:
        out.append("0")
    return "".join(reversed(out))


def _rand_b32(n: int) -> str:
    return "".join(_BASE32_ALPHABET[secrets.randbelow(32)] for _ in range(n))


def get_acquisition_build_props(build: Dict[str, Any]) -> Dict[str, Any]:
    """Return the ``properties`` dict for an acquisition build, defaulting to {}."""
    return dict(build.get("properties") or {})


def is_acquisition_build(build: Dict[str, Any]) -> bool:
    return build.get("pattern") == "acquisition"


def build_acquisition_run_context(
    build: Dict[str, Any],
    contract: Dict[str, Any],
    contract_dir: Path,
    *,
    sample_rows: Any = None,
    state_root: Optional[Path] = None,
    hook_chain: Any = None,
) -> Optional[Any]:
    """Assemble the ``RunContext`` shared by every acquisition runner.

    Resolves ``properties.source`` / ``sink``, env-interpolates the source
    connection, opens the ``FileStateStore`` (rooted at ``state_root`` or
    ``<contract_dir>/.fluid``), and builds the context. Returns ``None``
    when ``properties.source`` is absent — the caller returns exit code 1.
    ``hook_chain`` defaults to an empty chain; the duckdb runner passes
    one built from ``properties.preLand``.
    """
    from fluid_build.api.hooks import HookChain
    from fluid_build.api.runner import RunContext
    from fluid_build.api.source import SinkSpec, SourceSpec

    from ._cost import InMemoryCostTracker
    from ._lineage import resolve_lineage_emitter
    from ._state import FileStateStore
    from .base import _resolve_env_placeholders

    props = get_acquisition_build_props(build)
    source_dict = props.get("source")
    if not source_dict:
        LOG.error("acquisition build missing properties.source")
        return None
    source_dict = _resolve_env_placeholders(source_dict)
    source = SourceSpec.from_dict(source_dict)
    sink = SinkSpec.from_dict(props.get("sink"))

    store = FileStateStore(state_root or (contract_dir / ".fluid"))
    return RunContext(
        run_id=generate_run_id(),
        product_id=contract.get("id", "unknown"),
        build_id=build.get("id", "unknown"),
        contract=contract,
        source=source,
        sink=sink,
        state_store=store,
        hook_chain=hook_chain if hook_chain is not None else HookChain(hooks=[]),
        # Resolved from OPENLINEAGE_URL / FLUID_OPENLINEAGE_URL. Returns the
        # null emitter when unconfigured, so emission stays opt-in and free.
        lineage=resolve_lineage_emitter(),
        cost_tracker=InMemoryCostTracker(),
        workdir=str(contract_dir),
        sample_rows=sample_rows,
    )


def begin_acquisition_run(ctx: Any, runner: Any) -> Tuple[str, float]:
    """Open an acquisition run — timestamp, schema-policy gate, duration clock.

    Returns ``(started_at, t_start)``; raises if the schema-evolution gate
    rejects the run. Shared by every acquisition runner's ``_execute``.

    Also emits the OpenLineage START event. This is the single chokepoint
    all 6 acquisition runners pass through, so wiring it here gives every
    engine lineage at once, the same leverage pattern already used for
    replay (``_state.set_cursor``) and late-arrival policy.
    """
    started_at = utc_now_iso()
    enforce_schema_policy_or_raise(ctx, runner)
    t_start = time.time()
    emit_start_lineage_event(ctx, started_at=started_at)
    return started_at, t_start


def emit_start_lineage_event(ctx: Any, *, started_at: Optional[str] = None) -> str:
    """Emit the OpenLineage START event for ``ctx``'s run and return its time.

    :func:`begin_acquisition_run` emits it once the schema gate has passed. A
    runner that refuses a run before opening it (the duckdb runner refuses a
    masking policy it cannot apply before the gate and before anything
    connects) calls this directly, so the FAIL that
    :func:`emit_terminal_lineage_event` then emits has a START with the same
    runId, as every other outcome does.
    """
    from fluid_build.api.lineage import RunEventType

    from ._lineage import emit_run_event

    event_time = started_at or utc_now_iso()
    emit_run_event(ctx, event_type=RunEventType.START, event_time=event_time)
    return event_time


def failed_run_result(ctx: Any, *, engine: str, started_at: str, t_start: float, err: str) -> Any:
    """Build a FAILED ``RunResult`` — the shared per-runner failure factory.

    Keyword-only so the call site is unambiguous regardless of how each
    runner historically ordered its ``_failed`` arguments.
    """
    from fluid_build.api.runner import RunResult, RunState

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
        facets={"engine": engine, "duration_seconds": time.time() - t_start},
    )


def setdefault_env(key: str, value: Optional[str]) -> bool:
    """Set ``os.environ[key] = value`` IFF the env var is currently unset AND
    the value is non-empty. Returns ``True`` if a value was actually set.

    Used by destination-credential bridges to translate FLUID's canonical
    env-var naming to engine-specific naming WITHOUT clobbering operator
    overrides — anything the operator explicitly exported wins.
    """
    if not value:
        return False
    if os.environ.get(key):
        return False
    os.environ[key] = value
    return True


# Destination credential dispatch lives in ``_credentials.py`` (per-engine
# introspectors + pydantic-settings credential layer). The runners import
# ``make_destination`` from there. The deprecated ``_DESTINATION_FACTORIES``
# / ``register_destination`` / ``bridge_destination_env`` machinery that
# used to live here was removed in favour of that single, OSS-delegating
# layer — see ``_credentials.py`` for the rationale and OSS receipts.


# Registry: (engine, source_kind) → adapter that translates the FLUID generic
# connection dict (post secretRef resolution) into whatever shape the engine's
# source connector expects.
#
# Why per (engine, kind)? Each (engine, kind) pair has its own quirks. The
# Airbyte ``source-postgres`` connector wants ``username``; ``source-mysql``
# wants ``username`` too but with different ssl_mode defaults; Meltano's
# ``tap-postgres`` is fine with FLUID's generic ``user``; dlt's
# ``sql_database`` accepts both. Putting the per-connector translation in
# one place per (engine, kind) keeps engine runners thin and lets us add
# new connectors without touching runner code.
#
# Adapters live in ``fluid_build.build_runners.<engine>.sources`` (or
# similar) and register themselves at import time via
# ``register_source_adapter``. Each engine's ``__init__.py`` should
# ``import . sources`` to fire registration when the package loads.
_SOURCE_ADAPTERS: Dict[Tuple[str, str], Callable[[Dict[str, Any]], Dict[str, Any]]] = {}


def register_source_adapter(
    engine: str, kind: str
) -> Callable[
    [Callable[[Dict[str, Any]], Dict[str, Any]]], Callable[[Dict[str, Any]], Dict[str, Any]]
]:
    """Decorator that registers a source-config adapter for (engine, kind).

    The adapter receives a copy of the FLUID-shaped connection dict (after
    secretRef resolution and schema extraction) and returns the engine-
    specific shape with field renames, type coercions, and connector-
    required defaults applied.

    Example
    -------
    >>> @register_source_adapter("airbyte", "postgres")
    ... def _airbyte_postgres(connection):
    ...     out = dict(connection)
    ...     if "user" in out and "username" not in out:
    ...         out["username"] = out.pop("user")
    ...     if "port" in out:
    ...         try: out["port"] = int(out["port"])
    ...         except (TypeError, ValueError): pass
    ...     out.setdefault("ssl_mode", {"mode": "disable"})
    ...     return out
    """

    def _wrap(
        fn: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
        _SOURCE_ADAPTERS[(engine, kind)] = fn
        return fn

    return _wrap


def adapt_source_config(engine: str, kind: str, connection: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the registered (engine, kind) source adapter, if any.

    Returns the connection dict unchanged (shallow copy) when no adapter is
    registered — non-fatal so engines can grow their adapter coverage
    incrementally without breaking the unadapted long tail.
    """
    adapter = _SOURCE_ADAPTERS.get((engine, kind))
    return adapter(dict(connection)) if adapter else dict(connection)


def apply_loopback_host_override(connection: Dict[str, Any]) -> None:
    """In-place: substitute loopback host with operator-provided override.

    Some engines (notably Airbyte's PyAirbyte mode, which runs each source
    connector as a Docker container) reach the host through a different
    address than what the operator's shell uses. The contract author writes
    ``host: localhost`` (correct from the contract's perspective — the data
    is on the operator's machine); the runner needs to translate that to
    whatever the engine's container runtime uses to reach the host.

    Container-runtime variants:

    - Docker Desktop (macOS/Windows): ``host.docker.internal``
    - Linux Docker (recent): ``host.docker.internal`` (when launched with
      ``--add-host=host.docker.internal:host-gateway``) OR the bridge IP.
    - Podman: ``host.containers.internal``
    - Kubernetes: a pod-internal Service name or the host's pod-network IP.

    Rather than hard-code one of these (which biases the runner to one
    runtime), runners call this helper which consults an operator-
    controlled env var. Two are accepted, in precedence order:

    1. ``FLUID_RUNNER_HOST_OVERRIDE`` — FLUID-canonical name (wins when set).
    2. ``TESTCONTAINERS_HOST_OVERRIDE`` — testcontainers-python ecosystem
       convention (https://github.com/testcontainers/testcontainers-python).
       Operators who already use testcontainers for integration tests get
       PyAirbyte/Debezium reachability with zero extra config.

    When set and the connection's host is a loopback address (any form —
    ``localhost``, 127.0.0.0/8, ``::1`` — see :func:`_is_loopback_host`),
    the override replaces the host. No env var, OR non-loopback host → no-op.

    This is intentionally NOT engine-specific: every engine that runs in a
    container faces the same problem, and the operator's network topology
    answers it once for all of them.
    """
    override = os.environ.get("FLUID_RUNNER_HOST_OVERRIDE") or os.environ.get(
        "TESTCONTAINERS_HOST_OVERRIDE"
    )
    if not override:
        return
    if _is_loopback_host(connection.get("host")):
        connection["host"] = override


def extract_source_schemas(connection: Dict[str, Any]) -> List[str]:
    """Return the list of source schemas/namespaces declared on a connection.

    The 0.7.3 acquisitionSource.connection schema documents two equivalent
    fields: ``schema`` (single, convenience) and ``schemas`` (list,
    canonical). When both are set, ``schemas`` wins. When neither is set,
    returns an empty list — runners interpret that as "engine default"
    (typically ``public`` for Postgres, ``dbo`` for SQL Server, etc.).

    Each acquisition runner translates this generic contract concept into
    its engine-specific config key:

    - dlt sql_database         → ``sql_database(schema=schemas[0])`` (single)
    - Meltano tap-postgres     → ``filter_schemas: schemas`` (list)
    - Airbyte source-postgres  → ``schemas: schemas`` (list)
    - Debezium postgres        → ``schema.include.list = ",".join(schemas)``
    - Kafka Connect JDBC src   → ``schema.pattern = schemas[0]`` (regex, single)

    Centralising the read here keeps the contract syntax consistent across
    engines and lets us evolve the schema (e.g. adding ``exclude_schemas``)
    in one place.
    """
    if connection.get("schemas"):
        return [str(s) for s in connection["schemas"]]
    if connection.get("schema"):
        return [str(connection["schema"])]
    return []


def resolve_connection_secrets(
    connection: Dict[str, Any], *, target_field: str = "password"
) -> Dict[str, Any]:
    """Resolve a connection block's ``secretRef`` into a literal credential field.

    Most acquisition runners (dlt sql_database, meltano tap-postgres, airbyte
    source-postgres, debezium connectors, ...) want the secret as the
    ``password`` field of the underlying client, which is the default.

    Behaviour
    ---------
    1. Returns a NEW dict; never mutates the input.
    2. No ``secretRef`` -> returns a shallow copy unchanged.
    3. The value is fetched through :func:`fluid_build.secrets.resolve_secret`,
       so every scheme gets the same treatment: the grammar, the operator's
       policy (scheme, locator prefix, stage, and EVERY destination this
       connection carries), the run's cache or its run-secrets file, a
       ``SecretStr`` registered with the redactor, and the audit event. A
       refusal is raised before any store is asked, as a typed
       :class:`~fluid_build.secrets.SecretError` naming the reference, never
       the value.
    4. The resolved value is placed in ``connection[target_field]`` IFF that
       field is empty. An inline literal always wins over the secretRef, and
       then the reference is never read and nothing is policed: nothing is
       fetched or sent on its account. (A 0.7.7 contract cannot carry the
       literal: ``password`` is ``writeOnly`` there.)
    5. ``secretRef`` is removed from the returned dict so downstream client
       SDKs don't see a stray field.

    Args:
        connection: The raw ``properties.source.connection`` dict (the
            ``ConnectionSpec.raw`` view).
        target_field: Field to populate with the resolved secret: ``password``
            for SQL-flavoured connections, ``token`` or ``access_token`` for
            REST sources. The runner chooses, because the reference is
            engine-agnostic.
    """
    out = dict(connection)
    secret_ref = out.pop("secretRef", None)
    if not secret_ref:
        return out
    if out.get(target_field):
        return out
    from fluid_build.secrets import resolve_secret

    # The whole connection, so EVERY destination it carries has to clear.
    value = resolve_secret(str(secret_ref), connection=out)
    out[target_field] = value.get_secret_value()
    return out


def refuse_config_expressions(value: Any, *, where: str) -> None:
    """Raise ``ValueError`` when contract text bound for a connector's config holds ``${``.

    Debezium Server reads ``application.properties`` through SmallRye Config,
    which expands a ``${...}`` expression in a value from every config source,
    its environment included (``ExpressionConfigSourceInterceptor``); its one
    escape, ``\\${``, changes how the rest of the value is read. A Kafka Connect
    worker resolves ``${provider:path:key}`` in a connector config with the
    config providers it declares (``ConfigTransformer``), which has no escape.
    Either sends what it read, a variable or a file, on to wherever the
    connector connects. forge puts no expression into contract text (its own
    ``${FLUID_DBZ_...}`` lines hold credentials it moved to the environment),
    so a value or a key that holds ``${`` is refused, a value of any type read
    as the text a properties file would get (``str``: a YAML ``!!binary`` is
    ``b'...'``). The error names where it is, ``where`` and the path below it,
    and never the value.
    """
    from fluid_build._validation_issues import dotted
    from fluid_build.providers._iceberg_catalog_auth import EXPRESSION_RE

    pending: List[Tuple[Tuple[Any, ...], Any]] = [((), value)]
    seen: Set[int] = set()
    while pending:
        path, item = pending.pop()
        if not isinstance(item, (Mapping, list, tuple)):
            if item is not None and EXPRESSION_RE.search(str(item)):
                raise ValueError(_expression_refusal(where, dotted(path)))
        elif id(item) not in seen:
            seen.add(id(item))
            items = item.items() if isinstance(item, Mapping) else enumerate(item)
            for key, child in items:
                if EXPRESSION_RE.search(str(key)):
                    raise ValueError(_expression_refusal(where, dotted(path), in_key=True))
                pending.append(((*path, key), child))


def _expression_refusal(where: str, below: str, *, in_key: bool = False) -> str:
    label = f"{where}{'' if not below or below.startswith('[') else '.'}{below}"
    what = f"a key under {label}" if in_key else label
    return (
        f"{what} holds a ${{...}} expression (the value is not shown). The connector "
        "expands it where it runs, Debezium Server from its environment and a Kafka "
        "Connect worker through its config providers, so it could carry a variable or a "
        "file of that machine to a host the contract chose. Write the value itself; a "
        "secret belongs in a secretRef."
    )


def refuse_contract_expressions(
    source: Any, engine_props: Mapping[str, Any], *, engine: str
) -> None:
    """:func:`refuse_config_expressions` for the contract text a connector run reads.

    ``source`` is the run's own source (``ctx.source``, read from the build the
    dispatcher passed in, which another build may share an id with), every field
    of it, its connection's ``secretRef`` aside: the secret it names is the
    operator's, not contract text. Then ``properties.<engine>``.
    """
    spec = {
        "kind": source.kind,
        "mode": source.mode,
        "connection": {k: v for k, v in source.connection.raw.items() if k != "secretRef"},
        "cursor_field": source.cursor_field,
        "watermark": source.watermark,
        "streams": list(source.streams or []),
        "reader": source.reader,
    }
    refuse_config_expressions(spec, where="properties.source")
    refuse_config_expressions(engine_props, where=f"properties.{engine}")


def secret_resolved(connection: Mapping[str, Any]) -> bool:
    """Whether :func:`resolve_connection_secrets` fills ``password`` from the store."""
    return bool(connection.get("secretRef")) and not connection.get("password")


def refuse_sent_config_expressions(
    config: Mapping[str, Any], *, where: str, secret_keys: Sequence[str] = ()
) -> None:
    """:func:`refuse_config_expressions` for a connector config as it is sent.

    The last check before a REST call, over everything the config holds: a
    setting forge derives from another part of the contract included. Left out
    are only ``secret_keys``, the settings the caller wrote the store's resolved
    secret to, which is the operator's and not contract text.
    """
    refuse_config_expressions(
        {key: value for key, value in config.items() if key not in secret_keys}, where=where
    )


def _default_succeeded_states() -> Tuple:
    """Default success set ``(SUCCEEDED, PARTIAL)``. Imported lazily so
    callers don't need ``RunState`` at module-load time."""
    from fluid_build.api.runner import RunState

    return (RunState.SUCCEEDED, RunState.PARTIAL)


def _sanitize_error_text(err_raw: Any) -> str:
    """Apply two transforms to a runner's ``result.error`` string before
    it reaches the user's terminal:

    1. **Redact secrets.** Runtime exceptions inside the duckdb
       postgres / mysql extensions routinely echo the libpq DSN —
       including the password — into the exception message.
       ``redact_secret_text`` catches the same patterns the global
       logging filter does (``password=…``, ``api_key=…``, JWTs, etc.).
    2. **Strip ANSI / control chars.** A contract-supplied error string
       can carry ANSI escapes (``\\x1b[2J``) or carriage returns to
       overwrite prior terminal output. Strip every C0 control char
       except newline and tab, plus full CSI sequences.
    """
    from fluid_build.observability.secret_redactor import redact_secret_text

    text = redact_secret_text(str(err_raw or "(no error message captured)"))
    return _CONTROL_CHAR_RE.sub("", text)


def finalize_run_result(
    engine: str,
    build_id: str,
    result: Any,
    *,
    succeeded_states: Optional[Tuple] = None,
    logger: Optional[logging.Logger] = None,
) -> int:
    """Convert a ``RunResult`` to a CLI exit code AND surface failures.

    On success: returns 0.
    On failure: ``LOG.error`` the run (routed through the global
    ``SecretRedactingFilter``), emit through ``console.error`` for the
    user-facing stderr message (also redacted via ``_redact_str``),
    and return 1. Both pipes apply redaction so DSN passwords leaking
    out of upstream extension errors don't reach the terminal.

    Args:
        engine: Engine name for the log line.
        build_id: Build identifier (use ``ctx.build_id`` consistently).
        result: ``RunResult`` from the runner.
        succeeded_states: Tuple of ``RunState`` values to treat as
            success. Defaults to ``(SUCCEEDED, PARTIAL)``. The duckdb
            runner overrides to ``(SUCCEEDED,)`` because partial-stream
            failures raise ``PartialFailureError`` upstream.
        logger: Optional ``logging.Logger``. Defaults to
            ``logging.getLogger("fluid.acquire." + engine)``.
    """
    if succeeded_states is None:
        succeeded_states = _default_succeeded_states()

    log = logger or logging.getLogger(f"fluid.acquire.{engine}")
    if result.state in succeeded_states:
        return 0

    err_safe = _sanitize_error_text(getattr(result, "error", None))
    log.error("acquire.%s.failed build_id=%s err=%s", engine, build_id, err_safe)

    # Route the user-visible failure through the project's structured
    # stderr pipe. ``cli.console.error`` writes to stderr (RichConsole
    # when available, plain print otherwise), applies the project's
    # ``_redact_str`` sanitiser, and is the sink CodeQL recognises as
    # safe — same path used elsewhere for structured error display.
    # Best-effort: if the console module isn't importable (e.g. test
    # contexts that strip optional deps), fall back to a plain stderr
    # write so the user still sees the message.
    msg = f"{engine} build '{build_id}' failed: {err_safe}"
    try:
        from fluid_build._console import error as console_error

        console_error(msg)
    except Exception:  # pragma: no cover — defensive fallback
        import sys

        sys.stderr.write(f"\n❌ {msg}\n")
    return 1


# Canonical run-record dict shape — every runner's state-store write
# uses this unless it overrides via ``record_dict``.
def _canonical_run_record(result: Any) -> Dict[str, Any]:
    return {
        "run_id": result.run_id,
        "state": result.state.value,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "records_total": result.records_total,
        "streams": [
            {"name": s.name, "state": s.state.value, "records": s.records} for s in result.streams
        ],
        "error": result.error,
        "facets": result.facets,
    }


# Runners that land ONE expose per build: a build naming no outputs beside several exposes
# is a guess for them, so it refuses. Debezium and Kafka Connect sinks cover every expose.
_ONE_EXPOSE_PER_BUILD_RUNNERS = frozenset({"duckdb", "dlt", "airbyte", "meltano"})


def _contract_exposes(ctx: Any) -> List[Dict[str, Any]]:
    return [e for e in ctx.contract.get("exposes") or [] if isinstance(e, dict)]


def landed_expose(ctx: Any) -> Dict[str, Any]:
    """The expose this build lands: the first one its ``outputs`` resolves to.

    ``build_output_exposes`` is the one place that maps a build to its exposes; its typed
    ``BuildOutputsError`` (``build_output_unknown_expose``, ``build_outputs_undeclared``)
    propagates. A build the contract does not list, or a contract with no exposes, keeps
    ``exposes[0]`` (``{}`` when there is none).
    """
    from fluid_build.util.contract import build_output_exposes, get_builds

    exposes = _contract_exposes(ctx)
    build_id = getattr(ctx, "build_id", None)
    build = next(
        (b for b in get_builds(ctx.contract) if isinstance(b, dict) and b.get("id") == build_id),
        None,
    )
    if build is not None:
        resolved = build_output_exposes(ctx.contract, build)
        if resolved:
            return resolved[0]
    return exposes[0] if exposes else {}


def enforce_schema_policy_or_raise(ctx: Any, runner: Any) -> None:
    """Apply the schema-evolution decision matrix to ``ctx``'s stream.

    Shared across all 6 acquisition runners (was previously duckdb-only).
    Reads the contract's declared schema (``exposes[].contract.schema``)
    as the baseline and the current stream schema (via
    ``runner.fingerprint(ctx)``) as the candidate. Resolves a per-event
    decision under ``schemaPolicy`` and raises ``SchemaDriftError`` for
    the typed-catalog renderer when any decision is FAIL.

    Best-effort: when the contract has no baseline schema or the runner's
    fingerprint method raises (typical for runners that don't yet
    implement deep schema introspection), this function silently
    no-ops. The runner falls through to its normal execution path.

    Called at the top of each runner's ``_execute`` so the user gets
    a structured error before any side-effecting writes. All 6
    acquisition engines call this helper.
    """
    try:
        from fluid_build.api.schema import SchemaColumn, SchemaPolicy
        from fluid_build.build_runners._schema_evolution import (
            raise_if_strict_drift,
        )
        from fluid_build.build_runners._schema_evolution import (
            resolve as resolve_decisions,
        )
    except Exception:  # pragma: no cover — defensive
        return

    # The baseline is the schema of the expose THIS build lands (``landed_expose``).
    # An output that is no exposeId always refuses; a build with no outputs beside several
    # exposes refuses for the runners that land one expose per build, while the sinks of
    # Debezium and Kafka Connect cover every expose of a build that names none, so for
    # them the first expose stays the schema baseline.
    from fluid_build.util.contract import BuildOutputsError

    try:
        expose = landed_expose(ctx)
    except BuildOutputsError as exc:
        if (
            exc.code != "build_outputs_undeclared"
            or getattr(runner, "name", None) in _ONE_EXPOSE_PER_BUILD_RUNNERS
        ):
            raise
        exposes = _contract_exposes(ctx)
        expose = exposes[0] if exposes else {}
    contract_block = expose.get("contract") or {}
    declared_schema = contract_block.get("schema") or []
    policy_str = contract_block.get("schemaPolicy") or "evolve_safe"
    if not declared_schema:
        return  # First run; live schema becomes the baseline elsewhere.

    try:
        policy = SchemaPolicy(policy_str)
    except Exception:
        return

    baseline = [
        SchemaColumn(
            name=c.get("name", ""),
            type=c.get("type", ""),
            nullable=bool(c.get("nullable", True)),
        )
        for c in declared_schema
        if isinstance(c, dict) and c.get("name")
    ]
    if not baseline:
        return

    try:
        current_fp = runner.fingerprint(ctx)
        current = list(current_fp.columns or [])
    except Exception:
        # Runner doesn't support fingerprinting yet (e.g. some engines
        # only know the upstream schema after the connector is created).
        # Skip the check rather than blocking the run.
        return
    if not current:
        return
    # Code-as-config runners (dlt, debezium, airbyte, meltano, kafka_connect)
    # surface stream names as placeholder columns because computing the real
    # schema requires running the source. Comparing those stream-name columns
    # against the contract's real columns produces spurious added/removed
    # decisions. The runner declares a placeholder by setting
    # SchemaFingerprint.is_placeholder=True (or using
    # SchemaFingerprint.placeholder(...)). Skip the gate in that case — drift
    # gets detected at run-time by the engine itself once it actually reads
    # the source.
    if getattr(current_fp, "is_placeholder", False):
        return

    plan = resolve_decisions(
        baseline=baseline,
        current=current,
        policy=policy,
        overrides=contract_block.get("evolutionOverrides") or {},
        is_first_run=False,
    )
    _log_family_renames(baseline, current)
    if not plan.has_failure:
        return

    try:
        from fluid_build.api.schema import SchemaFingerprint

        baseline_digest = SchemaFingerprint.of(baseline, captured_at=utc_now_iso()).digest
        current_digest = current_fp.digest
    except Exception:
        baseline_digest = "baseline"
        current_digest = "current"

    raise_if_strict_drift(
        plan,
        baseline_digest=baseline_digest,
        current_digest=current_digest,
    )


def _log_family_renames(
    baseline: Sequence[Any],
    current: Sequence[Any],
) -> None:
    """Tell the operator about a type change the drift gate deliberately allowed.

    ``diff_columns`` emits no event when a column's type changed only between
    two spellings of one physical type (DuckDB 1.5's DOUBLE vs 2.0's HUGEINT,
    TIMESTAMP vs TIMESTAMP WITH TIME ZONE), so that upgrading DuckDB does not
    raise a false ``SchemaDriftError``. Suppressing the FAILURE is the intent;
    suppressing the NOTICE as well is not, because the two types are not always
    value-identical -- 1.5 landed a lossy 17-significant-digit float where 2.0
    lands the exact 128-bit integer.

    Logged whatever the policy decides, and before the gate's early return, so
    a contract that declared ``schemaPolicy: strict`` still hears that the
    physical type moved even though the run is allowed to continue.
    """
    from fluid_build.build_runners._schema_evolution import family_rename_note

    by_name = {getattr(c, "name", None): c for c in baseline}
    for col in current:
        was = by_name.get(getattr(col, "name", None))
        if was is None:
            continue
        note = family_rename_note(getattr(was, "type", "") or "", getattr(col, "type", "") or "")
        if note:
            LOG.warning(
                "acquisition_schema_family_rename",
                extra={"column": getattr(col, "name", None), "detail": note},
            )


def write_run_record(
    *,
    state_store: Any,
    ctx: Any,
    result: Any,
    record_dict: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist a run record into the state store.

    Pure write; no finalize logic. Useful for runners that need the
    record on disk BEFORE deciding to raise a typed error (e.g. the
    duckdb runner's ``PartialFailureError``). When the runner can
    write-and-finalize in one go, prefer
    :func:`write_run_record_and_finalize`.

    ``record_dict`` defaults to :func:`_canonical_run_record`; pass an
    explicit dict when an engine carries extra per-stream fields
    (dlt's ``duration_seconds`` / per-stream ``error``, meltano's
    ``cursor_advanced``).
    """
    state_store.write_run_record(
        ctx.product_id,
        ctx.build_id,
        record_dict if record_dict is not None else _canonical_run_record(result),
    )


def write_run_record_and_finalize(
    *,
    engine: str,
    ctx: Any,
    result: Any,
    state_store: Any,
    succeeded_states: Optional[Tuple] = None,
    record_dict: Optional[Dict[str, Any]] = None,
) -> int:
    """Combined ``state_store.write_run_record(...) + finalize_run_result(...)``.

    Used at the bottom of every ``execute_<engine>_build`` function so
    all 6 runners share one pipeline: same record schema, same
    success-state logic, same failure-surfacing.

    Args:
        engine: Engine name for the log line (e.g. ``"debezium"``).
        ctx: ``RunContext`` — used for ``product_id`` + ``build_id``.
        result: ``RunResult`` returned by the runner.
        state_store: ``FileStateStore`` (or compatible) to persist into.
        succeeded_states: Override the default ``(SUCCEEDED, PARTIAL)``
            success set. The duckdb runner overrides to ``(SUCCEEDED,)``.
        record_dict: Optional engine-specific record shape. When ``None``
            uses :func:`_canonical_run_record`. dlt and meltano pass
            explicit dicts because their per-stream records carry extra
            fields (``duration_seconds``, ``cursor_advanced``).
    """
    write_run_record(state_store=state_store, ctx=ctx, result=result, record_dict=record_dict)
    emit_terminal_lineage_event(ctx, result=result, succeeded_states=succeeded_states)
    return finalize_run_result(engine, ctx.build_id, result, succeeded_states=succeeded_states)


def emit_terminal_lineage_event(
    ctx: Any,
    *,
    result: Any,
    succeeded_states: Optional[Tuple] = None,
) -> None:
    """Emit the OpenLineage terminal event for a finished run.

    Maps FLUID's ``RunState`` onto the OpenLineage vocabulary:
    ``CANCELLED`` becomes ``ABORT``, anything outside the success set
    becomes ``FAIL``, and the rest become ``COMPLETE``. ``PARTIAL`` counts
    as ``COMPLETE`` by default because it is in the default success set,
    and the per-stream detail is already carried in the run facets.
    """
    from fluid_build.api.runner import RunState

    from ._lineage import emit_run_event

    if succeeded_states is None:
        succeeded_states = _default_succeeded_states()

    state = getattr(result, "state", None)
    if state == RunState.CANCELLED:
        event_type = _lineage_event_type("ABORT")
    elif state in succeeded_states:
        event_type = _lineage_event_type("COMPLETE")
    else:
        event_type = _lineage_event_type("FAIL")

    finished_at = getattr(result, "finished_at", None) or utc_now_iso()
    emit_run_event(ctx, event_type=event_type, event_time=finished_at, result=result)


def _lineage_event_type(name: str) -> Any:
    from fluid_build.api.lineage import RunEventType

    return RunEventType[name]
