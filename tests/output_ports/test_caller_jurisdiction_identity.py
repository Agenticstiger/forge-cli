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
from types import SimpleNamespace
from typing import Any, Dict, Mapping, Optional

import pytest

from fluid_build.cli.mcp_output_port import _jurisdiction_gate_unsatisfiable
from fluid_build.output_ports.mcp.auth import DEFAULT_JWT_CLAIM_MAPPINGS, JURISDICTION_ATTR
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


def test_stdio_with_a_pinned_contract_refuses_at_startup() -> None:
    """Stdio carries no headers, so no call could ever succeed. Say so once."""
    message = _jurisdiction_gate_unsatisfiable(_policy(jurisdiction="EU"), transport="stdio")
    assert message is not None
    assert "EU" in message
    # The message has to be actionable, not just correct.
    assert "--transport http" in message
    assert "crossBorderTransfer" in message


def test_http_without_auth_refuses_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The subtler half: HTTP alone is not enough.

    `_AuthMiddleware` returns early when auth is not configured, so it never
    stamps the attributes this gate reads. An earlier version of this feature's
    own documentation missed exactly this and blamed the transport instead.
    """
    monkeypatch.delenv("FLUID_MCP_AUTH_MODE", raising=False)
    message = _jurisdiction_gate_unsatisfiable(_policy(jurisdiction="EU"), transport="http")
    assert message is not None
    assert "FLUID_MCP_AUTH_MODE" in message


@pytest.mark.parametrize("mode", ["jwt", "shared-token", "JWT"])
def test_http_with_auth_is_allowed_to_start(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLUID_MCP_AUTH_MODE", mode)
    assert _jurisdiction_gate_unsatisfiable(_policy(jurisdiction="EU"), transport="http") is None


@pytest.mark.parametrize("mode", ["", "none", "off", "disabled"])
def test_auth_mode_set_to_a_disabling_value_still_refuses(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Set-but-empty is the configuration mistake this has to survive."""
    monkeypatch.setenv("FLUID_MCP_AUTH_MODE", mode)
    assert (
        _jurisdiction_gate_unsatisfiable(_policy(jurisdiction="EU"), transport="http") is not None
    )


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_an_unpinned_contract_never_blocks_startup(
    transport: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control that keeps this from breaking every existing deployment.

    Without it the tests above would also pass if the guard simply refused
    everything, which would take out every contract that pins nothing.
    """
    monkeypatch.delenv("FLUID_MCP_AUTH_MODE", raising=False)
    assert _jurisdiction_gate_unsatisfiable(_policy(), transport=transport) is None


@pytest.mark.parametrize("catch_all", ["Global", "Multi-Region"])
def test_a_catch_all_jurisdiction_never_blocks_startup(catch_all: str) -> None:
    """`Multi-Region` is the one that would have hurt: no caller is ever in it."""
    assert (
        _jurisdiction_gate_unsatisfiable(_policy(jurisdiction=catch_all), transport="stdio") is None
    )


def test_permitted_cross_border_transfer_never_blocks_startup() -> None:
    policy = _policy(jurisdiction="EU", crossBorderTransfer=True)
    assert _jurisdiction_gate_unsatisfiable(policy, transport="stdio") is None
