# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The caller-jurisdiction gate, end to end.

Three parts had to meet for this to do anything: `decide()` learned the rules
(#574), the contract's own `sovereignty` block learned to produce them (#576),
and nothing extracted a caller's jurisdiction from a credential. This is the
third part, and these are the tests that decide whether it is worth having.

The one that matters is `test_a_self_attested_jurisdiction_is_ignored`. Every
other control here degrades gracefully if it leaks: a caller who lies about its
`model` gets a narrower policy. A caller who can lie about its jurisdiction
walks straight through a data-residency rule by typing a string, and does so
most easily on the unauthenticated deployments least equipped to notice.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Mapping, Optional

import pytest

from fluid_build.cli.mcp_output_port import _jurisdiction_gate_unsatisfiable
from fluid_build.output_ports.mcp.auth import (
    DEFAULT_JWT_CLAIM_MAPPINGS,
    JURISDICTION_ATTR,
    AuthValidator,
    extract_mtls_identity,
    parse_trusted_proxies,
)
from fluid_build.output_ports.mcp.policy import OutputPortPolicy
from fluid_build.output_ports.mcp.server import OutputPortMcpServer
from fluid_build.policy.decision import ReasonCode

_EXPOSE: Dict[str, Any] = {"exposeId": "demo", "kind": "table"}


def _contract(**sovereignty: Any) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "fluidVersion": "0.7.5",
        "kind": "DataProduct",
        "id": "demo.v1",
        "exposes": [_EXPOSE],
    }
    if sovereignty:
        doc["sovereignty"] = sovereignty
    return doc


def _server(contract: Mapping[str, Any]) -> OutputPortMcpServer:
    policy = OutputPortPolicy.from_contract_and_flags(expose=_EXPOSE, contract=contract)
    return OutputPortMcpServer(
        contract=dict(contract),
        expose=_EXPOSE,
        policy=policy,
        logger=logging.getLogger("test.caller_jurisdiction"),
    )


def _ctx(
    *, client_extra: Optional[Dict[str, Any]] = None, scope: Optional[Dict[str, Any]] = None
) -> SimpleNamespace:
    """One request: self-attested clientInfo, plus the verified transport scope."""
    client_info = SimpleNamespace(model_extra=dict(client_extra or {}))
    session = SimpleNamespace(client_params=SimpleNamespace(clientInfo=client_info))
    request = SimpleNamespace(scope=scope) if scope is not None else None
    return SimpleNamespace(session=session, request=request)


# ---------------------------------------------------------------------------
# 1. the security property
# ---------------------------------------------------------------------------


def test_a_self_attested_jurisdiction_is_ignored() -> None:
    """The whole point. A caller claiming its own jurisdiction must not be believed.

    This is the shape of the attack: no auth configured, so `clientInfo` is
    otherwise authoritative, and the client simply asserts it is in the EU.
    """
    server = _server(_contract(jurisdiction="EU"))
    resolved = server._resolve_verified_jurisdiction(_ctx(client_extra={"jurisdiction": "EU"}))
    assert resolved is None, (
        "a jurisdiction typed into clientInfo was accepted as verified — a caller "
        "can now authorise itself past a data-residency rule with a string"
    )


def test_a_self_attested_jurisdiction_is_ignored_even_alongside_a_verified_one() -> None:
    """The verified value wins; the self-attested one does not merely lose, it is unread."""
    server = _server(_contract(jurisdiction="EU"))
    resolved = server._resolve_verified_jurisdiction(
        _ctx(
            client_extra={"jurisdiction": "EU"},
            scope={"fluid_auth_attrs": {JURISDICTION_ATTR: "US"}, "fluid_auth_kind": "jwt"},
        )
    )
    assert resolved == "US"


def test_a_verified_jurisdiction_is_read() -> None:
    server = _server(_contract(jurisdiction="EU"))
    ctx = _ctx(scope={"fluid_auth_attrs": {JURISDICTION_ATTR: "EU"}, "fluid_auth_kind": "jwt"})
    assert server._resolve_verified_jurisdiction(ctx) == "EU"


@pytest.mark.parametrize(
    "ctx_kwargs",
    [
        {},  # stdio: no request, so no scope
        {"scope": {}},  # http, auth disabled: middleware short-circuited
        {"scope": {"fluid_auth_attrs": {}}},  # authed, but no jurisdiction claim
        {"scope": {"fluid_auth_attrs": {JURISDICTION_ATTR: ""}}},  # empty claim
    ],
)
def test_absent_or_empty_resolves_to_unknown(ctx_kwargs: Dict[str, Any]) -> None:
    """Each of these is a real deployment, and each must mean "unknown", not "fine"."""
    server = _server(_contract(jurisdiction="EU"))
    assert server._resolve_verified_jurisdiction(_ctx(**ctx_kwargs)) is None


def test_no_request_context_is_unknown_not_a_crash() -> None:
    server = _server(_contract(jurisdiction="EU"))
    assert server._resolve_verified_jurisdiction(None) is None


# ---------------------------------------------------------------------------
# 2. the claim actually reaches the gate
# ---------------------------------------------------------------------------


def test_jurisdiction_is_mapped_from_a_jwt_by_default() -> None:
    """An operator who does nothing still gets the claim mapped.

    Without this the gate is not weaker, it is shut: an unmapped claim reads as
    unknown, and a pinned contract refuses unknown.
    """
    assert DEFAULT_JWT_CLAIM_MAPPINGS.get("jurisdiction") == JURISDICTION_ATTR


def test_a_verified_in_jurisdiction_caller_is_allowed() -> None:
    policy = OutputPortPolicy.from_contract_and_flags(
        expose=_EXPOSE, contract=_contract(jurisdiction="EU")
    )
    allowed, reason = policy.check_tool_call(
        tool="describe",
        model_id=None,
        use_case=None,
        caller_jurisdiction="EU",
        caller_jurisdiction_verified=True,
    )
    assert allowed, reason


def test_a_verified_out_of_jurisdiction_caller_is_denied() -> None:
    policy = OutputPortPolicy.from_contract_and_flags(
        expose=_EXPOSE, contract=_contract(jurisdiction="EU")
    )
    allowed, reason = policy.check_tool_call(
        tool="describe",
        model_id=None,
        use_case=None,
        caller_jurisdiction="US",
        caller_jurisdiction_verified=True,
    )
    assert not allowed
    assert reason == ReasonCode.NOT_IN_ALLOWED_JURISDICTIONS.value


def test_an_unverified_claim_does_not_satisfy_the_rule() -> None:
    """Passing the right answer without proof is the same as passing nothing."""
    policy = OutputPortPolicy.from_contract_and_flags(
        expose=_EXPOSE, contract=_contract(jurisdiction="EU")
    )
    allowed, reason = policy.check_tool_call(
        tool="describe",
        model_id=None,
        use_case=None,
        caller_jurisdiction="EU",
        caller_jurisdiction_verified=False,
    )
    assert not allowed
    assert reason == ReasonCode.MISSING_CALLER_JURISDICTION.value


def test_an_unpinned_contract_ignores_the_claim_entirely() -> None:
    """The negative control. Most contracts pin nothing and must be untouched by all this."""
    policy = OutputPortPolicy.from_contract_and_flags(expose=_EXPOSE, contract=_contract())
    assert policy.allowed_caller_jurisdictions is None
    allowed, _ = policy.check_tool_call(tool="describe", model_id=None, use_case=None)
    assert allowed


# ---------------------------------------------------------------------------
# 3. refusing at startup instead of on every call
# ---------------------------------------------------------------------------


def _policy(**sovereignty: Any) -> OutputPortPolicy:
    return OutputPortPolicy.from_contract_and_flags(
        expose=_EXPOSE, contract=_contract(**sovereignty)
    )


def _gate(policy: OutputPortPolicy, validator: Optional[AuthValidator]) -> Optional[str]:
    """The startup gate as ``_run_serve`` calls it: stdio when ``validator`` is None."""
    transport = "stdio" if validator is None else "http"
    return _jurisdiction_gate_unsatisfiable(policy, transport=transport, http_auth=validator)


def _maybe_validator(kwargs: Optional[Dict[str, Any]]) -> Optional[AuthValidator]:
    """``None`` (stdio) for ``None``, else the validator ``kwargs`` describe."""
    return None if kwargs is None else AuthValidator(**kwargs)


def _assert_names_the_way_out(message: Optional[str]) -> str:
    """Every refusal names the pinned jurisdiction and both ways out."""
    assert message is not None
    assert "EU" in message
    assert "FLUID_MCP_AUTH_MODE=jwt" in message
    assert "crossBorderTransfer" in message
    return message


TOKEN = "test-shared-token-not-a-secret"  # pragma: allowlist secret

_JWT: Dict[str, Any] = {
    "mode": "jwt",
    "jwt_issuer": "https://issuer.example.invalid/",
    "jwt_audience": "fluid",
    "jwt_jwks_url": "https://issuer.example.invalid/jwks.json",
}

#: Every way ``--transport http`` serves with auth off.
_AUTH_OFF: Dict[str, Dict[str, Any]] = {
    "mode-none": {"mode": "none"},
    "shared-token-without-token": {"mode": "shared-token"},
    "jwt-incomplete": {"mode": "jwt", "jwt_issuer": "https://issuer.example.invalid/"},
}

_AUTH_ENV_VARS = (
    "FLUID_MCP_AUTH_MODE",
    "FLUID_MCP_AUTH_TOKEN",
    "FLUID_MCP_JWT_ISSUER",
    "FLUID_MCP_JWT_AUDIENCE",
    "FLUID_MCP_JWT_JWKS_URL",
    "FLUID_MCP_JWT_CLAIM_MAPPING",
    "FLUID_MCP_TRUSTED_PROXIES",
)


def _from_env(monkeypatch: pytest.MonkeyPatch, env: Dict[str, str]) -> AuthValidator:
    """The validator ``_http_preflight`` builds when exactly ``env`` is set."""
    for name in _AUTH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return AuthValidator.from_env()


def test_stdio_with_a_pinned_contract_refuses_at_startup() -> None:
    """Stdio carries no headers, so no call could ever succeed. Say so once."""
    # The message has to be actionable, not just correct.
    message = _assert_names_the_way_out(_gate(_policy(jurisdiction="EU"), None))
    assert "--transport http" in message


@pytest.mark.parametrize("kwargs", list(_AUTH_OFF.values()), ids=list(_AUTH_OFF))
def test_http_with_auth_off_refuses_too(kwargs: Dict[str, Any]) -> None:
    """The subtler half: HTTP alone is not enough.

    The HTTP auth gate (`fluid_build._mcp_http`) skips auth when it is not
    configured, so it never stamps the attributes this gate reads. An earlier
    version of this feature's own documentation missed exactly this and blamed
    the transport instead.
    """
    validator = AuthValidator(**kwargs)
    message = _assert_names_the_way_out(_gate(_policy(jurisdiction="EU"), validator))
    assert "refusing to serve unauthenticated" in message
    # Names what keeps auth off, not just that it is off.
    assert validator.disabled_reason() in message


@pytest.mark.parametrize("trusted_proxies", ["", "127.0.0.1"], ids=["direct", "trusted-proxy"])
def test_http_with_shared_token_auth_refuses(trusted_proxies: str) -> None:
    """The gap #889's security review found: the gateway started, then denied every call.

    A shared token says who may call, not where the caller is, and a trusted
    proxy forwards only a client certificate's CN and fingerprint.
    """
    validator = AuthValidator(
        mode="shared-token",
        shared_token=TOKEN,
        trusted_proxies=parse_trusted_proxies(trusted_proxies),
    )
    message = _assert_names_the_way_out(_gate(_policy(jurisdiction="EU"), validator))
    assert "refusing to serve with shared-token auth" in message
    assert TOKEN not in message


def test_nothing_a_shared_token_request_carries_verifies_a_jurisdiction() -> None:
    """The premise of the refusal above, pinned so it cannot drift silently.

    This replays what the HTTP gate stamps for an authenticated shared-token
    request from a trusted proxy that forwards every header it can. If a shared
    token or a proxy header ever learns to carry a jurisdiction, this fails
    first, and the gate has to change with it.
    """
    validator = AuthValidator(
        mode="shared-token",
        shared_token=TOKEN,
        trusted_proxies=parse_trusted_proxies("127.0.0.1"),
    )
    headers = {
        "authorization": f"Bearer {TOKEN}",
        "x-client-cn": "C0001",
        "x-client-fingerprint": "ab12cd34",
        "x-client-jurisdiction": "EU",
        "jurisdiction": "EU",
    }
    decision = validator.validate(headers)
    assert decision.allowed
    attrs = dict(decision.caller_attributes)
    attrs.update(
        extract_mtls_identity(
            headers, peer_host="127.0.0.1", trusted_proxies=validator.trusted_proxies
        )
    )
    assert attrs == {"client_cn": "C0001", "client_fingerprint": "ab12cd34"}
    ctx = _ctx(
        client_extra={"jurisdiction": "EU"},
        scope={"fluid_auth_attrs": attrs, "fluid_auth_kind": decision.identity_kind},
    )
    assert _server(_contract(jurisdiction="EU"))._resolve_verified_jurisdiction(ctx) is None


@pytest.mark.parametrize(
    "mappings",
    [None, {"jurisdiction": "region", "https://idp.example/jurisdiction": "jurisdiction"}],
    ids=["default-mapping", "custom-claim"],
)
def test_http_with_jwt_auth_that_maps_a_jurisdiction_is_allowed_to_start(
    mappings: Optional[Dict[str, str]],
) -> None:
    """The one configuration that can verify a jurisdiction keeps starting."""
    if mappings is not None:
        mappings = {**DEFAULT_JWT_CLAIM_MAPPINGS, **mappings}
    validator = AuthValidator(**_JWT, jwt_claim_mappings=mappings)
    assert _gate(_policy(jurisdiction="EU"), validator) is None


def test_jwt_auth_that_maps_no_claim_to_jurisdiction_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mapping the claim away locks every caller out, so say so at startup."""
    validator = _from_env(
        monkeypatch,
        {
            "FLUID_MCP_AUTH_MODE": "jwt",
            "FLUID_MCP_JWT_ISSUER": _JWT["jwt_issuer"],
            "FLUID_MCP_JWT_AUDIENCE": _JWT["jwt_audience"],
            "FLUID_MCP_JWT_JWKS_URL": _JWT["jwt_jwks_url"],
            "FLUID_MCP_JWT_CLAIM_MAPPING": "jurisdiction=region",
        },
    )
    assert validator.is_enabled()
    message = _gate(_policy(jurisdiction="EU"), validator)
    assert message is not None
    assert "FLUID_MCP_JWT_CLAIM_MAPPING=<claim>=jurisdiction" in message
    assert "crossBorderTransfer" in message


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"FLUID_MCP_AUTH_TOKEN": TOKEN},
        {"FLUID_MCP_AUTH_MODE": "none"},
        {"FLUID_MCP_AUTH_MODE": "shared-token", "FLUID_MCP_AUTH_TOKEN": TOKEN},
        {"FLUID_MCP_AUTH_MODE": "jwt"},
    ],
    ids=["unset", "unset-with-token", "none", "shared-token", "jwt-incomplete"],
)
def test_every_auth_environment_but_jwt_refuses(
    env: Dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate reads the validator ``_http_preflight`` built, never the variable.

    An unset ``FLUID_MCP_AUTH_MODE`` means ``shared-token`` to the validator,
    so with a token the request IS authenticated and still carries no
    jurisdiction. ``off``, ``disabled``, set-but-empty and any other value the
    validator refuses stop the gateway earlier, in ``_http_preflight``
    (``test_cli_unknown_auth_mode_exits_2_with_valid_modes_and_no_traceback``).
    """
    message = _gate(_policy(jurisdiction="EU"), _from_env(monkeypatch, env))
    assert message is not None
    assert "EU" in message
    assert TOKEN not in message


@pytest.mark.parametrize(
    "kwargs",
    [None, *_AUTH_OFF.values(), {"mode": "shared-token", "shared_token": TOKEN}, _JWT],
    ids=["stdio", *_AUTH_OFF, "shared-token", "jwt"],
)
def test_an_unpinned_contract_never_blocks_startup(kwargs: Optional[Dict[str, Any]]) -> None:
    """The control that keeps this from breaking every existing deployment.

    Without it the tests above would also pass if the guard simply refused
    everything, which would take out every contract that pins nothing.
    """
    assert _gate(_policy(), _maybe_validator(kwargs)) is None


@pytest.mark.parametrize("catch_all", ["Global", "Multi-Region"])
def test_a_catch_all_jurisdiction_never_blocks_startup(catch_all: str) -> None:
    """`Multi-Region` is the one that would have hurt: no caller is ever in it."""
    assert _gate(_policy(jurisdiction=catch_all), None) is None


@pytest.mark.parametrize(
    "kwargs", [None, {"mode": "shared-token", "shared_token": TOKEN}], ids=["stdio", "shared-token"]
)
def test_permitted_cross_border_transfer_never_blocks_startup(
    kwargs: Optional[Dict[str, Any]],
) -> None:
    """The way out every refusal names has to work."""
    policy = _policy(jurisdiction="EU", crossBorderTransfer=True)
    assert _gate(policy, _maybe_validator(kwargs)) is None


# ---------------------------------------------------------------------------
# 4. the real CLI stops before it listens
# ---------------------------------------------------------------------------


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _serve_pinned_over_http(
    tmp_path: Path, env_overrides: Dict[str, str]
) -> subprocess.CompletedProcess:
    """Run ``fluid mcp output-port serve`` on an EU-pinned contract over HTTP.

    A gateway that starts would listen until the timeout, so a regression shows
    up as ``TimeoutExpired`` rather than as a wrong exit code.
    """
    (tmp_path / "customers.csv").write_text(
        "customer_id,segment\nC0001,enterprise\n", encoding="utf-8"
    )
    contract = tmp_path / "contract.fluid.yaml"
    contract.write_text(
        textwrap.dedent("""\
            fluidVersion: "0.7.4"
            kind: DataProduct
            id: silver.demo.customer_segments_v1
            name: Customer Segments
            metadata:
              layer: Silver
              owner: { team: demo }
            sovereignty:
              jurisdiction: EU
            exposes:
              - exposeId: customer_segments
                kind: table
                contract:
                  schema:
                    - { name: customer_id, type: STRING, required: true }
                    - { name: segment, type: STRING }
                binding:
                  platform: local
                  format: csv
                  location: { path: ./customers.csv, table: customer_segments }
            """),
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k not in _AUTH_ENV_VARS}
    for noisy in ("FLUID_LOG_LEVEL", "FLUID_DEBUG", "FLUID_SHOW_REGISTRATION"):
        env.pop(noisy, None)
    env.update(
        {
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "HOME": str(tmp_path / "home"),
            "FLUID_QUIET": "1",
            "FLUID_NONINTERACTIVE": "1",
            "FLUID_AUDIT_ROOT": str(tmp_path / "audit"),
            **env_overrides,
        }
    )
    (tmp_path / "home").mkdir(exist_ok=True)
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
            "http",
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


@pytest.mark.parametrize(
    "env, headline",
    [
        (
            {"FLUID_MCP_AUTH_MODE": "shared-token", "FLUID_MCP_AUTH_TOKEN": TOKEN},
            "refusing to serve with shared-token auth",
        ),
        (
            {
                "FLUID_MCP_AUTH_MODE": "shared-token",
                "FLUID_MCP_AUTH_TOKEN": TOKEN,
                "FLUID_MCP_TRUSTED_PROXIES": "127.0.0.1",
            },
            "refusing to serve with shared-token auth",
        ),
        (
            {"FLUID_MCP_AUTH_MODE": "jwt", "FLUID_MCP_JWT_ISSUER": _JWT["jwt_issuer"]},
            "refusing to serve unauthenticated",
        ),
        (
            {
                "FLUID_MCP_AUTH_MODE": "jwt",
                "FLUID_MCP_JWT_ISSUER": _JWT["jwt_issuer"],
                "FLUID_MCP_JWT_AUDIENCE": _JWT["jwt_audience"],
                "FLUID_MCP_JWT_JWKS_URL": _JWT["jwt_jwks_url"],
                "FLUID_MCP_JWT_CLAIM_MAPPING": "jurisdiction=region",
            },
            "refusing to serve: no JWT claim maps to 'jurisdiction'",
        ),
        ({"FLUID_MCP_AUTH_MODE": "none"}, "refusing to serve unauthenticated"),
    ],
    ids=[
        "shared-token",
        "shared-token-behind-trusted-proxy",
        "jwt-incomplete",
        "jwt-claim-mapped-away",
        "none",
    ],
)
def test_cli_refuses_before_listening(tmp_path: Path, env: Dict[str, str], headline: str) -> None:
    proc = _serve_pinned_over_http(tmp_path, env)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, output
    assert f"fluid mcp output-port: {headline}." in output
    assert "sovereignty.jurisdiction to EU" in output
    assert "MCP Streamable HTTP at" not in output
    assert "Traceback" not in output
    assert TOKEN not in output
