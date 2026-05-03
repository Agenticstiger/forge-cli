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
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from fluid_build.copilot.store.audit_trail import write_audit_event
from fluid_build.copilot.store.factory import resolve_store
from fluid_build.copilot.store.history import archive_snapshot
from fluid_build.forge_datamodel.emit.fluid_contract import build_contract_from_logical
from fluid_build.forge_datamodel.emit.validator import FluidContractValidator

COMMAND = "mcp"
MCP_PROTOCOL_VERSION = "2025-06-18"


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

_SOURCE_ENUM = [
    "snowflake",
    "unity",
    "bigquery",
    "dataplex",
    "glue",
    "datahub",
    "datamesh_manager",
]
_TECHNIQUE_ENUM = ["data_vault_2", "dimensional"]

_CREDENTIALS_PROP = {
    "type": "object",
    "description": (
        "Credential lookup envelope. Pass ONLY the credential_id; "
        "the server never accepts raw secrets over the MCP wire. "
        "credential_id maps to a row in ~/.fluid/sources.yaml that "
        "was set up via `fluid ai setup --source <catalog> --name <credential-id>`."
    ),
    "properties": {
        "credential_id": {
            "type": "string",
            "description": (
                "Saved credential name from ~/.fluid/sources.yaml — same value "
                "you pass to `fluid forge data-model from-source --credential-id`."
            ),
        },
    },
    "required": ["credential_id"],
    "additionalProperties": True,  # operators can pass adapter-specific keys
}

_SCOPE_PROP = {
    "type": "object",
    "description": (
        "Catalog scope — the database/schema/catalog identifier the "
        "adapter should read from. Field names are catalog-specific: "
        "Snowflake / BigQuery / DataHub use database+schema; "
        "Unity uses catalog+schema; Glue uses database; "
        "DMM uses domain (passed via the database field)."
    ),
    "properties": {
        "database": {"type": "string"},
        "schema": {"type": "string"},
        "schema_name": {
            "type": "string",
            "description": "Alias for 'schema' on adapters that prefer it.",
        },
        "catalog": {
            "type": "string",
            "description": "Top-level catalog name (Unity / Dataplex entry-group / DataHub platform).",
        },
        "tables": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional explicit table-name list; if omitted, every table in scope is enumerated.",
        },
    },
    "additionalProperties": False,
}

_FQN_REQ = {
    "type": "string",
    "description": (
        "Fully-qualified table name in the catalog's native form. "
        "Snowflake: DB.SCHEMA.TABLE. Unity: catalog.schema.table. "
        "BigQuery: project.dataset.table. Glue: database.table. "
        "DataHub: urn:li:dataset:(...) or shortform snowflake.db.table."
    ),
}

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
            "catalog scope. Reads tables / lineage / glossary, runs the staged "
            "pipeline (Logical → Builder → Readme → Transformation → Validator), "
            "and writes the contract + .model.json sidecar. Requires "
            "credentials.credential_id and an output_path that resolves under "
            "--writable-paths."
        ),
        mutates_files=True,
        file_path_args=("output_path", "logical_path"),
        writes_namespaces=("history", "audit"),
        input_schema={
            "type": "object",
            "properties": {
                "source": {"type": "string", "enum": _SOURCE_ENUM},
                "credentials": _CREDENTIALS_PROP,
                "scope": _SCOPE_PROP,
                "technique": {
                    "type": "string",
                    "enum": _TECHNIQUE_ENUM,
                    "default": "data_vault_2",
                    "description": "Modeling technique: Data Vault 2.0 or Dimensional (Kimball).",
                },
                "name": {
                    "type": "string",
                    "description": "Logical-model name. Defaults to the schema name.",
                },
                "engine": {
                    "type": "string",
                    "default": "dbt",
                    "description": "Build engine for the emitted Fluid contract.",
                },
                "output_path": {
                    "type": "string",
                    "description": (
                        "Destination path for the Fluid contract. Must resolve "
                        "under one of the server's --writable-paths roots."
                    ),
                },
                "logical_path": {
                    "type": "string",
                    "description": (
                        "Optional explicit path for the .model.json sidecar; "
                        "defaults to <output_path>.model.json."
                    ),
                },
                "allow_metadata_service": {"type": "boolean", "default": False},
            },
            "required": ["source", "credentials", "scope", "output_path"],
            "additionalProperties": False,
        },
    ),
}


DEFAULT_WRITABLE_NAMESPACES: Tuple[str, ...] = ("history", "audit")
"""Namespaces implicitly granted write access when the operator hasn't
specified ``--writable-namespaces`` explicitly. ``history`` powers the
per-artifact snapshot bucket; ``audit`` powers the forensic trail.
Both are needed for *any* mutating tool to behave responsibly."""


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
                f"Tool {tool!r} cannot read from {target} " f"(outside --readable-paths allowlist)"
            )

    if cap.mutates_files:
        for arg_name in cap.file_path_args:
            raw = arguments.get(arg_name)
            if raw is None:
                continue
            target = Path(str(raw)).expanduser().resolve()
            if not _path_is_writable(target, policy.writable_paths):
                raise PermissionError(
                    f"Tool {tool!r} cannot write to {target} "
                    f"(outside --writable-paths allowlist)"
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


def run(args, logger: logging.Logger) -> int:
    action = getattr(args, "mcp_action", None)
    if action is None:
        # Bare ``fluid mcp`` — render the friendly guide instead of
        # exiting non-zero with a generic "subcommand required" error.
        return _render_mcp_guide()
    if action != "serve":
        return 1
    policy = _build_policy_from_args(args)
    return _serve_stdio(policy=policy, logger=logger)


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
# stdio loop
# ----------------------------------------------------------------------


def _serve_stdio(*, policy: McpPolicy, logger: logging.Logger) -> int:
    advertised = _filter_visible_tools(_tool_definitions(), policy)
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = request.get("method")
        # MCP notifications carry no ``id`` and expect NO response.
        # Per spec, the most important is ``notifications/initialized``
        # which the client sends after ``initialize``. Returning an
        # error response here breaks well-behaved clients (Claude
        # Desktop, Cursor, the @modelcontextprotocol SDK).
        is_notification = "id" not in request
        if is_notification:
            # We don't yet act on any notifications, but accept and
            # silently consume them so the protocol handshake completes.
            continue
        response: Dict[str, Any] = {"jsonrpc": "2.0", "id": request.get("id")}
        try:
            if method == "initialize":
                params = request.get("params") or {}
                response["result"] = {
                    "protocolVersion": params.get("protocolVersion") or MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "forge-cli-mcp", "version": "1.0.0"},
                }
            elif method == "tools/list":
                response["result"] = {"tools": advertised}
            elif method == "tools/call":
                params = request.get("params") or {}
                name = params.get("name")
                arguments = params.get("arguments") or {}
                check_tool_permission(str(name or ""), arguments, policy=policy)
                response["result"] = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                _call_tool(
                                    name,
                                    arguments,
                                    read_only=policy.read_only,
                                    allow_inline_credentials=policy.allow_inline_credentials,
                                ),
                                indent=2,
                                default=str,
                            ),
                        }
                    ],
                    "isError": False,
                }
            else:
                response["error"] = {"code": -32601, "message": f"Unknown method {method}"}
        except PermissionError as exc:
            logger.debug("mcp_permission_denied: %s", exc)
            response["error"] = {"code": -32001, "message": f"Permission denied: {exc}"}
        except Exception as exc:  # noqa: BLE001
            logger.debug("mcp_request_failed: %s", exc)
            response["error"] = {"code": -32000, "message": str(exc)}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()
    return 0


def _tool_definitions() -> List[Dict[str, Any]]:
    """Derive the tools/list advertisement from ``TOOL_CAPABILITIES`` so
    the registry remains the single source of truth.

    When a tool defines ``input_schema``, it is advertised under
    ``inputSchema`` (MCP spec key) so clients like Claude Code and
    Cursor can drive typed autocomplete for the tool's arguments.
    Tools without a schema fall back to a permissive empty-object
    advertisement — accepted by the spec, but unhelpful for editor UX.
    """
    out: List[Dict[str, Any]] = []
    for cap in TOOL_CAPABILITIES.values():
        entry: Dict[str, Any] = {"name": cap.name, "description": cap.description}
        if cap.input_schema is not None:
            entry["inputSchema"] = cap.input_schema
        else:
            # Empty-object schema is the lowest-friction default — same
            # semantics as today (any args accepted), but at least
            # explicit rather than implicit.
            entry["inputSchema"] = {"type": "object", "properties": {}}
        out.append(entry)
    return out


def _filter_visible_tools(tools: List[Dict[str, Any]], policy: McpPolicy) -> List[Dict[str, Any]]:
    """Drop tools that would always be rejected so upstream clients
    (Claude Code, Cursor) don't advertise options doomed to fail."""
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

    raise RuntimeError(f"Unknown tool {name}")


# ---------------------------------------------------------------------
# V1.5 source-catalog dispatch helpers.
# ---------------------------------------------------------------------


def _list_source_adapters() -> List[Dict[str, Any]]:
    """Enumerate the source-catalog adapters this build of forge-cli
    can dispatch to.

    The list is static — it reflects what code is shipped, not what
    the operator has configured. To list configured *credentials*
    (which catalogs the operator has actually set up), use the
    ``fluid ai status`` CLI surface (Sprint C). The MCP tool is
    deliberately inventory-only: it tells the LLM which catalog
    types are reachable, not which specific credentials are saved.

    Every adapter listed here is implemented in
    ``fluid_build.copilot.catalog.<name>`` and follows the 9
    patterns documented in ``catalog._patterns``. Future adapters
    (Apache Atlas, Alation, Microsoft Purview, …) get added here
    when they land — and inherit the same patterns automatically.
    """
    return [
        {"name": "snowflake", "status": "available"},
        {"name": "unity", "status": "available"},
        {"name": "bigquery", "status": "available"},
        {"name": "dataplex", "status": "available"},
        {"name": "glue", "status": "available"},
        {"name": "datahub", "status": "available"},
        {"name": "datamesh_manager", "status": "available"},
    ]


# Single source of truth for catalog dispatch. Every adapter
# implements ``CatalogAdapter.from_resolver`` so we just need the
# class reference here. New adapters land by adding one entry.
_SOURCE_ADAPTERS: Dict[str, str] = {
    "snowflake": "fluid_build.copilot.catalog.snowflake:SnowflakeCatalogAdapter",
    "unity": "fluid_build.copilot.catalog.unity:UnityCatalogAdapter",
    "bigquery": "fluid_build.copilot.catalog.bigquery:BigQueryCatalogAdapter",
    "dataplex": "fluid_build.copilot.catalog.dataplex:DataplexCatalogAdapter",
    "glue": "fluid_build.copilot.catalog.glue:GlueCatalogAdapter",
    "datahub": "fluid_build.copilot.catalog.datahub:DataHubCatalogAdapter",
    "datamesh_manager": (
        "fluid_build.copilot.catalog.datamesh_manager:DataMeshManagerCatalogAdapter"
    ),
}


def _import_adapter_class(dotted_path: str) -> Any:
    """Resolve ``module.path:ClassName`` into the actual class.

    Lazy import keeps ``fluid --help`` cold-start fast — only the
    requested adapter's module is imported. Pattern 4 applied at the
    dispatch layer.
    """
    module_path, class_name = dotted_path.split(":", 1)
    module = __import__(module_path, fromlist=[class_name])
    return getattr(module, class_name)


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
    if not source:
        supported = ", ".join(sorted(_SOURCE_ADAPTERS))
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
    if source not in _SOURCE_ADAPTERS:
        supported = ", ".join(sorted(_SOURCE_ADAPTERS))
        raise RuntimeError(
            f"Unknown source-catalog adapter: {source!r}. " f"Supported: {supported}."
        )
    resolver = CredentialResolver(
        allow_metadata_service=bool(arguments.get("allow_metadata_service", False))
    )
    adapter_cls = _import_adapter_class(_SOURCE_ADAPTERS[source])
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
