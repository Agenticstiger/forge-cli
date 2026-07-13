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
* ``cli``     — argparse ``run()`` + friendly guide.

---

Cold-path note (mirrors ``cli/forge.py``'s PEP 562 deferral). Importing this
package used to eagerly pull all five submodules, and ``models`` imports
``pydantic`` (~122 modules) + ``copilot.modeling_techniques`` (~81 modules) at
module scope. ``register_core_commands`` imports this package during
``build_parser()`` (to reach :func:`register`), so that eager chain landed
``pydantic`` on the ``fluid --help`` cold path.

The fix: the submodule re-exports are now resolved lazily via a module-level
``__getattr__`` (PEP 562), so ``from fluid_build.cli.mcp import McpPolicy`` (and
every other historical re-export) still works but imports the owning submodule
only on first *attribute access*. :func:`register` is defined here and builds
its argparse surface **without importing ``models`` / ``policy``** — the heavy
:func:`run` handler is deferred to command-execution time. The invariant is
pinned by ``tests/perf/test_startup_budget.py``.
"""

from __future__ import annotations

import argparse
import importlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — type-checkers only; never at runtime
    # Re-export the public/​tested names for static analysis without paying the
    # heavy import cost at runtime (the ``__getattr__`` below resolves them).
    from fluid_build.cli.mcp.dispatch import (  # noqa: F401
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
    from fluid_build.cli.mcp.models import (  # noqa: F401
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
    from fluid_build.cli.mcp.policy import (  # noqa: F401
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
    from fluid_build.cli.mcp.server import (  # noqa: F401
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

# Command name + default writable namespaces. These are trivial, stable
# constants that mirror ``models.COMMAND`` / ``models.DEFAULT_WRITABLE_NAMESPACES``
# but are inlined here so :func:`register` builds the parser WITHOUT importing
# the heavy ``models`` module (which drags ``pydantic`` +
# ``copilot.modeling_techniques`` onto the ``fluid --help`` cold path). The
# ``models`` values remain the canonical source of truth for the running server;
# ``tests/perf/test_startup_budget.py`` pins these two mirrors in sync.
_COMMAND = "mcp"
_DEFAULT_WRITABLE_NAMESPACES = ("history", "audit")


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``fluid mcp`` argparse surface.

    Cold-path safe: builds the parser using only argparse + the lightweight
    ``mcp_output_port`` helper, deferring the heavy :func:`run` handler (which
    needs ``policy`` → ``models`` → ``pydantic``) to command-execution time.

    Kept structurally in lock-step with :func:`fluid_build.cli.mcp.cli.register`
    (the canonical implementation, retained for the deferred ``run`` logic);
    ``tests/perf/test_startup_budget.py`` asserts the two produce an identical
    ``fluid mcp serve`` flag surface so this cold-path copy never drifts.
    """
    parser = subparsers.add_parser(_COMMAND, help="Serve staged forge tools over MCP stdio")
    # ``required=False`` so a bare ``fluid mcp`` doesn't blow up with the
    # bare-bones argparse "the following arguments are required: mcp_action"
    # error. ``run`` catches the ``mcp_action is None`` case and renders a
    # Rich-friendly panel describing the ``serve`` action.
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
            "to. Default: " + ",".join(_DEFAULT_WRITABLE_NAMESPACES) + "."
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
    # Attach the consumer-side output-port action group under the same
    # ``fluid mcp`` parent so operators see one MCP surface with two distinct
    # flavours (authoring vs consumption). ``mcp_output_port`` is lightweight
    # (argparse + ``_common``) and stays off the heavy import path.
    from fluid_build.cli.mcp_output_port import attach_to_mcp_subparsers

    attach_to_mcp_subparsers(sp)


def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Dispatch a ``fluid mcp`` invocation.

    Thin, cold-path-safe entry point: the real handler lives in
    :func:`fluid_build.cli.mcp.cli.run` and is imported lazily here so the
    ``policy`` → ``models`` → ``pydantic`` chain only loads when the command is
    actually executed (never during ``build_parser()`` / ``fluid --help``).
    """
    from fluid_build.cli.mcp.cli import run as _cli_run

    return _cli_run(args, logger)


# ---------------------------------------------------------------------------
# Lazy re-exports (PEP 562). ``name -> submodule`` for every symbol the historic
# eager ``from fluid_build.cli.mcp import X`` surface exposed. Resolved on first
# attribute access so the owning submodule (and its heavy deps) load lazily.
# ``register`` / ``run`` are defined above as real, cold-path-safe attributes.
# ---------------------------------------------------------------------------
_LAZY_EXPORTS = {
    # dispatch
    "_SOURCE_ADAPTERS": "dispatch",
    "_build_source_adapter": "dispatch",
    "_call_tool": "dispatch",
    "_dispatch_enrich_contract_suggestions": "dispatch",
    "_dispatch_forge_from_jdbc_source": "dispatch",
    "_dispatch_score_contract_quality": "dispatch",
    "_import_adapter_class": "dispatch",
    "_list_source_adapters": "dispatch",
    "_resolve_contract_argument": "dispatch",
    "_run_forge_inproc": "dispatch",
    "_scope_from_args": "dispatch",
    "write_audit_event": "dispatch",
    # models
    "COMMAND": "models",
    "DEFAULT_WRITABLE_NAMESPACES": "models",
    "MCP_PROTOCOL_VERSION": "models",
    "TOOL_CAPABILITIES": "models",
    "CredentialsArg": "models",
    "ScopeArg": "models",
    "get_sampling_context": "models",
    "_reset_sampling_context": "models",
    "_set_sampling_context": "models",
    # policy
    "McpPolicy": "policy",
    "check_tool_permission": "policy",
    "_tool_definitions": "policy",
    "_filter_visible_tools": "policy",
    "_build_policy_from_args": "policy",
    "_current_policy": "policy",
    "_set_policy": "policy",
    "_policy": "policy",
    "_path_is_writable": "policy",
    # server — NB ``_mcp_app`` is resolved from ``server`` (the lazy ``None``
    # global), NOT ``policy`` (whose ``_mcp_app`` __getattr__ shim would BUILD
    # the FastMCP app and re-pull the MCP server SDK onto import — see #265).
    "_get_mcp_app": "server",
    "_mcp_app": "server",
    "read_logical_model": "server",
    "update_entity": "server",
    "add_relationship": "server",
    "regenerate_physical": "server",
    "validate_contract": "server",
    "diff_models": "server",
    "search_semantic_memory": "server",
    "list_source_adapters": "server",
    "list_source_tables": "server",
    "inspect_source_table": "server",
    "list_source_lineage": "server",
    "list_source_glossary": "server",
    "forge_from_source": "server",
    "forge_run": "server",
    "score_contract_quality": "server",
    "enrich_contract_suggestions": "server",
}


def __getattr__(name: str):
    """Lazily resolve a re-exported submodule symbol (PEP 562).

    Keeps ``pydantic`` + ``copilot.modeling_techniques`` (via ``models``) and
    the MCP server SDK (via ``server``) off the ``fluid --help`` cold path while
    preserving every historic ``from fluid_build.cli.mcp import X`` call site.
    Resolution is fresh each call (``sys.modules`` caches the submodule, and
    ``getattr`` on it is O(1)) so live values such as ``server._mcp_app`` are
    never shadowed by a stale snapshot.
    """
    submodule = _LAZY_EXPORTS.get(name)
    if submodule is not None:
        module = importlib.import_module(f"fluid_build.cli.mcp.{submodule}")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list:
    return sorted(set(globals()) | set(_LAZY_EXPORTS) | {"register", "run"})


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
