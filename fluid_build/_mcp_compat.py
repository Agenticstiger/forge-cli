"""Version-compat seam for the MCP Python SDK (1.x and 2.x).

The SDK's 2.0.0 (2026-07-28) renamed ``FastMCP`` to ``MCPServer``
(``mcp.server.mcpserver``), inverted the lowlevel ``Server`` registration
API (decorators -> ``on_*`` constructor handlers with a ctx-first
signature), and renamed model fields from camelCase to snake_case at the
*attribute* level (``result.isError`` -> ``result.is_error``; constructor
kwargs accept both spellings).  Everything else forge-cli touches survived
with a compatible shape — verified against the installed 2.0.0 wheel, not
the migration guide, which oversells several removals:

- ``mcp.server.stdio.stdio_server`` and ``mcp.server.sse.SseServerTransport``
  still exist with v1-compatible interfaces (the guide calls them internal;
  the ``mcp-sdk-drift`` CI job is the tripwire if a 2.x minor drops them).
- ``ClientSession`` / ``ClientSessionGroup`` / ``StdioServerParameters`` and
  ``mcp.client.session_group.{SseServerParameters,StreamableHttpParameters}``
  are intact, as are ``ServerSession.create_message`` and
  ``ServerSession.check_client_capability``.
- ``mcp.shared.memory.create_connected_server_and_client_session`` is GONE
  in v2; only ``create_client_server_memory_streams`` remains (see
  ``open_inmemory_session``).

This module is the ONLY place allowed to branch on the SDK generation.
Runtime code keeps the ``mcp>=1.20,<2.0`` install pin (pyproject.toml)
until the 2.x drift leg has a green track record — the dual support here
is what turns that leg green in the first place.

Tier-0 leaf rules: no module-scope ``mcp`` import (the startup budget in
``tests/perf/test_startup_budget.py`` forbids ``mcp`` on ``fluid --help``);
every SDK import below is function-local, and the version probe uses
``find_spec`` (metadata only, imports nothing).
"""

from __future__ import annotations

import functools
import importlib.util
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

if TYPE_CHECKING:  # pragma: no cover - annotation-only
    from mcp.server.lowlevel import Server

_MISSING = object()


@functools.lru_cache(maxsize=1)
def is_v2() -> bool:
    """True when the installed SDK is the 2.x generation.

    Feature-probe (``mcp.server.mcpserver`` exists only in 2.x) — never
    parse ``mcp.__version__``.  ``find_spec`` reads packaging metadata
    without importing ``mcp``, so the cold-path budget is unaffected.
    Memoised with ``lru_cache`` per house style (lock-guarded, and
    ``is_v2.cache_clear()`` is the test reset hook).
    """
    return importlib.util.find_spec("mcp.server.mcpserver") is not None


def attr(obj: Any, snake: str, camel: str, default: Any = None) -> Any:
    """Read an SDK model field across the v2 camelCase->snake_case rename.

    v1 exposes ``camel`` attributes, v2 exposes ``snake``; a single-name
    ``getattr(obj, name, default)`` therefore fails *silently* on the other
    generation (returns ``default`` — errors read as success, schemas read
    as empty).  Always route SDK-model reads whose v1 spelling is camelCase
    through this helper.
    """
    value = getattr(obj, snake, _MISSING)
    if value is not _MISSING:
        return value
    return getattr(obj, camel, default)


def get_server_api() -> tuple[type, type]:
    """Return ``(ServerCls, Context)`` for the high-level decorator API.

    v2: ``mcp.server.mcpserver.MCPServer``; v1: ``mcp.server.fastmcp.FastMCP``.
    Both accept ``ServerCls(name=...)`` and register tools via
    ``.tool(**kwargs)(fn)``, and both inject ``ctx`` as a handler parameter,
    so callers need no further branching.
    """
    if is_v2():
        from mcp.server.mcpserver import Context, MCPServer

        return MCPServer, Context
    from mcp.server.fastmcp import Context, FastMCP  # type: ignore[no-redef]

    return FastMCP, Context


def build_lowlevel_server(
    name: str,
    *,
    version: Optional[str] = None,
    lifespan: Any = None,
    on_list_tools: Optional[Callable[[], Awaitable[List[Any]]]] = None,
    on_list_resources: Optional[Callable[[], Awaitable[List[Any]]]] = None,
    on_read_resource: Optional[Callable[[str], Awaitable[str]]] = None,
    on_call_tool: Optional[Callable[[Any, str, Dict[str, Any]], Awaitable[Any]]] = None,
) -> "Server":
    """Build a lowlevel ``Server`` with version-neutral handlers.

    Caller-facing handler contract (identical on both generations):

    - ``on_list_tools() -> list[Tool]``
    - ``on_list_resources() -> list[Resource]``
    - ``on_read_resource(uri: str) -> str`` (payload text; wrapped as
      ``text/plain`` contents, matching the v1 SDK's str handling)
    - ``on_call_tool(ctx, name, arguments) -> list[content] | CallToolResult``
      where ``ctx`` is the version-native request context (v1: the
      ``server.request_context`` property value; v2: the handler-passed
      ``ServerRequestContext``).  Both expose ``.session`` and ``.request``
      via ``getattr``, which is all identity resolution reads.

    v1 registers thin adapters through the decorator API; v2 passes them as
    ``on_*`` constructor kwargs and wraps returns in the typed ``*Result``
    models the 2.x lowlevel server requires.
    """
    from mcp.server.lowlevel import Server  # local: keep ``fluid --help`` light

    # Forward ``version`` / ``lifespan`` only when set: an explicit None
    # clobbers the SDK's defaults (v1's default lifespan is a real async CM
    # — None crashes ``server.run`` with "'NoneType' object is not
    # callable"; v2 requires ``server_version`` to be a str at
    # ``create_initialization_options()``).
    base_kwargs: Dict[str, Any] = {}
    if version is not None:
        base_kwargs["version"] = version
    if lifespan is not None:
        base_kwargs["lifespan"] = lifespan

    if not is_v2():
        server = Server(name, **base_kwargs)

        if on_list_tools is not None:

            @server.list_tools()
            async def _list_tools() -> List[Any]:  # pragma: no cover - thin adapter
                return await on_list_tools()

        if on_list_resources is not None:

            @server.list_resources()
            async def _list_resources() -> List[Any]:  # pragma: no cover - thin adapter
                return await on_list_resources()

        if on_read_resource is not None:

            @server.read_resource()
            async def _read_resource(uri: Any) -> str:
                return await on_read_resource(str(uri))

        if on_call_tool is not None:

            @server.call_tool()
            async def _call_tool(tool_name: str, arguments: Dict[str, Any]) -> Any:
                try:
                    ctx: Any = server.request_context
                except Exception:  # noqa: BLE001 - no active request context
                    ctx = None
                return await on_call_tool(ctx, tool_name, arguments or {})

        return server

    from mcp import types as _t

    handlers: Dict[str, Any] = {}

    if on_list_tools is not None:

        async def _list_tools_v2(ctx: Any, params: Any) -> Any:
            return _t.ListToolsResult(tools=await on_list_tools())

        handlers["on_list_tools"] = _list_tools_v2

    if on_list_resources is not None:

        async def _list_resources_v2(ctx: Any, params: Any) -> Any:
            return _t.ListResourcesResult(resources=await on_list_resources())

        handlers["on_list_resources"] = _list_resources_v2

    if on_read_resource is not None:

        async def _read_resource_v2(ctx: Any, params: Any) -> Any:
            text = await on_read_resource(str(params.uri))
            return _t.ReadResourceResult(
                contents=[
                    _t.TextResourceContents(uri=str(params.uri), text=text, mimeType="text/plain")
                ]
            )

        handlers["on_read_resource"] = _read_resource_v2

    if on_call_tool is not None:

        async def _call_tool_v2(ctx: Any, params: Any) -> Any:
            response = await on_call_tool(ctx, params.name, params.arguments or {})
            if isinstance(response, _t.CallToolResult):
                return response
            # v1's decorator wrapped bare content lists; mirror that here.
            return _t.CallToolResult(content=list(response))

        handlers["on_call_tool"] = _call_tool_v2

    return Server(name, **base_kwargs, **handlers)


def self_attesting_client_kwargs(
    name: str,
    version: str,
    **fluid_attrs: Any,
) -> Dict[str, Any]:
    """``ClientSession`` kwargs for a client self-attesting its identity.

    forge-cli's MCP output port resolves a caller's self-attested identity
    (``model``, ``useCase``, tenant attributes…) from two channels:

    - **SDK 1.x**: extra fields on ``clientInfo`` (v1's ``Implementation``
      is ``extra="allow"``).
    - **SDK 2.x**: a ``fluid`` block under the client's declared
      capabilities — v2's ``Implementation`` silently DROPS unknown fields
      at wire-parse (upstream regression vs v1), so the extras channel is
      dead there; ``ClientSession(extensions={"fluid": ...})`` becomes
      ``capabilities.extensions["fluid"]`` on the wire.

    Returns the version-correct kwargs so tests (and embedding clients)
    can self-attest identically on both generations.
    """
    from mcp.types import Implementation

    if is_v2():
        return {
            "client_info": Implementation(name=name, version=version),
            "extensions": {"fluid": dict(fluid_attrs)},
        }
    return {"client_info": Implementation(name=name, version=version, **fluid_attrs)}


@asynccontextmanager
async def open_inmemory_session(server: "Server", **client_kwargs: Any) -> AsyncIterator[Any]:
    """Yield a ``ClientSession`` wired to ``server`` over in-memory streams.

    v1 delegates to ``mcp.shared.memory.create_connected_server_and_client_session``;
    v2 removed that helper, so we recompose it from the surviving
    ``create_client_server_memory_streams`` + ``server.run`` (same stream
    signature on both generations).  ``client_kwargs`` pass through to
    ``ClientSession`` (e.g. ``client_info=``, ``sampling_callback=``).
    """
    if not is_v2():
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(server, **client_kwargs) as session:
            yield session
        return

    import anyio
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: server.run(
                    server_read,
                    server_write,
                    server.create_initialization_options(),
                    raise_exceptions=True,
                )
            )
            async with ClientSession(client_read, client_write, **client_kwargs) as session:
                await session.initialize()
                yield session
            tg.cancel_scope.cancel()
