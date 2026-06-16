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

"""Minimal MCP stdio server for staged forge model operations.

The server exposes seven tools over MCP stdio and applies a four-layer
access-control model before every ``tools/call``:

1. **Tool allow/deny list** — only the tools explicitly permitted by
   the active :class:`McpPolicy` are callable. Denied tools are also
   hidden from ``tools/list`` so upstream agents don't advertise them.
2. **Read-only gate** — ``--read-only`` blocks any tool that mutates
   filesystem paths or writes store namespaces.
3. **Read sandbox** — ``--readable-paths`` pins filesystem roots that
   path-based read tools may inspect. Defaults to the current working
   directory.
4. **Write sandboxes** — ``--writable-paths`` pins the filesystem roots
   under which mutating tools may write; ``--writable-namespaces`` pins
   the conceptual store namespaces they may write. Defaults are safe:
   readable_paths=cwd, writable_paths=cwd,
   writable_namespaces={"history", "audit"}.

Defense in depth: ``_call_tool`` still honours the legacy
``read_only=True`` kwarg so direct callers (unit tests, Python scripts)
can bypass the MCP framing and still get the safety.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional, Tuple

import yaml
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import SamplingMessage, TextContent
from pydantic import BaseModel, ConfigDict, Field

from fluid_build.copilot.store.audit_trail import write_audit_event
from fluid_build.copilot.store.factory import resolve_store
from fluid_build.copilot.store.history import archive_snapshot
from fluid_build.forge_datamodel.emit.fluid_contract import build_contract_from_logical
from fluid_build.forge_datamodel.emit.validator import FluidContractValidator

COMMAND = "mcp"
MCP_PROTOCOL_VERSION = "2025-06-18"

# ---------------------------------------------------------------------------
# Sampling-context bridge — request-scoped via contextvars + anyio token
#
# When a tool that drives forge's copilot starts, it captures the active SDK
# ``Context`` and an `anyio` event-loop token (the canonical bridge between
# a worker thread and the SDK's anyio loop). The values are stored in
# :class:`contextvars.ContextVar` so they propagate automatically across the
# ``await asyncio.to_thread(...)`` boundary (Python ≥3.9 guarantees this).
# :class:`fluid_build.cli.forge_copilot_llm_providers.MCPSamplingProvider`
# (running in the worker thread) reads them via :func:`get_sampling_context`
# and dispatches ``ctx.session.create_message`` back into the SDK's loop via
# :func:`anyio.from_thread.run` — matching the SDK's own anyio-based dialect.
#
# Borrowed-not-built per /borrow-before-build:
#   contextvars — Python stdlib (https://docs.python.org/3/library/contextvars.html);
#   anyio.from_thread — https://anyio.readthedocs.io/en/stable/threads.html
# ---------------------------------------------------------------------------

_SAMPLING_CTX: ContextVar[Optional[Context]] = ContextVar("forge_mcp_sampling_ctx", default=None)
_SAMPLING_TOKEN: ContextVar[Optional[Any]] = ContextVar(
    "forge_mcp_sampling_anyio_token", default=None
)


def _set_sampling_context(ctx: Optional[Context], anyio_token: Optional[Any]) -> Tuple:
    """Set the active sampling context. Returns a token tuple for
    :func:`_reset_sampling_context` to undo (use in a ``try/finally``)."""
    ctx_token = _SAMPLING_CTX.set(ctx)
    token_token = _SAMPLING_TOKEN.set(anyio_token)
    return ctx_token, token_token


def _reset_sampling_context(tokens: Tuple) -> None:
    """Restore the previous sampling context. Symmetric to :func:`_set_sampling_context`."""
    ctx_token, token_token = tokens
    _SAMPLING_CTX.reset(ctx_token)
    _SAMPLING_TOKEN.reset(token_token)


def get_sampling_context() -> Tuple[Optional[Context], Optional[Any]]:
    """Return ``(ctx, anyio_token)`` if a tool with sampling-capable Context
    is active. Read by
    :class:`fluid_build.cli.forge_copilot_llm_providers.MCPSamplingProvider`
    to route LLM calls back through ``ctx.session.create_message`` to the IDE.
    """
    return _SAMPLING_CTX.get(), _SAMPLING_TOKEN.get()


# ----------------------------------------------------------------------
# Tool capability model + policy
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCapability:
    """Declarative permission metadata for one MCP tool.

    Attributes
    ----------
    name:
        Tool identifier as advertised in ``tools/list``.
    description:
        Short human-readable description.
    mutates_files:
        True if the tool writes at least one filesystem path named by
        ``file_path_args``.
    file_path_args:
        Argument keys whose values are filesystem paths the tool may
        write. Each must resolve under some ``McpPolicy.writable_paths``
        entry before the tool is permitted to run.
    read_path_args:
        Argument keys whose values are filesystem paths the tool may
        read. Each must resolve under some ``McpPolicy.readable_paths``
        entry before the tool is permitted to run.
    writes_namespaces:
        Conceptual store namespaces (e.g. ``"history"``, ``"audit"``,
        ``"memory/semantic"``) the tool writes. Each must be present in
        ``McpPolicy.writable_namespaces``.
    reads_namespaces:
        Namespaces the tool reads from. Informational only today — no
        read allowlist is enforced (reading is the low-risk side).
    """

    name: str
    description: str
    mutates_files: bool = False
    file_path_args: Tuple[str, ...] = ()
    read_path_args: Tuple[str, ...] = ()
    writes_namespaces: Tuple[str, ...] = ()
    reads_namespaces: Tuple[str, ...] = ()
    input_schema: Optional[Dict[str, Any]] = None
    """Optional JSON Schema describing the tool's ``arguments`` object.

    When present, advertised under ``inputSchema`` in ``tools/list`` so
    MCP clients (Claude Code, Cursor) can offer typed autocomplete on
    the tool's arguments. ``None`` falls back to the legacy free-form
    object — accepted but unhelpful for editor UX.
    """


# ---------------------------------------------------------------------
# JSON Schema fragments for tool ``inputSchema`` fields.
#
# Reused across multiple tool definitions below. Schemas are
# deliberately minimal but complete enough to drive Claude Code /
# Cursor / VS Code MCP client autocomplete:
#
# * Each tool's required vs. optional fields are pinned.
# * Each user-facing argument has a ``description`` an LLM agent can
#   read to know what to put there.
# * Enum values are listed where the CLI dispatch only accepts a
#   closed set (e.g. supported source catalogs, technique).
# * ``additionalProperties: false`` keeps a stray typo from silently
#   passing through.
# ---------------------------------------------------------------------

# Source lists derive from the shared registry
# (``copilot.catalog.source_registry``) — the single source of truth shared
# with the CLI — so built-in AND plugin (``fluid_build.source_adapters``)
# sources flow into every MCP surface at once: the curated
# ``TOOL_CAPABILITIES`` schemas, the dynamic tool-signature enums, and
# ``list_source_adapters``. Plugins present at import time are included.
# Discovery is cheap: plugin classes load lazily at dispatch, not here.
from fluid_build.copilot.catalog import source_registry as _source_registry

_source_registry.discover_source_adapters()
_CATALOG_SOURCE_LIST = tuple(_source_registry.list_catalog_sources())
# JDBC-introspectable databases. The catalog tools (list_source_tables /
# inspect_source_table / list_source_lineage / list_source_glossary) do NOT
# accept these — JDBC is a one-shot synthesis path only. ``forge_from_source``
# is the only tool that dispatches to JDBC (via ``_run_from_jdbc_source``).
_JDBC_SOURCE_LIST = tuple(_source_registry.list_jdbc_sources())
_SOURCE_ENUM = list(_CATALOG_SOURCE_LIST)
_FORGE_FROM_SOURCE_ENUM = sorted(set(_CATALOG_SOURCE_LIST) | set(_JDBC_SOURCE_LIST))

# Dynamic Literal aliases for the tool signatures: a registry-driven enum
# preserves LLM autocomplete (the JSON-Schema ``enum`` still ships) while
# accepting plugin source names. ``Literal[tuple(...)]`` is evaluated at
# import; pydantic emits the enum and validates membership (verified).
_CatalogSourceLiteral = Literal[tuple(_SOURCE_ENUM)]  # type: ignore[valid-type]
_ForgeSourceLiteral = Literal[tuple(_FORGE_FROM_SOURCE_ENUM)]  # type: ignore[valid-type]
# Modeling techniques offered over MCP come from the pluggable registry
# (issue #248), EXCLUDING ``custom`` — the bring-your-own-model technique needs
# a ``--logical-model`` file path the MCP wire can't supply. Built-in
# data_vault_2 / dimensional / flat + any plugin techniques flow through.
from fluid_build.copilot import modeling_techniques as _modeling_techniques

_modeling_techniques.discover_modeling_techniques()
_TECHNIQUE_ENUM = [
    name
    for name in _modeling_techniques.list_modeling_techniques()
    if not ((_t := _modeling_techniques.get_modeling_technique(name)) and _t.requires_logical_model)
]
_TechniqueLiteral = Literal[tuple(_TECHNIQUE_ENUM)]  # type: ignore[valid-type]

_CREDENTIALS_DESCRIPTION = (
    "Credential lookup envelope. Pass ONLY the credential_id; "
    "the server never accepts raw secrets over the MCP wire. "
    "credential_id maps to a row in ~/.fluid/sources.yaml that "
    "was set up via `fluid ai setup --source <catalog> --name <credential-id>`."
)
_CREDENTIAL_ID_DESCRIPTION = (
    "Saved credential name from ~/.fluid/sources.yaml — same value "
    "you pass to `fluid forge data-model from-source --credential-id`."
)

_CREDENTIALS_PROP = {
    "type": "object",
    "description": _CREDENTIALS_DESCRIPTION,
    "properties": {
        "credential_id": {
            "type": "string",
            "description": _CREDENTIAL_ID_DESCRIPTION,
        },
    },
    "required": ["credential_id"],
    "additionalProperties": True,  # operators can pass adapter-specific keys
}

_SCOPE_DESCRIPTION = (
    "Catalog scope — the database/schema/catalog identifier the "
    "adapter should read from. Field names are catalog-specific: "
    "Snowflake / BigQuery / DataHub use database+schema; "
    "Unity uses catalog+schema; Glue uses database; "
    "DMM uses domain (passed via the database field)."
)
_SCOPE_SCHEMA_ALIAS_DESCRIPTION = "Alias for 'schema' on adapters that prefer it."
_SCOPE_CATALOG_DESCRIPTION = (
    "Top-level catalog name (Unity / Dataplex entry-group / DataHub platform)."
)
_SCOPE_TABLES_DESCRIPTION = (
    "Optional explicit table-name list; if omitted, every table in scope is enumerated."
)

_SCOPE_PROP = {
    "type": "object",
    "description": _SCOPE_DESCRIPTION,
    "properties": {
        "database": {"type": "string"},
        "schema": {"type": "string"},
        "schema_name": {
            "type": "string",
            "description": _SCOPE_SCHEMA_ALIAS_DESCRIPTION,
        },
        "catalog": {
            "type": "string",
            "description": _SCOPE_CATALOG_DESCRIPTION,
        },
        "tables": {
            "type": "array",
            "items": {"type": "string"},
            "description": _SCOPE_TABLES_DESCRIPTION,
        },
    },
    "additionalProperties": False,
}

_FQN_DESCRIPTION = (
    "Fully-qualified table name in the catalog's native form. "
    "Snowflake: DB.SCHEMA.TABLE. Unity: catalog.schema.table. "
    "BigQuery: project.dataset.table. Glue: database.table. "
    "DataHub: urn:li:dataset:(...) or shortform snowflake.db.table."
)
_FQN_REQ = {
    "type": "string",
    "description": _FQN_DESCRIPTION,
}


# ---------------------------------------------------------------------
# Pydantic envelope models — single source of truth for tool argument
# schemas advertised to MCP clients.
#
# Why these exist: FastMCP derives ``inputSchema`` for ``tools/list``
# from the Python function signature via
# ``mcp.server.fastmcp.utilities.func_metadata``. Plain ``dict`` /
# bare ``str`` parameter types produce ``{type: "string"}`` / a bare
# ``{type: "object"}`` — descriptions + enums never reach the client,
# breaking Claude Code / Cursor / IDE autocomplete.
#
# Migrating to ``Annotated[T, Field(description=...)]`` + per-envelope
# Pydantic ``BaseModel`` keeps the curated descriptions and enums in
# the wire schema. Pattern borrowed from the official
# ``mcp-server-fetch`` reference (Anthropic's official Python MCP
# server uses ``BaseModel`` + ``Annotated`` for tool argument shapes:
# https://github.com/modelcontextprotocol/servers/tree/main/src/fetch)
# and the FastMCP docs:
# https://gofastmcp.com/servers/tools#parameter-documentation
#
# The legacy ``TOOL_CAPABILITIES[*].input_schema`` registry below
# remains as the canonical description-source-of-truth — every
# description string here is also exposed as a module-level constant
# reused by the legacy registry, so the two surfaces never drift.
# ``tests/cli/test_mcp_judge_enrich_tools.py`` pins both.
# ---------------------------------------------------------------------


class CredentialsArg(BaseModel):
    """Credential lookup envelope for source-catalog tools.

    The MCP server NEVER accepts raw secrets over the LLM-facing wire —
    only a ``credential_id`` pointing at a saved entry in the OS
    keyring + ``~/.fluid/sources.yaml``. A trusted in-process CLI
    harness can opt-in via ``fluid mcp serve --allow-inline-credentials``.
    """

    # ``extra = "allow"`` because operators can pass adapter-specific keys
    # (e.g. ``inline`` when --allow-inline-credentials is on). This mirrors
    # ``_CREDENTIALS_PROP["additionalProperties"] = True`` in the legacy
    # registry.
    model_config = ConfigDict(extra="allow")

    credential_id: Optional[str] = Field(
        default=None,
        description=_CREDENTIAL_ID_DESCRIPTION,
    )


class ScopeArg(BaseModel):
    """Catalog scope envelope — database / schema / catalog identifier."""

    # Curated registry pins ``additionalProperties: false``; ``extra='forbid'``
    # on the BaseModel produces the same in the emitted JSON Schema.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    database: Optional[str] = Field(default=None)
    # ``schema`` is a Pydantic-reserved attribute, so we expose it via the
    # alias and use ``schema_name`` internally. Pydantic emits the property
    # under the alias when ``by_alias=True`` is set (FastMCP does — see
    # ``Tool.from_function`` -> ``model_json_schema(by_alias=True)``).
    schema_name: Optional[str] = Field(
        default=None,
        alias="schema",
    )
    catalog: Optional[str] = Field(
        default=None,
        description=_SCOPE_CATALOG_DESCRIPTION,
    )
    tables: Optional[List[str]] = Field(
        default=None,
        description=_SCOPE_TABLES_DESCRIPTION,
    )


# ---------------------------------------------------------------------
# Per-tool argument descriptions — module-level constants so the legacy
# ``TOOL_CAPABILITIES[*].input_schema`` registry and the FastMCP-derived
# ``Annotated[T, Field(description=...)]`` signatures stay symmetric.
# ``test_mcp_judge_enrich_tools.py::test_curated_registry_matches_signatures``
# pins them.
# ---------------------------------------------------------------------

_PATH_LOGICAL_DESCRIPTION = "Path to the .model.json logical sidecar file."
_PATH_LOGICAL_SHORT_DESCRIPTION = "Path to the .model.json sidecar."
_ENTITY_DESCRIPTION = "Conceptual entity id to update."
_UPDATES_DESCRIPTION = 'Field updates to apply (e.g. {"name": "Customer", "description": "..."}).'
_RELATIONSHIP_DESCRIPTION = (
    "Conceptual relationship payload — must validate as "
    "ConceptualRelationship (name, source, target, cardinality)."
)
_REGEN_CONTRACT_OUT_DESCRIPTION = (
    "Output path for the regenerated Fluid contract. Defaults to " "<path>.fluid.yaml when omitted."
)
_REGEN_ENGINE_DESCRIPTION = "Build engine for the emitted contract (default: dbt)."
_VALIDATE_LOGICAL_PATH_DESCRIPTION = "Optional path to a .model.json sidecar to validate."
_VALIDATE_CONTRACT_PATH_DESCRIPTION = "Optional path to a Fluid contract to validate."
_DIFF_OLD_DESCRIPTION = "Path to the older .model.json sidecar."
_DIFF_NEW_DESCRIPTION = "Path to the newer .model.json sidecar."
_SEMANTIC_QUERY_DESCRIPTION = "Free-text query to search past forged models against."
_SEMANTIC_MODE_DESCRIPTION = "Retrieval mode. 'hybrid' is best when the VectorBackend is enabled."
_SEMANTIC_LIMIT_DESCRIPTION = "Maximum number of records to return."
_ADAPTER_DISPATCH_DESCRIPTION = "Catalog adapter to dispatch against."
_ALLOW_METADATA_SERVICE_DESCRIPTION = (
    "Allow the credential resolver to fall back to cloud-metadata-service "
    "auth (instance profile, workload identity). Off by default."
)
_TECHNIQUE_DESCRIPTION = "Modeling technique: Data Vault 2.0 or Dimensional (Kimball)."
_NAME_DESCRIPTION = "Logical-model name. Defaults to the schema name."
_ENGINE_FORGE_DESCRIPTION = "Build engine for the emitted Fluid contract."
_OUTPUT_PATH_DESCRIPTION = (
    "Destination path for the Fluid contract. Must resolve "
    "under one of the server's --writable-paths roots."
)
_LOGICAL_PATH_OUT_DESCRIPTION = (
    "Optional explicit path for the .model.json sidecar; " "defaults to <output_path>.model.json."
)
_FORGE_FROM_SOURCE_URI_DESCRIPTION = (
    "JDBC URI for postgres/postgresql/mysql/sqlite sources. "
    "Carries credentials inline (no credential_id needed). "
    "Example: postgresql://user:pass@host:5432/db. "
    "Ignored for catalog sources (snowflake/unity/bigquery/dataplex/glue/datahub/datamesh_manager)."
)
_FORGE_RUN_MODE_DESCRIPTION = "Forge run mode (see tool description)."
_FORGE_RUN_TARGET_DIR_DESCRIPTION = (
    "Workspace-relative directory for the produced "
    "contract.fluid.yaml. Must lie under one of the "
    "server's --writable-paths roots."
)
_FORGE_RUN_PRODUCT_TYPE_DESCRIPTION = "Data Mesh product type or medallion layer."
_FORGE_RUN_PROMPT_DESCRIPTION = (
    "For mode='diag': the prompt to send to the IDE's LLM. " "Ignored in other modes."
)
_FORGE_RUN_FROM_PRODUCTS_DESCRIPTION = (
    "For mode='ai': upstream product ids or paths to "
    "compose this product from. Repeatable. Ignored "
    "in other modes."
)
_SCORE_CONTRACT_PATH_DESCRIPTION = "Path to a contract.fluid.yaml file."
_SCORE_INLINE_DESCRIPTION = "Inline contract dict (alternative to contract_path)."
_SCORE_INCLUDE_ARTIFACTS_DESCRIPTION = (
    "If true, run enrichment first and feed artifacts to the judge."
)


TOOL_CAPABILITIES: Dict[str, ToolCapability] = {
    "read_logical_model": ToolCapability(
        name="read_logical_model",
        description="Read a logical model sidecar",
        read_path_args=("path",),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the .model.json logical sidecar file.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    "update_entity": ToolCapability(
        name="update_entity",
        description="Rename or update a conceptual entity in the logical sidecar",
        mutates_files=True,
        file_path_args=("path",),
        writes_namespaces=("history", "audit"),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the .model.json sidecar."},
                "entity": {"type": "string", "description": "Conceptual entity id to update."},
                "updates": {
                    "type": "object",
                    "description": 'Field updates to apply (e.g. {"name": "Customer", "description": "..."}).',
                    "additionalProperties": True,
                },
            },
            "required": ["path", "entity"],
            "additionalProperties": False,
        },
    ),
    "add_relationship": ToolCapability(
        name="add_relationship",
        description="Append a conceptual relationship to the logical sidecar",
        mutates_files=True,
        file_path_args=("path",),
        writes_namespaces=("history", "audit"),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the .model.json sidecar."},
                "relationship": {
                    "type": "object",
                    "description": (
                        "Conceptual relationship payload — must validate as "
                        "ConceptualRelationship (name, source, target, cardinality)."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["path", "relationship"],
            "additionalProperties": False,
        },
    ),
    "regenerate_physical": ToolCapability(
        name="regenerate_physical",
        description="Regenerate a contract from a logical sidecar",
        mutates_files=True,
        file_path_args=("path", "contract_path"),
        writes_namespaces=("history", "audit"),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the .model.json logical sidecar.",
                },
                "contract_path": {
                    "type": "string",
                    "description": (
                        "Output path for the regenerated Fluid contract. Defaults to "
                        "<path>.fluid.yaml when omitted."
                    ),
                },
                "engine": {
                    "type": "string",
                    "description": "Build engine for the emitted contract (default: dbt).",
                    "default": "dbt",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    "validate_contract": ToolCapability(
        name="validate_contract",
        description="Validate a logical sidecar and/or contract",
        read_path_args=("logical_path", "contract_path"),
        input_schema={
            "type": "object",
            "properties": {
                "logical_path": {
                    "type": "string",
                    "description": "Optional path to a .model.json sidecar to validate.",
                },
                "contract_path": {
                    "type": "string",
                    "description": "Optional path to a Fluid contract to validate.",
                },
            },
            "additionalProperties": False,
        },
    ),
    "diff_models": ToolCapability(
        name="diff_models",
        description="Diff two model sidecars",
        read_path_args=("old", "new"),
        input_schema={
            "type": "object",
            "properties": {
                "old": {"type": "string", "description": "Path to the older .model.json sidecar."},
                "new": {"type": "string", "description": "Path to the newer .model.json sidecar."},
            },
            "required": ["old", "new"],
            "additionalProperties": False,
        },
    ),
    "search_semantic_memory": ToolCapability(
        name="search_semantic_memory",
        description="Search the semantic memory namespace",
        reads_namespaces=("memory/semantic",),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text query to search past forged models against.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["exact", "keyword", "vector", "hybrid"],
                    "description": "Retrieval mode. 'hybrid' is best when the VectorBackend is enabled.",
                    "default": "hybrid",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 5,
                    "description": "Maximum number of records to return.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    # ---------------------------------------------------------------
    # V1.5 — metadata source-catalog tools.
    #
    # User-facing vocabulary uses "source" / "metadata source" to
    # disambiguate from forge-cli's existing publish-target catalog
    # role (``providers/catalogs/`` writes to DMM / Splunk / etc.).
    # Every tool here is READ-ONLY against external metadata APIs
    # (Snowflake INFORMATION_SCHEMA, Unity tables.get, …) — no data
    # values are ever fetched. ``forge_from_source`` is the only
    # one that mutates files (writes a Fluid contract / sidecar),
    # so it carries ``mutates_files=True``.
    #
    # MCP-specific defense: every tool's ``arguments`` MUST include
    # ``credentials.credential_id`` — never the credential value.
    # See ``copilot/catalog/credentials.py`` for the resolver chain.
    # ---------------------------------------------------------------
    "list_source_adapters": ToolCapability(
        name="list_source_adapters",
        description=(
            "List the metadata-source catalog adapters this server can dispatch "
            "(snowflake, unity, bigquery, dataplex, glue, datahub, datamesh_manager). "
            "Read-only — does not contact any external catalog."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    "list_source_tables": ToolCapability(
        name="list_source_tables",
        description=(
            "Enumerate tables in a metadata-source catalog scope "
            "(database/schema/catalog as the catalog defines it). "
            "Returns lightweight CatalogTable summaries; use inspect_source_table "
            "for full per-table metadata. Requires credentials.credential_id."
        ),
        reads_namespaces=("audit",),
        input_schema={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": _SOURCE_ENUM,
                    "description": "Catalog adapter to dispatch against.",
                },
                "credentials": _CREDENTIALS_PROP,
                "scope": _SCOPE_PROP,
                "allow_metadata_service": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Allow the credential resolver to fall back to "
                        "cloud-metadata-service auth (instance profile, "
                        "workload identity). Off by default."
                    ),
                },
            },
            "required": ["source", "credentials"],
            "additionalProperties": False,
        },
    ),
    "inspect_source_table": ToolCapability(
        name="inspect_source_table",
        description=(
            "Return full metadata for one fully-qualified table in a "
            "metadata-source catalog (columns, descriptions, owner, tags, "
            "primary key, foreign keys). Requires credentials.credential_id."
        ),
        reads_namespaces=("audit",),
        input_schema={
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": _SOURCE_ENUM},
                "credentials": _CREDENTIALS_PROP,
                "fqn": _FQN_REQ,
                "allow_metadata_service": {"type": "boolean", "default": False},
            },
            "required": ["source", "credentials", "fqn"],
            "additionalProperties": False,
        },
    ),
    "list_source_lineage": ToolCapability(
        name="list_source_lineage",
        description=(
            "Return upstream + downstream lineage chains for one fully-qualified "
            "table in a metadata-source catalog. Returns empty lists when the "
            "catalog has no lineage data. Requires credentials.credential_id."
        ),
        reads_namespaces=("audit",),
        input_schema={
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": _SOURCE_ENUM},
                "credentials": _CREDENTIALS_PROP,
                "fqn": _FQN_REQ,
                "allow_metadata_service": {"type": "boolean", "default": False},
            },
            "required": ["source", "credentials", "fqn"],
            "additionalProperties": False,
        },
    ),
    "list_source_glossary": ToolCapability(
        name="list_source_glossary",
        description=(
            "Return business-glossary terms relevant to a metadata-source "
            "catalog scope. Requires credentials.credential_id."
        ),
        reads_namespaces=("audit",),
        input_schema={
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": _SOURCE_ENUM},
                "credentials": _CREDENTIALS_PROP,
                "scope": _SCOPE_PROP,
                "allow_metadata_service": {"type": "boolean", "default": False},
            },
            "required": ["source", "credentials"],
            "additionalProperties": False,
        },
    ),
    "forge_from_source": ToolCapability(
        name="forge_from_source",
        description=(
            "Forge a logical data-model + Fluid contract from a metadata-source "
            "catalog scope OR a JDBC-introspectable database. Catalog sources "
            "(snowflake/unity/bigquery/dataplex/glue/datahub/datamesh_manager) "
            "read tables / lineage / glossary, run the staged pipeline (Logical "
            "→ Builder → Readme → Transformation → Validator), and write the "
            "contract + .model.json sidecar — they require "
            "credentials.credential_id and a scope. JDBC sources "
            "(postgres/postgresql/mysql/sqlite) use duckdb-extension "
            "introspection — they require ``uri`` (URI carries credentials "
            "inline). Both branches write to output_path which must resolve "
            "under --writable-paths."
        ),
        mutates_files=True,
        file_path_args=("output_path", "logical_path"),
        writes_namespaces=("history", "audit"),
        input_schema={
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": _FORGE_FROM_SOURCE_ENUM},
                "credentials": _CREDENTIALS_PROP,
                "scope": _SCOPE_PROP,
                "uri": {
                    "type": "string",
                    "description": _FORGE_FROM_SOURCE_URI_DESCRIPTION,
                },
                "technique": {
                    "type": "string",
                    "enum": _TECHNIQUE_ENUM,
                    "default": "data_vault_2",
                    "description": _TECHNIQUE_DESCRIPTION,
                },
                "name": {
                    "type": "string",
                    "description": _NAME_DESCRIPTION,
                },
                "engine": {
                    "type": "string",
                    "default": "dbt",
                    "description": _ENGINE_FORGE_DESCRIPTION,
                },
                "output_path": {
                    "type": "string",
                    "description": _OUTPUT_PATH_DESCRIPTION,
                },
                "logical_path": {
                    "type": "string",
                    "description": _LOGICAL_PATH_OUT_DESCRIPTION,
                },
                "allow_metadata_service": {
                    "type": "boolean",
                    "default": False,
                    "description": _ALLOW_METADATA_SERVICE_DESCRIPTION,
                },
            },
            # Catalog sources need credentials + scope; JDBC sources need
            # ``uri``. The Python dispatcher (``_call_tool``) does the
            # source-specific required-field check post-decode. Listing only
            # ``source`` + ``output_path`` here keeps both shapes valid at
            # the JSON Schema layer while preserving useful client autocomplete.
            "required": ["source", "output_path"],
            "additionalProperties": False,
        },
    ),
    "forge_run": ToolCapability(
        name="forge_run",
        description=(
            "Run `fluid forge` inside the MCP subprocess so LLM calls route "
            "back through the IDE via `sampling/createMessage`. The IDE pays "
            "for the LLM — no API key on the user's machine.\n\n"
            "Modes:\n"
            "  - 'blank': deterministic scaffold, no LLM (always works).\n"
            "  - 'diag': single sampling round-trip with the given prompt "
            "(diagnostic; proves the IDE's LLM is reachable).\n"
            "  - 'ai': full forge copilot loop with sampling-backed LLM "
            "(requires client to advertise the 'sampling' capability)."
        ),
        mutates_files=True,
        file_path_args=("target_dir",),
        writes_namespaces=("history", "audit"),
        input_schema={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["blank", "diag", "ai"],
                    "default": "blank",
                    "description": "Forge run mode (see tool description).",
                },
                "target_dir": {
                    "type": "string",
                    "description": (
                        "Workspace-relative directory for the produced "
                        "contract.fluid.yaml. Must lie under one of the "
                        "server's --writable-paths roots."
                    ),
                },
                "data_product_type": {
                    "type": "string",
                    "enum": ["SDP", "ADP", "CDP", "Bronze", "Silver", "Gold"],
                    "description": "Data Mesh product type or medallion layer.",
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "For mode='diag': the prompt to send to the IDE's LLM. "
                        "Ignored in other modes."
                    ),
                },
                "from_products": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "For mode='ai': upstream product ids or paths to "
                        "compose this product from. Repeatable. Ignored "
                        "in other modes."
                    ),
                },
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
    ),
    "score_contract_quality": ToolCapability(
        name="score_contract_quality",
        description=(
            "Run the out-of-loop LLM-as-judge on a finalised data-product "
            "contract and return a 6-axis scorecard (correctness, "
            "completeness, security, governance, performance, "
            "documentation; each 0..5; total 0..30). Read-only — does NOT "
            "modify the contract on disk. Pass either ``contract_path`` "
            "or ``contract`` (inline dict). Set ``include_artifacts=true`` "
            "to also run the deterministic enrichment pass first so the "
            "judge sees recommended dbt tests / freshness / clustering "
            "and credits those axes accordingly."
        ),
        read_path_args=("contract_path",),
        input_schema={
            "type": "object",
            "properties": {
                "contract_path": {
                    "type": "string",
                    "description": "Path to a contract.fluid.yaml file.",
                },
                "contract": {
                    "type": "object",
                    "description": "Inline contract dict (alternative to contract_path).",
                    "additionalProperties": True,
                },
                "include_artifacts": {
                    "type": "boolean",
                    "description": "If true, run enrichment first and feed artifacts to the judge.",
                    "default": False,
                },
            },
            "additionalProperties": False,
        },
    ),
    "enrich_contract_suggestions": ToolCapability(
        name="enrich_contract_suggestions",
        description=(
            "Run the post-synthesis deterministic enrichment pass over a "
            "contract and return suggested additions (dbt tests, freshness "
            "block, physical layout). Read-only — does NOT modify the "
            "contract on disk. Equivalent to what ``fluid forge`` runs "
            "automatically after synthesis. Useful for command_center "
            "previews + 'what would enrichment add?' answers."
        ),
        read_path_args=("contract_path",),
        input_schema={
            "type": "object",
            "properties": {
                "contract_path": {
                    "type": "string",
                    "description": "Path to a contract.fluid.yaml file.",
                },
                "contract": {
                    "type": "object",
                    "description": "Inline contract dict (alternative to contract_path).",
                    "additionalProperties": True,
                },
            },
            "additionalProperties": False,
        },
    ),
}


DEFAULT_WRITABLE_NAMESPACES: Tuple[str, ...] = ("history", "audit")
"""Namespaces implicitly granted write access when the operator hasn't
specified ``--writable-namespaces`` explicitly. ``history`` powers the
per-artifact snapshot bucket; ``audit`` powers the forensic trail.
Both are needed for *any* mutating tool to behave responsibly."""


# ---------------------------------------------------------------------
# Public helpers preserved for direct callers / introspection.
#
# Before the Phase 3 FastMCP migration these were used by ``_serve_stdio``
# to advertise tools and to filter the ``tools/list`` advertisement
# against the active policy. The SDK now owns the transport so the
# functions are no longer called internally, but tests + downstream
# tooling still import them for unit-level coverage of the capability
# registry and the visibility filter. Keep the surface stable.
# ---------------------------------------------------------------------


def _tool_definitions() -> List[Dict[str, Any]]:
    """Derive the tool-advertisement list from :data:`TOOL_CAPABILITIES`.

    Returns one ``{"name", "description", "inputSchema"}`` dict per tool.
    Tools with no declared ``input_schema`` fall back to a permissive
    empty-object schema (accepted by the MCP spec, but unhelpful for
    editor autocomplete — every shipped tool here has an explicit schema).
    """
    out: List[Dict[str, Any]] = []
    for cap in TOOL_CAPABILITIES.values():
        entry: Dict[str, Any] = {"name": cap.name, "description": cap.description}
        entry["inputSchema"] = (
            cap.input_schema if cap.input_schema is not None else {"type": "object"}
        )
        out.append(entry)
    return out


def _filter_visible_tools(tools: List[Dict[str, Any]], policy: "McpPolicy") -> List[Dict[str, Any]]:
    """Drop tools the active policy hides — denied tools and mutating
    tools under ``--read-only`` are removed so clients don't advertise
    options doomed to fail.

    Pre-FastMCP this filtered the ``tools/list`` payload before
    serialisation; the SDK now prunes via ``FastMCP.remove_tool`` at
    server-build time, but the function stays callable for unit tests
    and any in-tree tool that wants to compute the visible set without
    spinning up a server.
    """
    visible: List[Dict[str, Any]] = []
    for tool in tools:
        name = tool.get("name")
        if not isinstance(name, str):
            continue
        cap = TOOL_CAPABILITIES.get(name)
        if cap is None or not policy.is_tool_allowed(name):
            continue
        needs_write = cap.mutates_files or bool(cap.writes_namespaces)
        if policy.read_only and needs_write:
            continue
        visible.append(tool)
    return visible


@dataclass(frozen=True)
class McpPolicy:
    """Access-control policy for an MCP stdio server process.

    ``allowed_tools = None`` means "all tools allowed"; an empty tuple
    means "no tools allowed" (useful for a pure-inspection server).
    """

    read_only: bool = False
    allowed_tools: Optional[Tuple[str, ...]] = None
    denied_tools: Tuple[str, ...] = ()
    readable_paths: Tuple[Path, ...] = field(default_factory=lambda: (Path.cwd().resolve(),))
    writable_paths: Tuple[Path, ...] = field(default_factory=lambda: (Path.cwd().resolve(),))
    writable_namespaces: Tuple[str, ...] = DEFAULT_WRITABLE_NAMESPACES
    # Default OFF — credential-bearing tools must look up secrets via
    # ``credential_id`` against the OS keyring + sources.yaml.  Trusted
    # CLI callers (e.g. an in-process MCP harness) can flip this on
    # via ``--allow-inline-credentials``.  Surface labelled HIGH-risk
    # because the inline shape lets a peer push raw secrets through
    # the otherwise-secret-free MCP wire.
    allow_inline_credentials: bool = False

    def is_tool_allowed(self, tool: str) -> bool:
        if tool in self.denied_tools:
            return False
        if self.allowed_tools is None:
            return True
        return tool in self.allowed_tools


def check_tool_permission(
    tool: str,
    arguments: Dict[str, Any],
    *,
    policy: McpPolicy,
) -> None:
    """Raise :class:`PermissionError` when ``tool(arguments)`` is denied.

    Order of checks:

    1. Unknown tool → ``RuntimeError`` (programming error, not an auth
       failure).
    2. Tool allow/deny list.
    3. Read-only gate (only if the tool actually mutates something).
    4. Read sandbox: every populated ``read_path_args`` argument must
       resolve under some entry of ``policy.readable_paths``.
    5. Filesystem sandbox: every populated ``file_path_args`` argument
       must resolve under some entry of ``policy.writable_paths``. An
       argument that is absent is skipped — the tool body itself will
       raise for missing-required-arg.
    6. Store-namespace allowlist: every ``writes_namespaces`` entry must
       be listed in ``policy.writable_namespaces``.
    """

    cap = TOOL_CAPABILITIES.get(tool)
    if cap is None:
        raise RuntimeError(f"Unknown tool {tool}")

    if not policy.is_tool_allowed(tool):
        raise PermissionError(f"Tool {tool!r} not in allowlist")

    needs_write = cap.mutates_files or bool(cap.writes_namespaces)
    if policy.read_only and needs_write:
        raise PermissionError(f"Tool {tool!r} requires writes; server is read-only")

    for arg_name in cap.read_path_args:
        raw = arguments.get(arg_name)
        if raw is None:
            continue
        target = Path(str(raw)).expanduser().resolve()
        if not _path_is_allowed(target, policy.readable_paths):
            raise PermissionError(
                f"Tool {tool!r} cannot read from {target} (outside --readable-paths allowlist)"
            )

    if cap.mutates_files:
        for arg_name in cap.file_path_args:
            raw = arguments.get(arg_name)
            if raw is None:
                continue
            target = Path(str(raw)).expanduser().resolve()
            if not _path_is_writable(target, policy.writable_paths):
                raise PermissionError(
                    f"Tool {tool!r} cannot write to {target} (outside --writable-paths allowlist)"
                )

    for ns in cap.writes_namespaces:
        if ns not in policy.writable_namespaces:
            raise PermissionError(
                f"Tool {tool!r} writes namespace {ns!r} which is not "
                f"in the --writable-namespaces allowlist"
            )


def _path_is_allowed(target: Path, roots: Tuple[Path, ...]) -> bool:
    """True if ``target`` resolves under at least one allowed root."""
    if not roots:
        return False
    for root in roots:
        try:
            target.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _path_is_writable(target: Path, writable_roots: Tuple[Path, ...]) -> bool:
    """True if ``target`` resolves under at least one of ``writable_roots``."""
    return _path_is_allowed(target, writable_roots)


def _csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _build_policy_from_args(args) -> McpPolicy:
    """Translate an argparse ``Namespace`` into an :class:`McpPolicy`."""
    raw_read_paths = _csv(getattr(args, "readable_paths", None))
    if raw_read_paths:
        readable_paths: Tuple[Path, ...] = tuple(
            Path(p).expanduser().resolve() for p in raw_read_paths
        )
    else:
        readable_paths = (Path.cwd().resolve(),)

    raw_paths = _csv(getattr(args, "writable_paths", None))
    if raw_paths:
        writable_paths: Tuple[Path, ...] = tuple(Path(p).expanduser().resolve() for p in raw_paths)
    else:
        writable_paths = (Path.cwd().resolve(),)

    raw_namespaces = _csv(getattr(args, "writable_namespaces", None))
    if raw_namespaces:
        writable_namespaces: Tuple[str, ...] = tuple(raw_namespaces)
    else:
        writable_namespaces = DEFAULT_WRITABLE_NAMESPACES

    allow = _csv(getattr(args, "allow_tools", None))
    deny = _csv(getattr(args, "deny_tools", None))

    return McpPolicy(
        read_only=bool(getattr(args, "read_only", False)),
        allowed_tools=tuple(allow) if allow else None,
        denied_tools=tuple(deny),
        readable_paths=readable_paths,
        writable_paths=writable_paths,
        writable_namespaces=writable_namespaces,
        allow_inline_credentials=bool(getattr(args, "allow_inline_credentials", False)),
    )


# ----------------------------------------------------------------------
# argparse wiring
# ----------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(COMMAND, help="Serve staged forge tools over MCP stdio")
    # ``required=False`` so a bare ``fluid mcp`` doesn't blow up
    # with the bare-bones argparse "the following arguments are
    # required: mcp_action" error.  ``run`` catches the
    # ``mcp_action is None`` case and renders a Rich-friendly panel
    # describing the ``serve`` action.
    parser.set_defaults(func=run)
    sp = parser.add_subparsers(dest="mcp_action", required=False)
    serve = sp.add_parser("serve", help="Run the MCP stdio server")
    serve.add_argument(
        "--read-only",
        action="store_true",
        help="Reject any tool that mutates files or store namespaces.",
    )
    serve.add_argument(
        "--allow-tools",
        default=None,
        metavar="TOOL[,TOOL...]",
        help=(
            "Comma-separated allowlist of tool names. Tools outside the "
            "list are blocked and hidden from tools/list. Default: all "
            "tools allowed."
        ),
    )
    serve.add_argument(
        "--deny-tools",
        default=None,
        metavar="TOOL[,TOOL...]",
        help=(
            "Comma-separated blocklist of tool names. Evaluated before "
            "--allow-tools so denial wins."
        ),
    )
    serve.add_argument(
        "--readable-paths",
        default=None,
        metavar="PATH[,PATH...]",
        help=(
            "Comma-separated filesystem roots path-based read tools may inspect. "
            "Paths are resolved at startup. Default: current working directory."
        ),
    )
    serve.add_argument(
        "--writable-paths",
        default=None,
        metavar="PATH[,PATH...]",
        help=(
            "Comma-separated filesystem roots mutating tools may write "
            "under. Paths are resolved at startup. Default: current "
            "working directory."
        ),
    )
    serve.add_argument(
        "--writable-namespaces",
        default=None,
        metavar="NS[,NS...]",
        help=(
            "Comma-separated store namespaces mutating tools may write "
            "to. Default: " + ",".join(DEFAULT_WRITABLE_NAMESPACES) + "."
        ),
    )
    serve.add_argument(
        "--allow-inline-credentials",
        action="store_true",
        help=(
            "Permit MCP clients to pass raw catalog credentials via "
            "``credentials.inline``. OFF by default because the MCP "
            "wire is normally LLM-facing and the contract is that "
            "the server only accepts ``credential_id`` lookups. Turn "
            "this on only for trusted in-process CLI harnesses."
        ),
    )
    serve.set_defaults(func=run)
    # Attach the consumer-side output-port action group under the
    # same ``fluid mcp`` parent so operators see one MCP surface
    # with two distinct flavours (authoring vs consumption).
    from fluid_build.cli.mcp_output_port import attach_to_mcp_subparsers

    attach_to_mcp_subparsers(sp)


def run(args, logger: logging.Logger) -> int:
    action = getattr(args, "mcp_action", None)
    if action is None:
        # Bare ``fluid mcp`` — render the friendly guide instead of
        # exiting non-zero with a generic "subcommand required" error.
        return _render_mcp_guide()
    if action == "serve":
        policy = _build_policy_from_args(args)
        _set_policy(policy)
        app = _build_fastmcp_app(policy)
        try:
            asyncio.run(app.run_stdio_async())
        except KeyboardInterrupt:
            return 0
        return 0
    # ``output-port`` (and any future action) supplies its own
    # ``func`` via attach_to_mcp_subparsers; argparse calls it
    # automatically. We only reach this branch when an action
    # exists but didn't set ``func`` — keep it informative.
    func = getattr(args, "func", None)
    if callable(func):
        return func(args, logger)
    return 1


def _render_mcp_guide() -> int:
    """Render an intuitive guide for ``fluid mcp`` with no
    subcommand.  Today there's only one sub-action (``serve``),
    so the panel doubles as a walkthrough of the most useful
    flags rather than a multi-row picker.
    """

    from fluid_build.cli._subcommand_guide import (
        SubcommandEntry,
        SubcommandGuide,
        render_subcommand_guide,
    )

    entries = [
        SubcommandEntry(
            name="serve",
            description=(
                "Run the MCP stdio server.  Exposes the staged forge tool "
                "surface (catalog reads, contract regeneration, semantic "
                "memory search) over JSON-RPC for Claude Code, Cursor, "
                "Continue, and any MCP client."
            ),
            example="fluid mcp serve --read-only",
        ),
    ]
    guide = SubcommandGuide(
        command_path="fluid mcp",
        headline=(
            "Serve forge tools over the Model Context Protocol so MCP "
            "clients can drive forge from inside the editor."
        ),
        entries=entries,
        # Single-subcommand surface; the recommendation is implicit.
        hint_provider=None,
        quick_start=(
            "fluid mcp serve --read-only "
            "(safe default — denies any tool that mutates files or store namespaces)"
        ),
    )
    return render_subcommand_guide(guide)


# ----------------------------------------------------------------------
# Transport — FastMCP server
#
# We delegate stdio framing, JSON-RPC routing, tools/list advertisement,
# initialize handshake, and sampling round-trip to the official
# ``modelcontextprotocol/python-sdk`` (FastMCP). Each forge tool is registered
# via ``@_mcp_app.tool()`` and dispatched into a worker thread so blocking
# code paths (file I/O, the FluidContractValidator, the forge.run() copilot
# loop) don't stall the asyncio loop the SDK runs on. Tools that need an LLM
# (``forge_run`` mode='ai', the diagnostic ``forge_run`` mode='diag') call
# ``ctx.session.create_message`` — the canonical server-side sampling
# primitive — so the IDE pays for the LLM, not the user.
#
# Borrowed-not-built per /borrow-before-build (skill receipts in
# AGENT_IDE.md "Related work" section).
# ----------------------------------------------------------------------


_mcp_app = FastMCP(name="forge-cli-mcp")
_current_policy: Optional[McpPolicy] = None


def _set_policy(policy: McpPolicy) -> None:
    """Install the active policy for this connection.

    Tools read the policy via :func:`_policy` to gate access. Single-stdio-
    connection scope, so module-level is safe.
    """
    global _current_policy
    _current_policy = policy


def _policy() -> McpPolicy:
    if _current_policy is None:
        raise RuntimeError("MCP policy not initialised — call _set_policy first")
    return _current_policy


def _build_fastmcp_app(policy: McpPolicy) -> FastMCP:
    """Return the FastMCP app, after pruning tools the policy hides.

    Tools that are denied (``--deny-tools``) or that would always be rejected
    (mutating tools under ``--read-only``) are removed from the SDK's tool
    registry so they never appear in ``tools/list``.
    """
    for name in list(TOOL_CAPABILITIES.keys()):
        cap = TOOL_CAPABILITIES[name]
        needs_write = cap.mutates_files or bool(cap.writes_namespaces)
        denied = not policy.is_tool_allowed(name)
        read_only_blocked = policy.read_only and needs_write
        if denied or read_only_blocked:
            try:
                _mcp_app.remove_tool(name)
            except Exception:  # noqa: BLE001
                pass
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
    check_tool_permission(name, arguments, policy=_policy())
    return _call_tool(
        name,
        arguments,
        read_only=_policy().read_only,
        allow_inline_credentials=_policy().allow_inline_credentials,
    )


# ----------------------------------------------------------------------
# Tool registrations (14 tools — one @_mcp_app.tool() per capability in
# TOOL_CAPABILITIES). Each is a thin async wrapper that gates on policy and
# delegates the actual work to :func:`_call_tool` (sync, threaded) or, for
# ``forge_run``, talks to ``ctx.session.create_message`` directly.
#
# Why explicit signatures with ``Annotated[T, Field(description=...)]``:
# FastMCP derives ``inputSchema`` from the Python function signature via
# ``mcp.server.fastmcp.utilities.func_metadata``. Annotated metadata
# (including Pydantic ``Field`` descriptions, ``Literal`` enums, and
# ``BaseModel`` nested envelopes) flows through verbatim into the
# emitted JSON Schema — so MCP clients (Claude Code / Cursor / Kiro)
# see the curated descriptions + enum values for autocomplete.
#
# Pattern borrowed from ``mcp-server-fetch`` (the official reference
# Python MCP server uses ``BaseModel`` + ``Annotated`` for argument
# shapes) and the FastMCP docs (gofastmcp.com/servers/tools). The
# legacy ``TOOL_CAPABILITIES[*].input_schema`` registry remains the
# canonical permission-gate source and pins description-symmetry
# via ``tests/cli/test_mcp_judge_enrich_tools.py``.
# ----------------------------------------------------------------------


@_mcp_app.tool(description=TOOL_CAPABILITIES["read_logical_model"].description)
async def read_logical_model(
    path: Annotated[str, Field(description=_PATH_LOGICAL_DESCRIPTION)],
) -> Dict[str, Any]:
    return await _dispatch_sync_tool("read_logical_model", {"path": path})


@_mcp_app.tool(description=TOOL_CAPABILITIES["score_contract_quality"].description)
async def score_contract_quality(
    contract_path: Annotated[
        Optional[str], Field(description=_SCORE_CONTRACT_PATH_DESCRIPTION)
    ] = None,
    contract: Annotated[
        Optional[Dict[str, Any]], Field(description=_SCORE_INLINE_DESCRIPTION)
    ] = None,
    include_artifacts: Annotated[
        bool, Field(description=_SCORE_INCLUDE_ARTIFACTS_DESCRIPTION)
    ] = False,
) -> Dict[str, Any]:
    """Run the 6-axis LLM-as-judge over a contract. Read-only."""
    args: Dict[str, Any] = {"include_artifacts": include_artifacts}
    if contract_path:
        args["contract_path"] = contract_path
    if contract is not None:
        args["contract"] = contract
    return await _dispatch_sync_tool("score_contract_quality", args)


@_mcp_app.tool(description=TOOL_CAPABILITIES["enrich_contract_suggestions"].description)
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


@_mcp_app.tool(description=TOOL_CAPABILITIES["update_entity"].description)
async def update_entity(
    path: Annotated[str, Field(description=_PATH_LOGICAL_SHORT_DESCRIPTION)],
    entity: Annotated[str, Field(description=_ENTITY_DESCRIPTION)],
    updates: Annotated[Optional[Dict[str, Any]], Field(description=_UPDATES_DESCRIPTION)] = None,
) -> Dict[str, Any]:
    return await _dispatch_sync_tool(
        "update_entity", {"path": path, "entity": entity, "updates": updates or {}}
    )


@_mcp_app.tool(description=TOOL_CAPABILITIES["add_relationship"].description)
async def add_relationship(
    path: Annotated[str, Field(description=_PATH_LOGICAL_SHORT_DESCRIPTION)],
    relationship: Annotated[Dict[str, Any], Field(description=_RELATIONSHIP_DESCRIPTION)],
) -> Dict[str, Any]:
    return await _dispatch_sync_tool(
        "add_relationship", {"path": path, "relationship": relationship}
    )


@_mcp_app.tool(description=TOOL_CAPABILITIES["regenerate_physical"].description)
async def regenerate_physical(
    path: Annotated[str, Field(description=_PATH_LOGICAL_DESCRIPTION)],
    contract_path: Annotated[
        Optional[str], Field(description=_REGEN_CONTRACT_OUT_DESCRIPTION)
    ] = None,
    engine: Annotated[str, Field(description=_REGEN_ENGINE_DESCRIPTION)] = "dbt",
) -> Dict[str, Any]:
    args = {"path": path, "engine": engine}
    if contract_path:
        args["contract_path"] = contract_path
    return await _dispatch_sync_tool("regenerate_physical", args)


@_mcp_app.tool(description=TOOL_CAPABILITIES["validate_contract"].description)
async def validate_contract(
    logical_path: Annotated[
        Optional[str], Field(description=_VALIDATE_LOGICAL_PATH_DESCRIPTION)
    ] = None,
    contract_path: Annotated[
        Optional[str], Field(description=_VALIDATE_CONTRACT_PATH_DESCRIPTION)
    ] = None,
) -> Dict[str, Any]:
    args: Dict[str, Any] = {}
    if logical_path:
        args["logical_path"] = logical_path
    if contract_path:
        args["contract_path"] = contract_path
    return await _dispatch_sync_tool("validate_contract", args)


@_mcp_app.tool(description=TOOL_CAPABILITIES["diff_models"].description)
async def diff_models(
    old: Annotated[str, Field(description=_DIFF_OLD_DESCRIPTION)],
    new: Annotated[str, Field(description=_DIFF_NEW_DESCRIPTION)],
) -> Dict[str, Any]:
    return await _dispatch_sync_tool("diff_models", {"old": old, "new": new})


@_mcp_app.tool(description=TOOL_CAPABILITIES["search_semantic_memory"].description)
async def search_semantic_memory(
    query: Annotated[str, Field(description=_SEMANTIC_QUERY_DESCRIPTION)],
    mode: Annotated[
        Optional[Literal["exact", "keyword", "vector", "hybrid"]],
        Field(description=_SEMANTIC_MODE_DESCRIPTION),
    ] = "hybrid",
    limit: Annotated[
        Optional[int], Field(description=_SEMANTIC_LIMIT_DESCRIPTION, ge=1, le=50)
    ] = 5,
    namespace: Optional[str] = None,
) -> Dict[str, Any]:
    args: Dict[str, Any] = {"query": query, "limit": limit or 5, "mode": mode or "hybrid"}
    if namespace:
        args["namespace"] = namespace
    return await _dispatch_sync_tool("search_semantic_memory", args)


@_mcp_app.tool(description=TOOL_CAPABILITIES["list_source_adapters"].description)
async def list_source_adapters() -> Dict[str, Any]:
    return await _dispatch_sync_tool("list_source_adapters", {})


@_mcp_app.tool(description=TOOL_CAPABILITIES["list_source_tables"].description)
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
    return await _dispatch_sync_tool(
        "list_source_tables",
        {
            "source": source,
            "credentials": _dump_envelope(credentials),
            "scope": _dump_envelope(scope),
            "allow_metadata_service": allow_metadata_service,
        },
    )


@_mcp_app.tool(description=TOOL_CAPABILITIES["inspect_source_table"].description)
async def inspect_source_table(
    source: Annotated[
        _CatalogSourceLiteral,
        Field(description=_ADAPTER_DISPATCH_DESCRIPTION),
    ],
    credentials: Annotated[CredentialsArg, Field(description=_CREDENTIALS_DESCRIPTION)],
    fqn: Annotated[str, Field(description=_FQN_DESCRIPTION)],
    allow_metadata_service: Annotated[
        bool, Field(description=_ALLOW_METADATA_SERVICE_DESCRIPTION)
    ] = False,
) -> Dict[str, Any]:
    return await _dispatch_sync_tool(
        "inspect_source_table",
        {
            "source": source,
            "credentials": _dump_envelope(credentials),
            "fqn": fqn,
            "allow_metadata_service": allow_metadata_service,
        },
    )


@_mcp_app.tool(description=TOOL_CAPABILITIES["list_source_lineage"].description)
async def list_source_lineage(
    source: Annotated[
        _CatalogSourceLiteral,
        Field(description=_ADAPTER_DISPATCH_DESCRIPTION),
    ],
    credentials: Annotated[CredentialsArg, Field(description=_CREDENTIALS_DESCRIPTION)],
    fqn: Annotated[str, Field(description=_FQN_DESCRIPTION)],
    direction: Annotated[
        Literal["both", "upstream", "downstream"],
        Field(description="Lineage direction to traverse."),
    ] = "both",
    depth: Annotated[int, Field(description="Maximum lineage hops to walk.", ge=1, le=20)] = 3,
    allow_metadata_service: Annotated[
        bool, Field(description=_ALLOW_METADATA_SERVICE_DESCRIPTION)
    ] = False,
) -> Dict[str, Any]:
    return await _dispatch_sync_tool(
        "list_source_lineage",
        {
            "source": source,
            "credentials": _dump_envelope(credentials),
            "fqn": fqn,
            "direction": direction,
            "depth": depth,
            "allow_metadata_service": allow_metadata_service,
        },
    )


@_mcp_app.tool(description=TOOL_CAPABILITIES["list_source_glossary"].description)
async def list_source_glossary(
    source: Annotated[
        _CatalogSourceLiteral,
        Field(description=_ADAPTER_DISPATCH_DESCRIPTION),
    ],
    credentials: Annotated[CredentialsArg, Field(description=_CREDENTIALS_DESCRIPTION)],
    scope: Annotated[Optional[ScopeArg], Field(description=_SCOPE_DESCRIPTION)] = None,
    query: Annotated[Optional[str], Field(description="Free-text glossary search query.")] = None,
    limit: Annotated[
        int, Field(description="Maximum number of glossary terms.", ge=1, le=500)
    ] = 50,
    allow_metadata_service: Annotated[
        bool, Field(description=_ALLOW_METADATA_SERVICE_DESCRIPTION)
    ] = False,
) -> Dict[str, Any]:
    args: Dict[str, Any] = {
        "source": source,
        "credentials": _dump_envelope(credentials),
        "limit": limit,
        "allow_metadata_service": allow_metadata_service,
    }
    if scope is not None:
        args["scope"] = _dump_envelope(scope)
    if query:
        args["query"] = query
    return await _dispatch_sync_tool("list_source_glossary", args)


@_mcp_app.tool(description=TOOL_CAPABILITIES["forge_from_source"].description)
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


@_mcp_app.tool(description=TOOL_CAPABILITIES["forge_run"].description)
async def forge_run(
    mode: str,
    target_dir: Optional[str] = None,
    data_product_type: Optional[str] = None,
    prompt: Optional[str] = None,
    from_products: Optional[List[str]] = None,
    ctx: Context = None,
) -> Dict[str, Any]:
    """Run fluid forge inside MCP with sampling-backed LLM.

    See ``TOOL_CAPABILITIES["forge_run"]`` for the mode semantics. Diag mode
    sends one ``sampling/createMessage`` round-trip via ``ctx.session.create_message``
    (the canonical SDK primitive); blank/ai modes install the sampling-context
    bridge so :class:`MCPSamplingProvider` can route LLM calls back to the
    IDE from inside ``forge.run()``.
    """
    # Pass the FULL argument set to the permission gate so the writable-paths
    # sandbox check actually runs on ``target_dir`` (the tool's only declared
    # ``file_path_args``). Passing a thin ``{"mode": mode}`` dict here would
    # cause :func:`check_tool_permission` to silently skip the check
    # (``arguments.get("target_dir")`` returns ``None`` → ``continue`` in the
    # gate loop), turning the documented sandbox guarantee into a write-anywhere
    # primitive. Tracked as security-review finding #1 in the Phase 3 audit.
    permission_args: Dict[str, Any] = {
        "mode": mode,
        "target_dir": target_dir,
        "data_product_type": data_product_type,
        "prompt": prompt,
        "from_products": from_products,
    }
    check_tool_permission("forge_run", permission_args, policy=_policy())
    if _policy().read_only:
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
            "stop_reason": getattr(result, "stopReason", None),
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
            _reset_sampling_context(sampling_tokens)

    raise RuntimeError(f"unknown forge_run mode: {mode_norm!r}")


def _run_forge_inproc(
    mode: str,
    target_dir: str,
    data_product_type: Optional[str],
    from_products: Optional[List[str]],
) -> Dict[str, Any]:
    """Run ``fluid forge`` in-process (sync). Called from ``forge_run`` via
    ``asyncio.to_thread``. ``MCPSamplingProvider`` (if used) routes back to
    the IDE's LLM via the sampling-context bridge.

    Critical: ``fluid forge --agent`` writes JSON-Lines progress events to
    stdout, and stdout IS the MCP wire — the MCP client's JSON-RPC parser
    rejects anything that isn't a well-formed ``JSONRPCMessage``. We capture
    forge's stdout for the duration of the run and surface the parsed events
    in the tool result (so Claude Code / Cursor / Kiro see structured forge
    progress alongside the standard ``exit_code`` + ``contract_path`` fields)
    without polluting the MCP wire.
    """
    import argparse as _argparse
    import contextlib
    import io

    from fluid_build.cli import forge as forge_mod

    # Defense-in-depth: re-validate ``target_dir`` against the active
    # ``--writable-paths`` policy. The async tool wrapper at ``forge_run``
    # already gates via ``check_tool_permission`` with the full argument
    # dict, but a future regression in that wrapper (e.g. forgetting to
    # plumb a new argument) must NOT silently let an attacker-controlled
    # path through to ``mkdir(parents=True)`` + ``write_text``. This
    # second check is the belt-and-braces fail-closed gate.
    policy = _policy()
    resolved = Path(str(target_dir)).expanduser().resolve()
    if not _path_is_writable(resolved, policy.writable_paths):
        raise PermissionError(
            f"forge_run: target_dir {resolved} is not within any "
            f"--writable-paths root ({', '.join(str(p) for p in policy.writable_paths)})"
        )

    parser = _argparse.ArgumentParser()
    sp = parser.add_subparsers()
    forge_mod.register(sp)

    argv = ["forge", "--agent", "-d", str(target_dir)]
    if data_product_type:
        argv += ["--data-product-type", str(data_product_type)]
    if mode == "blank":
        argv += ["--blank"]
    elif mode == "ai":
        for fp in from_products or []:
            argv += ["--from-product", str(fp)]
        argv += ["--llm-provider", "mcp-sampling"]

    args = parser.parse_args(argv)
    forge_logger = logging.getLogger("fluid.mcp.forge_run")
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        rc = forge_mod.run(args, forge_logger)
    raw_stdout = captured.getvalue()

    # Forge's --agent mode emits one JSON object per line. Parse them out;
    # everything else (Rich console banners, etc.) is best-effort discarded
    # since stdout was redirected to suppress it from the wire.
    events: List[Dict[str, Any]] = []
    for line in raw_stdout.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("event"), str):
            events.append(obj)

    contract_path = Path(target_dir) / "contract.fluid.yaml"
    return {
        "mode": mode,
        "exit_code": rc,
        "target_dir": str(target_dir),
        "contract_path": str(contract_path),
        "contract_exists": contract_path.is_file(),
        "events": events,
    }


def _call_tool(
    name: str,
    arguments: Dict[str, Any],
    *,
    read_only: bool,
    allow_inline_credentials: bool = False,
) -> Dict[str, Any]:
    from fluid_build.cli.forge_data_model import diff_logical_models
    from fluid_build.copilot.schemas.stage_outputs import ConceptualRelationship, LogicalDraft

    if name == "read_logical_model":
        path = Path(arguments["path"])
        return LogicalDraft.model_validate_json(path.read_text(encoding="utf-8")).model_dump(
            mode="json", by_alias=True
        )
    if name == "update_entity":
        if read_only:
            raise RuntimeError("Server is running in read-only mode")
        path = Path(arguments["path"])
        logical = LogicalDraft.model_validate_json(path.read_text(encoding="utf-8"))
        before = logical.model_dump(mode="json", by_alias=True)
        target = arguments["entity"]
        updates = arguments.get("updates") or {}
        if logical.conceptual:
            for entity in logical.conceptual.entities:
                if entity.name == target:
                    for key, value in updates.items():
                        if hasattr(entity, key):
                            setattr(entity, key, value)
        path.write_text(logical.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
        archive_snapshot(contract={}, logical_model=before)
        write_audit_event("mcp_update_entity", payload={"path": str(path), "entity": target})
        return {"updated": True}
    if name == "add_relationship":
        if read_only:
            raise RuntimeError("Server is running in read-only mode")
        path = Path(arguments["path"])
        logical = LogicalDraft.model_validate_json(path.read_text(encoding="utf-8"))
        if logical.conceptual is None:
            raise RuntimeError("Logical model has no conceptual section")
        before = logical.model_dump(mode="json", by_alias=True)
        logical.conceptual.relationships.append(ConceptualRelationship(**arguments["relationship"]))
        path.write_text(logical.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
        archive_snapshot(contract={}, logical_model=before)
        write_audit_event("mcp_add_relationship", payload={"path": str(path)})
        return {"updated": True}
    if name == "regenerate_physical":
        if read_only:
            raise RuntimeError("Server is running in read-only mode")
        path = Path(arguments["path"])
        logical = LogicalDraft.model_validate_json(path.read_text(encoding="utf-8"))
        contract = build_contract_from_logical(
            logical, build_engine=str(arguments.get("engine") or "dbt")
        )
        contract_path = Path(arguments.get("contract_path") or f"{path}.fluid.yaml")
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
        archive_snapshot(
            contract=contract, logical_model=logical.model_dump(mode="json", by_alias=True)
        )
        write_audit_event(
            "mcp_regenerate_physical",
            payload={"path": str(path), "contract_path": str(contract_path)},
        )
        return {"contract_path": str(contract_path)}
    if name == "validate_contract":
        validator = FluidContractValidator()
        logical = None
        contract = None
        if arguments.get("logical_path"):
            logical = LogicalDraft.model_validate_json(
                Path(arguments["logical_path"]).read_text(encoding="utf-8")
            )
        if arguments.get("contract_path"):
            contract = yaml.safe_load(Path(arguments["contract_path"]).read_text(encoding="utf-8"))
        return validator.validate(logical=logical, contract=contract).model_dump(
            mode="json", by_alias=True
        )
    if name == "diff_models":
        return diff_logical_models(Path(arguments["old"]), Path(arguments["new"]))
    if name == "search_semantic_memory":
        store = resolve_store(workspace_root=Path.cwd())
        results = store.search(
            "memory/semantic",
            str(arguments.get("query") or ""),
            mode=str(arguments.get("mode") or "hybrid"),
            limit=int(arguments.get("limit") or 5),
        )
        return {"results": [record.value for record in results]}

    # ---------------------------------------------------------------
    # V1.5 — metadata source-catalog tools.
    # ---------------------------------------------------------------
    if name == "list_source_adapters":
        return {
            "adapters": _list_source_adapters(),
        }

    if name == "list_source_tables":
        adapter = _build_source_adapter(
            arguments, allow_inline_credentials=allow_inline_credentials
        )
        scope = _scope_from_args(arguments)
        tables = adapter.list_tables(scope)
        write_audit_event(
            "mcp_list_source_tables",
            payload={
                **adapter.audit_context(),
                "scope": scope.model_dump(mode="json", by_alias=True),
                "result_count": len(tables),
            },
        )
        return {"tables": [t.model_dump(mode="json", by_alias=True) for t in tables]}

    if name == "inspect_source_table":
        adapter = _build_source_adapter(
            arguments, allow_inline_credentials=allow_inline_credentials
        )
        fqn = str(arguments.get("fqn") or "")
        if not fqn:
            raise RuntimeError("inspect_source_table requires 'fqn'")
        table = adapter.get_table(fqn)
        write_audit_event(
            "mcp_inspect_source_table",
            payload={**adapter.audit_context(), "fqn": fqn},
        )
        return table.model_dump(mode="json", by_alias=True)

    if name == "list_source_lineage":
        adapter = _build_source_adapter(
            arguments, allow_inline_credentials=allow_inline_credentials
        )
        fqn = str(arguments.get("fqn") or "")
        if not fqn:
            raise RuntimeError("list_source_lineage requires 'fqn'")
        lineage = adapter.get_lineage(fqn)
        write_audit_event(
            "mcp_list_source_lineage",
            payload={**adapter.audit_context(), "fqn": fqn},
        )
        return lineage.model_dump(mode="json", by_alias=True)

    if name == "list_source_glossary":
        adapter = _build_source_adapter(
            arguments, allow_inline_credentials=allow_inline_credentials
        )
        scope = _scope_from_args(arguments)
        terms = adapter.list_glossary_terms(scope)
        write_audit_event(
            "mcp_list_source_glossary",
            payload={**adapter.audit_context()},
        )
        return {"terms": [t.model_dump(mode="json", by_alias=True) for t in terms]}

    if name == "forge_from_source":
        if read_only:
            raise RuntimeError("Server is running in read-only mode")

        # JDBC sources route through a separate code path that doesn't
        # need a credential resolver — the URI carries everything. This
        # mirrors ``cli/forge_data_model.py::run_from_source_command``
        # which forks ``--source <jdbc>`` early to ``_run_from_jdbc_source``.
        # MCP gets the same one-shot synthesis (no separate connect step).
        jdbc_kinds = {"postgres", "postgresql", "mysql", "sqlite"}
        source_value = str(arguments.get("source") or "").lower().strip()
        if source_value in jdbc_kinds:
            return _dispatch_forge_from_jdbc_source(arguments)

        # forge_from_source is the V1.5 marquee tool: enumerate the
        # catalog scope, pull per-table metadata, run the staged
        # Logical pipeline, then write a Fluid contract plus .model.json
        # sidecar. Keeping this one-shot avoids making MCP clients guess
        # that they need a second regenerate_physical call.
        #
        # ``resolve_store`` is imported at module level — do NOT
        # re-import inside this branch, otherwise Python treats it
        # as a function-local for the entire ``_call_tool`` body
        # and the search_semantic_memory branch above this one
        # crashes with UnboundLocalError.
        from fluid_build.copilot.agents.base import StageSession
        from fluid_build.copilot.agents.logical_agent import LogicalAgent

        adapter = _build_source_adapter(
            arguments, allow_inline_credentials=allow_inline_credentials
        )
        scope = _scope_from_args(arguments)
        technique = str(arguments.get("technique") or "data_vault_2")
        engine = str(arguments.get("engine") or "dbt")
        model_name = str(arguments.get("name") or scope.schema_name or "forged_model")
        output_path = arguments.get("output_path")
        if not output_path:
            raise RuntimeError("forge_from_source requires 'output_path'")

        store = resolve_store(workspace_root=Path.cwd())
        session = StageSession(store=store)
        logical = LogicalAgent().from_catalog(
            session,
            name=model_name,
            adapter=adapter,
            scope=scope,
            technique=technique,
        )
        sidecar_payload = logical.model_dump(mode="json", by_alias=True)
        contract = build_contract_from_logical(logical, build_engine=engine)

        contract_path = Path(str(output_path))
        sidecar_path = (
            Path(str(arguments.get("logical_path")))
            if arguments.get("logical_path")
            else contract_path.with_name(f"{contract_path.name}.model.json")
        )
        contract.setdefault("labels", {})
        contract["labels"] = dict(contract["labels"])
        contract["labels"]["modelSidecar"] = sidecar_path.name

        validation = FluidContractValidator().validate(logical=logical, contract=contract)
        if not validation.passes_schema:
            raise RuntimeError(
                "forge_from_source produced an invalid contract: "
                + "; ".join(issue.message for issue in validation.issues[:5])
            )

        contract_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text(
            json.dumps(sidecar_payload, indent=2, default=str), encoding="utf-8"
        )
        contract_path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
        archive_snapshot(contract=contract, logical_model=sidecar_payload)

        write_audit_event(
            "mcp_forge_from_source",
            payload={
                **adapter.audit_context(),
                "scope": scope.model_dump(mode="json", by_alias=True),
                "technique": technique,
                "model_name": model_name,
                "contract_path": str(contract_path),
                "sidecar_path": str(sidecar_path) if sidecar_path else None,
                "table_count": (
                    len(logical.dimensional.facts) + len(logical.dimensional.dimensions)
                    if logical.dimensional
                    else (len(logical.dv2.hubs) if logical.dv2 else 0)
                ),
            },
        )
        return {
            "logical": sidecar_payload,
            "contract_path": str(contract_path),
            "sidecar_path": str(sidecar_path) if sidecar_path else None,
            "validation": validation.model_dump(mode="json", by_alias=True),
        }

    # ``forge_run`` is NOT dispatched through ``_call_tool`` — it's a FastMCP
    # tool with its own async implementation (see ``forge_run`` above) so it
    # can call ``ctx.session.create_message`` for the sampling round-trip.
    # Any caller that lands here with name='forge_run' is a bug.

    if name == "score_contract_quality":
        return _dispatch_score_contract_quality(arguments)
    if name == "enrich_contract_suggestions":
        return _dispatch_enrich_contract_suggestions(arguments)
    raise RuntimeError(f"Unknown tool {name}")


def _resolve_contract_argument(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Accept either ``contract_path`` (filesystem) or ``contract`` (inline dict).

    Path takes precedence when both supplied. The path read is what the
    capability's ``read_path_args`` policy gates on; inline dicts come
    straight from the MCP caller (command_center / IDE) without
    filesystem confinement.
    """
    path_arg = arguments.get("contract_path")
    inline = arguments.get("contract")
    if path_arg:
        text = Path(path_arg).read_text(encoding="utf-8")
        loaded = yaml.safe_load(text) or {}
        if not isinstance(loaded, dict):
            raise RuntimeError(f"Contract at {path_arg!r} did not parse to a dict")
        return loaded
    if inline is not None:
        if not isinstance(inline, dict):
            raise RuntimeError("'contract' argument must be a dict")
        return inline
    raise RuntimeError("Pass either 'contract_path' or 'contract'")


def _dispatch_score_contract_quality(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """MCP shim around :class:`JudgeAgent`. Read-only — no contract writes."""
    from fluid_build.copilot.agents.judge_agent import JudgeAgent

    contract = _resolve_contract_argument(arguments)
    build_artifacts: Optional[Dict[str, Any]] = None
    if bool(arguments.get("include_artifacts")):
        from fluid_build.copilot.enrichment import enrich_contract as _enrich

        try:
            build_artifacts = _enrich(contract)
        except Exception:  # noqa: BLE001 — judging without artifacts is still valid
            build_artifacts = None
    result = JudgeAgent().judge(contract, build_artifacts=build_artifacts)
    return {
        "total": result.total,
        "axes": {axis: score.score for axis, score in result.axes.items()},
        "axis_reasoning": {axis: score.reasoning for axis, score in result.axes.items()},
        "axis_suggestions": {axis: list(score.suggestions) for axis, score in result.axes.items()},
        "model": result.model,
        "critique_applied": bool(getattr(result, "critique_applied", False)),
        "max_total": len(result.axes) * 5,
    }


def _dispatch_enrich_contract_suggestions(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """MCP shim around :func:`enrich_contract`. Read-only — returns
    suggestions; does not apply them to the contract on disk."""
    from fluid_build.copilot.enrichment import enrich_contract as _enrich

    contract = _resolve_contract_argument(arguments)
    artifacts = _enrich(contract)
    if artifacts is None:
        return {"enabled": False, "artifacts": None}
    return {"enabled": True, "artifacts": artifacts}


# ---------------------------------------------------------------------
# V1.5 source-catalog dispatch helpers.
# ---------------------------------------------------------------------


def _list_source_adapters() -> List[Dict[str, Any]]:
    """Enumerate the source-catalog + JDBC adapters this build of
    forge-cli can dispatch to.

    The list is static — it reflects what code is shipped, not what
    the operator has configured. To list configured *credentials*
    (which catalogs the operator has actually set up), use the
    ``fluid ai status`` CLI surface (Sprint C). The MCP tool is
    deliberately inventory-only: it tells the LLM which catalog
    types are reachable, not which specific credentials are saved.

    Catalog adapters (kind=catalog) are implemented in
    ``fluid_build.copilot.catalog.<name>`` and follow the 9 patterns
    in ``catalog._patterns``. JDBC adapters (kind=jdbc) route through
    :mod:`fluid_build.cli._forge_data_model_jdbc` — duckdb-extension
    introspection over a ``--uri`` payload.

    Future adapters (Apache Atlas, Alation, Microsoft Purview, …) get
    added here when they land — and inherit the same patterns
    automatically.
    """
    catalog_entries = [
        {"name": name, "status": "available", "kind": "catalog"} for name in _CATALOG_SOURCE_LIST
    ]
    jdbc_entries = [
        {"name": name, "status": "available", "kind": "jdbc"} for name in _JDBC_SOURCE_LIST
    ]
    return catalog_entries + jdbc_entries


# Catalog dispatch + the JDBC source set now live in the shared registry
# ``copilot.catalog.source_registry`` (the single source of truth shared
# with the CLI, plus ``fluid_build.source_adapters`` plugins).
# ``_build_source_adapter`` resolves catalog adapter classes through it;
# ``forge_from_source`` short-circuits JDBC sources to the duckdb scanner
# (``_dispatch_forge_from_jdbc_source``) before calling it (catalog-only).


def _dispatch_forge_from_jdbc_source(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """MCP shim around :func:`_run_from_jdbc_source` (the CLI's JDBC path).

    JDBC sources (postgres / postgresql / mysql / sqlite) carry credentials
    inline in a ``--uri`` payload, so they bypass the credential resolver
    entirely. This shim builds a minimal argparse-Namespace shaped like the
    ``fluid forge data-model from-source`` parser produces, dispatches to
    the shared duckdb-attach helper, and re-reads the produced contract so
    the MCP caller gets the same dict shape catalog sources return.

    Audit event: ``mcp_forge_from_jdbc_source`` is written (no credentials —
    URI password is masked; only the source kind + output path land in the
    forensic trail).
    """
    import argparse as _argparse

    from fluid_build.cli._forge_data_model_jdbc import _run_from_jdbc_source

    source = str(arguments.get("source") or "").lower().strip()
    uri = arguments.get("uri")
    output_path = arguments.get("output_path")
    if not uri:
        raise RuntimeError(
            f"forge_from_source --source {source!r} requires 'uri'. "
            "Example: postgresql://user:pass@host:5432/db"
        )
    if not output_path:
        raise RuntimeError("forge_from_source requires 'output_path'")

    # The JDBC helper consumes ``args.source``, ``args.uri``, ``args.output``,
    # ``args.name``, ``args.schema_name``, ``args.tables``. Build an
    # argparse Namespace with exactly those attributes — leaving the rest
    # absent is fine since the helper uses ``getattr(args, ..., None)``.
    scope = arguments.get("scope") or {}
    if not isinstance(scope, dict):
        scope = {}
    namespace = _argparse.Namespace(
        source=source,
        uri=str(uri),
        output=str(output_path),
        name=arguments.get("name"),
        schema_name=scope.get("schema") or scope.get("schema_name"),
        tables=scope.get("tables") or None,
    )
    jdbc_logger = logging.getLogger("fluid.mcp.forge_from_jdbc_source")
    rc = _run_from_jdbc_source(namespace, jdbc_logger)
    if rc != 0:
        raise RuntimeError(
            f"forge_from_source: JDBC introspection failed for source={source!r} "
            f"(exit_code={rc}). See server logs."
        )

    # Re-read the emitted contract so the MCP caller gets a structured
    # response (mirroring the catalog branch's shape).
    contract_path = Path(str(output_path))
    contract_text = contract_path.read_text(encoding="utf-8")
    contract_data = yaml.safe_load(contract_text) or {}

    write_audit_event(
        "mcp_forge_from_jdbc_source",
        payload={
            "source": source,
            "output_path": str(contract_path),
            # Deliberately NOT logging the URI — passwords can be embedded.
        },
    )

    return {
        "kind": "jdbc",
        "source": source,
        "contract_path": str(contract_path),
        "contract_exists": contract_path.is_file(),
        "table_count": len(contract_data.get("exposes") or []),
    }


def _build_source_adapter(
    arguments: Dict[str, Any],
    *,
    allow_inline_credentials: bool = False,
) -> Any:
    """Resolve the right adapter from MCP tool arguments.

    Every catalog tool's ``arguments`` MUST include:

    * ``source``: which catalog (``"snowflake"`` / ``"unity"`` /
      ``"bigquery"`` / ``"dataplex"`` / ``"glue"`` / ``"datahub"``
      / ``"datamesh_manager"``).
    * ``credentials.credential_id``: how to authenticate. The MCP
      server NEVER accepts a credential value via the LLM-facing
      wire — only a ``credential_id`` pointing at a saved entry in
      the keyring + ``~/.fluid/sources.yaml``.

    A trusted in-process CLI harness can opt into accepting
    ``credentials.inline = {...}`` by starting the server with
    ``fluid mcp serve --allow-inline-credentials``. The default
    rejects ``inline`` so a malicious LLM client cannot push raw
    secrets through the tool surface.

    The resolver merges keyring + ``~/.fluid/sources.yaml`` into a
    typed Credentials object the adapter consumes. Each adapter's
    ``from_resolver`` classmethod is the canonical entry point.
    """
    from fluid_build.copilot.catalog.credentials import CredentialResolver

    source = str(arguments.get("source") or "").lower().strip()
    catalog_sources = _source_registry.list_catalog_sources()
    if not source:
        supported = ", ".join(catalog_sources)
        raise RuntimeError(f"Source-catalog tools require 'source' (one of: {supported}).")
    credentials_arg = arguments.get("credentials") or {}
    credential_id = credentials_arg.get("credential_id")
    inline = credentials_arg.get("inline")
    # SECURITY: refuse raw inline secrets unless the operator
    # explicitly enabled them at server startup.  The MCP wire is
    # normally LLM-facing; the documented contract is that secrets
    # are looked up via ``credential_id`` against the local
    # keyring + sources.yaml, never sent over the wire.
    if inline and not allow_inline_credentials:
        raise RuntimeError(
            "Source-catalog tools refused inline credentials over MCP. "
            "Pass credentials.credential_id (a name configured via "
            "`fluid ai setup --source <catalog> --name <name>`) instead, or restart the "
            "server with --allow-inline-credentials if the caller is "
            "a trusted in-process CLI harness."
        )
    if not credential_id and not inline:
        raise RuntimeError(
            "Source-catalog tools require credentials.credential_id "
            "(or credentials.inline for direct CLI callers when the "
            "server is started with --allow-inline-credentials)."
        )
    if source not in catalog_sources:
        supported = ", ".join(catalog_sources)
        raise RuntimeError(f"Unknown source-catalog adapter: {source!r}. Supported: {supported}.")
    resolver = CredentialResolver(
        allow_metadata_service=bool(arguments.get("allow_metadata_service", False))
    )
    adapter_cls = _source_registry.resolve_catalog_adapter_class(source)
    return adapter_cls.from_resolver(
        resolver, credential_id=credential_id, inline_credentials=inline
    )


def _scope_from_args(arguments: Dict[str, Any]) -> Any:
    """Build a ``CatalogScope`` from MCP tool arguments.

    Accepts the JSON shape::

        {"scope": {"database": "DEMO_DB", "schema": "SEEDED", "tables": [...]}}

    or a flat shape with the scope fields at the top level. Both
    are tolerated to keep the LLM-facing schema forgiving.
    """
    from fluid_build.copilot.catalog.models import CatalogScope

    raw = arguments.get("scope")
    if isinstance(raw, dict):
        return CatalogScope.model_validate(raw)
    # Flat fallback: pull individual keys.
    flat = {
        k: arguments[k]
        for k in ("database", "schema", "schema_name", "catalog", "tables")
        if k in arguments
    }
    return CatalogScope.model_validate(flat)
