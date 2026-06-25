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
MCP stdio server for staged forge model operations.

The server exposes fourteen tools over MCP stdio and applies a four-layer
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

---

Package structure (split from monolithic ``mcp.py`` per issue #11):

* ``models``   — ToolCapability registry, Pydantic argument envelopes, constants.
* ``policy``   — McpPolicy dataclass, permission gating, policy builder.
* ``server``  — FastMCP app and 14 async tool registrations.
* ``dispatch`` — sync ``_call_tool`` and all dispatch helpers.
* ``cli``     — argparse ``register()`` + ``run()``.

All names are re-exported here so existing imports
(``from fluid_build.cli.mcp import McpPolicy``) keep working unchanged.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Re-export everything from the package modules for backward compatibility.
# ``from fluid_build.cli.mcp import X`` resolves to this file (the package
# __init__.py), which forwards to the appropriate submodule.
# ---------------------------------------------------------------------------
from fluid_build.cli.mcp.cli import register, run
from fluid_build.cli.mcp.dispatch import (
    _SOURCE_ADAPTERS,
    _build_source_adapter,
    _call_tool,
    _dispatch_enrich_contract_suggestions,
    _dispatch_forge_from_jdbc_source,
    _dispatch_score_contract_quality,
    _import_adapter_class,
    _list_source_adapters,
    _resolve_contract_argument,
    _run_forge_inproc,
    _scope_from_args,
    write_audit_event,
)
from fluid_build.cli.mcp.models import (
    COMMAND,
    DEFAULT_WRITABLE_NAMESPACES,
    MCP_PROTOCOL_VERSION,
    TOOL_CAPABILITIES,
    CredentialsArg,
    ScopeArg,
    _reset_sampling_context,
    _set_sampling_context,
    get_sampling_context,
)
from fluid_build.cli.mcp.policy import (
    McpPolicy,
    _build_policy_from_args,
    _current_policy,
    _filter_visible_tools,
    _path_is_writable,
    _policy,
    _set_policy,
    _tool_definitions,
    check_tool_permission,
)

# NB: ``_mcp_app`` + ``_get_mcp_app`` are imported from ``server`` (NOT
# ``policy``). ``server._mcp_app`` is the lazy ``None`` global — importing it
# here does NOT build the FastMCP app, so the MCP server SDK stays off the
# package-import / ``fluid --help`` hot path (#265). ``policy._mcp_app`` is a
# ``__getattr__`` shim that WOULD build the app on access; routing through it
# at package import would re-introduce the eager-SDK regression.
from fluid_build.cli.mcp.server import (
    _get_mcp_app,
    _mcp_app,
    add_relationship,
    diff_models,
    enrich_contract_suggestions,
    forge_from_source,
    forge_run,
    inspect_source_table,
    list_source_adapters,
    list_source_glossary,
    list_source_lineage,
    list_source_tables,
    read_logical_model,
    regenerate_physical,
    score_contract_quality,
    search_semantic_memory,
    update_entity,
    validate_contract,
)

__all__ = [
    # CLI
    "register",
    "run",
    # Constants
    "COMMAND",
    "MCP_PROTOCOL_VERSION",
    "DEFAULT_WRITABLE_NAMESPACES",
    # Models
    "TOOL_CAPABILITIES",
    "CredentialsArg",
    "ScopeArg",
    "get_sampling_context",
    "_reset_sampling_context",
    "_set_sampling_context",
    # Policy
    "McpPolicy",
    "check_tool_permission",
    "_tool_definitions",
    "_filter_visible_tools",
    "_build_policy_from_args",
    "_mcp_app",
    "_get_mcp_app",
    "_current_policy",
    "_set_policy",
    "_policy",
    "_path_is_writable",
    # Tools (re-exported from server)
    "read_logical_model",
    "update_entity",
    "add_relationship",
    "regenerate_physical",
    "validate_contract",
    "diff_models",
    "search_semantic_memory",
    "list_source_adapters",
    "list_source_tables",
    "inspect_source_table",
    "list_source_lineage",
    "list_source_glossary",
    "forge_from_source",
    "forge_run",
    "score_contract_quality",
    "enrich_contract_suggestions",
    # Dispatch
    "_call_tool",
    "_dispatch_score_contract_quality",
    "_dispatch_enrich_contract_suggestions",
    "_dispatch_forge_from_jdbc_source",
    "_list_source_adapters",
    "_SOURCE_ADAPTERS",
    "_import_adapter_class",
    "_scope_from_args",
    "_resolve_contract_argument",
    "_run_forge_inproc",
    "_build_source_adapter",
    "write_audit_event",
]
