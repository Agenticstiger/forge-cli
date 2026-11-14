# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Streamable HTTP serving for forge's two MCP servers.

``fluid mcp serve`` (the authoring tools) and ``fluid mcp output-port serve``
(one expose of a data product) both serve the MCP Streamable HTTP transport
(spec revision 2025-03-26, which deprecated HTTP+SSE) on one endpoint,
:data:`MCP_PATH`, through this module, so the two cannot drift apart on the
part that faces the network.

Every request passes, in order:

1. :class:`_HttpGuard`, a pure ASGI middleware ahead of routing (so a caller
   without a token cannot even learn which paths exist):

   * a browser ``Origin`` that is not this machine gets 403. The spec says a
     server MUST validate ``Origin``; clients that are not browsers send none.
   * when auth is configured (``AuthValidator``, ``FLUID_MCP_AUTH_MODE``), a
     missing or wrong bearer token gets 401 with ``WWW-Authenticate``. A
     verified request carries its identity on the ASGI scope:
     ``fluid_auth_kind`` and ``fluid_auth_attrs`` (what the output port's row
     filters and audit records read), and ``user``. The SDK's session manager
     ties each ``Mcp-Session-Id`` to the ``user`` that opened it and answers a
     request that presents another identity's session ID with 404, as if the
     session did not exist.

2. The SDK's ``StreamableHTTPSessionManager``: sessions, SSE response streams,
   the idle timeout, the session limit and the request-size limit. When auth is
   off, which :func:`bind_refusal` allows only on a loopback address, its
   DNS-rebinding check also requires a loopback ``Host`` header, so a web page
   that rebinds its own name to 127.0.0.1 cannot drive the server. With auth on
   that ``Host`` check is off: the server may sit behind a proxy that forwards
   its public name, and a rebinding page cannot send the bearer token anyway.

Borrowed, not built: the SDK's session manager, transport-security check and
session-owner binding (identical in mcp 1.30+ and 2.2+, so nothing here
branches on the SDK generation); forge's own ``AuthValidator``; the bearer gate
in front of the SDK follows sparfenyuk/mcp-proxy (MIT, ``--auth-bearer-token``).

Tier-0 leaf: stdlib only at module scope. ``mcp``, starlette and uvicorn are
imported inside the functions that serve, so this module stays off the
``fluid --help`` path (``tests/perf/test_startup_budget.py``).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
from typing import Any, Callable, List, Mapping, Optional
from urllib.parse import urlsplit

#: The one path both servers answer MCP on.
MCP_PATH = "/mcp"

#: Shortest shared token accepted on an address other than loopback. Nothing
#: slows down guessing, so the token must be long enough not to be guessed:
#: 32 characters is ``openssl rand -hex 16``; the refusal suggests 32 bytes.
MIN_SHARED_TOKEN_LENGTH = 32

#: Seconds uvicorn waits for open connections at shutdown before closing them.
#: An MCP client keeps a GET stream open for its whole session, so without a
#: bound a connected client would hold Ctrl-C off forever.
SHUTDOWN_GRACE_SECONDS = 5

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")

OnAuthenticated = Callable[[Any, Mapping[str, str]], None]


class UnsafeBindError(ValueError):
    """Serving on this address would expose the server without protection."""


def is_loopback_host(host: str) -> bool:
    """True when ``host`` names only this machine: ``localhost`` or a loopback
    IP literal (``127.0.0.0/8``, ``::1``).

    Any other hostname counts as reachable from elsewhere, even one that
    resolves to 127.0.0.1 today: what a name resolves to can change after the
    check. An empty host binds every interface, so it is not loopback either.
    """
    value = (host or "").strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def bind_refusal(*, command: str, host: str, port: int, validator: Any) -> Optional[str]:
    """The message explaining why ``command`` must not listen on ``host``, or
    ``None`` when it may.

    A server without auth may listen only on a loopback address. Beyond
    loopback it needs auth, and a shared token at least
    :data:`MIN_SHARED_TOKEN_LENGTH` characters long. The message never
    contains a configured value.
    """
    if is_loopback_host(host):
        return None
    where = f"{host}:{port}"
    if not validator.is_enabled():
        return (
            f"{command}: refusing to listen on {where} without authentication.\n"
            f"  Auth is off ({validator.disabled_reason()}), so anyone who can reach "
            f"{where} could call every tool.\n"
            "  Keep it on this machine:  --host 127.0.0.1\n"
            '  Or require a token:       export FLUID_MCP_AUTH_TOKEN="$(openssl rand -hex 32)"\n'
            "    (clients then send 'Authorization: Bearer <token>'), or set "
            "FLUID_MCP_AUTH_MODE=jwt with FLUID_MCP_JWT_ISSUER, FLUID_MCP_JWT_AUDIENCE and "
            "FLUID_MCP_JWT_JWKS_URL.\n"
        )
    token = validator.shared_token if validator.mode == "shared-token" else None
    if token is not None and len(token) < MIN_SHARED_TOKEN_LENGTH:
        return (
            f"{command}: refusing to listen on {where} with a short token.\n"
            f"  FLUID_MCP_AUTH_TOKEN is shorter than {MIN_SHARED_TOKEN_LENGTH} characters. "
            "A server reachable from other machines can be sent guesses as fast as the "
            "network allows.\n"
            '  Generate one:  export FLUID_MCP_AUTH_TOKEN="$(openssl rand -hex 32)"\n'
        )
    return None


def _host_name(host: str) -> str:
    """``host`` as it appears in a URL or ``Host`` header (IPv6 in brackets)."""
    value = host.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return value.lower()
    return f"[{address.compressed}]" if address.version == 6 else address.compressed


def endpoint_url(host: str, port: int) -> str:
    """The URL a client connects to, e.g. ``http://127.0.0.1:8766/mcp``."""
    return f"http://{_host_name(host)}:{port}{MCP_PATH}"


def startup_message(*, command: str, host: str, port: int, validator: Any, what: str) -> str:
    """The lines a server prints to stderr as it starts: where to connect,
    which auth applies and, when auth is off, who can call it (``what``
    names the tools, e.g. "these tools"). Never contains a configured value."""
    url = endpoint_url(host, port)
    if validator.is_enabled():
        message = (
            f"{command}: MCP Streamable HTTP at {url} (auth: {validator.mode}; "
            "clients send 'Authorization: Bearer <token>').\n"
        )
        if not is_loopback_host(host):
            message += (
                "  This is plain HTTP: terminate TLS in front of it (a reverse proxy or "
                "your platform's ingress), or the token crosses the network readable.\n"
            )
        return message
    return (
        f"{command}: MCP Streamable HTTP at {url}\n"
        f"  Auth is off ({validator.disabled_reason()}): any process on this machine "
        f"can call {what}. Set FLUID_MCP_AUTH_TOKEN to require "
        "'Authorization: Bearer <token>'.\n"
    )


def _loopback_names(host: str) -> List[str]:
    """The loopback names clients use, plus the bound address itself (a server
    on 127.0.0.2 is reached as ``127.0.0.2:<port>``)."""
    names = list(_LOOPBACK_HOSTS)
    own = _host_name(host)
    if own not in names:
        names.append(own)
    return names


def _origin_is_loopback(origin: str) -> bool:
    """True for an ``Origin`` served from this machine (a local dev tool such
    as the MCP Inspector). ``null`` and anything unparsable are not."""
    try:
        parts = urlsplit(origin)
        hostname = parts.hostname or ""
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and is_loopback_host(hostname)


def _session_owner(scope: Mapping[str, Any]) -> Any:
    """The identity the SDK ties a session to: the auth kind plus a digest of
    the verified caller attributes, which is everything the servers' policy
    reads. Two requests with the same attributes are authorised identically,
    so sharing a session between them grants nothing; different attributes
    (another JWT subject, another forwarded client certificate) get 404."""
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    kind = str(scope.get("fluid_auth_kind") or "none")
    attrs = scope.get("fluid_auth_attrs") or {}
    digest = hashlib.sha256(
        json.dumps(attrs, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    # The raw bearer token is deliberately not kept: nothing downstream needs it.
    return AuthenticatedUser(
        AccessToken(token="", client_id=f"fluid-{kind}", scopes=[], subject=digest)
    )


async def _send_error(
    scope: Any,
    receive: Any,
    send: Any,
    *,
    status: int,
    error: str,
    message: str,
    headers: Optional[Mapping[str, str]] = None,
) -> None:
    from starlette.responses import JSONResponse

    response = JSONResponse(
        {"error": error, "message": message}, status_code=status, headers=dict(headers or {})
    )
    await response(scope, receive, send)


class _HttpGuard:
    """Origin check, then bearer auth, in front of the MCP app. Pure ASGI
    (not Starlette's ``BaseHTTPMiddleware``, which buffers and wraps streaming
    responses). The lifespan scope passes straight through; no other
    non-HTTP scope reaches the app."""

    def __init__(
        self,
        app: Any,
        *,
        validator: Any,
        logger: logging.Logger,
        on_authenticated: Optional[OnAuthenticated] = None,
    ) -> None:
        self._app = app
        self._validator = validator
        self._logger = logger
        self._on_authenticated = on_authenticated

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind == "lifespan":
            await self._app(scope, receive, send)
            return
        if kind != "http":
            # MCP has no WebSocket transport and uvicorn runs with ws="none";
            # nothing but HTTP reaches the app, authenticated or not.
            return
        from starlette.requests import HTTPConnection

        conn = HTTPConnection(scope)
        origin = conn.headers.get("origin")
        if origin and not _origin_is_loopback(origin):
            await _send_error(
                scope,
                receive,
                send,
                status=403,
                error="forbidden_origin",
                message=(
                    "cross-origin browser requests are refused; connect from an MCP "
                    "client, or from a page served on this machine"
                ),
            )
            return
        if self._validator.is_enabled():
            headers = dict(conn.headers)
            decision = self._validator.validate(headers)
            if not decision.allowed:
                reason = str(decision.deny_reason or "denied")
                # The wire gets a stable code (a JWT failure is named by its
                # exception class); the full text goes to the debug log only.
                code = reason.split(":", 1)[0]
                self._logger.debug(
                    "mcp_http_auth_refused kind=%s reason=%s", decision.identity_kind, reason
                )
                challenge = 'Bearer realm="fluid-mcp"'
                if reason != "missing-bearer-token":
                    challenge += ', error="invalid_token"'
                await _send_error(
                    scope,
                    receive,
                    send,
                    status=401,
                    error="unauthorized",
                    message=f"auth ({decision.identity_kind}) refused: {code}",
                    headers={"WWW-Authenticate": challenge},
                )
                return
            scope["fluid_auth_attrs"] = dict(decision.caller_attributes)
            scope["fluid_auth_kind"] = decision.identity_kind
            if self._on_authenticated is not None:
                self._on_authenticated(conn, headers)
            scope["user"] = _session_owner(scope)
        await self._app(scope, receive, send)


class _SessionEndpoint:
    """Hands every :data:`MCP_PATH` request (POST, GET, DELETE) to the session
    manager. A class, not a function, so Starlette routes the raw ASGI call
    instead of wrapping it as a request/response handler; the SDK's own
    ``StreamableHTTPASGIApp`` does the same."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self._manager.handle_request(scope, receive, send)


def build_streamable_http_app(
    server: Any,
    *,
    host: str,
    validator: Any,
    logger: logging.Logger,
    on_authenticated: Optional[OnAuthenticated] = None,
) -> Any:
    """Return the ASGI app serving the lowlevel MCP ``server`` on
    :data:`MCP_PATH`.

    ``validator`` is an ``AuthValidator``. ``on_authenticated(conn, headers)``
    runs after a request's token is verified, before the request reaches the
    server; the output port binds a trusted proxy's client certificate there.
    """
    from contextlib import asynccontextmanager

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.applications import Starlette
    from starlette.routing import Route

    if validator.is_enabled():
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        names = _loopback_names(host)
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{name}:*" for name in names],
            allowed_origins=[
                f"{scheme}://{name}:*" for name in names for scheme in ("http", "https")
            ],
        )
    manager = StreamableHTTPSessionManager(app=server, security_settings=security)

    @asynccontextmanager
    async def _lifespan(_app: Any):
        async with manager.run():
            yield

    app = Starlette(
        routes=[Route(MCP_PATH, endpoint=_SessionEndpoint(manager))], lifespan=_lifespan
    )
    return _HttpGuard(app, validator=validator, logger=logger, on_authenticated=on_authenticated)


async def serve_streamable_http(
    server: Any,
    *,
    command: str,
    host: str,
    port: int,
    validator: Any,
    logger: logging.Logger,
    on_authenticated: Optional[OnAuthenticated] = None,
    on_uvicorn: Optional[Callable[[Any], None]] = None,
) -> None:
    """Serve ``server`` on ``http://host:port/mcp`` until uvicorn stops.

    Refuses with :class:`UnsafeBindError` whatever :func:`bind_refusal`
    refuses, so no caller can open an unauthenticated server to the network.
    ``on_uvicorn`` receives the ``uvicorn.Server`` before it starts, so a
    caller can stop it (``should_exit`` / ``force_exit``).
    """
    refusal = bind_refusal(command=command, host=host, port=port, validator=validator)
    if refusal is not None:
        raise UnsafeBindError(refusal)
    import uvicorn

    # uvicorn binds the bare address: "[::1]" (URL form) does not resolve.
    bind_host = host.strip()
    if bind_host.startswith("[") and bind_host.endswith("]"):
        bind_host = bind_host[1:-1]
    app = build_streamable_http_app(
        server,
        host=host,
        validator=validator,
        logger=logger,
        on_authenticated=on_authenticated,
    )
    # proxy_headers=False: uvicorn otherwise rewrites the client address from
    # X-Forwarded-For whenever the peer is in FORWARDED_ALLOW_IPS (default
    # 127.0.0.1), and the trusted-proxy check must read the TCP peer.
    config = uvicorn.Config(
        app,
        host=bind_host,
        port=port,
        log_level="info",
        proxy_headers=False,
        lifespan="on",
        ws="none",
        timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
    )
    uvicorn_server = uvicorn.Server(config)
    if on_uvicorn is not None:
        on_uvicorn(uvicorn_server)
    await uvicorn_server.serve()
