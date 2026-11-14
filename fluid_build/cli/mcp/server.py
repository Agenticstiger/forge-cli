# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""
MCP FastMCP server: app instance and 7 async tool registrations.

Split from ``fluid_build.cli.mcp`` (issue #11) to reduce the 2 446-line
monolith into focused, testable modules.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    # Annotation-only. The MCP server SDK is heavy (~87 modules / ~100ms).
    # Importing this module only registers the ``fluid mcp`` subparser —
    # which happens on every ``fluid`` invocation, including ``--help`` —
    # so the SDK is imported lazily at serve time (see ``_forge_tool`` /
    # ``_get_mcp_app``) to keep the hot path light. Runtime uses import
    # locally where needed. Pinned by tests/perf/test_startup_budget.py.
    #
    # The high-level app + per-request context classes differ per SDK
    # generation (v1 ``fastmcp.FastMCP``/``Context``, v2
    # ``mcpserver.MCPServer``/``Context``), so annotations use permissive
    # aliases; ``_get_mcp_app`` binds the version-correct ``Context`` class
    # into module globals before any tool registration (the ``eval_str``
    # annotation resolution below depends on that).

    FastMCP = Any
    Context = Any

from fluid_build.cli.mcp.models import (
    _ADAPTER_DISPATCH_DESCRIPTION,
    _ALLOW_METADATA_SERVICE_DESCRIPTION,
    _CREDENTIALS_DESCRIPTION,
    _DIFF_NEW_DESCRIPTION,
    _DIFF_OLD_DESCRIPTION,
    _FQN_DESCRIPTION,
    _PATH_LOGICAL_DESCRIPTION,
    _PATH_LOGICAL_SHORT_DESCRIPTION,
    _REGEN_CONTRACT_OUT_DESCRIPTION,
    _REGEN_ENGINE_DESCRIPTION,
    _RESPONSE_FORMAT_DESCRIPTION,
    _SCOPE_DESCRIPTION,
    _SOURCE_KIND_DESCRIPTION,
    _VALIDATE_CONTRACT_PATH_DESCRIPTION,
    _VALIDATE_LOGICAL_PATH_DESCRIPTION,
    SERVER_INSTRUCTIONS,
    SOURCE_CATALOG_TOOL,
    TOOL_CAPABILITIES,
    CredentialsArg,
    ScopeArg,
    _CatalogSourceLiteral,
    _ResponseFormatLiteral,
    _SourceCatalogKindLiteral,
)
from fluid_build.cli.mcp.policy import _policy

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Lazy FastMCP app (preserves #265). Importing this module must NOT import
# the MCP server SDK — it only registers the ``fluid mcp`` subparser, which
# runs on every ``fluid`` invocation (including ``--help``). The
# ``@_forge_tool(...)`` decorators below only RECORD each tool (no SDK
# needed); the real ``FastMCP`` app is instantiated and the recorded tools
# registered onto it only on first ``_get_mcp_app()`` (serve time).
# Pinned by tests/perf/test_startup_budget.py.
# ----------------------------------------------------------------------
_PENDING_TOOLS: List[Tuple[Any, Dict[str, Any]]] = []
_mcp_app: Optional["FastMCP"] = None


def _forge_tool(**tool_kwargs: Any):
    """Defer FastMCP tool registration so importing this module doesn't load
    the MCP server SDK.

    Records ``(fn, FastMCP.tool kwargs)``; the real registration happens in
    :func:`_get_mcp_app` at serve time. Returns ``fn`` unchanged so the tool
    coroutine stays a plain module-level function (callable directly in tests).
    """

    def _register(fn):
        _PENDING_TOOLS.append((fn, tool_kwargs))
        return fn

    return _register


def _get_mcp_app() -> "FastMCP":
    """Build (once) and return the FastMCP app.

    Imports the MCP server SDK lazily and registers every ``@_forge_tool``
    tool. Cached on the module global so repeated ``serve`` builds — and the
    policy-driven ``remove_tool`` pruning in
    ``fluid_build.cli.mcp.policy._build_fastmcp_app`` — operate on a single
    app instance.
    """
    global _mcp_app
    if _mcp_app is None:
        from fluid_build._mcp_compat import get_server_api

        ServerCls, Context = get_server_api()

        # The server introspects each tool's signature with ``eval_str=True``.
        # A tool that annotates a session param ``ctx: Context`` under
        # ``from __future__ import annotations`` has a string annotation
        # evaluated against THIS module's globals. Bind the
        # version-correct ``Context`` into globals here (build time) so the
        # eval resolves — without importing the SDK at module-load time, which
        # is the whole point of the laziness.
        globals()["Context"] = Context

        app = ServerCls(name="forge-cli-mcp", instructions=SERVER_INSTRUCTIONS)
        for fn, tool_kwargs in _PENDING_TOOLS:
            app.tool(**tool_kwargs)(fn)
        _register_artifact_resource(app)
        _mcp_app = app
    return _mcp_app


#: Set by ``fluid mcp serve --transport http`` (:func:`serve_tools_off_loop`).
#: Over HTTP one event loop serves uvicorn and every client's session, and a
#: catalog tool waits on the network, so a tool must not run on that loop:
#: one slow call would stall every other client and delay Ctrl-C. Each call
#: runs on the one thread of ``_tool_executor()`` instead, so calls stay one at
#: a time, as stdio runs them one at a time on the loop: the tools were never
#: written to run concurrently.
_TOOLS_OFF_LOOP = False
_TOOL_EXECUTOR: Optional[concurrent.futures.ThreadPoolExecutor] = None
_TOOL_EXECUTOR_GUARD = threading.Lock()


def serve_tools_off_loop() -> None:
    """Run every sync tool on a dedicated worker thread, one at a time (HTTP serving)."""
    global _TOOLS_OFF_LOOP
    _TOOLS_OFF_LOOP = True


def _tool_executor() -> concurrent.futures.ThreadPoolExecutor:
    """The single worker thread tool calls run on when served over HTTP.

    One worker, not a lock: a call waiting its turn sits in the executor's
    queue and holds no thread, so the default executor (the SDK uses it
    through ``asyncio.to_thread``) is never starved. A client that
    cancels its call (``notifications/cancelled``, or a dropped session)
    cancels the queued future, so a waiting call never runs; a running call
    cannot be cancelled and keeps the thread until it finishes, so the next
    call never runs beside it.
    """
    global _TOOL_EXECUTOR
    with _TOOL_EXECUTOR_GUARD:
        if _TOOL_EXECUTOR is None:
            _TOOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="fluid-mcp-tool"
            )
        return _TOOL_EXECUTOR


async def _dispatch_sync_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Permission-gate + sync execution for a fast read/write tool.

    NB: over stdio this deliberately calls ``_call_tool`` SYNCHRONOUSLY (no
    ``asyncio.to_thread``). With one client the event-loop block is
    harmless — and going through a worker thread causes a real race when
    a client pipelines requests (e.g. the
    ``tests/test_mcp_protocol_smoke.py`` smoke test sends 3 messages then
    closes stdin): the SDK can hit stdin-EOF and start shutting down
    before the worker thread's response makes it to stdout, producing
    a missing-response failure plus ``ValueError: I/O operation on
    closed file`` on the worker thread. Sync dispatch guarantees each
    response is fully flushed before the next request is read.

    Over HTTP (``_TOOLS_OFF_LOOP``) the call runs in a worker thread
    instead; there is no stdin to close, and the loop must stay free.
    """
    from fluid_build.cli.mcp.dispatch import McpToolRefusal, _call_tool

    check_tool_permission = __import__(
        "fluid_build.cli.mcp.policy", fromlist=["check_tool_permission"]
    ).check_tool_permission
    check_tool_permission(name, arguments, policy=_policy())
    call = functools.partial(
        _call_tool,
        name,
        arguments,
        read_only=_policy().read_only,
        allow_inline_credentials=_policy().allow_inline_credentials,
        allow_metadata_service=_policy().allow_metadata_service,
    )
    try:
        if _TOOLS_OFF_LOOP:
            # As asyncio.to_thread does: the call keeps this request's context.
            context = contextvars.copy_context()
            result = await asyncio.get_running_loop().run_in_executor(
                _tool_executor(), functools.partial(context.run, call)
            )
        else:
            result = call()
    except McpToolRefusal as exc:
        # Surface the refusal (which names the server flag to set) as the
        # SDK's ToolError; SDK 2.x would mask a bare RuntimeError.
        from fluid_build._mcp_compat import get_tool_error

        raise get_tool_error()(str(exc)) from exc
    from fluid_build.cli.mcp import artifacts

    return artifacts.offload_if_large(result, tool=name, hint=_OFFLOAD_HINTS.get(name, ""))


#: Per-tool advice an offloaded result adds on getting a smaller one.
_OFFLOAD_HINTS = {
    SOURCE_CATALOG_TOOL: (
        "For less, name the tables you need in scope.tables, and leave "
        "response_format at concise, the default."
    ),
}


def _register_artifact_resource(app: Any) -> None:
    """Serve offloaded tool results (``fluid://artifacts/<id>``) through
    ``resources/read``; :mod:`fluid_build.cli.mcp.artifacts` keeps them."""
    from fluid_build.cli.mcp import artifacts

    @app.resource(
        artifacts.URI_TEMPLATE,
        name="tool_result",
        description="A tool result too large to send inline, as JSON.",
        mime_type="application/json",
    )
    def tool_result(artifact_id: str) -> str:
        try:
            return artifacts.read(artifact_id)
        except LookupError as exc:
            # As a ResourceError the message reaches an SDK 2.x client.
            from fluid_build._mcp_compat import get_resource_error

            raise get_resource_error()(str(exc)) from exc


# ----------------------------------------------------------------------
# Tool registrations (7 tools, one @_forge_tool() per capability in
# TOOL_CAPABILITIES). Each is a thin async wrapper that gates on policy and
# delegates the actual work to :func:`_call_tool`.
#
# Why explicit signatures with ``Annotated[T, Field(description=...)]``:
# FastMCP derives ``inputSchema`` for ``tools/list`` from the Python
# function signature via ``mcp.server.fastmcp.utilities.func_metadata``.
# Annotated metadata (including Pydantic ``Field`` descriptions,
# ``Literal`` enums, and ``BaseModel`` nested envelopes) flows through
# verbatim into the emitted JSON Schema — so MCP clients (Claude Code /
# Cursor / Kiro) see the curated descriptions + enum values for
# autocomplete.
#
# Pattern borrowed from ``mcp-server-fetch`` (the official reference
# Python MCP server uses ``BaseModel`` + ``Annotated`` for argument
# shapes) and the FastMCP docs (gofastmcp.com/servers/tools).
# The legacy ``TOOL_CAPABILITIES[*].input_schema`` registry remains the
# canonical permission-gate source and pins description-symmetry
# via ``tests/cli/test_mcp_advertised_schema.py``.
# ----------------------------------------------------------------------


@_forge_tool(description=TOOL_CAPABILITIES["read_logical_model"].description)
async def read_logical_model(
    path: Annotated[str, Field(description=_PATH_LOGICAL_DESCRIPTION)],
) -> Dict[str, Any]:
    """Read a logical model sidecar."""
    return await _dispatch_sync_tool("read_logical_model", {"path": path})


@_forge_tool(description=TOOL_CAPABILITIES["update_entity"].description)
async def update_entity(
    path: Annotated[str, Field(description=_PATH_LOGICAL_SHORT_DESCRIPTION)],
    entity: Annotated[str, Field(description="Conceptual entity id to update.")],
    updates: Annotated[
        Optional[Dict[str, Any]],
        Field(
            description='Field updates to apply (e.g. {"name": "Customer", "description": "..."}).'
        ),
    ] = None,
) -> Dict[str, Any]:
    """Rename or update a conceptual entity in the logical sidecar."""
    return await _dispatch_sync_tool(
        "update_entity", {"path": path, "entity": entity, "updates": updates}
    )


@_forge_tool(description=TOOL_CAPABILITIES["add_relationship"].description)
async def add_relationship(
    path: Annotated[str, Field(description=_PATH_LOGICAL_SHORT_DESCRIPTION)],
    relationship: Annotated[
        Dict[str, Any],
        Field(
            description="Conceptual relationship payload — must validate as ConceptualRelationship (name, source, target, cardinality)."
        ),
    ],
) -> Dict[str, Any]:
    """Append a conceptual relationship to the logical sidecar."""
    return await _dispatch_sync_tool(
        "add_relationship", {"path": path, "relationship": relationship}
    )


@_forge_tool(description=TOOL_CAPABILITIES["regenerate_physical"].description)
async def regenerate_physical(
    path: Annotated[str, Field(description="Path to the .model.json logical sidecar.")],
    contract_path: Annotated[
        Optional[str], Field(description=_REGEN_CONTRACT_OUT_DESCRIPTION)
    ] = None,
    engine: Annotated[Optional[str], Field(description=_REGEN_ENGINE_DESCRIPTION)] = None,
) -> Dict[str, Any]:
    """Regenerate a contract from a logical sidecar."""
    return await _dispatch_sync_tool(
        "regenerate_physical", {"path": path, "contract_path": contract_path, "engine": engine}
    )


@_forge_tool(description=TOOL_CAPABILITIES["validate_contract"].description)
async def validate_contract(
    logical_path: Annotated[
        Optional[str], Field(description=_VALIDATE_LOGICAL_PATH_DESCRIPTION)
    ] = None,
    contract_path: Annotated[
        Optional[str], Field(description=_VALIDATE_CONTRACT_PATH_DESCRIPTION)
    ] = None,
) -> Dict[str, Any]:
    """Validate a logical sidecar and/or contract."""
    return await _dispatch_sync_tool(
        "validate_contract", {"logical_path": logical_path, "contract_path": contract_path}
    )


@_forge_tool(description=TOOL_CAPABILITIES["diff_models"].description)
async def diff_models(
    old: Annotated[str, Field(description=_DIFF_OLD_DESCRIPTION)],
    new: Annotated[str, Field(description=_DIFF_NEW_DESCRIPTION)],
) -> Dict[str, Any]:
    """Diff two model sidecars."""
    return await _dispatch_sync_tool("diff_models", {"old": old, "new": new})


@_forge_tool(description=TOOL_CAPABILITIES[SOURCE_CATALOG_TOOL].description)
async def source_catalog(
    kind: Annotated[_SourceCatalogKindLiteral, Field(description=_SOURCE_KIND_DESCRIPTION)],
    source: Annotated[
        Optional[_CatalogSourceLiteral],
        Field(description=_ADAPTER_DISPATCH_DESCRIPTION),
    ] = None,
    credentials: Annotated[
        Optional[CredentialsArg], Field(description=_CREDENTIALS_DESCRIPTION)
    ] = None,
    scope: Annotated[Optional[ScopeArg], Field(description=_SCOPE_DESCRIPTION)] = None,
    fqn: Annotated[Optional[str], Field(description=_FQN_DESCRIPTION)] = None,
    allow_metadata_service: Annotated[
        bool, Field(description=_ALLOW_METADATA_SERVICE_DESCRIPTION)
    ] = False,
    response_format: Annotated[
        _ResponseFormatLiteral, Field(description=_RESPONSE_FORMAT_DESCRIPTION)
    ] = "concise",
) -> Dict[str, Any]:
    """Read source-catalog metadata; ``kind`` picks what."""
    return await _dispatch_sync_tool(
        SOURCE_CATALOG_TOOL,
        {
            "kind": kind,
            "source": source,
            "credentials": _dump_envelope(credentials) if credentials else None,
            "scope": _dump_envelope(scope) if scope else None,
            "fqn": fqn,
            "allow_metadata_service": allow_metadata_service,
            "response_format": response_format,
        },
    )


def _dump_envelope(env: Optional[BaseModel]) -> Dict[str, Any]:
    """Serialise an envelope back to a plain dict for ``_dispatch_sync_tool``.

    FastMCP validates incoming JSON against the Pydantic models and passes
    typed instances into the tool body. Permission gating + the legacy
    ``_call_tool`` dispatch still speak plain dicts, so we round-trip here.
    ``by_alias=True`` preserves the wire-shape (``schema`` not
    ``schema_name``) so the existing ``_scope_from_args`` flat-fallback
    keeps working. ``exclude_none=True`` leaves out what the client did not
    send: a scope dumped with ``"tables": None`` fails ``CatalogScope``
    (``tables`` is a list), so every listing with a scope that named no
    tables, the usual case, failed through the SDK.
    """
    if env is None:
        return {}
    return env.model_dump(mode="json", by_alias=True, exclude_none=True)
