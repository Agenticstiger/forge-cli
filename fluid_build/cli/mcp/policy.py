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
MCP access-control policy — tool visibility filtering, permission gating,
and the ``McpPolicy`` dataclass.

Split from ``fluid_build.cli.mcp`` (issue #11) to reduce the 2 446-line
monolith into focused, testable modules.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fluid_build.cli.mcp.models import (
    DEFAULT_WRITABLE_NAMESPACES,
    TOOL_CAPABILITIES,
)


# ``_mcp_app`` is the FastMCP app; it lives in server.py and is built lazily
# (preserves #265 — importing the package must not load the MCP server SDK).
# Accessing ``policy._mcp_app`` triggers the build via ``_get_mcp_app()`` and
# caches the result. We resolve it lazily to break the import cycle:
# policy.py -> server.py (on first access) -> policy.py (already loaded).
def __getattr__(name: str):
    if name == "_mcp_app":
        from fluid_build.cli.mcp.server import _get_mcp_app

        app = _get_mcp_app()
        global _mcp_app
        _mcp_app = app
        return _mcp_app
    raise AttributeError(name)


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


def _filter_visible_tools(tools: List[Dict[str, Any]], policy: McpPolicy) -> List[Dict[str, Any]]:
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
            # Resolve the root for behavioural parity with the pre-split
            # monolith: ``target`` is already resolved by the caller, so a
            # raw (symlinked / non-resolved) root would otherwise reject a
            # legitimately-writable path. Resolving keeps the gate exactly as
            # permissive as before — no looser.
            target.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def _path_is_writable(target: Path, writable_roots: Tuple[Path, ...]) -> bool:
    """Return True if *target* is at or under one of *writable_roots*.

    The path must also be a child of the read root (read-only paths are
    not writable even if they're explicitly in writable_paths).
    """
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


# ---------------------------------------------------------------------------
# FastMCP app lives in server.py (where @tool() decorators run at import time).
# Accessed here via __getattr__ lazy import (see top of module).
# ---------------------------------------------------------------------------

_current_policy: Optional[McpPolicy] = None


def _set_policy(policy: McpPolicy) -> None:
    """Install the active policy for this connection.

    Tools read the policy via :func:`_policy` to gate access. Single-stdio-
    connection scope, so module-level is safe.
    """
    global _current_policy
    _current_policy = policy


def _policy() -> McpPolicy:
    """Return the active MCP policy. Raises if not yet set."""
    if _current_policy is None:
        raise RuntimeError("MCP policy not initialised — call _set_policy first")
    return _current_policy


def _build_fastmcp_app(policy: McpPolicy) -> Any:
    """Return the FastMCP app, after pruning tools the policy hides.

    Tools that are denied (``--deny-tools``) or that would always be rejected
    (mutating tools under ``--read-only``) are removed from the SDK's tool
    registry so they never appear in ``tools/list``.
    """
    # _mcp_app lives in server.py and is built lazily (preserves #265).
    # Go through the lazy builder so the SDK is imported only here, at serve
    # time — never at package-import time.
    from fluid_build.cli.mcp.server import _get_mcp_app

    app = _get_mcp_app()
    for name in list(TOOL_CAPABILITIES.keys()):
        cap = TOOL_CAPABILITIES[name]
        needs_write = cap.mutates_files or bool(cap.writes_namespaces)
        denied = not policy.is_tool_allowed(name)
        read_only_blocked = policy.read_only and needs_write
        if denied or read_only_blocked:
            try:
                app.remove_tool(name)
            except Exception:  # noqa: BLE001
                pass
    return app
