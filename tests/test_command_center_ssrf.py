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

"""SSRF hardening for the Command Center client + reporter.

Both the detection client (``cli/_command_center.py``) and the async
reporter (``observability/reporter.py``) accept a user/config-supplied
URL and POST an ``X-API-Key`` against it. A poisoned config could point
that URL at a cloud-metadata endpoint and exfil the API key. The
``_command_center_host_allowed`` gate refuses private/link-local/
loopback/metadata hosts unless they are loopback (the documented
localhost default) or explicitly allow-listed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from fluid_build.cli._command_center import (
    CommandCenterClient,
)
from fluid_build.cli._command_center import (
    _command_center_host_allowed as _cc_client_gate,
)
from fluid_build.observability.config import CommandCenterConfig
from fluid_build.observability.reporter import (
    CommandCenterReporter,
)
from fluid_build.observability.reporter import (
    _command_center_host_allowed as _cc_reporter_gate,
)

_CLIENT_PRIV = "fluid_build.cli._command_center._hostname_is_private"
# reporter.py imports ``_hostname_is_private`` at top level from
# the tier-0 ``fluid_build._net`` leaf (PR #139 fixed the structural
# observability ↔ build_runners cycle, so the previous lazy import
# is no longer needed). Patch the alias bound on the reporter module
# itself — that's the name the gate function actually looks up.
_REPORTER_PRIV = "fluid_build.observability.reporter._hostname_is_private"


# ──────────────────── the shared gate (client copy) ────────────────────


class TestCommandCenterHostGate:
    def test_localhost_default_is_allowed(self):
        """The documented default ``http://localhost:8000`` must not be
        refused — it is a local dev server, not an SSRF target."""
        assert _cc_client_gate("http://localhost:8000") is True

    def test_loopback_ip_is_allowed(self):
        assert _cc_client_gate("http://127.0.0.1:8000") is True

    def test_metadata_endpoint_is_refused(self):
        assert _cc_client_gate("http://169.254.169.254/") is False

    def test_rfc1918_host_is_refused(self):
        assert _cc_client_gate("http://10.2.3.4:8000") is False

    def test_public_host_is_allowed(self):
        with patch(_CLIENT_PRIV, return_value=False):
            assert _cc_client_gate("https://cc.example.com") is True

    def test_allowlist_opts_internal_host_back_in(self, monkeypatch):
        monkeypatch.setenv("FLUID_COMMAND_CENTER_HOST_ALLOWLIST", "cc.internal")
        with patch(_CLIENT_PRIV, return_value=True):
            assert _cc_client_gate("https://host.cc.internal") is True

    def test_none_url_is_refused(self):
        assert _cc_client_gate(None) is False

    def test_no_host_url_is_refused(self):
        assert _cc_client_gate("not-a-url") is False


# ──────────────────── reporter start() gate ─────────────────────────────


class TestReporterSsrfGate:
    def test_reporter_refuses_metadata_url_on_start(self):
        """A reporter configured against the metadata service must
        disable itself on ``start()`` rather than POST the X-API-Key."""
        cfg = CommandCenterConfig(
            url="http://169.254.169.254", api_key="cc_secretkey", enabled=True
        )
        reporter = CommandCenterReporter(cfg)
        reporter.start()
        assert reporter.enabled is False
        assert reporter.running is False
        reporter.stop()

    def test_reporter_refuses_rfc1918_url_on_start(self):
        cfg = CommandCenterConfig(
            url="http://192.168.1.50:8000", api_key="cc_secretkey", enabled=True
        )
        reporter = CommandCenterReporter(cfg)
        reporter.start()
        assert reporter.enabled is False
        reporter.stop()

    def test_reporter_allows_localhost(self):
        """Localhost is the documented default — the reporter must
        still start against it."""
        cfg = CommandCenterConfig(url="http://localhost:8000", api_key="cc_secretkey", enabled=True)
        reporter = CommandCenterReporter(cfg)
        try:
            reporter.start()
            assert reporter.enabled is True
            assert reporter.running is True
        finally:
            reporter.stop()

    def test_reporter_allows_public_url(self):
        cfg = CommandCenterConfig(
            url="https://cc.example.com", api_key="cc_secretkey", enabled=True
        )
        reporter = CommandCenterReporter(cfg)
        try:
            with patch(_REPORTER_PRIV, return_value=False):
                reporter.start()
            assert reporter.enabled is True
        finally:
            reporter.stop()

    def test_reporter_allowlist_opts_internal_back_in(self, monkeypatch):
        monkeypatch.setenv("FLUID_COMMAND_CENTER_HOST_ALLOWLIST", "cc.internal")
        cfg = CommandCenterConfig(
            url="https://host.cc.internal", api_key="cc_secretkey", enabled=True
        )
        reporter = CommandCenterReporter(cfg)
        try:
            with patch(_REPORTER_PRIV, return_value=True):
                reporter.start()
            assert reporter.enabled is True
        finally:
            reporter.stop()


# ──────────────────── client _check_availability gate ──────────────────


class TestClientCheckAvailabilityGate:
    def test_check_availability_refuses_metadata_url(self, monkeypatch):
        """``_check_availability`` must bail before any ``requests``
        call when the configured URL is the metadata service."""
        monkeypatch.setenv("FLUID_COMMAND_CENTER_URL", "http://169.254.169.254")
        with patch("fluid_build.cli._command_center.requests") as mock_requests:
            client = CommandCenterClient()
        assert client.available is False
        # No HTTP probe should have been issued.
        assert not mock_requests.get.called
        assert not mock_requests.head.called

    def test_check_availability_refuses_rfc1918_url(self, monkeypatch):
        monkeypatch.setenv("FLUID_COMMAND_CENTER_URL", "http://10.0.0.9:8000")
        with patch("fluid_build.cli._command_center.requests") as mock_requests:
            client = CommandCenterClient()
        assert client.available is False
        assert not mock_requests.get.called

    def test_check_availability_probes_localhost_default(self, monkeypatch):
        """Localhost (the default) is allowed — the probe IS issued."""
        monkeypatch.delenv("FLUID_COMMAND_CENTER_URL", raising=False)
        monkeypatch.setenv("FLUID_DISABLE_CC_DETECTION", "false")
        with patch("fluid_build.cli._command_center.requests") as mock_requests:
            mock_requests.get.return_value = MagicMock(status_code=500)
            mock_requests.exceptions = __import__("requests").exceptions  # real exception classes
            client = CommandCenterClient()
        # Localhost default must reach the probe (status 500 → unavailable,
        # but the request WAS attempted, proving the gate let it through).
        assert mock_requests.get.called
