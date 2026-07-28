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

"""Tests for the audit webhook forwarder — the multi-instance HA
layer that POSTs every audit event to a SIEM aggregator. It must
FAIL OPEN on a webhook outage so an endpoint blip never blocks the
local-disk audit write.
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
