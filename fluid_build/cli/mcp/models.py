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
MCP data models — ToolCapability registry, Pydantic argument envelopes,
and shared constants.

Split from ``fluid_build.cli.mcp`` (issue #11) to reduce the 2 446-line
monolith into focused, testable modules.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Dict, List, Literal, Optional, Tuple

import yaml
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    # Annotation-only. ``Context`` is used solely in type annotations
    # (``Optional[Context]`` on the sampling-context helpers); under
    # ``from __future__ import annotations`` those are lazy strings, never
    # evaluated at runtime. Importing the MCP server SDK at module load would
    # pull ~87 heavy modules onto the ``fluid --help`` / parser-build hot path
    # and revert #265 — so the SDK stays behind this guard. Pinned by
    # tests/perf/test_startup_budget.py.
    from mcp.server.fastmcp import Context

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

_SAMPLING_CTX: ContextVar[Optional[Any]] = ContextVar("forge_mcp_sampling_ctx", default=None)
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


# ---------------------------------------------------------------------------
# Tool capability model + policy
# ---------------------------------------------------------------------------


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

_CATALOG_SOURCE_LIST = (
    "snowflake",
    "unity",
    "bigquery",
    "dataplex",
    "glue",
    "datahub",
    "datamesh_manager",
)
# JDBC-introspectable databases. The catalog tools (list_source_tables /
# inspect_source_table / list_source_lineage / list_source_glossary) do NOT
# accept these — JDBC is a one-shot synthesis path only. ``forge_from_source``
# is the only tool that dispatches to JDBC (via ``_run_from_jdbc_source``).
_JDBC_SOURCE_LIST = (
    "postgres",
    "postgresql",
    "mysql",
    "sqlite",
)
_SOURCE_ENUM = list(_CATALOG_SOURCE_LIST)
_FORGE_FROM_SOURCE_ENUM = list(_CATALOG_SOURCE_LIST + _JDBC_SOURCE_LIST)

# Modeling-technique enum is derived from the pluggable modeling-technique
# registry (issue #248), EXCLUDING ``custom`` — the bring-your-own-model
# technique needs a ``--logical-model`` file path the MCP wire can't supply.
# Built-in data_vault_2 / dimensional / flat + any plugin techniques flow
# through. Hardcoding the list (e.g. ``["data_vault_2", "dimensional"]``) drops
# the source-aligned ``flat`` technique and any future plugins.
from fluid_build.copilot import modeling_techniques as _modeling_techniques

_modeling_techniques.discover_modeling_techniques()
_TECHNIQUE_ENUM = [
    name
    for name in _modeling_techniques.list_modeling_techniques()
    if not ((_t := _modeling_techniques.get_modeling_technique(name)) and _t.requires_logical_model)
]

# Closed-enum ``Literal`` aliases for the FastMCP-derived tool signatures
# (server.py). FastMCP introspects the Python signature and emits the
# ``Literal`` members verbatim into the published ``inputSchema.enum`` — so the
# technique enum advertised to MCP clients tracks the registry (incl. ``flat``)
# rather than a hardcoded subset.
_CatalogSourceLiteral = Literal[tuple(_SOURCE_ENUM)]  # type: ignore[valid-type]
_ForgeSourceLiteral = Literal[tuple(_FORGE_FROM_SOURCE_ENUM)]  # type: ignore[valid-type]
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
