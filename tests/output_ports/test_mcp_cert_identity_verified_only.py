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

"""``${caller.client_cn}`` / ``${caller.client_fingerprint}`` bind only from a
verified certificate.

Those two attributes name a client certificate. The only party that can vouch
for one is the mTLS-terminating proxy listed in ``FLUID_MCP_TRUSTED_PROXIES``,
and the gateway reads that proxy's headers only on an authenticated request.
So, like ``jurisdiction``, they are verified-only: a value the client supplies
about itself never binds them, whatever the auth mode, and a gateway that could
never honour them refuses to start instead of serving every call a denial.

These tests drive the resolver, the real HTTP/SSE transport, the in-memory
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

from mcp import ClientSession  # noqa: E402
from mcp.client.sse import sse_client  # noqa: E402

from fluid_build._mcp_compat import (  # noqa: E402
    open_inmemory_session,
    self_attesting_client_kwargs,
)
from fluid_build.output_ports.mcp import OutputPortMcpServer, OutputPortPolicy  # noqa: E402
from fluid_build.output_ports.mcp import auth as auth_mod  # noqa: E402

from ._fixtures import make_expose, write_customer_csv  # noqa: E402
from .test_extensibility_and_runtime import _free_port, _running_sse_server  # noqa: E402

TOKEN = "test-shared-token-not-a-secret"  # pragma: allowlist secret
CERT_ATTRS = ("client_cn", "client_fingerprint")

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
        "rowFilters": [{"column": "customer_id", "equals": "${caller.client_cn}"}],
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
            client_info={"client_cn": "C0002", "client_fingerprint": "ff", "tenant_id": "acme"},
            scope=scope,
        )
    )
    for attr in CERT_ATTRS:
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
# End to end: unauthenticated HTTP/SSE and stdio
# ---------------------------------------------------------------------


async def _sample_over_sse(port: int, headers: Dict[str, str], **attest: Any) -> Dict[str, Any]:
    async with sse_client(f"http://127.0.0.1:{port}/sse", headers=headers) as (read, write):
        async with ClientSession(
            read, write, **self_attesting_client_kwargs("cn-test", "1.0.0", **attest)
        ) as session:
            await session.initialize()
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
    naming its own CN gets no rows: neither the proxy header nor the
    self-attested value binds ``${caller.client_cn}``."""
    audit_root = _isolated_env(
        monkeypatch,
        tmp_path,
        {**UNAUTHENTICATED_HTTP_ENVS[auth_env], "FLUID_MCP_TRUSTED_PROXIES": "127.0.0.1,::1"},
    )
    server = _server(_cn_expose(tmp_path), tmp_path)
    port = _free_port()
    with _running_sse_server(server, port):
        payload = await _sample_over_sse(
            port, {"X-Client-CN": "C0001"}, client_cn="C0002", client_fingerprint="ff"
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
    with _running_sse_server(server, port):
        payload = await _sample_over_sse(port, {}, client_cn="C0003")
    assert payload.get("rows") is None, payload
    assert payload.get("error"), payload


@pytest.mark.asyncio
async def test_stdio_does_not_bind_a_self_attested_cn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _isolated_env(monkeypatch, tmp_path, {})
    server = _server(_cn_expose(tmp_path), tmp_path)
    async with open_inmemory_session(
        server.server, **self_attesting_client_kwargs("cn-test", "1.0.0", client_cn="C0002")
    ) as client:
        result = await client.call_tool("sample", {"limit": 10})
    payload = json.loads(result.content[0].text)
    assert payload.get("rows") is None, payload
    assert payload.get("error"), payload


# ---------------------------------------------------------------------
# CLI: a configuration that can never honour a certificate refuses to start
# ---------------------------------------------------------------------


def _write_contract(tmp_path: Path, *, cn_filter: bool) -> Path:
    (tmp_path / "customers.csv").write_text(
        "customer_id,segment\nC0001,enterprise\nC0002,smb\nC0003,consumer\n",
        encoding="utf-8",
    )
    text = textwrap.dedent(
        """\
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
        """
    )
    if cn_filter:
        text += (
            "    policy:\n"
            "      rowFilters:\n"
            '        - { column: customer_id, equals: "${caller.client_cn}" }\n'
        )
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(text, encoding="utf-8")
    return contract


def _serve(tmp_path: Path, env_overrides: Dict[str, str], *, transport: str, cn_filter: bool):
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
def test_cli_certificate_row_filter_without_auth_refuses_to_start(tmp_path: Path, auth_env: str):
    proc = _serve(tmp_path, UNAUTHENTICATED_HTTP_ENVS[auth_env], transport="http", cn_filter=True)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "caller.client_cn" in output
    assert "Traceback" not in output


def test_cli_certificate_row_filter_over_stdio_refuses_to_start(tmp_path: Path):
    proc = _serve(tmp_path, {}, transport="stdio", cn_filter=True)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert "caller.client_cn" in output
    assert "Traceback" not in output


def test_cli_stdio_without_a_certificate_filter_still_serves(tmp_path: Path):
    """stdio does not read FLUID_MCP_TRUSTED_PROXIES; only a contract that
    needs a certificate makes it refuse."""
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


def test_gate_allows_an_authenticated_gateway_behind_a_trusted_proxy(tmp_path: Path):
    from fluid_build.cli.mcp_output_port import _cert_identity_gate_unsatisfiable

    validator = _validator(
        mode="shared-token",
        shared_token=TOKEN,
        trusted_proxies=auth_mod.parse_trusted_proxies("127.0.0.1"),
    )
    assert (
        _cert_identity_gate_unsatisfiable(
            _cn_expose(tmp_path), transport="http", http_auth=validator
        )
        is None
    )


def test_gate_allows_an_unauthenticated_gateway_that_needs_no_certificate(tmp_path: Path):
    from fluid_build.cli.mcp_output_port import _cert_identity_gate_unsatisfiable

    expose = _cn_expose(tmp_path)
    expose["policy"] = {"rowFilters": [{"column": "tenant_id", "equals": "${caller.tenant_id}"}]}
    assert (
        _cert_identity_gate_unsatisfiable(
            expose, transport="http", http_auth=_validator(mode="none")
        )
        is None
    )


def test_gate_sees_a_certificate_attribute_inside_an_in_list(tmp_path: Path):
    from fluid_build.cli.mcp_output_port import _cert_identity_gate_unsatisfiable

    expose = _cn_expose(tmp_path)
    expose["policy"] = {
        "rowFilters": [{"column": "fp", "in": ["${caller.client_fingerprint}", "static"]}]
    }
    message = _cert_identity_gate_unsatisfiable(
        expose, transport="http", http_auth=_validator(mode="none")
    )
    assert message is not None and "caller.client_fingerprint" in message


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
def test_gate_message_names_what_keeps_auth_off(tmp_path: Path, kwargs, names):
    from fluid_build.cli.mcp_output_port import _cert_identity_gate_unsatisfiable

    message = _cert_identity_gate_unsatisfiable(
        _cn_expose(tmp_path), transport="http", http_auth=_validator(**kwargs)
    )
    assert message is not None
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
