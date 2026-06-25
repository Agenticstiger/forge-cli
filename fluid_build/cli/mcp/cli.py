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
MCP CLI entry point — argparse registration and ``run()``.

Split from ``fluid_build.cli.mcp`` (issue #11) to reduce the 2 446-line
monolith into focused, testable modules.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from fluid_build.cli.mcp.models import COMMAND, DEFAULT_WRITABLE_NAMESPACES
from fluid_build.cli.mcp.policy import _build_fastmcp_app, _build_policy_from_args, _set_policy


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
