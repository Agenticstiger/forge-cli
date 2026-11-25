# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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


def test_row_filter_resolves_to_the_callers_qualified_predicate():
    """``policy.authz.rowFilters`` resolves to the predicate of the filter naming the
    caller's verified principal, its columns qualified with the projection alias.
    The predicate is the contract's own literal SQL and binds no parameter, so no
    caller value ever reaches it."""
    from fluid_build.output_ports.mcp.query_compiler import row_filter_predicate

    expose = {
        "exposeId": "demo",
        "contract": {"schema": [{"name": "tenant_id"}, {"name": "region"}]},
        "policy": {
            "authz": {
                "rowFilters": [
                    {"principal": "tenant:acme", "where": "tenant_id = 'acme'"},
                    {"principal": "group:us", "where": "region IN ('us', 'eu')"},
                ]
            }
        },
    }
    assert row_filter_predicate(expose, {"principal": "group:us"}) == (
        "((_fluid_rows.region IN ('us', 'eu')))"
    )


def test_row_filter_fail_closed_when_the_principal_is_missing():
    """A request with no principal a filter names must raise
    ``RowFilterIdentityMissing`` so the gateway never serves rows under an
    undefined identity."""
    from fluid_build.output_ports.mcp.drivers.base import RowFilterIdentityMissing
    from fluid_build.output_ports.mcp.query_compiler import row_filter_predicate

    expose = {
        "exposeId": "demo",
        "contract": {"schema": [{"name": "tenant_id"}]},
        "policy": {
            "authz": {"rowFilters": [{"principal": "tenant:acme", "where": "tenant_id = 'acme'"}]}
        },
    }
    with pytest.raises(RowFilterIdentityMissing, match="principal"):
        row_filter_predicate(expose, {"model_id": "claude-haiku", "tenant_id": "acme"})


def test_row_filter_no_op_when_no_filters_configured():
    """Empty / absent rowFilters → no predicate."""
    from fluid_build.output_ports.mcp.query_compiler import row_filter_predicate

    assert row_filter_predicate({"exposeId": "demo"}, {"principal": "anything"}) is None


def test_row_filter_rejects_an_injection_shaped_predicate():
    """A predicate is parsed, not trusted: a statement separator in the contract
    fails fast instead of reaching the engine."""
    from fluid_build.output_ports.mcp.drivers.base import RowFilterUnavailable
    from fluid_build.output_ports.mcp.query_compiler import row_filter_predicate

    expose = {
        "exposeId": "demo",
        "contract": {"schema": [{"name": "tenant_id"}]},
        "policy": {
            "authz": {
                "rowFilters": [{"principal": "tenant:x", "where": "tenant_id = 'x'; DROP TABLE t"}]
            }
        },
    }
    with pytest.raises(RowFilterUnavailable):
        row_filter_predicate(expose, {"principal": "tenant:x"})


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
