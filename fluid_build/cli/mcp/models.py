# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""
MCP data models — ToolCapability registry, Pydantic argument envelopes,
and shared constants.

Split from ``fluid_build.cli.mcp`` (issue #11) to reduce the 2 446-line
monolith into focused, testable modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

COMMAND = "mcp"
MCP_PROTOCOL_VERSION = "2025-06-18"

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
_SOURCE_ENUM = list(_CATALOG_SOURCE_LIST)

_CatalogSourceLiteral = Literal[tuple(_SOURCE_ENUM)]  # type: ignore[valid-type]

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
# ``tests/cli/test_mcp_advertised_schema.py`` pins both.
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
# ``test_mcp_advertised_schema.py::test_curated_registry_matches_signatures``
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
_ADAPTER_DISPATCH_DESCRIPTION = "Catalog adapter to dispatch against."
_ALLOW_METADATA_SERVICE_DESCRIPTION = (
    "Request the credential resolver's fallback to "
    "cloud-metadata-service auth (instance profile, workload identity, gcloud "
    "ADC). Off by default. Refused with an error unless the server was started "
    "with --allow-metadata-service (or FLUID_ALLOW_METADATA_SERVICE=1)."
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
    # ---------------------------------------------------------------
    # V1.5 — metadata source-catalog tools.
    #
    # User-facing vocabulary uses "source" / "metadata source" to
    # disambiguate from forge-cli's existing publish-target catalog
    # role (``providers/catalogs/`` writes to DMM / Splunk / etc.).
    # Every tool here is READ-ONLY against external metadata APIs
    # (Snowflake INFORMATION_SCHEMA, Unity tables.get, …) — no data
    # values are ever fetched.
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
                    "description": _ALLOW_METADATA_SERVICE_DESCRIPTION,
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
                "allow_metadata_service": {
                    "type": "boolean",
                    "default": False,
                    "description": _ALLOW_METADATA_SERVICE_DESCRIPTION,
                },
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
                "allow_metadata_service": {
                    "type": "boolean",
                    "default": False,
                    "description": _ALLOW_METADATA_SERVICE_DESCRIPTION,
                },
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
                "allow_metadata_service": {
                    "type": "boolean",
                    "default": False,
                    "description": _ALLOW_METADATA_SERVICE_DESCRIPTION,
                },
            },
            "required": ["source", "credentials"],
            "additionalProperties": False,
        },
    ),
}


DEFAULT_WRITABLE_NAMESPACES: Tuple[str, ...] = ("history", "audit")
"""Namespaces implicitly granted write access when the operator hasn't
specified ``--writable-namespaces`` explicitly. ``history`` powers the
per-artifact snapshot bucket; ``audit`` powers the forensic trail.
Both are needed for *any* mutating tool to behave responsibly."""
