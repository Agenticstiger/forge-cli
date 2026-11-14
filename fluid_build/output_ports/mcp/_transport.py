# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Transport / lifecycle for the MCP output-port server.

Physically extracted from the ``OutputPortMcpServer`` god-class in
``server.py``. These functions own *how the server is served* —
stdio (the SDK pipe transport) and Streamable HTTP (shared with
``fluid mcp serve`` in ``fluid_build._mcp_http``) —
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
import logging
import signal
from typing import TYPE_CHECKING, Any, List, Mapping, Optional, Set, Tuple

from mcp.server.stdio import stdio_server

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .server import OutputPortMcpServer


async def run_http_async(
    srv: "OutputPortMcpServer", *, host: str = "127.0.0.1", port: int = 8765
) -> None:
    """Serve the gateway over MCP Streamable HTTP at ``http://host:port/mcp``.

    The transport is shared with ``fluid mcp serve``
    (:func:`fluid_build._mcp_http.serve_streamable_http`): the SDK's
    ``StreamableHTTPSessionManager`` behind a bearer-auth gate, on starlette +
    uvicorn from the ``[mcp]`` extra. It replaced HTTP+SSE (``/sse`` +
    ``/messages/``), which the MCP spec deprecated in its 2025-03-26 revision.

    Authentication follows ``FLUID_MCP_AUTH_MODE`` (``shared-token`` with
    ``FLUID_MCP_AUTH_TOKEN``, ``jwt``, or ``none``). A request without a valid
    ``Authorization: Bearer`` header gets 401 before it reaches a session; the
    shared-token comparison is ``hmac.compare_digest``. A verified request's
    identity rides on its ASGI scope (``fluid_auth_attrs`` /
    ``fluid_auth_kind``), and :meth:`OutputPortMcpServer._resolve_request_identity`
    reads it per ``tools/call`` from ``request_context.request.scope`` — for
    Streamable HTTP, the POST that carried the call — so one gateway serving many
    clients never stamps one client's identity onto another. Without auth the
    gateway listens only on loopback (``bind_refusal``).

    Shared tokens can be replayed or leak, so for production front the gateway
    with an mTLS / OAuth proxy; ``examples/mcp-output-port-docker/proxy/`` has a
    Caddy template. A trusted proxy's forwarded client certificate binds through
    :class:`_ForwardedCertIdentity`.
    """
    from fluid_build._mcp_http import MCP_PATH, serve_streamable_http

    from .auth import AuthValidator

    # Resolve the auth strategy from FLUID_MCP_AUTH_MODE
    # (shared-token / jwt / none). Unconfigured (mode=none, or shared-token
    # without FLUID_MCP_AUTH_TOKEN) means the gateway runs unauthenticated,
    # loopback only, with a loud warning.
    auth_validator = AuthValidator.from_env()
    if not auth_validator.is_enabled():
        srv.state.logger.warning(
            "output_port_http_no_auth_configured: gateway is unauthenticated, so it "
            "listens on loopback only. Set FLUID_MCP_AUTH_MODE=jwt|shared-token + "
            "matching config OR front with mTLS/OAuth proxy before exposing to an "
            "untrusted network."
        )

    forwarded_certs = _ForwardedCertIdentity(auth_validator, srv.state.logger)

    def _stash(server: Any) -> None:
        # Stash the uvicorn Server so stop_http() (or a test harness, or a
        # SIGTERM handler for the HTTP path) can flip ``should_exit`` and
        # unwind serve() cleanly. Without this the only way to stop an
        # HTTP-mode gateway was process kill, and a test that spun one in a
        # daemon thread leaked a spinning event loop for the rest of the run.
        srv._uvicorn_server = server

    srv.state.logger.info(
        "output_port_http_serve_start",
        extra={
            "host": host,
            "port": port,
            "mcp_path": MCP_PATH,
            "auth_mode": auth_validator.mode,
            "auth_enabled": auth_validator.is_enabled(),
            "trusted_proxies": [str(n) for n in auth_validator.trusted_proxies],
        },
    )
    try:
        await serve_streamable_http(
            srv.server,
            command="fluid mcp output-port",
            host=host,
            port=port,
            validator=auth_validator,
            logger=srv.state.logger,
            on_authenticated=forwarded_certs.bind,
            on_uvicorn=_stash,
        )
    finally:
        srv._uvicorn_server = None


class _ForwardedCertIdentity:
    """Binds the client certificate an mTLS-terminating proxy forwarded
    (``X-Client-CN`` / ``X-Client-Fingerprint``) onto an authenticated
    request — only when the TCP peer is listed in
    ``FLUID_MCP_TRUSTED_PROXIES``; anyone else could have set the headers
    itself.

    When honoured the values bind ``${caller.client_cn}`` /
    ``${caller.client_fingerprint}`` (via ``fluid_auth_attrs``) and are kept
    apart under ``fluid_mtls_identity`` so the data_access audit record can
    name the certificate next to ``authKind`` without mistaking a token claim
    for it.

    Ignored headers are logged at WARNING with the real reason and the peer
    address, once per (reason, peer). An operator who has not yet listed
    their proxy sees which address to add instead of a bare row-filter
    denial. The set of logged pairs is bounded so a client cycling source
    addresses cannot turn the log into a flood: after
    ``MAX_LOGGED_PEERS`` pairs one final line says further ones are
    suppressed. Header values are never written to the log.
    """

    #: Distinct (reason, peer) pairs logged before the rest are suppressed.
    MAX_LOGGED_PEERS = 32

    def __init__(self, validator: Any, logger: logging.Logger) -> None:
        self._trusted = validator.trusted_proxies
        self._logger = logger
        self._logged: Set[Tuple[str, str]] = set()
        self._suppressed = False

    def bind(self, request: Any, headers: Mapping[str, str]) -> None:
        from .auth import extract_mtls_identity, has_mtls_identity_headers

        peer_host = request.client.host if request.client else None
        identity = extract_mtls_identity(
            headers, peer_host=peer_host, trusted_proxies=self._trusted
        )
        if identity:
            request.scope["fluid_auth_attrs"].update(identity)
            request.scope["fluid_mtls_identity"] = identity
            return
        if self._suppressed or not has_mtls_identity_headers(headers):
            return
        self._log_ignored(peer_host, headers)

    def _log_ignored(self, peer_host: Optional[str], headers: Mapping[str, str]) -> None:
        from .auth import ignored_mtls_headers_reason

        reason = ignored_mtls_headers_reason(
            headers, peer_host=peer_host, trusted_proxies=self._trusted
        )
        peer = peer_host or "unknown"
        key = (reason, peer)
        if key in self._logged:
            return
        if len(self._logged) >= self.MAX_LOGGED_PEERS:
            self._suppressed = True
            self._logger.warning(
                "output_port_mtls_headers_ignored: further ignored X-Client-CN / "
                "X-Client-Fingerprint warnings are suppressed after %d distinct peers.",
                self.MAX_LOGGED_PEERS,
            )
            return
        self._logged.add(key)
        if reason == "unknown_peer":
            why = "the TCP peer address is unknown, so it cannot be a trusted proxy"
        elif reason == "untrusted_peer":
            why = (
                f"the peer {peer} is not in FLUID_MCP_TRUSTED_PROXIES. If {peer} is "
                f"your mTLS-terminating proxy, set FLUID_MCP_TRUSTED_PROXIES={peer}; "
                "otherwise the client set these headers itself"
            )
        elif reason == "empty":
            why = f"the trusted proxy {peer} sent them with empty values"
        else:
            why = f"the values the trusted proxy {peer} sent contain control characters"
        self._logger.warning(
            "output_port_mtls_headers_ignored: X-Client-CN / X-Client-Fingerprint "
            "ignored because %s. Any row filter on ${caller.client_cn} or "
            "${caller.client_fingerprint} denies this request (logged once per "
            "reason and peer).",
            why,
            extra={"peer": peer, "reason": reason},
        )


def stop_http(srv: "OutputPortMcpServer", *, force: bool = False) -> None:
    """Signal a running Streamable HTTP transport to shut down.

    Sets uvicorn's ``should_exit`` flag so the ``serve()`` loop
    returns at its next iteration. Safe to call from any thread
    (the flag is a plain bool uvicorn polls). No-op when the
    gateway isn't running in HTTP mode.

    When ``force`` is True, ALSO set uvicorn's ``force_exit``. By
    itself ``should_exit`` only breaks the accept loop — uvicorn's
    ``shutdown()`` then WAITS for any still-open connection to drain
    (a client's half-open GET stream whose server-side handler hasn't
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
    HTTP client that drops mid-call — makes the SDK raise
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
    ``"http"`` (MCP Streamable HTTP on ``host:port/mcp``). Returns the process
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
        # piping requests; an HTTP drop) is a clean disconnect, not a
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
