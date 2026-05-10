# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Shared helpers for acquisition runners.

- ``RunIdGenerator`` — ULID-style monotonic ids that survive replay.
- ``build_run_context`` — assemble a ``RunContext`` from a contract + build.
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
from typing import Any, Callable, Dict, List, Optional, Tuple

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


def _is_loopback_host(host: Any) -> bool:
    """Return True for any address that resolves to the local loopback.

    Accepts:
    - The canonical hostname ``localhost`` (case-insensitive). We don't
      probe the OS resolver because that would couple the helper to whatever
      ``/etc/hosts`` the operator's machine happens to have configured —
      ``localhost`` is the one universally-understood loopback name.
    - Any address in the IPv4 loopback range ``127.0.0.0/8`` (so
      ``127.0.0.2``, ``127.1.2.3`` etc. are caught, not just ``127.0.0.1``).
    - The IPv6 loopback ``::1``.

    Anything else (real hostnames, public IPs, link-local addresses) is
    treated as non-loopback and left untouched by callers.
    """
    if not isinstance(host, str) or not host.strip():
        return False
    h = host.strip().lower()
    if h == "localhost":
        return True
    # Strip ``[`` / ``]`` for bracketed IPv6 forms like ``[::1]``.
    candidate = h.lstrip("[").rstrip("]")
    try:
        import ipaddress

        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


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


# secretRef URI scheme → fluid_build.secrets.SecretSource attribute name.
# Adding a new backend = one entry here + the corresponding SecretSource
# value already exists in fluid_build/secrets.py.
_SECRET_REF_BACKENDS: Dict[str, str] = {
    "vault": "HASHICORP_VAULT",
    "aws": "AWS_SECRETS_MANAGER",
    "gcp": "GCP_SECRET_MANAGER",
    "azure": "AZURE_KEY_VAULT",
    "file": "LOCAL_FILE",
}


def resolve_secret_ref(secret_ref: str) -> str:
    """Resolve a single ``secretRef`` URI to its literal value.

    secretRef syntax (per FLUID 0.7.3 schema): ``<scheme>://<identifier>``.

    Schemes
    -------
    - ``env://VAR_NAME`` — read ``os.environ[VAR_NAME]``. Short-circuits to
      ``os.environ`` to avoid spinning up a ``SecretManager`` for the
      hot-path local-dev case.
    - ``vault://path``    → HashiCorp Vault    via ``SecretManager``
    - ``aws://name``      → AWS Secrets Manager via ``SecretManager``
    - ``gcp://name``      → GCP Secret Manager  via ``SecretManager``
    - ``azure://name``    → Azure Key Vault     via ``SecretManager``
    - ``file://path``     → local encrypted file via ``SecretManager``

    The cloud / vault schemes delegate to ``fluid_build.secrets.SecretManager``,
    which already implements the backend-specific clients. Adding a new scheme
    is a one-entry change in ``_SECRET_REF_BACKENDS`` (assuming the matching
    ``SecretSource`` already exists in ``fluid_build/secrets.py``).

    Args:
        secret_ref: URI of the form ``<scheme>://<identifier>``.

    Returns:
        The resolved literal secret string.

    Raises:
        ValueError: ``secret_ref`` is malformed, references an unset env var,
            or uses an unsupported scheme.
    """
    if "://" not in secret_ref:
        raise ValueError(f"secretRef must be of the form '<scheme>://<identifier>': {secret_ref!r}")
    scheme, _, ident = secret_ref.partition("://")
    scheme = scheme.strip().lower()
    ident = ident.strip()
    if not scheme or not ident:
        raise ValueError(f"secretRef must be of the form '<scheme>://<identifier>': {secret_ref!r}")

    # Hot path: env:// short-circuit to os.environ.
    if scheme == "env":
        value = os.environ.get(ident)
        if value is None:
            raise ValueError(f"secretRef env://{ident}: environment variable not set")
        return value

    # Cloud / vault path: delegate to the existing SecretManager registry.
    backend = _SECRET_REF_BACKENDS.get(scheme)
    if backend is None:
        supported = ["env"] + sorted(_SECRET_REF_BACKENDS)
        raise ValueError(
            f"secretRef scheme {scheme!r} is not supported. Supported schemes: {supported}"
        )

    # Lazy import — the SecretManager pulls in optional cloud SDKs that we
    # don't want to load on every acquisition runner import.
    from fluid_build.secrets import SecretConfig, SecretManager, SecretSource

    source = getattr(SecretSource, backend)
    manager = SecretManager(SecretConfig(source=source))
    value = manager.get_secret(ident, required=True)
    if value is None:
        raise ValueError(f"secretRef {secret_ref}: backend returned no value")
    return value


def resolve_connection_secrets(
    connection: Dict[str, Any], *, target_field: str = "password"
) -> Dict[str, Any]:
    """Resolve a connection block's ``secretRef`` into a literal credential field.

    Convenience wrapper over :func:`resolve_secret_ref` for the common case
    where a SQL- or REST-style connection block has a single ``secretRef``
    that should be placed into a named credential field. Most acquisition
    runners (dlt sql_database, meltano tap-postgres, airbyte source-postgres,
    debezium connectors, …) want the secret as the ``password`` field of the
    underlying client — that's the default.

    Behaviour
    ---------
    1. Returns a NEW dict; never mutates the input.
    2. No ``secretRef`` → returns a shallow copy unchanged.
    3. ``secretRef`` is resolved via :func:`resolve_secret_ref`. Any scheme
       supported there works here.
    4. The resolved value is placed in ``connection[target_field]`` IFF that
       field is empty. An inline literal always wins over the secretRef.
    5. ``secretRef`` is removed from the returned dict so downstream client
       SDKs don't see a stray field.

    Args:
        connection: The raw ``properties.source.connection`` dict (the
            ``ConnectionSpec.raw`` view).
        target_field: Field to populate with the resolved secret. Most
            SQL-flavoured connections want ``"password"`` (the default).
            HTTP / REST sources that authenticate with a token can pass
            ``target_field="token"``; OAuth2 flows can pass
            ``target_field="access_token"``. The choice belongs to the
            runner because the secretRef schema is engine-agnostic.

    Returns:
        A new connection dict with the secret resolved into ``target_field``
        and ``secretRef`` removed.

    Example
    -------
    >>> os.environ["PG_PASSWORD"] = "s3cret"
    >>> resolve_connection_secrets({
    ...     "host": "db.example.com",
    ...     "port": 5432,
    ...     "user": "alice",
    ...     "secretRef": "env://PG_PASSWORD",
    ... })
    {'host': 'db.example.com', 'port': 5432, 'user': 'alice', 'password': 's3cret'}
    """
    out = dict(connection)
    secret_ref = out.pop("secretRef", None)
    if not secret_ref:
        return out
    value = resolve_secret_ref(secret_ref)
    if not out.get(target_field):
        out[target_field] = value
    return out


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
        from fluid_build.cli.console import error as console_error

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

    expose = (ctx.contract.get("exposes") or [{}])[0]
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
    return finalize_run_result(engine, ctx.build_id, result, succeeded_states=succeeded_states)
