# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The Streamable HTTP transport both MCP servers share (``fluid_build._mcp_http``).

Unit tests for the bind policy, then a real uvicorn on a free loopback port
in front of a minimal lowlevel MCP server: the bearer gate, the Origin and
Host checks, session ownership and per-request identity. The two servers'
own end-to-end tests live in ``tests/cli/test_mcp_serve_http.py`` and
``tests/output_ports/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

import pytest

pytest.importorskip("mcp")
pytest.importorskip("uvicorn")
httpx = pytest.importorskip("httpx")

from fluid_build import _mcp_http  # noqa: E402
from fluid_build._mcp_compat import (  # noqa: E402
    build_lowlevel_server,
    open_streamable_http_session,
)
from fluid_build.output_ports.mcp.auth import (  # noqa: E402
    AuthDecision,
    AuthValidator,
    _bearer_token,
)

LONG_TOKEN = "0123456789abcdef" * 4  # pragma: allowlist secret
LOGGER = logging.getLogger("fluid.test.mcp_http")
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "raw", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class _TokenIsIdentity:
    """A validator for which every listed token is a different caller
    (``{"sub": <token>}``), like two JWT subjects."""

    mode = "jwt"
    shared_token = None
    trusted_proxies = ()

    def __init__(self, *tokens: str) -> None:
        self._tokens = set(tokens)

    def is_enabled(self) -> bool:
        return True

    def disabled_reason(self) -> str:  # pragma: no cover - auth is on
        return ""

    def validate(self, headers: Dict[str, str]) -> AuthDecision:
        token = _bearer_token(headers)
        if token not in self._tokens:
            return AuthDecision(
                allowed=False, deny_reason="invalid-bearer-token", identity_kind="jwt"
            )
        return AuthDecision(allowed=True, caller_attributes={"sub": token}, identity_kind="jwt")


def _whoami_server() -> Any:
    """A lowlevel server with one tool that reports the verified identity the
    transport put on the request it arrived in."""
    from mcp.types import TextContent, Tool

    async def _list_tools() -> List[Any]:
        return [Tool(name="whoami", description="who is calling", inputSchema={"type": "object"})]

    async def _call_tool(ctx: Any, name: str, arguments: Dict[str, Any]) -> List[Any]:
        scope = getattr(getattr(ctx, "request", None), "scope", None) or {}
        body = {
            "kind": scope.get("fluid_auth_kind"),
            "attrs": scope.get("fluid_auth_attrs"),
        }
        return [TextContent(type="text", text=json.dumps(body))]

    return build_lowlevel_server("whoami", on_list_tools=_list_tools, on_call_tool=_call_tool)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _serving(validator: Any, *, host: str = "127.0.0.1") -> Iterator[str]:
    """Run the shared transport in a background thread; yield its /mcp URL."""
    port = _free_port()
    holder: Dict[str, Any] = {}

    def _run() -> None:
        try:
            asyncio.run(
                _mcp_http.serve_streamable_http(
                    _whoami_server(),
                    command="test",
                    host=host,
                    port=port,
                    validator=validator,
                    logger=LOGGER,
                    on_uvicorn=lambda server: holder.__setitem__("server", server),
                )
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced by the bind wait below
            holder["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            if "error" in holder:
                raise holder["error"]
            time.sleep(0.05)
    else:
        raise RuntimeError(f"server did not bind {port}: {holder.get('error')!r}")
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server = holder.get("server")
        if server is not None:
            server.should_exit = True
            server.force_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive(), "uvicorn thread did not stop"


def _post(url: str, body: Dict[str, Any], **headers: str) -> httpx.Response:
    return httpx.post(url, json=body, headers={**MCP_HEADERS, **headers}, timeout=5)


# ---------------------------------------------------------------------
# Bind policy
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "host, loopback",
    [
        ("127.0.0.1", True),
        ("127.0.0.2", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("::1", True),
        ("[::1]", True),
        ("0.0.0.0", False),
        ("::", False),
        ("", False),
        ("10.1.2.3", False),
        ("forge.internal", False),
        ("localhost.", False),
    ],
)
def test_loopback_means_this_machine_only(host: str, loopback: bool) -> None:
    assert _mcp_http.is_loopback_host(host) is loopback


def test_an_unauthenticated_server_may_listen_on_loopback() -> None:
    validator = AuthValidator(mode="none")
    assert (
        _mcp_http.bind_refusal(command="x", host="127.0.0.1", port=1, validator=validator) is None
    )


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "10.1.2.3", "forge.internal"])
@pytest.mark.parametrize(
    "validator",
    [
        AuthValidator(mode="none"),
        AuthValidator(mode="shared-token"),
        AuthValidator(mode="jwt", jwt_issuer="https://issuer.invalid/"),
    ],
    ids=["mode-none", "token-unset", "jwt-incomplete"],
)
def test_an_unauthenticated_server_is_refused_beyond_loopback(host: str, validator: Any) -> None:
    message = _mcp_http.bind_refusal(command="fluid x", host=host, port=8766, validator=validator)
    assert message is not None
    assert message.startswith("fluid x: refusing to listen on")
    assert "--host 127.0.0.1" in message
    assert "FLUID_MCP_AUTH_TOKEN" in message
    assert validator.disabled_reason() in message


def test_a_short_shared_token_is_refused_beyond_loopback() -> None:
    short = "s3cret-but-short"  # pragma: allowlist secret
    validator = AuthValidator(mode="shared-token", shared_token=short)
    message = _mcp_http.bind_refusal(command="x", host="0.0.0.0", port=1, validator=validator)
    assert message is not None and "shorter than 32" in message
    assert short not in message
    # The same token is fine on loopback, where only local processes reach it.
    assert _mcp_http.bind_refusal(command="x", host="::1", port=1, validator=validator) is None


def test_a_long_token_or_a_complete_jwt_setup_may_listen_anywhere() -> None:
    token = AuthValidator(mode="shared-token", shared_token=LONG_TOKEN)
    jwt = AuthValidator(
        mode="jwt",
        jwt_issuer="https://issuer.invalid/",
        jwt_audience="forge",
        jwt_jwks_url="https://issuer.invalid/jwks",
    )
    for validator in (token, jwt):
        assert (
            _mcp_http.bind_refusal(command="x", host="0.0.0.0", port=1, validator=validator) is None
        )


def test_the_library_entry_point_refuses_an_unsafe_bind_before_listening() -> None:
    port = _free_port()
    with pytest.raises(_mcp_http.UnsafeBindError, match="without authentication"):
        asyncio.run(
            _mcp_http.serve_streamable_http(
                _whoami_server(),
                command="x",
                host="0.0.0.0",
                port=port,
                validator=AuthValidator(mode="none"),
                logger=LOGGER,
            )
        )
    with socket.socket() as sock:  # nothing was left listening
        sock.bind(("0.0.0.0", port))


def test_endpoint_url_brackets_ipv6() -> None:
    assert _mcp_http.endpoint_url("::1", 8766) == "http://[::1]:8766/mcp"
    assert _mcp_http.endpoint_url("127.0.0.1", 8765) == "http://127.0.0.1:8765/mcp"


def test_the_startup_message_names_the_url_and_never_the_token() -> None:
    on = AuthValidator(mode="shared-token", shared_token=LONG_TOKEN)
    off = AuthValidator(mode="shared-token")
    text_on = _mcp_http.startup_message(
        command="c", host="127.0.0.1", port=1, validator=on, what="these tools"
    )
    text_off = _mcp_http.startup_message(
        command="c", host="127.0.0.1", port=1, validator=off, what="these tools"
    )
    assert "http://127.0.0.1:1/mcp" in text_on and LONG_TOKEN not in text_on
    assert "plain HTTP" not in text_on
    assert "any process on this machine can call these tools" in text_off
    exposed = _mcp_http.startup_message(
        command="c", host="0.0.0.0", port=1, validator=on, what="these tools"
    )
    assert "terminate TLS in front of it" in exposed


# ---------------------------------------------------------------------
# On the wire
# ---------------------------------------------------------------------


def test_the_bearer_gate_refuses_before_any_session_exists() -> None:
    validator = AuthValidator(mode="shared-token", shared_token=LONG_TOKEN)
    with _serving(validator) as url:
        missing = _post(url, INIT)
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == 'Bearer realm="fluid-mcp"'
        assert missing.json()["error"] == "unauthorized"
        assert "mcp-session-id" not in missing.headers
        wrong = _post(url, INIT, Authorization="Bearer nope")
        assert wrong.status_code == 401
        assert 'error="invalid_token"' in wrong.headers["www-authenticate"]
        # Any path, so a caller without a token cannot map the server.
        assert httpx.get(url.replace("/mcp", "/other"), timeout=5).status_code == 401
        right = _post(url, INIT, Authorization=f"Bearer {LONG_TOKEN}")
        assert right.status_code == 200
        assert right.headers.get("mcp-session-id")


@pytest.mark.asyncio
async def test_an_authenticated_client_lists_and_calls_tools() -> None:
    validator = AuthValidator(mode="shared-token", shared_token=LONG_TOKEN)
    with _serving(validator) as url:
        async with open_streamable_http_session(
            url, headers={"Authorization": f"Bearer {LONG_TOKEN}"}
        ) as session:
            listing = await session.list_tools()
            assert [tool.name for tool in listing.tools] == ["whoami"]
            result = await session.call_tool("whoami", {})
            assert json.loads(result.content[0].text) == {"kind": "shared-token", "attrs": {}}


@pytest.mark.parametrize("auth", [False, True], ids=["auth-off", "auth-on"])
def test_a_foreign_browser_origin_is_refused_and_a_local_one_is_not(auth: bool) -> None:
    validator = (
        AuthValidator(mode="shared-token", shared_token=LONG_TOKEN)
        if auth
        else AuthValidator(mode="none")
    )
    headers = {"Authorization": f"Bearer {LONG_TOKEN}"} if auth else {}
    with _serving(validator) as url:
        for origin in ("https://evil.example", "null", "http://127.0.0.1.evil.example"):
            refused = _post(url, INIT, Origin=origin, **headers)
            assert refused.status_code == 403, origin
            assert refused.json()["error"] == "forbidden_origin"
        local = _post(url, INIT, Origin="http://localhost:6274", **headers)
        assert local.status_code == 200


def test_an_unauthenticated_server_answers_only_a_loopback_host_header() -> None:
    """DNS rebinding: a page that points its own name at 127.0.0.1 still sends
    its own name as Host, and the SDK check answers 421."""
    with _serving(AuthValidator(mode="none")) as url:
        assert _post(url, INIT, Host="evil.example:80").status_code == 421
        assert _post(url, INIT).status_code == 200


def test_an_authenticated_server_accepts_a_proxied_host_header() -> None:
    """Behind a reverse proxy the Host header is the public name. With auth on
    the bearer token is the protection, so the Host check does not apply."""
    validator = AuthValidator(mode="shared-token", shared_token=LONG_TOKEN)
    with _serving(validator) as url:
        response = _post(url, INIT, Host="mcp.example.com", Authorization=f"Bearer {LONG_TOKEN}")
        assert response.status_code == 200


def test_a_websocket_upgrade_never_reaches_the_app() -> None:
    """MCP has no WebSocket transport. uvicorn runs with ws="none", so an
    upgrade request is plain HTTP and meets the bearer gate like any other."""
    validator = AuthValidator(mode="shared-token", shared_token=LONG_TOKEN)
    with _serving(validator) as url:
        upgrade = httpx.get(
            url,
            headers={
                "Connection": "Upgrade",
                "Upgrade": "websocket",
                "Sec-WebSocket-Version": "13",
                "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
            },
            timeout=5,
        )
        assert upgrade.status_code == 401


def test_the_gate_forwards_no_scope_but_http_and_lifespan() -> None:
    seen: List[str] = []

    async def _app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    guard = _mcp_http._HttpGuard(_app, validator=AuthValidator(mode="none"), logger=LOGGER)
    for kind in ("lifespan", "websocket"):
        asyncio.run(guard({"type": kind}, None, None))
    assert seen == ["lifespan"]


def _open_session(url: str, token: str) -> str:
    opened = _post(url, INIT, Authorization=f"Bearer {token}")
    assert opened.status_code == 200
    session_id = opened.headers["mcp-session-id"]
    ack = _post(
        url,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        Authorization=f"Bearer {token}",
        **{"Mcp-Session-Id": session_id, "MCP-Protocol-Version": "2025-06-18"},
    )
    assert ack.status_code == 202
    return session_id


def test_a_session_answers_only_the_identity_that_opened_it() -> None:
    alice, bob = "alice-" + LONG_TOKEN, "bob-" + LONG_TOKEN
    with _serving(_TokenIsIdentity(alice, bob)) as url:
        session_id = _open_session(url, alice)
        listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        session = {"Mcp-Session-Id": session_id, "MCP-Protocol-Version": "2025-06-18"}
        stolen = _post(url, listing, Authorization=f"Bearer {bob}", **session)
        assert stolen.status_code == 404
        own = _post(url, listing, Authorization=f"Bearer {alice}", **session)
        assert own.status_code == 200
        assert "whoami" in own.text


@pytest.mark.asyncio
async def test_each_call_carries_the_identity_of_its_own_request() -> None:
    alice, bob = "alice-" + LONG_TOKEN, "bob-" + LONG_TOKEN
    seen: Dict[str, Optional[Dict[str, Any]]] = {}
    with _serving(_TokenIsIdentity(alice, bob)) as url:
        for token in (alice, bob):
            async with open_streamable_http_session(
                url, headers={"Authorization": f"Bearer {token}"}
            ) as session:
                result = await session.call_tool("whoami", {})
                seen[token] = json.loads(result.content[0].text)
    assert seen[alice] == {"kind": "jwt", "attrs": {"sub": alice}}
    assert seen[bob] == {"kind": "jwt", "attrs": {"sub": bob}}
