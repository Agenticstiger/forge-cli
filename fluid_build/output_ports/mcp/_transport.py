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

"""Transport / lifecycle for the MCP output-port server.

Physically extracted from the ``OutputPortMcpServer`` god-class in
``server.py``. These functions own *how the server is served* —
stdio (the SDK pipe transport) and HTTP/SSE (Starlette + uvicorn) —
which is a distinct concern from the protocol-handler registration +
policy enforcement that stays in ``server.py``.

Each takes the server instance (``srv``) explicitly rather than
``self``; ``server.py`` keeps thin delegating methods
(``run`` / ``run_async`` / ``run_http_async`` / ``stop_http``) so the
public surface and the CLI entry point (``run_stdio``) are unchanged.
``OutputPortMcpServer`` is imported only under ``TYPE_CHECKING`` —
runtime is duck-typed against ``srv.server`` / ``srv.state`` /
``srv._uvicorn_server`` — so ``server.py → _transport`` is the only
import edge (no cycle).
"""

from __future__ import annotations

import asyncio
import signal
from typing import TYPE_CHECKING, List

from mcp.server.stdio import stdio_server

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .server import OutputPortMcpServer


async def run_http_async(
    srv: "OutputPortMcpServer", *, host: str = "127.0.0.1", port: int = 8765
) -> None:
    """Serve the gateway over MCP-SSE on ``host:port``.

    Borrowed transport: ``mcp.server.sse.SseServerTransport`` +
    Starlette + uvicorn. The MCP SDK ships SseServerTransport;
    Starlette / uvicorn come along as transitive deps of the
    ``mcp`` extra. Operators connect via
    ``http://host:port/sse`` from any MCP client that supports
    the SSE transport (Claude Desktop, MCP Inspector,
    custom HTTP-MCP clients).

    Identity binding still flows through ``clientInfo`` —
    SSE-bound sessions are functionally identical to stdio
    ones; the only difference is the wire transport.

    Authentication: when ``FLUID_MCP_AUTH_TOKEN`` is set in the
    environment, the gateway requires every HTTP request to
    carry an ``Authorization: Bearer <token>`` header that
    matches. Wrong / missing token → 401 Unauthorized BEFORE
    the SSE handshake runs, so callers can't even open a
    session. Comparison uses ``hmac.compare_digest`` to defeat
    timing-side-channel guesses. This is a real defensive
    layer — better than nothing — but operators MUST still
    front the gateway with an mTLS / OAuth proxy for production
    because shared-secret tokens are vulnerable to replay /
    leakage. See ``examples/mcp-output-port-docker/proxy/`` for
    a Caddy reverse-proxy template that pairs this gateway with
    mTLS or OAuth2 enforcement.
    """
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Mount, Route

    from .auth import AuthValidator, extract_mtls_identity

    # Resolve the auth strategy from FLUID_MCP_AUTH_MODE
    # (shared-token / jwt / none). When the validator
    # is unconfigured (mode=none, or shared-token without
    # FLUID_MCP_AUTH_TOKEN), the gateway runs unauthenticated
    # and surfaces a loud warning.
    auth_validator = AuthValidator.from_env()
    if not auth_validator.is_enabled():
        srv.state.logger.warning(
            "output_port_http_no_auth_configured: gateway is unauthenticated. "
            "Set FLUID_MCP_AUTH_MODE=jwt|shared-token + matching "
            "config OR front with mTLS/OAuth proxy before exposing to an "
            "untrusted network."
        )

    class _AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if not auth_validator.is_enabled():
                return await call_next(request)
            decision = auth_validator.validate(dict(request.headers))
            if not decision.allowed:
                return JSONResponse(
                    {
                        "error": "unauthorized",
                        "message": (
                            f"auth ({decision.identity_kind}) refused: " f"{decision.deny_reason}"
                        ),
                    },
                    status_code=401,
                )
            # Stash the resolved caller attributes on the request
            # scope so the SSE handler below can merge them onto
            # the SessionState (cryptographic identity replaces
            # self-attestation for downstream rowFilter resolution).
            request.scope["fluid_auth_attrs"] = dict(decision.caller_attributes)
            request.scope["fluid_auth_kind"] = decision.identity_kind
            # Also pull mTLS metadata forwarded by the proxy so
            # the audit trail records BOTH the JWT identity AND
            # the cert that carried it.
            request.scope["fluid_auth_attrs"].update(extract_mtls_identity(dict(request.headers)))
            return await call_next(request)

    sse_path = "/sse"
    messages_path = "/messages/"
    sse = SseServerTransport(messages_path)

    async def handle_sse(request):
        # NOTE: deliberately NO per-connection write of identity onto
        # the shared SessionState here. One gateway process serves many
        # concurrent SSE clients over ONE SessionState, so stamping the
        # connecting client's caller_attributes / model_id / use_case
        # onto it bled the FIRST client's identity onto every later
        # client (wrong agentPolicy principal + wrong tenant rowFilter).
        # The _AuthMiddleware already stashes the verified cryptographic
        # attrs at ``request.scope["fluid_auth_attrs"]`` PER REQUEST; the
        # dispatcher resolves them fresh per tools/call via
        # ``OutputPortMcpServer._resolve_request_identity`` (which reads
        # request_context.request.scope — for SSE the POST /messages/
        # Request — and lets crypto win over self-attested clientInfo).
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await srv.server.run(
                streams[0],
                streams[1],
                srv.server.create_initialization_options(),
            )
        return Response()

    app = Starlette(
        routes=[
            Route(sse_path, endpoint=handle_sse),
            Mount(messages_path, app=sse.handle_post_message),
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    # Stash the uvicorn Server so stop_http() (or a test harness,
    # or a future SIGTERM handler for the HTTP path) can flip
    # ``should_exit`` and unwind serve() cleanly. Without this the
    # only way to stop an HTTP-mode gateway was process kill —
    # and a test that spun one in a daemon thread leaked a
    # spinning event loop for the rest of the run.
    srv._uvicorn_server = server
    srv.state.logger.info(
        "output_port_http_serve_start",
        extra={
            "host": host,
            "port": port,
            "sse_path": sse_path,
            "auth_mode": auth_validator.mode,
            "auth_enabled": auth_validator.is_enabled(),
        },
    )
    try:
        await server.serve()
    finally:
        srv._uvicorn_server = None


def stop_http(srv: "OutputPortMcpServer", *, force: bool = False) -> None:
    """Signal a running HTTP/SSE transport to shut down.

    Sets uvicorn's ``should_exit`` flag so the ``serve()`` loop
    returns at its next iteration. Safe to call from any thread
    (the flag is a plain bool uvicorn polls). No-op when the
    gateway isn't running in HTTP mode.

    When ``force`` is True, ALSO set uvicorn's ``force_exit``. By
    itself ``should_exit`` only breaks the accept loop — uvicorn's
    ``shutdown()`` then WAITS for any still-open connection to drain
    (a half-open MCP-SSE stream whose server-side handler hasn't
    noticed the client went away can hold this open for a long time).
    ``force_exit`` makes ``serve()`` return immediately by cancelling
    those connections instead of waiting. Graceful (``force=False``)
    is the right default for a production stop so in-flight tool calls
    finish; a hard stop is what a test harness — or any "shut down
    NOW" caller — needs so it never leaks a spinning server loop."""
    server = getattr(srv, "_uvicorn_server", None)
    if server is not None:
        server.should_exit = True
        if force:
            server.force_exit = True


async def run_async(srv: "OutputPortMcpServer") -> None:
    """Run the server on the SDK stdio transport until the
    client disconnects.

    Installs SIGTERM and SIGINT handlers that flip
    ``state._shutdown_event`` so the lifespan teardown can drain
    in-flight tool calls (up to 5s) before tearing down driver
    connections. SIGHUP is intentionally NOT trapped — operators
    use it for log-rotation triggers in some deployments.
    """

    loop = asyncio.get_running_loop()

    def _handle_sig(sig: signal.Signals) -> None:
        srv.state.logger.info(
            "output_port_signal_received",
            extra={"signal": sig.name, "in_flight": srv.state._in_flight},
        )
        if srv.state._shutdown_event is not None:
            srv.state._shutdown_event.set()
        # Cancel the main task so stdio_server unblocks.
        for task in asyncio.all_tasks(loop):
            if task is not asyncio.current_task():
                task.cancel()

    registered: List[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_sig, sig)
            registered.append(sig)
        except (NotImplementedError, RuntimeError):
            # Windows / restricted environments fall back to the
            # KeyboardInterrupt path in ``run()``.
            pass

    try:
        async with stdio_server() as (read_stream, write_stream):
            await srv.server.run(
                read_stream,
                write_stream,
                srv.server.create_initialization_options(),
            )
    finally:
        for sig in registered:
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                pass


def _iter_leaf_exceptions(exc: BaseException):
    """Yield leaf exceptions, flattening nested ``BaseExceptionGroup``s.

    The MCP SDK runs request handlers inside an asyncio TaskGroup, so a
    failure (or a connection teardown) arrives wrapped — and the group
    can itself nest, so we recurse rather than peeling one layer.
    """
    group = getattr(exc, "exceptions", None)
    if group:
        for sub in group:
            yield from _iter_leaf_exceptions(sub)
    else:
        yield exc


def _is_clean_disconnect(exc: BaseException) -> bool:
    """True when *every* leaf exception is a stream-teardown error.

    A stdio client that pipes its requests then closes stdin — and any
    HTTP/SSE client that drops mid-call — makes the SDK raise
    ``anyio.ClosedResourceError`` / ``BrokenResourceError`` /
    ``EndOfStream`` when it goes to write the (possibly executor-backed,
    so slightly delayed) response onto the now-closed stream. That's a
    normal lifecycle event, NOT a server fault: surfacing it as
    ``output_port_server_crashed`` (rc=1 + ERROR log) is a false alarm
    that pollutes audit trails and breaks well-behaved test harnesses.

    Matched by class name across the MRO so we don't take a hard import
    on ``anyio`` at module load — it ships transitively under the
    ``mcp`` extra, but keeping the reference lazy mirrors the rest of
    this module (uvicorn / starlette are imported inside the functions
    that need them). A mixed group (a real handler crash AND a
    disconnect) returns False so the genuine fault is still logged.
    """
    teardown_names = {
        "ClosedResourceError",
        "BrokenResourceError",
        "EndOfStream",
    }
    leaves = list(_iter_leaf_exceptions(exc))
    if not leaves:
        return False
    return all(
        any(klass.__name__ in teardown_names for klass in type(leaf).__mro__) for leaf in leaves
    )


def run(
    srv: "OutputPortMcpServer",
    *,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
) -> int:
    """Synchronous entry point used by the CLI. ``transport``
    is one of ``"stdio"`` (default — pipe with the MCP client) or
    ``"http"`` (MCP-SSE on ``host:port``). Returns the process
    exit code (0 on clean disconnect, non-zero on startup
    failure)."""
    try:
        if transport == "http":
            asyncio.run(run_http_async(srv, host=host, port=port))
        elif transport == "stdio":
            asyncio.run(run_async(srv))
        else:
            raise ValueError(f"unknown transport {transport!r}; supported: stdio, http")
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Both arrive on SIGTERM/SIGINT after the lifespan drain.
        return 0
    except Exception as exc:  # noqa: BLE001
        # A client that closes its end mid-response (stdio EOF after
        # piping requests; HTTP/SSE drop) is a clean disconnect, not a
        # crash — exit 0 without the alarming ERROR log.
        if _is_clean_disconnect(exc):
            srv.state.logger.info(
                "output_port_client_disconnected",
                extra={"in_flight": srv.state._in_flight},
            )
            return 0
        # The MCP SDK runs request handlers inside an asyncio TaskGroup,
        # so a tool-handler crash surfaces here as a BaseExceptionGroup
        # whose ``str()`` is just "unhandled errors in a TaskGroup". Log
        # each sub-exception WITH its traceback — swallowing the real
        # cause behind the group summary is a debugging dead end.
        sub_exceptions = getattr(exc, "exceptions", None)
        if sub_exceptions:
            for index, sub in enumerate(sub_exceptions, start=1):
                srv.state.logger.error(
                    "output_port_server_crashed [%d/%d]: %s",
                    index,
                    len(sub_exceptions),
                    sub,
                    exc_info=sub,
                )
        else:
            srv.state.logger.error("output_port_server_crashed: %s", exc, exc_info=exc)
        return 1
