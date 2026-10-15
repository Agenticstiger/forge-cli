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

"""Proxy-forwarded client-certificate identity is honoured only from a
configured trusted proxy.

The MCP output-port gateway can sit behind an mTLS-terminating proxy that
forwards the verified client certificate as ``X-Client-CN`` /
``X-Client-Fingerprint``. Those headers are only as trustworthy as the hop
that set them, so the gateway reads them solely when the TCP peer is listed
in ``FLUID_MCP_TRUSTED_PROXIES`` (uvicorn ``--forwarded-allow-ips``
semantics, default: trust nobody). These tests drive the real HTTP/SSE
transport, the real CLI and the real audit sink.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

duckdb = pytest.importorskip("duckdb")

from mcp import ClientSession  # noqa: E402
from mcp.client.sse import sse_client  # noqa: E402

from fluid_build._mcp_compat import self_attesting_client_kwargs  # noqa: E402
from fluid_build.output_ports.mcp import OutputPortMcpServer, OutputPortPolicy  # noqa: E402
from fluid_build.output_ports.mcp import auth as auth_mod  # noqa: E402

from ._fixtures import make_expose, write_customer_csv  # noqa: E402
from .test_cli_list_and_doctor import _make_demo  # noqa: E402
from .test_extensibility_and_runtime import _free_port, _running_sse_server  # noqa: E402

TOKEN = "test-shared-token-not-a-secret"
CERT_HEADERS = {"X-Client-CN": "C0001", "X-Client-Fingerprint": "ab12cd34"}


# ---------------------------------------------------------------------
# Unit: the trust decision itself
# ---------------------------------------------------------------------


def test_cert_headers_ignored_when_no_proxy_is_trusted():
    assert (
        auth_mod.extract_mtls_identity(CERT_HEADERS, peer_host="127.0.0.1", trusted_proxies=())
        == {}
    )


def test_cert_headers_ignored_from_a_peer_outside_the_trusted_set():
    trusted = auth_mod.parse_trusted_proxies("10.0.0.0/8")
    assert (
        auth_mod.extract_mtls_identity(CERT_HEADERS, peer_host="192.0.2.7", trusted_proxies=trusted)
        == {}
    )


def test_cert_headers_ignored_when_the_peer_is_unknown():
    trusted = auth_mod.parse_trusted_proxies("127.0.0.1")
    assert (
        auth_mod.extract_mtls_identity(CERT_HEADERS, peer_host=None, trusted_proxies=trusted) == {}
    )


@pytest.mark.parametrize(
    "spec, peer",
    [
        ("127.0.0.1", "127.0.0.1"),
        ("10.0.0.0/8", "10.1.2.3"),
        ("192.0.2.1, 10.0.0.0/8", "10.200.0.1"),
        ("::1", "::1"),
        ("fd00::/8", "fd00::42"),
    ],
)
def test_cert_headers_honoured_from_a_trusted_proxy(spec: str, peer: str):
    trusted = auth_mod.parse_trusted_proxies(spec)
    assert auth_mod.extract_mtls_identity(
        CERT_HEADERS, peer_host=peer, trusted_proxies=trusted
    ) == {
        "client_cn": "C0001",
        "client_fingerprint": "ab12cd34",
    }


def test_cert_header_values_with_control_characters_are_refused():
    trusted = auth_mod.parse_trusted_proxies("127.0.0.1")
    headers = {"X-Client-CN": "alice\r\nX-Injected: 1", "X-Client-Fingerprint": "ab\x00cd"}
    assert (
        auth_mod.extract_mtls_identity(headers, peer_host="127.0.0.1", trusted_proxies=trusted)
        == {}
    )


@pytest.mark.parametrize("spec", ["", "  ", None])
def test_trusted_proxies_default_is_empty(spec):
    assert auth_mod.parse_trusted_proxies(spec) == ()


@pytest.mark.parametrize("spec", ["not-an-ip", "10.0.0.0/33", "*", "127.0.0.1,,", "localhost"])
def test_malformed_trusted_proxies_is_a_typed_config_error(spec: str):
    with pytest.raises(auth_mod.AuthConfigError, match="FLUID_MCP_TRUSTED_PROXIES"):
        auth_mod.parse_trusted_proxies(spec)


@pytest.mark.parametrize("mode", ["mtls", "off", "JWT", ""])
def test_unknown_auth_mode_is_a_typed_config_error_listing_valid_modes(mode: str):
    with pytest.raises(auth_mod.AuthConfigError) as excinfo:
        auth_mod.AuthValidator(mode=mode)
    # Still a ValueError so existing ``except ValueError`` callers keep working.
    assert isinstance(excinfo.value, ValueError)
    assert excinfo.value.env_var == "FLUID_MCP_AUTH_MODE"
    assert excinfo.value.expected == "shared-token, jwt, none"
    for valid in ("shared-token", "jwt", "none"):
        assert valid in str(excinfo.value)


def test_from_env_reads_trusted_proxies(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FLUID_MCP_AUTH_MODE", "shared-token")
    monkeypatch.setenv("FLUID_MCP_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("FLUID_MCP_TRUSTED_PROXIES", "10.0.0.0/8,127.0.0.1")
    validator = auth_mod.AuthValidator.from_env()
    assert auth_mod.extract_mtls_identity(
        CERT_HEADERS, peer_host="10.9.9.9", trusted_proxies=validator.trusted_proxies
    )


def test_audit_identity_without_a_request_is_authkind_none():
    """stdio has no HTTP request, so no auth layer ran."""
    from fluid_build.output_ports.mcp.server import _audit_identity

    assert _audit_identity(None) == {"authKind": "none"}


def test_audit_identity_reads_only_the_trusted_proxy_channel():
    """A token claim that happens to be named client_cn is not reported as
    the certificate; only what the trusted-proxy check stamped is."""
    from types import SimpleNamespace

    from fluid_build.output_ports.mcp.server import _audit_identity

    scope = {"fluid_auth_kind": "jwt", "fluid_auth_attrs": {"client_cn": "from-a-claim"}}
    ctx = SimpleNamespace(request=SimpleNamespace(scope=scope))
    assert _audit_identity(ctx) == {"authKind": "jwt"}


# ---------------------------------------------------------------------
# Operator diagnostics: why forwarded certificate headers were ignored
# ---------------------------------------------------------------------


def _binder(spec: str, caplog: pytest.LogCaptureFixture):
    import logging

    from fluid_build.output_ports.mcp._transport import _ForwardedCertIdentity

    logger = logging.getLogger("fluid.test.forwarded_certs")
    caplog.set_level(logging.DEBUG, logger=logger.name)
    validator = auth_mod.AuthValidator(
        mode="shared-token",
        shared_token=TOKEN,
        trusted_proxies=auth_mod.parse_trusted_proxies(spec),
    )
    return _ForwardedCertIdentity(validator, logger)


def _bind(binder, peer: str, headers: Dict[str, str]) -> Dict[str, Any]:
    from types import SimpleNamespace

    request = SimpleNamespace(client=SimpleNamespace(host=peer), scope={"fluid_auth_attrs": {}})
    binder.bind(request, headers)
    return request.scope


def _ignored_records(caplog: pytest.LogCaptureFixture):
    return [r for r in caplog.records if "output_port_mtls_headers_ignored" in r.getMessage()]


def test_untrusted_peer_warning_names_the_peer_and_the_setting(caplog):
    binder = _binder("", caplog)
    _bind(binder, "192.0.2.9", CERT_HEADERS)
    (record,) = _ignored_records(caplog)
    assert record.levelname == "WARNING"
    message = record.getMessage()
    assert "192.0.2.9" in message
    assert "FLUID_MCP_TRUSTED_PROXIES=192.0.2.9" in message
    assert "C0001" not in message  # header values are not echoed


def test_empty_values_from_a_trusted_proxy_are_reported_as_empty(caplog):
    binder = _binder("127.0.0.1", caplog)
    _bind(binder, "127.0.0.1", {"X-Client-CN": "", "X-Client-Fingerprint": ""})
    (record,) = _ignored_records(caplog)
    assert "empty" in record.getMessage()
    assert "control characters" not in record.getMessage()


def test_control_characters_from_a_trusted_proxy_are_reported_as_such(caplog):
    binder = _binder("127.0.0.1", caplog)
    _bind(binder, "127.0.0.1", {"X-Client-CN": "a\x00b"})
    (record,) = _ignored_records(caplog)
    assert "control characters" in record.getMessage()


def test_one_reason_does_not_suppress_another(caplog):
    """A trusted proxy sending empty values must not hide a later untrusted
    peer, which is the case the log exists to diagnose."""
    binder = _binder("127.0.0.1", caplog)
    _bind(binder, "127.0.0.1", {"X-Client-CN": "", "X-Client-Fingerprint": ""})
    _bind(binder, "192.0.2.9", CERT_HEADERS)
    messages = [r.getMessage() for r in _ignored_records(caplog)]
    assert len(messages) == 2
    assert "192.0.2.9" in messages[1]


def test_ignored_header_warning_is_once_per_reason_and_peer_and_bounded(caplog):
    from fluid_build.output_ports.mcp._transport import _ForwardedCertIdentity

    binder = _binder("", caplog)
    for _ in range(3):
        _bind(binder, "192.0.2.9", CERT_HEADERS)
    assert len(_ignored_records(caplog)) == 1
    caplog.clear()
    limit = _ForwardedCertIdentity.MAX_LOGGED_PEERS
    for i in range(limit + 20):
        _bind(binder, f"198.51.100.{i}", CERT_HEADERS)
    records = _ignored_records(caplog)
    # One line per distinct peer up to the bound (192.0.2.9 already used a
    # slot), then a single "further ... suppressed" line, then silence.
    assert len(records) == limit
    assert "suppressed" in records[-1].getMessage()


def test_honoured_headers_bind_and_log_nothing(caplog):
    binder = _binder("127.0.0.1", caplog)
    scope = _bind(binder, "127.0.0.1", CERT_HEADERS)
    assert scope["fluid_mtls_identity"] == {"client_cn": "C0001", "client_fingerprint": "ab12cd34"}
    assert _ignored_records(caplog) == []


@pytest.mark.parametrize("attr", ["client_cn", "client_fingerprint"])
def test_missing_certificate_attribute_error_points_at_trusted_proxies(attr: str):
    from fluid_build.output_ports.mcp.query_compiler import (
        RowFilterIdentityMissing,
        compile_row_filter_clauses,
    )

    expose = {
        "policy": {"rowFilters": [{"column": "customer_id", "equals": f"${{caller.{attr}}}"}]}
    }
    with pytest.raises(RowFilterIdentityMissing, match="FLUID_MCP_TRUSTED_PROXIES"):
        compile_row_filter_clauses(expose, {"sub": "x"})


def test_missing_non_certificate_attribute_error_has_no_proxy_hint():
    from fluid_build.output_ports.mcp.query_compiler import (
        RowFilterIdentityMissing,
        compile_row_filter_clauses,
    )

    expose = {"policy": {"rowFilters": [{"column": "tenant", "equals": "${caller.tenant_id}"}]}}
    with pytest.raises(RowFilterIdentityMissing) as excinfo:
        compile_row_filter_clauses(expose, {"sub": "x"})
    assert "FLUID_MCP_TRUSTED_PROXIES" not in str(excinfo.value)


@pytest.mark.parametrize("bad", ["proxy.internal", "*"])
def test_trusted_proxies_error_example_is_a_single_address_not_a_wide_range(bad: str):
    with pytest.raises(auth_mod.AuthConfigError) as excinfo:
        auth_mod.parse_trusted_proxies(bad)
    assert "/8" not in str(excinfo.value)


# ---------------------------------------------------------------------
# End to end over the real HTTP/SSE transport
# ---------------------------------------------------------------------


def _cn_filtered_server(tmp_path: Path) -> OutputPortMcpServer:
    """An expose whose rowFilter keys on the forwarded certificate CN."""
    csv_path = write_customer_csv(tmp_path / "customers.csv")
    expose = make_expose(
        binding={
            "platform": "local",
            "format": "csv",
            "location": {"path": str(csv_path), "table": "customer_profiles"},
        },
    )
    expose["policy"] = {
        "rowFilters": [{"column": "customer_id", "equals": "${caller.client_cn}"}],
    }
    contract = {
        "fluidVersion": "0.7.4",
        "kind": "DataProduct",
        "id": "cn.local.demo",
        "exposes": [expose],
    }
    policy = OutputPortPolicy.from_contract_and_flags(
        expose=expose, readable_paths=(tmp_path.resolve(),)
    )
    return OutputPortMcpServer(contract=contract, expose=expose, policy=policy)


def _audit_records(audit_root: Path) -> List[Dict[str, Any]]:
    return [
        json.loads(p.read_text(encoding="utf-8"))["payload"]
        for p in sorted(audit_root.glob("*_data_access.json"))
    ]


async def _sample_over_sse(port: int, headers: Dict[str, str]) -> Dict[str, Any]:
    url = f"http://127.0.0.1:{port}/sse"
    async with sse_client(url, headers=headers) as (read_stream, write_stream):
        async with ClientSession(
            read_stream, write_stream, **self_attesting_client_kwargs("cn-test", "1.0.0")
        ) as session:
            await session.initialize()
            result = await session.call_tool("sample", {"limit": 10})
            return json.loads(result.content[0].text)


def _auth_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    audit_root = tmp_path / "audit"
    monkeypatch.setenv("FLUID_AUDIT_ROOT", str(audit_root))
    monkeypatch.setenv("FLUID_MCP_AUTH_MODE", "shared-token")
    monkeypatch.setenv("FLUID_MCP_AUTH_TOKEN", TOKEN)
    monkeypatch.delenv("FLUID_MCP_TRUSTED_PROXIES", raising=False)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    return audit_root


@pytest.mark.asyncio
async def test_direct_client_cannot_choose_its_certificate_cn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No trusted proxy configured: a client that sets X-Client-CN itself
    does not get rows selected by that value, and the audit record shows the
    real auth layer with no certificate identity."""
    audit_root = _auth_env(monkeypatch, tmp_path)
    server = _cn_filtered_server(tmp_path)
    port = _free_port()
    with _running_sse_server(server, port):
        payload = await _sample_over_sse(port, {"Authorization": f"Bearer {TOKEN}", **CERT_HEADERS})
    assert payload.get("rows") is None, payload
    assert payload.get("error"), payload
    records = _audit_records(audit_root)
    assert records, "a data_access record must be written"
    assert records[0]["authKind"] == "shared-token"
    assert "clientCertCn" not in records[0]
    assert "clientCertFingerprint" not in records[0]
    # The operator-facing audit trail says which setting would bind it.
    annotated = [
        json.loads(path.read_text(encoding="utf-8"))["payload"].get("annotatedMessage", "")
        for path in sorted(audit_root.glob("*.json"))
    ]
    assert any("FLUID_MCP_TRUSTED_PROXIES" in m for m in annotated), annotated


@pytest.mark.asyncio
async def test_forwarded_for_header_cannot_impersonate_a_trusted_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The trust check reads the TCP peer, not an X-Forwarded-For value the
    client wrote."""
    _auth_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FLUID_MCP_TRUSTED_PROXIES", "192.0.2.10")
    server = _cn_filtered_server(tmp_path)
    port = _free_port()
    with _running_sse_server(server, port):
        payload = await _sample_over_sse(
            port,
            {
                "Authorization": f"Bearer {TOKEN}",
                "X-Forwarded-For": "192.0.2.10",
                **CERT_HEADERS,
            },
        )
    assert payload.get("rows") is None, payload


@pytest.mark.asyncio
async def test_trusted_proxy_certificate_identity_binds_and_is_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    audit_root = _auth_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FLUID_MCP_TRUSTED_PROXIES", "127.0.0.1,::1")
    server = _cn_filtered_server(tmp_path)
    port = _free_port()
    with _running_sse_server(server, port):
        payload = await _sample_over_sse(port, {"Authorization": f"Bearer {TOKEN}", **CERT_HEADERS})
    assert payload.get("error") is None, payload
    assert [row["customer_id"] for row in payload["rows"]] == ["C0001"]
    record = _audit_records(audit_root)[0]
    assert record["authKind"] == "shared-token"
    assert record["clientCertCn"] == "C0001"
    assert record["clientCertFingerprint"] == "ab12cd34"


# ---------------------------------------------------------------------
# CLI: invalid configuration is a typed error, not a traceback
# ---------------------------------------------------------------------


def _serve(tmp_path: Path, env_overrides: Dict[str, str], *, transport: str):
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(repo_root),
            "HOME": str(tmp_path / "home"),
            "FLUID_QUIET": "1",
            "FLUID_NONINTERACTIVE": "1",
            "FLUID_AUDIT_ROOT": str(tmp_path / "audit"),
        }
    )
    for noisy in ("FLUID_LOG_LEVEL", "FLUID_DEBUG", "FLUID_SHOW_REGISTRATION"):
        env.pop(noisy, None)
    env.update(env_overrides)
    (tmp_path / "home").mkdir(exist_ok=True)
    contract = _make_demo(tmp_path)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "fluid_build",
            "mcp",
            "output-port",
            "serve",
            str(contract),
            "--transport",
            transport,
            "--port",
            str(_free_port()),
            "--readable-paths",
            str(tmp_path),
        ],
        cwd=str(tmp_path),
        env=env,
        input="",
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_unknown_auth_mode_exits_2_with_valid_modes_and_no_traceback(tmp_path: Path):
    proc = _serve(tmp_path, {"FLUID_MCP_AUTH_MODE": "mtls"}, transport="http")
    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "invalid_auth_mode" in output
    assert "shared-token" in output and "jwt" in output and "none" in output
    assert "Traceback" not in output


def test_cli_malformed_trusted_proxies_exits_2_without_traceback(tmp_path: Path):
    proc = _serve(
        tmp_path,
        {
            "FLUID_MCP_AUTH_MODE": "shared-token",
            "FLUID_MCP_AUTH_TOKEN": TOKEN,
            "FLUID_MCP_TRUSTED_PROXIES": "proxy.internal",
        },
        transport="http",
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "invalid_trusted_proxies" in output
    assert "Traceback" not in output


def test_cli_stdio_still_ignores_auth_mode(tmp_path: Path):
    """stdio never reads FLUID_MCP_AUTH_MODE; a value it tolerated before
    must not start failing now."""
    proc = _serve(tmp_path, {"FLUID_MCP_AUTH_MODE": "mtls"}, transport="stdio")
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "invalid_auth_mode" not in output
