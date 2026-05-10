# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for the multi-instance HA layers — audit webhook
forwarding + Redis-backed circuit breaker fallback. Both layers
must FAIL OPEN on backend outage so a Redis hiccup or webhook
endpoint blip doesn't take down every gateway worldwide.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.copilot.store.audit_trail import write_audit_event

# ---------------------------------------------------------------------
# Audit webhook forwarding
# ---------------------------------------------------------------------


def test_audit_webhook_disabled_when_env_var_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """No FLUID_MCP_AUDIT_WEBHOOK_URL → no httpx import, no thread
    spawn, just the local-disk write."""
    monkeypatch.delenv("FLUID_MCP_AUDIT_WEBHOOK_URL", raising=False)
    with patch("httpx.post") as posted:
        path = write_audit_event("data_access", payload={"x": 1}, root=tmp_path / "audit")
    # File landed locally.
    assert path.exists()
    # No webhook attempt.
    posted.assert_not_called()


def test_audit_webhook_posts_when_env_var_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When FLUID_MCP_AUDIT_WEBHOOK_URL is set, every event is
    POST-ed to the URL on a background thread (so the dispatch
    path doesn't pay the network round-trip)."""
    monkeypatch.setenv("FLUID_MCP_AUDIT_WEBHOOK_URL", "https://siem.example/audit")
    monkeypatch.setenv("FLUID_MCP_AUDIT_WEBHOOK_HEADER_AUTH", "Bearer s3cret")
    posted_payloads: List[Dict[str, Any]] = []
    posted_event = threading.Event()

    def _fake_post(url, json=None, headers=None, timeout=None):
        posted_payloads.append({"url": url, "json": json, "headers": dict(headers or {})})
        posted_event.set()
        return MagicMock(status_code=200)

    with patch("httpx.post", side_effect=_fake_post):
        path = write_audit_event(
            "data_access",
            payload={"tool": "sample", "decision": "allow"},
            root=tmp_path / "audit",
        )
        # Background thread is daemon; give it up to 2s to fire.
        assert posted_event.wait(timeout=2.0), "webhook POST never fired"

    # Local-disk write still happened.
    assert path.exists()
    # Webhook called once with the right URL + auth header + payload.
    assert len(posted_payloads) == 1
    sent = posted_payloads[0]
    assert sent["url"] == "https://siem.example/audit"
    assert sent["headers"]["authorization"] == "Bearer s3cret"
    assert sent["json"]["event"] == "data_access"
    assert sent["json"]["payload"]["decision"] == "allow"


def test_audit_webhook_failure_does_not_block_local_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """If the webhook is down, the local audit write still
    succeeds — fail-open is the right posture for compliance
    durability (lose remote, keep local)."""
    monkeypatch.setenv("FLUID_MCP_AUDIT_WEBHOOK_URL", "https://broken.example/audit")
    with patch("httpx.post", side_effect=RuntimeError("connection refused")):
        path = write_audit_event("data_access", payload={"x": 1}, root=tmp_path / "audit")
    assert path.exists(), "webhook outage must not block local write"
    body = json.loads(path.read_text())
    assert body["event"] == "data_access"


# ---------------------------------------------------------------------
# Redis-backed circuit breaker
# ---------------------------------------------------------------------


def test_circuit_breaker_redis_falls_back_to_in_process_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    """FLUID_MCP_CIRCUIT_BACKEND=redis with an unreachable Redis
    must fall back to in-process state, not block forever or
    fail-closed."""
    from fluid_build.output_ports.mcp.server import _CircuitBreaker

    monkeypatch.setenv("FLUID_MCP_CIRCUIT_BACKEND", "redis")
    monkeypatch.setenv("FLUID_MCP_CIRCUIT_REDIS_URL", "redis://nonexistent-host:1/0")

    cb = _CircuitBreaker(threshold=2, window_seconds=60.0, cooldown_seconds=30.0)
    # First call → tries Redis, fails, falls back to in-process.
    assert cb.is_open() is False
    # Trigger threshold via in-process counter.
    assert cb.record_failure() is False
    tripped = cb.record_failure()
    assert tripped is True
    assert cb.is_open() is True


def test_circuit_breaker_redis_backend_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
):
    """Without the env var, no Redis client is built — backwards-
    compatible single-process behaviour."""
    from fluid_build.output_ports.mcp.server import _CircuitBreaker

    monkeypatch.delenv("FLUID_MCP_CIRCUIT_BACKEND", raising=False)
    cb = _CircuitBreaker(threshold=2)
    assert cb._is_redis_enabled() is False
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open() is True
