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

"""
MCP FastMCP server — app instance and 14 async tool registrations.

Split from ``fluid_build.cli.mcp`` (issue #11) to reduce the 2 446-line
monolith into focused, testable modules.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Dict, List, Literal, Optional, Tuple

import yaml
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
    from mcp.types import SamplingMessage, TextContent

    FastMCP = Any
    Context = Any

from fluid_build._mcp_compat import attr as _mcp_attr
from fluid_build.cli.mcp.models import (
    _ADAPTER_DISPATCH_DESCRIPTION,
    _ALLOW_METADATA_SERVICE_DESCRIPTION,
    _CREDENTIAL_ID_DESCRIPTION,
    _CREDENTIALS_DESCRIPTION,
    _DIFF_NEW_DESCRIPTION,
    _DIFF_OLD_DESCRIPTION,
    _ENGINE_FORGE_DESCRIPTION,
    _FORGE_FROM_SOURCE_URI_DESCRIPTION,
    _FORGE_RUN_FROM_PRODUCTS_DESCRIPTION,
    _FORGE_RUN_MODE_DESCRIPTION,
    _FORGE_RUN_PRODUCT_TYPE_DESCRIPTION,
    _FORGE_RUN_PROMPT_DESCRIPTION,
    _FORGE_RUN_TARGET_DIR_DESCRIPTION,
    _LOGICAL_PATH_OUT_DESCRIPTION,
    _NAME_DESCRIPTION,
    _OUTPUT_PATH_DESCRIPTION,
    _PATH_LOGICAL_DESCRIPTION,
    _PATH_LOGICAL_SHORT_DESCRIPTION,
    _REGEN_CONTRACT_OUT_DESCRIPTION,
    _REGEN_ENGINE_DESCRIPTION,
    _SCOPE_CATALOG_DESCRIPTION,
    _SCOPE_DESCRIPTION,
    _SCOPE_TABLES_DESCRIPTION,
    _SCORE_CONTRACT_PATH_DESCRIPTION,
    _SCORE_INCLUDE_ARTIFACTS_DESCRIPTION,
    _SCORE_INLINE_DESCRIPTION,
    _SEMANTIC_LIMIT_DESCRIPTION,
    _SEMANTIC_MODE_DESCRIPTION,
    _SEMANTIC_QUERY_DESCRIPTION,
    _TECHNIQUE_DESCRIPTION,
    _VALIDATE_CONTRACT_PATH_DESCRIPTION,
    _VALIDATE_LOGICAL_PATH_DESCRIPTION,
    TOOL_CAPABILITIES,
    CredentialsArg,
    ScopeArg,
    _CatalogSourceLiteral,
    _ForgeSourceLiteral,
    _set_sampling_context,
    _TechniqueLiteral,
    get_sampling_context,
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
        # The ``forge_run`` tool annotates its session param ``ctx: Context``,
        # and under ``from __future__ import annotations`` that annotation is a
        # string evaluated against THIS module's globals. Bind the
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

    The slow tool — ``forge_run`` (in-process forge copilot loop, can
    take seconds; needs concurrency to service sampling round-trips) —
    keeps its own explicit ``asyncio.to_thread`` because it MUST run
    off the loop.
    """
    from fluid_build.cli.mcp.dispatch import _call_tool

    check_tool_permission = __import__(
        "fluid_build.cli.mcp.policy", fromlist=["check_tool_permission"]
    ).check_tool_permission
    check_tool_permission(name, arguments, policy=_policy())
    return _call_tool(
        name,
        arguments,
        read_only=_policy().read_only,
        allow_inline_credentials=_policy().allow_inline_credentials,
    )


# ----------------------------------------------------------------------
# Tool registrations (14 tools — one @_forge_tool() per capability in
# TOOL_CAPABILITIES). Each is a thin async wrapper that gates on policy and
# delegates the actual work to :func:`_call_tool` (sync, threaded) or, for
# ``forge_run``, talks to ``ctx.session.create_message`` directly.
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
# via ``tests/cli/test_mcp_judge_enrich_tools.py``.
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


@_forge_tool(description=TOOL_CAPABILITIES["search_semantic_memory"].description)
async def search_semantic_memory(
    query: Annotated[str, Field(description=_SEMANTIC_QUERY_DESCRIPTION)],
    mode: Annotated[
        Optional[Literal["exact", "keyword", "vector", "hybrid"]],
        Field(description=_SEMANTIC_MODE_DESCRIPTION),
    ] = None,
    limit: Annotated[Optional[int], Field(description=_SEMANTIC_LIMIT_DESCRIPTION)] = None,
) -> Dict[str, Any]:
    """Search the semantic memory namespace."""
    return await _dispatch_sync_tool(
        "search_semantic_memory", {"query": query, "mode": mode, "limit": limit}
    )


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


@_forge_tool(description=TOOL_CAPABILITIES["forge_from_source"].description)
async def forge_from_source(
    source: Annotated[
        _ForgeSourceLiteral,
        Field(description=_ADAPTER_DISPATCH_DESCRIPTION),
    ],
    output_path: Annotated[str, Field(description=_OUTPUT_PATH_DESCRIPTION)],
    credentials: Annotated[
        Optional[CredentialsArg], Field(description=_CREDENTIALS_DESCRIPTION)
    ] = None,
    scope: Annotated[Optional[ScopeArg], Field(description=_SCOPE_DESCRIPTION)] = None,
    uri: Annotated[Optional[str], Field(description=_FORGE_FROM_SOURCE_URI_DESCRIPTION)] = None,
    name: Annotated[Optional[str], Field(description=_NAME_DESCRIPTION)] = None,
    technique: Annotated[
        _TechniqueLiteral,
        Field(description=_TECHNIQUE_DESCRIPTION),
    ] = "data_vault_2",
    engine: Annotated[str, Field(description=_ENGINE_FORGE_DESCRIPTION)] = "dbt",
    logical_path: Annotated[Optional[str], Field(description=_LOGICAL_PATH_OUT_DESCRIPTION)] = None,
    allow_metadata_service: Annotated[
        bool, Field(description=_ALLOW_METADATA_SERVICE_DESCRIPTION)
    ] = False,
) -> Dict[str, Any]:
    """Forge a logical data-model + Fluid contract from a metadata-source catalog OR JDBC database."""
    args: Dict[str, Any] = {
        "source": source,
        "output_path": output_path,
        "technique": technique,
        "engine": engine,
        "allow_metadata_service": allow_metadata_service,
    }
    if credentials is not None:
        args["credentials"] = _dump_envelope(credentials)
    if scope is not None:
        args["scope"] = _dump_envelope(scope)
    if uri:
        args["uri"] = uri
    if name:
        args["name"] = name
    if logical_path:
        args["logical_path"] = logical_path
    return await _dispatch_sync_tool("forge_from_source", args)


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


@_forge_tool(description=TOOL_CAPABILITIES["forge_run"].description)
async def forge_run(
    mode: Annotated[Literal["blank", "diag", "ai"], Field(description=_FORGE_RUN_MODE_DESCRIPTION)],
    target_dir: Annotated[
        Optional[str], Field(description=_FORGE_RUN_TARGET_DIR_DESCRIPTION)
    ] = None,
    data_product_type: Annotated[
        Optional[Literal["SDP", "ADP", "CDP", "Bronze", "Silver", "Gold"]],
        Field(description=_FORGE_RUN_PRODUCT_TYPE_DESCRIPTION),
    ] = None,
    prompt: Annotated[Optional[str], Field(description=_FORGE_RUN_PROMPT_DESCRIPTION)] = None,
    from_products: Annotated[
        Optional[List[str]], Field(description=_FORGE_RUN_FROM_PRODUCTS_DESCRIPTION)
    ] = None,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Run fluid forge inside MCP with sampling-backed LLM.

    See ``TOOL_CAPABILITIES["forge_run"]`` for the mode semantics. Diag mode
    sends one ``sampling/createMessage`` round-trip via ``ctx.session.create_message``
    (the canonical SDK primitive); blank/ai modes install the sampling-context
    bridge so :class:`MCPSamplingProvider` can route LLM calls back to
    the IDE from inside ``forge.run()``.
    """
    # Pass the FULL argument set to the permission gate so the writable-paths
    # sandbox check actually runs on ``target_dir`` (the tool's only declared
    # ``file_path_args``). Passing a thin ``{"mode": mode}`` dict here would
    # cause :func:`check_tool_permission` to silently skip the check
    # (``arguments.get("target_dir")`` returns ``None`` → ``continue`` in the
    # gate loop), turning the documented sandbox guarantee into a write-anywhere
    # primitive. Tracked as security-review finding #1 in the Phase 3 audit.
    #
    # Resolve ``check_tool_permission`` / ``_policy`` / ``_run_forge_inproc``
    # through the package module object (``fluid_build.cli.mcp``) so test
    # monkeypatches on the package level (``monkeypatch.setattr(mcp_mod, ...)``)
    # flow through — the post-split equivalent of the monolith's single-module
    # global lookup. Pinned by tests/cli/test_mcp_forge_run_permission.py.
    import fluid_build.cli.mcp as _mcp_pkg

    check_tool_permission = _mcp_pkg.check_tool_permission
    _run_forge_inproc = _mcp_pkg._run_forge_inproc
    _policy_fn = _mcp_pkg._policy
    permission_args: Dict[str, Any] = {
        "mode": mode,
        "target_dir": target_dir,
        "data_product_type": data_product_type,
        "prompt": prompt,
        "from_products": from_products,
    }
    check_tool_permission("forge_run", permission_args, policy=_policy_fn())
    if _policy_fn().read_only:
        raise RuntimeError("Server is running in read-only mode")

    mode_norm = (mode or "blank").strip().lower()

    # Mode 'diag' — single sampling round-trip; proves the channel works.
    if mode_norm == "diag":
        if ctx is None:
            raise RuntimeError(
                "forge_run mode='diag' requires the MCP Context (the IDE must "
                "advertise the 'sampling' capability at initialize)."
            )
        # Pre-check the client advertised sampling capability so we fail
        # fast with an actionable message instead of hanging in
        # ``create_message`` waiting for a response the client will never
        # send. ``check_client_capability`` is the official SDK primitive.
        # Lazy SDK import — keeps ``mcp.types`` off the ``fluid --help`` path
        # (this tool only runs inside an active ``fluid mcp serve`` session).
        from mcp.types import ClientCapabilities, SamplingCapability

        if not ctx.session.check_client_capability(
            ClientCapabilities(sampling=SamplingCapability())
        ):
            raise RuntimeError(
                "Your MCP client did not advertise the 'sampling' capability "
                "at initialize, so forge_run mode='diag' / 'ai' cannot work. "
                "Use mode='blank' (deterministic scaffold, no LLM), or "
                "shell-run `fluid forge --agent --blank` with "
                "FLUID_LLM_BACKEND=litellm + an API key as fallback."
            )
        prompt_text = prompt or "Say 'hello from the IDE'."
        # Lazy SDK import — keeps ``mcp.types`` off the ``fluid --help`` path
        # (this tool only runs inside an active ``fluid mcp serve`` session).
        from mcp.types import SamplingMessage, TextContent

        try:
            result = await ctx.session.create_message(
                messages=[
                    SamplingMessage(
                        role="user",
                        content=TextContent(type="text", text=prompt_text),
                    )
                ],
                max_tokens=256,
                system_prompt="You are forge's diagnostic helper.",
                include_context="thisServer",
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "MCP sampling failed — this IDE may not support the "
                f"'sampling' capability. Underlying error: {exc}. Use "
                "mode='blank' or set FLUID_LLM_BACKEND=litellm with an API "
                "key as fallback."
            ) from exc

        # MCP SDK's CreateMessageResult exposes ``content`` as either a single
        # TextContent or a list of content blocks depending on client. Handle both.
        text = ""
        content = result.content
        if hasattr(content, "text"):
            text = content.text
        elif isinstance(content, list):
            text = "".join(b.text for b in content if hasattr(b, "text"))
        return {
            "mode": "diag",
            "prompt": prompt_text,
            "response_text": text,
            "model": getattr(result, "model", None),
            # Dual-name read: v1 exposes camelCase, v2 snake_case — a
            # single-name getattr silently returns None on the other one.
            "stop_reason": _mcp_attr(result, "stop_reason", "stopReason"),
        }

    # Modes 'blank' and 'ai' — run fluid forge in-process inside a worker
    # thread so the asyncio loop stays free to service sampling round-trips
    # (mode='ai' uses MCPSamplingProvider which calls back through the
    # sampling-context bridge). We capture the anyio event-loop token here
    # (via ``current_token()`` — the canonical anyio primitive) so the
    # worker thread can call back into the loop via ``anyio.from_thread.run``.
    if mode_norm in ("blank", "ai"):
        if not target_dir:
            raise RuntimeError("forge_run requires 'target_dir' for mode='blank'/'ai'")
        from anyio.lowlevel import current_token

        anyio_token = current_token()
        sampling_tokens = _set_sampling_context(ctx, anyio_token)
        try:
            return await asyncio.to_thread(
                _run_forge_inproc, mode_norm, target_dir, data_product_type, from_products
            )
        finally:
            from fluid_build.cli.mcp.models import _reset_sampling_context

            _reset_sampling_context(sampling_tokens)

    raise RuntimeError(f"unknown forge_run mode: {mode_norm!r}")


@_forge_tool(description=TOOL_CAPABILITIES["score_contract_quality"].description)
async def score_contract_quality(
    contract_path: Annotated[
        Optional[str], Field(description=_SCORE_CONTRACT_PATH_DESCRIPTION)
    ] = None,
    contract: Annotated[
        Optional[Dict[str, Any]], Field(description=_SCORE_INLINE_DESCRIPTION)
    ] = None,
    include_artifacts: Annotated[
        Optional[bool], Field(description=_SCORE_INCLUDE_ARTIFACTS_DESCRIPTION)
    ] = None,
) -> Dict[str, Any]:
    """Run the 6-axis LLM-as-judge over a contract. Read-only."""
    args: Dict[str, Any] = {"include_artifacts": include_artifacts}
    if contract_path:
        args["contract_path"] = contract_path
    if contract is not None:
        args["contract"] = contract
    return await _dispatch_sync_tool("score_contract_quality", args)


@_forge_tool(description=TOOL_CAPABILITIES["enrich_contract_suggestions"].description)
async def enrich_contract_suggestions(
    contract_path: Annotated[
        Optional[str], Field(description=_SCORE_CONTRACT_PATH_DESCRIPTION)
    ] = None,
    contract: Annotated[
        Optional[Dict[str, Any]], Field(description=_SCORE_INLINE_DESCRIPTION)
    ] = None,
) -> Dict[str, Any]:
    """Run the deterministic enrichment pass and return suggestions. Read-only."""
    args: Dict[str, Any] = {}
    if contract_path:
        args["contract_path"] = contract_path
    if contract is not None:
        args["contract"] = contract
    return await _dispatch_sync_tool("enrich_contract_suggestions", args)
