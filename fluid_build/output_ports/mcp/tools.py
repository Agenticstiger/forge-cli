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

"""Tool capability registry + tool-derivation logic for the consumer
MCP output-port server.

The Phase-1 surface advertises four tools — three always-on
(``describe``, ``sample``, ``query``) and one gated
(``query_sql``, only when the policy sets
``allow_free_form_sql=True``). Tool capabilities are declared once
in :data:`OUTPUT_PORT_TOOL_CAPABILITIES` and the dispatch layer in
:mod:`fluid_build.output_ports.mcp.server` reads them as the single
source of truth for permission checks and ``tools/list`` rendering.

Why this is a separate registry from the authoring server's
``TOOL_CAPABILITIES`` (in :mod:`fluid_build.cli.mcp`):

* Different threat model — authoring writes filesystem paths and
  store namespaces; the consumer side queries production data and
  never writes the caller's filesystem.
* Different permission knobs — authoring needs ``writable_paths`` /
  ``writable_namespaces``; consumer needs ``allow_free_form_sql`` /
  ``max_sample_rows``.
* Different lifecycle — authoring tools are global; consumer tools
  are derived from the bound expose's ``kind``, ``contract.schema``,
  and ``semantics`` so the advertised surface is precise.

Sharing a registry would dilute both. Two registries with the same
shape (:class:`ToolCapability`) is the right balance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple


@dataclass(frozen=True)
class ToolCapability:
    """Declarative metadata for one MCP tool advertised by the
    consumer output-port server.

    Mirrors the shape of the authoring-side
    ``fluid_build.cli.mcp.ToolCapability`` so operators familiar with
    one server have zero surprises with the other. Only the fields
    relevant to the consumer side are kept — namespace fields and
    write-path fields are deliberately absent.
    """

    name: str
    """Tool identifier as advertised in ``tools/list``."""

    description: str
    """Short, human-readable description shown to MCP clients."""

    requires_sql_allowlist: bool = False
    """When true, the tool only renders if
    :attr:`OutputPortPolicy.allow_free_form_sql` is set."""

    input_schema: Optional[Dict[str, Any]] = None
    """Optional JSON Schema describing the tool's ``arguments``.

    Mirrors the MCP ``inputSchema`` field; when present, advertised
    in ``tools/list`` so MCP clients (Claude Code, Cursor) can drive
    typed autocomplete on the tool's arguments.
    """


# ---------------------------------------------------------------------
# Reusable JSON-Schema fragments for tool input schemas.
#
# Kept short and explicit. Every argument has a description an LLM
# agent can read; ``additionalProperties: false`` blocks typos from
# silently passing through.
# ---------------------------------------------------------------------

_LIMIT_PROP = {
    "type": "integer",
    "minimum": 1,
    "description": (
        "Number of rows to return. Capped server-side by --max-sample-rows; "
        "asking for more than the cap returns the cap silently."
    ),
}

_FILTER_PROP = {
    "type": "object",
    "description": (
        "Equality filters keyed by dimension name. Each value MUST be "
        "a scalar (string, number, boolean) and MUST appear in the "
        "expose's semantic dimensions or contract.schema. Server "
        "rejects unknown keys to keep the surface narrow."
    ),
    "additionalProperties": True,
}


_DESCRIBE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "description": (
        "Returns the bound expose's metadata: schema, semantic model, "
        "QoS, classification, lineage hints. No arguments — the server "
        "is bound to one expose at startup."
    ),
}

_SAMPLE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": _LIMIT_PROP,
    },
    "additionalProperties": False,
}

_QUERY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "metric": {
            "type": "string",
            "description": (
                "Name of a metric defined in expose.semantics.metrics. "
                "Mutually exclusive with 'measure' — pick one. Resolved "
                "to a measure (or measure expression) by the server."
            ),
        },
        "measure": {
            "type": "string",
            "description": (
                "Name of a measure defined in expose.semantics.measures. "
                "Mutually exclusive with 'metric'."
            ),
        },
        "dimensions": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Dimension names defined in expose.semantics.dimensions "
                "or column names from contract.schema. The query GROUP BYs "
                "every entry."
            ),
        },
        "filters": _FILTER_PROP,
        "limit": _LIMIT_PROP,
    },
    "additionalProperties": False,
}

_QUERY_SQL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "sql": {
            "type": "string",
            "description": (
                "SELECT statement against the bound expose. Server "
                "rewrites the table reference to the contract's binding "
                "location and runs sql_safety.validate_sql_expression_allowlist "
                "on the body. Refused unless the server was started "
                "with --allow-sql."
            ),
        },
        "limit": _LIMIT_PROP,
    },
    "required": ["sql"],
    "additionalProperties": False,
}


OUTPUT_PORT_TOOL_CAPABILITIES: Dict[str, ToolCapability] = {
    "describe": ToolCapability(
        name="describe",
        description=(
            "Return the bound expose's metadata: schema, semantic model, "
            "QoS, policy, binding. No arguments; no engine round-trip."
        ),
        input_schema=_DESCRIBE_SCHEMA,
    ),
    "sample": ToolCapability(
        name="sample",
        description=(
            "Return up to --max-sample-rows rows of the bound expose. "
            "Restricted columns are masked."
        ),
        input_schema=_SAMPLE_SCHEMA,
    ),
    "query": ToolCapability(
        name="query",
        description=(
            "Run a predeclared semantic query. Pick a metric or measure "
            "from expose.semantics, group by zero or more dimensions, "
            "optionally filter on dimension keys. The server compiles "
            "to parameterised SQL — preferred over query_sql."
        ),
        input_schema=_QUERY_SCHEMA,
    ),
    "query_sql": ToolCapability(
        name="query_sql",
        description=(
            "Run caller-supplied SELECT SQL against the bound expose. "
            "Available only when the server is started with --allow-sql. "
            "Refuses any statement that references a restricted column."
        ),
        requires_sql_allowlist=True,
        input_schema=_QUERY_SQL_SCHEMA,
    ),
}


def derive_advertised_tools(
    *,
    expose: Mapping[str, Any],
    allow_free_form_sql: bool,
    extra_denied: Tuple[str, ...] = (),
) -> List[Dict[str, Any]]:
    """Render the ``tools/list`` advertisement for the bound expose.

    The returned list is the JSON shape MCP clients see — name,
    description, optional ``inputSchema``. Tools that would always be
    rejected by the policy or by the contract shape are dropped so
    MCP clients (Claude Code, Cursor) don't advertise calls doomed
    to fail. Specifically:

    * ``query_sql`` is hidden when ``allow_free_form_sql`` is False.
    * ``query`` is hidden when the expose has no ``semantics``
      block — the tool can't compile a useful statement without
      predeclared measures/metrics/dimensions, so showing it would
      just generate retry loops.
    * Anything in ``extra_denied`` is dropped regardless.
    """
    semantics = expose.get("semantics") or {}
    has_semantics = bool(
        semantics.get("metrics") or semantics.get("measures") or semantics.get("dimensions")
    )
    advertised: List[Dict[str, Any]] = []
    for cap in OUTPUT_PORT_TOOL_CAPABILITIES.values():
        if cap.name in extra_denied:
            continue
        if cap.requires_sql_allowlist and not allow_free_form_sql:
            continue
        if cap.name == "query" and not has_semantics:
            continue
        entry: Dict[str, Any] = {"name": cap.name, "description": cap.description}
        if cap.input_schema is not None:
            entry["inputSchema"] = cap.input_schema
        else:
            entry["inputSchema"] = {"type": "object", "properties": {}}
        advertised.append(entry)
    return advertised


def check_tool_permission(
    tool: str,
    *,
    allowed_tools: Optional[Tuple[str, ...]],
    denied_tools: Tuple[str, ...],
    allow_free_form_sql: bool,
) -> None:
    """Raise :class:`PermissionError` when ``tool`` is denied.

    Order of checks:

    1. Unknown tool → ``RuntimeError`` (programming error, not auth).
    2. Tool allow/deny list (deny wins).
    3. Free-form-SQL gate for tools that opt into it.
    """
    cap = OUTPUT_PORT_TOOL_CAPABILITIES.get(tool)
    if cap is None:
        raise RuntimeError(f"Unknown tool {tool}")
    if tool in denied_tools:
        raise PermissionError(f"Tool {tool!r} is in the deny list")
    if allowed_tools is not None and tool not in allowed_tools:
        raise PermissionError(f"Tool {tool!r} not in allowlist")
    if cap.requires_sql_allowlist and not allow_free_form_sql:
        raise PermissionError(
            f"Tool {tool!r} requires --allow-sql; the server was started without it"
        )
