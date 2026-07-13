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

"""Opt-in tool-search / deferred tool loading for the forge agent loop.

As the copilot's native tool set grows (workspace discovery, composition,
sampling, cost, memory, plus the opt-in web / dbt-MCP / live-DB delegates), the
full ``tools`` array shipped on every turn balloons — each tool's JSON-Schema
parameter block is the bulk of it, and with 15-30 tools that is thousands of
tokens burned per turn before the conversation even starts.

This module implements the **tool-search / deferred-loading** pattern so a large
tool set is discovered/loaded on demand instead of advertised all-eager. It is
**off by default** and only activates when the operator opts in with
``FLUID_FORGE_TOOL_SEARCH=1`` — when off, ``apply_tool_search`` is a pure no-op
and the tool listing is byte-for-byte unchanged.

When on, ``apply_tool_search`` rewrites the tool listing into:

* a small **CORE** set advertised with their FULL schema (the tools the model
  almost always needs first — workspace discovery + the propose/validate loop);
* every other tool as a lightweight **STUB** — its name, a one-line namespace
  hint, and a permissive parameter schema — so the model still knows the tool
  exists and can call it, but the heavy per-field schema is NOT paid up front;
* one **``search_tools``** meta-tool the model calls to load a deferred tool's
  full description + parameter schema on demand.

Borrow-before-build (see the PR body for the full search log):
  * The mechanism mirrors OpenAI's Agents-SDK **ToolSearchTool + defer_loading**
    (``@function_tool(defer_loading=True)`` advertises "only the tool's name in
    the prompt — no description, no parameter schema"; ``tool_namespace(...)``
    groups related tools; exactly one ``ToolSearchTool()`` lets the model load
    deferred tools/namespaces on demand). OpenAI runs that server-side on the
    Responses API; we realise the same shape **client-side** so it works across
    every provider (Anthropic / OpenAI-completions / Gemini) the forge loop
    already speaks — the deliberate divergence is that a stub keeps a *permissive*
    schema (a normal tool-use API requires one for a tool to be callable) and a
    one-line namespace hint (materially better model guidance at a tiny token
    cost); the real args are validated at dispatch by the tool's own Pydantic
    schema.
  * Loading + gating follow this repo's PEP-562 lazy-import conventions and the
    env-gated dbt-MCP / web-tools / live-DB delegate shape (``is_enabled`` reads
    the flag; the transform is a no-op when off). The module is pure stdlib and
    is imported function-locally from ``forge_copilot_tools`` so the
    ``fluid --help`` cold path is untouched.

Env vars:

* ``FLUID_FORGE_TOOL_SEARCH`` — ``1``/``true`` to activate deferred loading
  (default off → listing unchanged).
* ``FLUID_FORGE_TOOL_SEARCH_CORE`` — comma-separated tool names to use as the
  CORE (always-full) set instead of the built-in default.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Mapping, Optional

LOG = logging.getLogger("fluid.cli.forge_tool_search")

_TRUTHY = {"1", "true", "yes", "on"}

# The meta-tool the model calls to load deferred tools on demand.
SEARCH_TOOL_NAME = "search_tools"

# The default CORE set: the tools the model reaches for first on almost every
# run — workspace discovery + the propose/validate authoring loop. Everything
# else is deferred until the model searches for it. Override via
# FLUID_FORGE_TOOL_SEARCH_CORE.
DEFAULT_CORE_TOOLS = frozenset(
    {
        "discover_workspace",
        "list_templates",
        "propose_contract",
        "validate_contract",
        "discover_workspace_contracts",
    }
)

# Namespace grouping (OpenAI's tool_namespace analog): drives the stub hint and
# the search-by-namespace filter. Unknown tools fall back to "other"; the
# ``dbt.``-prefixed delegated MCP tools all group under "dbt".
_NAMESPACES: Dict[str, str] = {
    "discover_workspace": "discovery",
    "list_templates": "discovery",
    "list_schedulers": "discovery",
    "discover_workspace_contracts": "composition",
    "read_upstream_schema": "composition",
    "check_pii_classification": "composition",
    "read_logical_model": "composition",
    "propose_contract": "authoring",
    "validate_contract": "authoring",
    "generate_dlt_source": "authoring",
    "read_sample_schema": "sampling",
    "fetch_sample_rows": "sampling",
    "search_semantic_memory": "memory",
    "estimate_cost": "cost",
    "web_search": "web",
    "web_fetch": "web",
    "forge_data_model": "modeling",
}

# A deferred stub advertises a permissive schema — a normal tool-use API needs
# *some* schema for the tool to be callable, and the real validation happens at
# dispatch via the tool's Pydantic args model.
_STUB_SCHEMA: Dict[str, Any] = {"type": "object", "additionalProperties": True}


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------
def is_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when deferred tool loading is opted in via ``FLUID_FORGE_TOOL_SEARCH``."""
    env = env if env is not None else os.environ
    return str(env.get("FLUID_FORGE_TOOL_SEARCH", "")).strip().lower() in _TRUTHY


def is_search_tool(name: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """True when *name* is the search meta-tool and the pattern is enabled."""
    return bool(name) and name == SEARCH_TOOL_NAME and is_enabled(env)


def namespace_for(name: str) -> str:
    """Return the namespace label for a tool name."""
    if name.startswith("dbt."):
        return "dbt"
    return _NAMESPACES.get(name, "other")


def _core_tools(env: Mapping[str, str]) -> set:
    """Resolve the CORE (always-full) tool set from the env or the default."""
    raw = (env.get("FLUID_FORGE_TOOL_SEARCH_CORE") or "").strip()
    if raw:
        return {part.strip() for part in raw.split(",") if part.strip()}
    return set(DEFAULT_CORE_TOOLS)


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------
def _short_hint(description: str) -> str:
    """First clause of a tool description, bounded — cheap model guidance."""
    text = (description or "").strip().replace("\n", " ")
    # Cut at the first sentence boundary, then hard-cap.
    for sep in (". ", "; "):
        idx = text.find(sep)
        if 0 < idx < 120:
            text = text[:idx]
            break
    return text[:120].strip()


def _stub_for(definition: Dict[str, Any]) -> Dict[str, Any]:
    """Build the lightweight deferred stub for one tool definition."""
    name = definition.get("name", "")
    ns = namespace_for(name)
    hint = _short_hint(definition.get("description", ""))
    return {
        "name": name,
        "description": (
            f"[deferred · namespace={ns}] {hint} "
            f"Call {SEARCH_TOOL_NAME} to load its full description and parameters before use."
        ).strip(),
        "input_schema": dict(_STUB_SCHEMA),
    }


def _search_tool_definition(deferred: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the ``search_tools`` meta-tool def, listing what can be loaded."""
    namespaces = sorted({namespace_for(d.get("name", "")) for d in deferred})
    names = sorted(d.get("name", "") for d in deferred)
    return {
        "name": SEARCH_TOOL_NAME,
        "description": (
            "Discover and load deferred tools on demand. To save context, many "
            "tools are advertised as name-only stubs; call this to get a deferred "
            "tool's full description and parameter schema BEFORE you call it. "
            "Filter with a free-text 'query' or a 'namespace'; call with no "
            "arguments to list every namespace. "
            f"Available namespaces: {', '.join(namespaces)}. "
            f"Deferred tools: {', '.join(names)}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Free-text match against deferred tool names, "
                        "descriptions, and namespaces."
                    ),
                },
                "namespace": {
                    "type": "string",
                    "description": "Return every deferred tool in this namespace.",
                },
            },
            "additionalProperties": False,
        },
    }


def apply_tool_search(
    definitions: List[Dict[str, Any]],
    env: Optional[Mapping[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Rewrite a full tool listing into core-full + deferred-stub + search_tools.

    A pure no-op (returns ``definitions`` unchanged) when the pattern is
    disabled, so callers can wrap ``get_tool_definitions`` unconditionally.
    """
    if not is_enabled(env):
        return definitions
    core = _core_tools(env if env is not None else os.environ)

    eager: List[Dict[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    for definition in definitions:
        name = definition.get("name", "")
        if not name or name == SEARCH_TOOL_NAME:
            # Never defer (or duplicate) the search tool itself.
            continue
        if name in core:
            eager.append(definition)
        else:
            deferred.append(definition)

    if not deferred:
        # Nothing to defer — advertising search_tools would be pointless noise.
        return definitions

    stubs = [_stub_for(d) for d in deferred]
    return eager + [_search_tool_definition(deferred)] + stubs


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def dispatch_search_tool(
    arguments: Optional[Dict[str, Any]],
    *,
    all_definitions: List[Dict[str, Any]],
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Resolve a ``search_tools`` call against the full (pre-transform) listing.

    ``all_definitions`` is the untransformed tool listing (so the FULL parameter
    schemas are available to return). Core tools are excluded from results —
    they are already advertised in full, so searching for them is redundant.
    """
    args = arguments or {}
    core = _core_tools(env if env is not None else os.environ)
    deferred = [
        d
        for d in all_definitions
        if d.get("name") and d.get("name") not in core and d.get("name") != SEARCH_TOOL_NAME
    ]

    query = str(args.get("query") or "").strip().lower()
    namespace = str(args.get("namespace") or "").strip().lower()

    if not query and not namespace:
        # No filter → return the catalogue so the model can pick a namespace.
        catalogue: Dict[str, List[str]] = {}
        for d in deferred:
            catalogue.setdefault(namespace_for(d["name"]), []).append(d["name"])
        return {
            "namespaces": {k: sorted(v) for k, v in sorted(catalogue.items())},
            "hint": "Call search_tools again with a 'query' or 'namespace' to load full schemas.",
        }

    matches: List[Dict[str, Any]] = []
    for d in deferred:
        name = d["name"]
        ns = namespace_for(name)
        if namespace:
            if ns == namespace:
                matches.append(d)
            continue
        haystack = f"{name} {d.get('description', '')} {ns}".lower()
        if query in haystack:
            matches.append(d)

    return {"tools": matches, "count": len(matches)}
