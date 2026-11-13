# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""
MCP FastMCP server — app instance and 11 async tool registrations.

Split from ``fluid_build.cli.mcp`` (issue #11) to reduce the 2 446-line
monolith into focused, testable modules.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any, Dict, List, Optional, Tuple

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
    _PATH_LOGICAL_DESCRIPTION,
    _PATH_LOGICAL_SHORT_DESCRIPTION,
    _REGEN_CONTRACT_OUT_DESCRIPTION,
    _REGEN_ENGINE_DESCRIPTION,
    _SCOPE_DESCRIPTION,
    _VALIDATE_CONTRACT_PATH_DESCRIPTION,
    _VALIDATE_LOGICAL_PATH_DESCRIPTION,
    TOOL_CAPABILITIES,
    CredentialsArg,
    ScopeArg,
    _CatalogSourceLiteral,
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

        app = ServerCls(name="forge-cli-mcp")
        for fn, tool_kwargs in _PENDING_TOOLS:
            app.tool(**tool_kwargs)(fn)
        _mcp_app = app
    return _mcp_app


async def _dispatch_sync_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Permission-gate + sync execution for a fast read/write tool.

    NB: deliberately calls ``_call_tool`` SYNCHRONOUSLY (no
    ``asyncio.to_thread``). The fast tools (file reads, validator runs,
    catalog enumerations) complete in single-digit milliseconds, so the
    event-loop block is negligible — and going through a worker thread
    causes a real race when a client pipelines requests (e.g. the
    ``tests/test_mcp_protocol_smoke.py`` smoke test sends 3 messages then
    closes stdin): the SDK can hit stdin-EOF and start shutting down
    before the worker thread's response makes it to stdout, producing
    a missing-response failure plus ``ValueError: I/O operation on
    closed file`` on the worker thread. Sync dispatch guarantees each
    response is fully flushed before the next request is read.
    """
    from fluid_build.cli.mcp.dispatch import McpToolRefusal, _call_tool

    check_tool_permission = __import__(
        "fluid_build.cli.mcp.policy", fromlist=["check_tool_permission"]
    ).check_tool_permission
    check_tool_permission(name, arguments, policy=_policy())
    try:
        return _call_tool(
            name,
            arguments,
            read_only=_policy().read_only,
            allow_inline_credentials=_policy().allow_inline_credentials,
            allow_metadata_service=_policy().allow_metadata_service,
        )
    except McpToolRefusal as exc:
        # Surface the refusal (which names the server flag to set) as the
        # SDK's ToolError; SDK 2.x would mask a bare RuntimeError.
        from fluid_build._mcp_compat import get_tool_error

        raise get_tool_error()(str(exc)) from exc


# ----------------------------------------------------------------------
# Tool registrations (11 tools — one @_forge_tool() per capability in
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


@_forge_tool(description=TOOL_CAPABILITIES["list_source_adapters"].description)
async def list_source_adapters() -> Dict[str, Any]:
    """List the metadata-source catalog adapters this server can dispatch."""
    return await _dispatch_sync_tool("list_source_adapters", {})


@_forge_tool(description=TOOL_CAPABILITIES["list_source_tables"].description)
async def list_source_tables(
    source: Annotated[
        _CatalogSourceLiteral,
        Field(description=_ADAPTER_DISPATCH_DESCRIPTION),
    ],
    credentials: Annotated[CredentialsArg, Field(description=_CREDENTIALS_DESCRIPTION)],
    scope: Annotated[ScopeArg, Field(description=_SCOPE_DESCRIPTION)],
    allow_metadata_service: Annotated[
        bool, Field(description=_ALLOW_METADATA_SERVICE_DESCRIPTION)
    ] = False,
) -> Dict[str, Any]:
    """Enumerate tables in a metadata-source catalog scope."""
    return await _dispatch_sync_tool(
        "list_source_tables",
        {
            "source": source,
            "credentials": _dump_envelope(credentials),
            "scope": _dump_envelope(scope),
            "allow_metadata_service": allow_metadata_service,
        },
    )


@_forge_tool(description=TOOL_CAPABILITIES["inspect_source_table"].description)
async def inspect_source_table(
    source: Annotated[
        _CatalogSourceLiteral,
        Field(description=_ADAPTER_DISPATCH_DESCRIPTION),
    ],
    credentials: Annotated[CredentialsArg, Field(description=_CREDENTIALS_DESCRIPTION)],
    fqn: Annotated[
        str,
        Field(
            description="Fully-qualified table name (DB.SCHEMA.TABLE for Snowflake, project.dataset.table for BigQuery, etc.)."
        ),
    ],
    allow_metadata_service: Annotated[
        Optional[bool], Field(description=_ALLOW_METADATA_SERVICE_DESCRIPTION)
    ] = None,
) -> Dict[str, Any]:
    """Return full metadata for one fully-qualified table in a metadata-source catalog."""
    return await _dispatch_sync_tool(
        "inspect_source_table",
        {
            "source": source,
            "credentials": _dump_envelope(credentials),
            "fqn": fqn,
            "allow_metadata_service": allow_metadata_service,
        },
    )


@_forge_tool(description=TOOL_CAPABILITIES["list_source_lineage"].description)
async def list_source_lineage(
    source: Annotated[
        _CatalogSourceLiteral,
        Field(description=_ADAPTER_DISPATCH_DESCRIPTION),
    ],
    credentials: Annotated[CredentialsArg, Field(description=_CREDENTIALS_DESCRIPTION)],
    fqn: Annotated[
        str,
        Field(
            description="Fully-qualified table name (DB.SCHEMA.TABLE for Snowflake, project.dataset.table for BigQuery, etc.)."
        ),
    ],
    allow_metadata_service: Annotated[
        Optional[bool], Field(description=_ALLOW_METADATA_SERVICE_DESCRIPTION)
    ] = None,
) -> Dict[str, Any]:
    """Return upstream + downstream lineage chains for one fully-qualified table."""
    return await _dispatch_sync_tool(
        "list_source_lineage",
        {
            "source": source,
            "credentials": _dump_envelope(credentials),
            "fqn": fqn,
            "allow_metadata_service": allow_metadata_service,
        },
    )


@_forge_tool(description=TOOL_CAPABILITIES["list_source_glossary"].description)
async def list_source_glossary(
    source: Annotated[
        _CatalogSourceLiteral,
        Field(description=_ADAPTER_DISPATCH_DESCRIPTION),
    ],
    credentials: Annotated[CredentialsArg, Field(description=_CREDENTIALS_DESCRIPTION)],
    scope: Annotated[Optional[ScopeArg], Field(description=_SCOPE_DESCRIPTION)] = None,
    allow_metadata_service: Annotated[
        Optional[bool], Field(description=_ALLOW_METADATA_SERVICE_DESCRIPTION)
    ] = None,
) -> Dict[str, Any]:
    """Return business-glossary terms relevant to a metadata-source catalog scope."""
    return await _dispatch_sync_tool(
        "list_source_glossary",
        {
            "source": source,
            "credentials": _dump_envelope(credentials),
            "scope": _dump_envelope(scope) if scope else None,
            "allow_metadata_service": allow_metadata_service,
        },
    )


def _dump_envelope(env: Optional[BaseModel]) -> Dict[str, Any]:
    """Serialise an envelope back to a plain dict for ``_dispatch_sync_tool``.

    FastMCP validates incoming JSON against the Pydantic models and passes
    typed instances into the tool body. Permission gating + the legacy
    ``_call_tool`` dispatch still speak plain dicts, so we round-trip here.
    ``by_alias=True`` preserves the wire-shape (``schema`` not
    ``schema_name``) so the existing ``_scope_from_args`` flat-fallback
    keeps working.
    """
    if env is None:
        return {}
    return env.model_dump(mode="json", by_alias=True, exclude_none=False)
