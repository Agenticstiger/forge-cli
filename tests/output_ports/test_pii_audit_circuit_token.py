# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Unit tests for the v0.7.4 hardening layers added to the gateway:

* PII / PHI row-level redaction (``sensitivity:pii`` / ``sensitivity:phi``).
* Audit-log rotation by age + total size, atomic writes.
* Circuit-breaker tripping after repeated driver failures.
* Token-budget enforcement (``maxTokensPerDay`` + per-request cap).

These pin the contracts the integration / live-LLM tests rely on so
a future refactor can't silently regress the protective layers.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.copilot.store.audit_trail import (
    rotate_audit_directory,
    write_audit_event,
)
from fluid_build.output_ports.mcp.drivers.base import EngineDriver
from fluid_build.output_ports.mcp.policy import OutputPortPolicy

# ---------------------------------------------------------------------
# PII redaction at the row boundary
# ---------------------------------------------------------------------


def _pii_expose():
    return {
        "exposeId": "demo",
        "contract": {
            "schema": [
                {"name": "id", "type": "STRING", "sensitivity": "cleartext"},
                {"name": "email", "type": "STRING", "sensitivity": "pii"},
                {"name": "ssn", "type": "STRING", "sensitivity": "phi"},
                {"name": "tag", "type": "STRING", "sensitivity": "sensitive"},
                {"name": "name", "type": "STRING"},
            ]
        },
    }


def test_pii_columns_detected_for_pii_phi_and_sensitive_markers():
    pii = EngineDriver._compute_pii_columns(_pii_expose())
    assert pii == {"email", "ssn", "tag"}


def test_pii_values_are_redacted_columns_remain_visible():
    row = {
        "id": "x1",
        "email": "alice@example.com",
        "ssn": "123-45-6789",
        "tag": "internal-only",
        "name": "Alice",
    }
    masked = EngineDriver._mask_row(
        row,
        ["id", "email", "ssn", "tag", "name"],
        pii_lower={"email", "ssn", "tag"},
    )
    # Column keys still present (so the agent knows the field exists).
    assert set(masked.keys()) == {"id", "email", "ssn", "tag", "name"}
    # PII values are the redaction token.
    assert masked["email"] == EngineDriver.PII_TOKEN
    assert masked["ssn"] == EngineDriver.PII_TOKEN
    assert masked["tag"] == EngineDriver.PII_TOKEN
    # Non-PII values pass through.
    assert masked["id"] == "x1"
    assert masked["name"] == "Alice"


def test_pii_redaction_preserves_null_values_unchanged():
    row = {"id": "x1", "email": None, "name": "Alice"}
    masked = EngineDriver._mask_row(row, ["id", "email", "name"], pii_lower={"email"})
    # None stays None (no point redacting an absent value).
    assert masked["email"] is None


# ---------------------------------------------------------------------
# Audit log rotation
# ---------------------------------------------------------------------


def test_audit_rotation_removes_files_older_than_max_age(tmp_path: Path):
    root = tmp_path / "audit"
    root.mkdir()
    for i in range(20):
        write_audit_event("data_access", payload={"i": i}, root=root)
    # Force half of them to look 10 days old.
    files = sorted(root.glob("*.json"))
    old_mtime = time.time() - (10 * 86400)
    for path in files[:10]:
        os.utime(path, (old_mtime, old_mtime))

    counters = rotate_audit_directory(root=root, max_age_days=5, max_total_bytes=None)
    assert counters["removed_age"] == 10
    assert counters["kept"] == 10


def test_audit_rotation_size_cap_drops_oldest_until_under_budget(tmp_path: Path):
    root = tmp_path / "audit"
    root.mkdir()
    for i in range(50):
        write_audit_event("data_access", payload={"i": i}, root=root)
    total = sum(p.stat().st_size for p in root.glob("*.json"))
    counters = rotate_audit_directory(root=root, max_age_days=None, max_total_bytes=total // 4)
    assert counters["removed_size"] > 0
    assert counters["total_bytes_after"] <= total // 4


def test_audit_rotation_no_op_on_missing_dir(tmp_path: Path):
    counters = rotate_audit_directory(root=tmp_path / "does-not-exist")
    assert counters == {"removed_age": 0, "removed_size": 0, "kept": 0, "total_bytes_after": 0}


def test_audit_writer_atomic_no_part_files_left_behind(tmp_path: Path):
    root = tmp_path / "audit"
    for _ in range(10):
        write_audit_event("data_access", payload={"x": 1}, root=root)
    assert not list(root.glob("*.part")), "atomic-write tempfiles must not leak"


# ---------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------


def test_circuit_breaker_trips_after_threshold_failures():
    from fluid_build.output_ports.mcp.server import _CircuitBreaker

    cb = _CircuitBreaker(threshold=3, window_seconds=60.0, cooldown_seconds=10.0)
    assert cb.is_open() is False
    assert cb.record_failure() is False
    assert cb.record_failure() is False
    tripped = cb.record_failure()
    assert tripped is True
    assert cb.is_open() is True


def test_circuit_breaker_resets_after_cooldown():
    from fluid_build.output_ports.mcp.server import _CircuitBreaker

    cb = _CircuitBreaker(threshold=2, window_seconds=60.0, cooldown_seconds=0.05)
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open() is True
    time.sleep(0.07)
    # is_open() lazily resets when the cooldown has elapsed.
    assert cb.is_open() is False


def test_circuit_breaker_record_success_partially_heals():
    from fluid_build.output_ports.mcp.server import _CircuitBreaker

    cb = _CircuitBreaker(threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()  # erodes the failure count
    cb.record_failure()
    # Still under threshold (2 active failures): not tripped.
    assert cb.is_open() is False


def test_circuit_breaker_warns_once_when_redis_degrades(caplog):
    """Opting into the Redis fleet backend then losing Redis must NOT
    fail silently: the breaker degrades to in-process AND warns ONCE
    per process that fleet-wide coordination is OFF (parity with the
    rate limiter's ``redis-unavailable-fallback-open`` signal). A
    silent degrade would hide that the safety feature the operator
    opted into is no longer active."""
    import logging

    from fluid_build.output_ports.mcp import _circuit
    from fluid_build.output_ports.mcp.server import _CircuitBreaker

    # Reset the process-global one-shot guard so the assertion is
    # deterministic under pytest-randomly ordering.
    _circuit._WARNED_REDIS_DEGRADED = False

    cb = _CircuitBreaker()
    with caplog.at_level(logging.WARNING, logger="fluid.output_port.mcp.circuit"):
        cb._mark_redis_unavailable()
        cb._mark_redis_unavailable()  # second call must NOT re-warn

    assert cb._redis_unavailable is True
    degraded = [
        r for r in caplog.records if "fleet-wide breaker coordination is OFF" in r.getMessage()
    ]
    assert len(degraded) == 1, "expected exactly one degrade warning (one-shot)"
    # The warning must be actionable — it names the env var to set.
    assert "FLUID_MCP_CIRCUIT_REDIS_URL" in degraded[0].getMessage()


# ---------------------------------------------------------------------
# Token-budget enforcement on SessionState
# ---------------------------------------------------------------------


def test_token_budget_unbounded_when_field_absent():
    import logging

    from fluid_build.output_ports.mcp.server import SessionState

    expose: Dict[str, Any] = {"exposeId": "demo"}
    state = SessionState(
        contract={},
        expose=expose,
        policy=OutputPortPolicy.from_contract_and_flags(expose=expose),
        logger=logging.getLogger("test"),
    )
    assert state.check_token_budget(estimated_tokens=10_000_000) == (True, None)


def test_token_budget_denies_when_request_would_exceed_daily_cap():
    import logging

    from fluid_build.output_ports.mcp.server import SessionState

    expose: Dict[str, Any] = {
        "exposeId": "demo",
        "policy": {"agentPolicy": {"maxTokensPerDay": 1000}},
    }
    state = SessionState(
        contract={},
        expose=expose,
        policy=OutputPortPolicy.from_contract_and_flags(expose=expose),
        logger=logging.getLogger("test"),
    )
    state.record_tokens(950)
    ok, reason = state.check_token_budget(estimated_tokens=100)
    assert ok is False
    assert "token-budget-exceeded" in (reason or "")


def test_row_filter_compiles_to_parameterised_where_with_caller_attrs():
    """Per-tenant row filters resolve ${caller.<attr>} into bound
    parameters, not string interpolation. Catches the regression
    where a future contract author writes ``${caller.tenant_id}``
    expecting it to be a parameter and accidentally gets a literal
    SQL injection."""
    from fluid_build.output_ports.mcp.drivers.base import EngineDriver

    expose = {
        "exposeId": "demo",
        "policy": {
            "rowFilters": [
                {"column": "tenant_id", "equals": "${caller.tenant_id}"},
                {"column": "region", "in": "${caller.regions}"},
            ]
        },
    }
    where, params = EngineDriver.compile_row_filter_predicate(
        expose,
        caller_attributes={"tenant_id": "acme", "regions": ["us", "eu"]},
    )
    assert where.startswith(" WHERE ")
    assert '"tenant_id" = :p_0' in where
    assert '"region" IN (:p_1, :p_2)' in where
    assert params == ["acme", "us", "eu"]


def test_row_filter_fail_closed_when_caller_attribute_missing():
    """Missing caller attribute must raise ``RowFilterIdentityMissing``
    so the gateway never serves rows under an undefined identity."""
    from fluid_build.output_ports.mcp.drivers.base import (
        EngineDriver,
        RowFilterIdentityMissing,
    )

    expose = {
        "exposeId": "demo",
        "policy": {"rowFilters": [{"column": "tenant_id", "equals": "${caller.tenant_id}"}]},
    }
    with pytest.raises(RowFilterIdentityMissing, match="tenant_id"):
        EngineDriver.compile_row_filter_predicate(
            expose, caller_attributes={"model_id": "claude-haiku"}
        )


def test_row_filter_no_op_when_no_filters_configured():
    """Empty / absent rowFilters → no WHERE clause, no params."""
    from fluid_build.output_ports.mcp.drivers.base import EngineDriver

    where, params = EngineDriver.compile_row_filter_predicate(
        {"exposeId": "demo"}, caller_attributes={"tenant_id": "anything"}
    )
    assert where == ""
    assert params == []


def test_row_filter_rejects_invalid_identifier_via_sql_safety():
    """Column names get routed through validate_ident — an
    injection-shaped column name in the contract fails fast."""
    from fluid_build.output_ports.mcp.drivers.base import EngineDriver

    expose = {
        "exposeId": "demo",
        "policy": {"rowFilters": [{"column": "tenant_id; DROP TABLE", "equals": "x"}]},
    }
    with pytest.raises(Exception):
        EngineDriver.compile_row_filter_predicate(expose, caller_attributes={"tenant_id": "x"})


def test_rate_limit_redis_backend_falls_back_open_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    """When Redis is unreachable, the gateway must fall back to
    the in-process rate-limit and continue serving — failing
    closed on Redis outage would mean a Redis hiccup blocks every
    forge-cli installation worldwide."""
    import logging

    from fluid_build.output_ports.mcp.server import SessionState

    monkeypatch.setenv("FLUID_MCP_RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setenv("FLUID_MCP_RATE_LIMIT_REDIS_URL", "redis://nonexistent-host:1/0")
    state = SessionState(
        contract={"id": "demo"},
        expose={"exposeId": "demo"},
        policy=OutputPortPolicy.from_contract_and_flags(expose={"exposeId": "demo"}),
        logger=logging.getLogger("test"),
        rate_limit_calls=2,
    )
    # First two calls succeed (in-process fallback).
    assert state.check_rate_limit() == (True, None)
    assert state.check_rate_limit() == (True, None)
    # Third call hits the in-process cap.
    ok, reason = state.check_rate_limit()
    assert ok is False
    assert "rate-limit-exceeded" in (reason or "")


def test_token_budget_window_resets_after_24h():
    import logging

    from fluid_build.output_ports.mcp.server import SessionState

    expose: Dict[str, Any] = {
        "exposeId": "demo",
        "policy": {"agentPolicy": {"maxTokensPerDay": 100}},
    }
    state = SessionState(
        contract={},
        expose=expose,
        policy=OutputPortPolicy.from_contract_and_flags(expose=expose),
        logger=logging.getLogger("test"),
    )
    state.record_tokens(100)
    # Force the window to look stale.
    state._tokens_today_window_start = time.monotonic() - state._DAILY_WINDOW_SECONDS - 1
    ok, reason = state.check_token_budget(estimated_tokens=50)
    assert ok is True
    assert state._tokens_today == 0  # rolled
