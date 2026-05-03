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
from typing import Any, Dict, Optional, Tuple

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
