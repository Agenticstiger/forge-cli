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

"""Tool-use scaffolding for staged agents (E14).

Today every staged agent does one LLM call with a static prompt.
World-class agentic systems let agents call tools mid-draft —
e.g. BuilderAgent calls ``inspect_table`` against the catalog
while drafting, ModelerAgent retrieves from ``memory/semantic``
mid-prompt instead of seeded once at start.

This module ships the registry primitive. Agents that adopt
tool-use:

1. Register tools at construction via :class:`ToolRegistry`.
2. Pass the registry to :meth:`BaseStageAgent.call` (when the
   v1.6 multi-turn agent loop lands).
3. Each tool is a typed Pydantic-validated function that the
   LLM can invoke.

The registry is **opt-in**: agents without registered tools
fall through to the existing single-call path. v1.5 ships the
registry + the three default catalog tools; v1.6 wires them
into the modeler's agent loop.

Public surface:

* :class:`Tool` — typed tool definition (name, schema, callable).
* :class:`ToolRegistry` — per-agent tool catalog.
* :class:`ToolInvocation` — record of one tool call (for audit).
* :func:`build_default_tool_registry` — Snowflake / Unity / DataHub
  inspect tools the modeler can use.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Tool:
    """One callable tool an agent can invoke mid-draft.

    ``name`` is the LLM-facing name (must be a valid identifier).
    ``description`` is a one-line LLM-readable summary so the
    model knows when to call. ``input_schema`` is a JSON Schema
    describing the arguments object — same shape MCP advertises.
    ``handler`` is the Python function called with the validated
    arguments.

    The handler returns a JSON-serialisable dict. Errors raised
    by the handler are caught by the registry and surfaced as
    ``{"error": str(exc)}`` so the LLM can see what went wrong
    and retry with different arguments.
    """

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[..., Any]

    def call(self, arguments: Dict[str, Any]) -> Any:
        """Invoke the handler with validated arguments.

        Best-effort exception handling: a handler that raises
        returns ``{"error": "<exception message>"}`` so the LLM
        can incorporate the failure into its next turn.
        """
        try:
            sig = inspect.signature(self.handler)
            # Filter args to only those the handler accepts.
            accepted = {k: v for k, v in arguments.items() if k in sig.parameters}
            return self.handler(**accepted)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}


@dataclass
class ToolInvocation:
    """Record of one tool call — for audit + telemetry."""

    tool_name: str
    arguments: Dict[str, Any]
    result: Any
    success: bool


@dataclass
class ToolRegistry:
    """Per-agent tool catalog.

    Agents that want tool-use construct one of these and pass it
    to :meth:`BaseStageAgent.call` (v1.6 wiring). Today the
    registry is the structural primitive; the multi-turn LLM
    loop that consumes it lands in v1.6.
    """

    tools: Dict[str, Tool] = field(default_factory=dict)
    invocations: List[ToolInvocation] = field(default_factory=list)

    def register(self, tool: Tool) -> None:
        if not tool.name.replace("_", "").isalnum():
            raise ValueError(
                f"Tool name {tool.name!r} must be a valid identifier "
                "(letters / digits / underscore only)."
            )
        self.tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self.tools.get(name)

    def list_for_llm(self) -> List[Dict[str, Any]]:
        """Export the registered tools in the shape an LLM
        provider's tool-use API expects.

        Same shape MCP's ``tools/list`` advertises so the
        BUILTIN_LLM_PROVIDERS' ``build_tool_request`` can pass
        the list through unchanged."""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self.tools.values()
        ]

    def invoke(self, name: str, arguments: Dict[str, Any]) -> Any:
        """Look up a tool by name and call it. Records the
        invocation on ``invocations`` for audit."""
        tool = self.tools.get(name)
        if tool is None:
            result = {"error": f"unknown tool: {name!r}"}
            success = False
        else:
            result = tool.call(arguments)
            success = not (isinstance(result, dict) and "error" in result)
        self.invocations.append(
            ToolInvocation(
                tool_name=name,
                arguments=arguments,
                result=result,
                success=success,
            )
        )
        return result


def build_default_tool_registry(
    *,
    catalog_adapter: Optional[Any] = None,
    store: Optional[Any] = None,
) -> ToolRegistry:
    """Build the default tool catalog for v1.5+ staged agents.

    When a catalog adapter is available, register catalog
    introspection tools (``inspect_table``, ``list_tables``,
    ``get_lineage``). When a store is available, register
    semantic-memory retrieval (``search_semantic_memory``).

    Both are best-effort — missing inputs simply produce a
    smaller registry. The modeler's tool loop (v1.6) checks
    ``registry.tools`` to pick which tools the LLM can call.
    """
    registry = ToolRegistry()

    if catalog_adapter is not None:
        registry.register(
            Tool(
                name="inspect_table",
                description=(
                    "Return full metadata for one fully-qualified table. "
                    "Use during modelling when the intent mentions a table by "
                    "name and you need its columns / classifications."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"fqn": {"type": "string"}},
                    "required": ["fqn"],
                    "additionalProperties": False,
                },
                handler=lambda fqn: catalog_adapter.get_table(fqn).model_dump(),
            )
        )
        registry.register(
            Tool(
                name="list_tables",
                description=(
                    "Enumerate tables under a catalog scope. Use when the "
                    "intent refers to a database / schema by name and you "
                    "want to know which tables exist before referencing them."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "database": {"type": "string"},
                        "schema": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                handler=lambda **kwargs: [
                    t.model_dump() for t in catalog_adapter.list_tables(_make_scope(**kwargs))
                ],
            )
        )

    if store is not None:
        registry.register(
            Tool(
                name="search_semantic_memory",
                description=(
                    "Search the memory/semantic namespace for prior forged "
                    "models similar to the current draft. Use when an entity "
                    "or relationship feels familiar — there may be a past "
                    "model that can be a starting point."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                            "default": 3,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                handler=lambda query, limit=3: [
                    {"key": getattr(r, "key", None), "value": getattr(r, "value", None)}
                    for r in (
                        store.search("memory/semantic", query, mode="hybrid", limit=limit) or []
                    )
                ],
            )
        )

    return registry


def _make_scope(**kwargs):
    """Local helper: build a CatalogScope from kwargs without
    importing at module level (lazy)."""
    from fluid_build.copilot.catalog.models import CatalogScope

    return CatalogScope(**{k: v for k, v in kwargs.items() if v is not None})


__all__ = [
    "Tool",
    "ToolInvocation",
    "ToolRegistry",
    "build_default_tool_registry",
]
