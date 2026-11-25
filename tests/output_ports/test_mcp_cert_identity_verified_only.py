# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``client_cn`` / ``client_fingerprint`` and the row-filter ``principal`` bind
only from a verified credential.

The first two name a client certificate. The only party that can vouch for one is
the mTLS-terminating proxy listed in ``FLUID_MCP_TRUSTED_PROXIES``, and the gateway
reads that proxy's headers only on an authenticated request. The principal decides
which rows ``policy.authz.rowFilters`` lets a caller read. So, like
``jurisdiction``, all three are verified-only: a value the client supplies about
itself never binds them, whatever the auth mode, and a gateway that could never
bind a principal its row filters need refuses to start instead of serving every
call a denial.

These tests drive the resolver, the real Streamable HTTP transport, the in-memory
stdio session and the real CLI.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build._mcp_compat import (  # noqa: E402
    open_inmemory_session,
    open_streamable_http_session,
    self_attesting_client_kwargs,
)
from fluid_build.output_ports.mcp import OutputPortMcpServer, OutputPortPolicy  # noqa: E402
from fluid_build.output_ports.mcp import auth as auth_mod  # noqa: E402

from ._fixtures import make_expose, write_customer_csv  # noqa: E402
from .test_extensibility_and_runtime import _free_port, _running_http_server  # noqa: E402

TOKEN = "test-shared-token-not-a-secret"  # pragma: allowlist secret
CERT_ATTRS = ("client_cn", "client_fingerprint")
#: The principal the row filter on ``_cn_expose`` names; a client that states it
#: about itself must not read its rows.
CN_PRINCIPAL = "client:C0001"

#: The three ways ``--transport http`` ends up serving without auth.
UNAUTHENTICATED_HTTP_ENVS: Dict[str, Dict[str, str]] = {
    "mode-none": {"FLUID_MCP_AUTH_MODE": "none"},
    "jwt-incomplete": {
        "FLUID_MCP_AUTH_MODE": "jwt",
        "FLUID_MCP_JWT_ISSUER": "https://issuer.example.invalid/",
    },
    "shared-token-without-token": {"FLUID_MCP_AUTH_MODE": "shared-token"},
}
_AUTH_ENV_VARS = (
    "FLUID_MCP_AUTH_MODE",
    "FLUID_MCP_AUTH_TOKEN",
    "FLUID_MCP_JWT_ISSUER",
    "FLUID_MCP_JWT_AUDIENCE",
    "FLUID_MCP_JWT_JWKS_URL",
    "FLUID_MCP_TRUSTED_PROXIES",
    "FORWARDED_ALLOW_IPS",
)


def _cn_expose(tmp_path: Path) -> Dict[str, Any]:
    csv_path = write_customer_csv(tmp_path / "customers.csv")
    expose = make_expose(
        binding={
            "platform": "local",
            "format": "csv",
            "location": {"path": str(csv_path), "table": "customer_profiles"},
        },
    )
    expose["policy"] = {
        "authz": {"rowFilters": [{"principal": CN_PRINCIPAL, "where": "customer_id = 'C0001'"}]},
    }
    return expose


def _server(expose: Dict[str, Any], tmp_path: Path) -> OutputPortMcpServer:
    contract = {
        "fluidVersion": "0.7.4",
        "kind": "DataProduct",
        "id": "cn.local.demo",
        "exposes": [expose],
    }
    policy = OutputPortPolicy.from_contract_and_flags(
        expose=expose, readable_paths=(tmp_path.resolve(),)
    )
    return OutputPortMcpServer(
        contract=contract,
        expose=expose,
        policy=policy,
        logger=logging.getLogger("test.cert_identity"),
    )


# ---------------------------------------------------------------------
# Resolver: the self-attested tier never carries a certificate attribute
# ---------------------------------------------------------------------


def _request_context(
    *, client_info: Dict[str, Any], scope: Optional[Dict[str, Any]]
) -> SimpleNamespace:
    session = SimpleNamespace(
        client_params=SimpleNamespace(clientInfo=SimpleNamespace(model_extra=dict(client_info)))
    )
    request = SimpleNamespace(scope=scope) if scope is not None else None
    return SimpleNamespace(session=session, request=request)


@pytest.mark.parametrize(
    "scope",
    [None, {}],
    ids=["stdio", "http-without-auth"],
)
def test_self_attested_certificate_attributes_never_bind(tmp_path: Path, scope):
    server = _server(_cn_expose(tmp_path), tmp_path)
    _model, _use_case, attrs = server._resolve_request_identity(
        _request_context(
            client_info={
                "client_cn": "C0002",
                "client_fingerprint": "ff",
                "principal": CN_PRINCIPAL,
                "tenant_id": "acme",
            },
            scope=scope,
        )
    )
    for attr in (*CERT_ATTRS, "principal"):
        assert attr not in attrs, attrs
    # Unauthenticated self-attestation of other attributes is unchanged.
    assert attrs["tenant_id"] == "acme"


def test_verified_certificate_attributes_still_bind(tmp_path: Path):
    server = _server(_cn_expose(tmp_path), tmp_path)
    _model, _use_case, attrs = server._resolve_request_identity(
        _request_context(
            client_info={"client_cn": "C0002"},
            scope={
                "fluid_auth_kind": "shared-token",
                "fluid_auth_attrs": {"client_cn": "C0001", "client_fingerprint": "ab12"},
            },
        )
    )
    assert attrs["client_cn"] == "C0001"
    assert attrs["client_fingerprint"] == "ab12"


# ---------------------------------------------------------------------
# End to end: unauthenticated HTTP and stdio
# ---------------------------------------------------------------------


async def _sample_over_http(port: int, headers: Dict[str, str], **attest: Any) -> Dict[str, Any]:
    async with open_streamable_http_session(
        f"http://127.0.0.1:{port}/mcp",
        headers=headers,
        **self_attesting_client_kwargs("cn-test", "1.0.0", **attest),
    ) as session:
        result = await session.call_tool("sample", {"limit": 10})
        return json.loads(result.content[0].text)


def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: Dict[str, str]) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    audit_root = tmp_path / "audit"
    monkeypatch.setenv("FLUID_AUDIT_ROOT", str(audit_root))
    for name in _AUTH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return audit_root


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_env", list(UNAUTHENTICATED_HTTP_ENVS), ids=str)
async def test_unauthenticated_http_does_not_bind_a_self_attested_cn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth_env: str
):
    """Behind a listed proxy but with auth not actually enabled, a client
    naming its own CN and principal gets no rows: neither the proxy header nor
    the self-attested values bind, so the row filter refuses the call."""
    audit_root = _isolated_env(
        monkeypatch,
        tmp_path,
        {**UNAUTHENTICATED_HTTP_ENVS[auth_env], "FLUID_MCP_TRUSTED_PROXIES": "127.0.0.1,::1"},
    )
    server = _server(_cn_expose(tmp_path), tmp_path)
    port = _free_port()
    with _running_http_server(server, port):
        payload = await _sample_over_http(
            port,
            {"X-Client-CN": "C0001"},
            client_cn="C0002",
            client_fingerprint="ff",
            principal=CN_PRINCIPAL,
        )
    assert payload.get("rows") is None, payload
    assert payload.get("error"), payload
    records = [
        json.loads(p.read_text(encoding="utf-8"))["payload"]
        for p in sorted(audit_root.glob("*_data_access.json"))
    ]
    assert records and "clientCertCn" not in records[0], records


@pytest.mark.asyncio
async def test_unauthenticated_http_without_a_proxy_does_not_bind_a_self_attested_cn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _isolated_env(monkeypatch, tmp_path, {"FLUID_MCP_AUTH_MODE": "none"})
    server = _server(_cn_expose(tmp_path), tmp_path)
    port = _free_port()
    with _running_http_server(server, port):
        payload = await _sample_over_http(port, {}, client_cn="C0003", principal=CN_PRINCIPAL)
    assert payload.get("rows") is None, payload
    assert payload.get("error"), payload


@pytest.mark.asyncio
async def test_stdio_does_not_bind_a_self_attested_cn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _isolated_env(monkeypatch, tmp_path, {})
    server = _server(_cn_expose(tmp_path), tmp_path)
    async with open_inmemory_session(
        server.server,
        **self_attesting_client_kwargs(
            "cn-test", "1.0.0", client_cn="C0002", principal=CN_PRINCIPAL
        ),
    ) as client:
        result = await client.call_tool("sample", {"limit": 10})
    payload = json.loads(result.content[0].text)
    assert payload.get("rows") is None, payload
    assert payload.get("error"), payload


# ---------------------------------------------------------------------
# CLI: a configuration that can never honour a certificate or a principal refuses to start
# ---------------------------------------------------------------------


def _write_contract(tmp_path: Path, *, cn_filter: bool) -> Path:
    (tmp_path / "customers.csv").write_text(
        "customer_id,segment\nC0001,enterprise\nC0002,smb\nC0003,consumer\n",
        encoding="utf-8",
    )
    text = textwrap.dedent("""\
        fluidVersion: "0.7.4"
        kind: DataProduct
        id: silver.demo.customer_segments_v1
        name: Customer Segments
        metadata:
          layer: Silver
          owner: { team: demo, email: demo@example.com }
          businessContext: { domain: Demo }
        exposes:
          - exposeId: customer_segments
            kind: table
            title: Customer Segments
            contract:
              schema:
                - { name: customer_id, type: STRING, required: true }
                - { name: segment, type: STRING }
            binding:
              platform: local
              format: csv
              location: { path: ./customers.csv, table: customer_segments }
        """)
    if cn_filter:
        text += (
            "    policy:\n"
            "      authz:\n"
            "        rowFilters:\n"
            '          - { principal: "client:C0001", where: "customer_id = \'C0001\'" }\n'
        )
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(text, encoding="utf-8")
    return contract


def _serve(
    tmp_path: Path,
    env_overrides: Dict[str, str],
    *,
    transport: str,
    cn_filter: bool,
    extra_args: tuple = (),
):
    repo_root = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if k not in _AUTH_ENV_VARS}
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
    contract = _write_contract(tmp_path, cn_filter=cn_filter)
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
            *extra_args,
        ],
        cwd=str(tmp_path),
        env=env,
        input="",
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("auth_env", list(UNAUTHENTICATED_HTTP_ENVS), ids=str)
def test_cli_trusted_proxies_without_auth_refuses_to_start(tmp_path: Path, auth_env: str):
    proc = _serve(
        tmp_path,
        {**UNAUTHENTICATED_HTTP_ENVS[auth_env], "FLUID_MCP_TRUSTED_PROXIES": "127.0.0.1"},
        transport="http",
        cn_filter=False,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "FLUID_MCP_TRUSTED_PROXIES" in output
    assert "Traceback" not in output
    assert TOKEN not in output


@pytest.mark.parametrize("auth_env", list(UNAUTHENTICATED_HTTP_ENVS), ids=str)
def test_cli_row_filter_without_auth_refuses_to_start(tmp_path: Path, auth_env: str):
    proc = _serve(tmp_path, UNAUTHENTICATED_HTTP_ENVS[auth_env], transport="http", cn_filter=True)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "policy.authz.rowFilters" in output and "HTTP auth is off" in output
    assert "Traceback" not in output


@pytest.mark.parametrize("auth_env", list(UNAUTHENTICATED_HTTP_ENVS), ids=str)
def test_cli_unauthenticated_http_beyond_loopback_refuses_to_start(tmp_path: Path, auth_env: str):
    """Without auth the gateway listens on loopback only: a wider --host
    stops with exit 2 before the contract is even loaded."""
    proc = _serve(
        tmp_path,
        UNAUTHENTICATED_HTTP_ENVS[auth_env],
        transport="http",
        cn_filter=False,
        extra_args=("--host", "0.0.0.0"),
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "fluid mcp output-port: refusing to listen on 0.0.0.0" in output
    assert "--host 127.0.0.1" in output
    assert "Traceback" not in output


def test_cli_row_filter_over_stdio_refuses_to_start(tmp_path: Path):
    proc = _serve(tmp_path, {}, transport="stdio", cn_filter=True)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "policy.authz.rowFilters" in output and "carries no verified identity" in output
    assert "Traceback" not in output


def test_cli_stdio_without_a_row_filter_still_serves(tmp_path: Path):
    """stdio does not read FLUID_MCP_TRUSTED_PROXIES; only a contract whose row
    filters need a verified principal makes it refuse."""
    proc = _serve(
        tmp_path, {"FLUID_MCP_TRUSTED_PROXIES": "127.0.0.1"}, transport="stdio", cn_filter=False
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output


# ---------------------------------------------------------------------
# The startup gate, configuration by configuration
# ---------------------------------------------------------------------


def _validator(**kwargs: Any) -> auth_mod.AuthValidator:
    return auth_mod.AuthValidator(**kwargs)


def test_gate_allows_an_authenticated_gateway_behind_a_trusted_proxy():
    from fluid_build.cli.mcp_output_port import _cert_identity_gate_unsatisfiable

    validator = _validator(
        mode="shared-token",
        shared_token=TOKEN,
        trusted_proxies=auth_mod.parse_trusted_proxies("127.0.0.1"),
    )
    assert _cert_identity_gate_unsatisfiable(transport="http", http_auth=validator) is None


def test_gate_allows_an_unauthenticated_gateway_that_relies_on_no_proxy():
    from fluid_build.cli.mcp_output_port import _cert_identity_gate_unsatisfiable

    assert (
        _cert_identity_gate_unsatisfiable(transport="http", http_auth=_validator(mode="none"))
        is None
    )
    assert _cert_identity_gate_unsatisfiable(transport="stdio", http_auth=None) is None


@pytest.mark.parametrize(
    "kwargs, names",
    [
        ({"mode": "none"}, ["FLUID_MCP_AUTH_MODE=none"]),
        ({"mode": "shared-token"}, ["FLUID_MCP_AUTH_TOKEN"]),
        (
            {"mode": "jwt", "jwt_issuer": "https://issuer.example.invalid/"},
            ["FLUID_MCP_JWT_AUDIENCE", "FLUID_MCP_JWT_JWKS_URL"],
        ),
    ],
)
def test_gate_message_names_what_keeps_auth_off(kwargs, names):
    from fluid_build.cli.mcp_output_port import _cert_identity_gate_unsatisfiable

    validator = _validator(**kwargs, trusted_proxies=auth_mod.parse_trusted_proxies("127.0.0.1"))
    message = _cert_identity_gate_unsatisfiable(transport="http", http_auth=validator)
    assert message is not None and "FLUID_MCP_TRUSTED_PROXIES" in message
    for name in names:
        assert name in message, message


# ---------------------------------------------------------------------
# FLUID_MCP_TRUSTED_PROXIES: a host address with a short prefix is a typo
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec, network",
    [("127.0.0.1/0", "0.0.0.0/0"), ("10.0.4.17/8", "10.0.0.0/8"), ("::1/0", "::/0")],
)
def test_host_address_with_a_wide_prefix_is_refused(spec: str, network: str):
    with pytest.raises(auth_mod.AuthConfigError) as excinfo:
        auth_mod.parse_trusted_proxies(spec)
    message = str(excinfo.value)
    assert spec in message
    # Names both readings so the operator picks the one they meant.
    assert network in message
    host = spec.split("/", 1)[0]
    assert f"{host}/{'128' if ':' in host else '32'}" in message or f"'{host}'" in message


@pytest.mark.parametrize(
    "spec", ["10.0.4.17", "10.0.4.17/32", "10.0.0.0/8", "0.0.0.0/0,::/0", "fd00::/8"]
)
def test_network_addresses_are_still_accepted(spec: str):
    assert auth_mod.parse_trusted_proxies(spec)
