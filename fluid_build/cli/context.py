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

from __future__ import annotations

import argparse
import json
import logging
import os

from fluid_build.cli.console import cprint

from ._common import CLIError
from ._logging import info

COMMAND = "config"
CTX_PATH = ".fluid/context.json"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(COMMAND, help="Get/Set default provider/project/region")
    # ``required=False`` so a bare ``fluid config`` doesn't blow up
    # with the bare-bones argparse "the following arguments are
    # required: verb" error.  ``run`` catches the ``args.verb is None``
    # case and renders a Rich-friendly panel.
    sp = p.add_subparsers(dest="verb", required=False)
    sp.add_parser("list", help="Show current context")
    g = sp.add_parser("set", help="Set a key")
    g.add_argument("key", choices=["provider", "project", "region"], help="Key")
    g.add_argument("value", help="Value")
    sp.add_parser("get", help="Get a key").add_argument("key")
    p.set_defaults(cmd=COMMAND, func=run)


def _read():
    try:
        with open(CTX_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _write(d):
    os.makedirs(os.path.dirname(CTX_PATH), exist_ok=True)
    with open(CTX_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def run(args, logger: logging.Logger) -> int:
    try:
        verb = getattr(args, "verb", None)
        if verb is None:
            # Bare ``fluid config`` / ``fluid context`` — render an
            # intuitive guide instead of the legacy argparse error.
            return _render_context_guide()
        ctx = _read()
        if verb == "list":
            cprint(json.dumps(ctx, indent=2))
            return 0
        if verb == "get":
            cprint(ctx.get(args.key))
            return 0
        if verb == "set":
            ctx[args.key] = args.value
            _write(ctx)
            info(logger, "context_set", key=args.key, value=args.value)
            return 0
        return 2
    except Exception as e:
        raise CLIError(1, "context_failed", {"error": str(e)})


def _render_context_guide() -> int:
    """Render an intuitive guide for ``fluid config`` (and its
    ``fluid context`` alias) when no verb is supplied.  Detects an
    existing ``.fluid/context.json`` in the cwd and recommends
    ``list`` so the operator sees what's currently set; otherwise
    points at ``set`` as the right starting move.
    """

    from pathlib import Path

    from fluid_build.cli._subcommand_guide import (
        SubcommandEntry,
        SubcommandGuide,
        SubcommandHint,
        render_subcommand_guide,
    )

    entries = [
        SubcommandEntry(
            name="list",
            description="Show the current context (provider / project / region).",
            example="fluid config list",
        ),
        SubcommandEntry(
            name="get",
            description="Read one key from the current context.",
            example="fluid config get provider",
        ),
        SubcommandEntry(
            name="set",
            description="Set one key in the current context.",
            example="fluid config set provider gcp",
        ),
    ]

    def _detect_hint() -> "SubcommandHint | None":
        if (Path.cwd() / CTX_PATH).is_file():
            return SubcommandHint(
                subcommand="list",
                rationale="found .fluid/context.json in cwd — see what's currently set.",
            )
        return SubcommandHint(
            subcommand="set",
            rationale="no .fluid/context.json yet — set provider / project / region first.",
        )

    guide = SubcommandGuide(
        command_path="fluid config",
        headline=(
            "Get/Set the workspace default provider, project, and region.  "
            "Persisted to .fluid/context.json so other fluid commands can "
            "read sensible defaults."
        ),
        entries=entries,
        hint_provider=_detect_hint,
        quick_start="fluid config list",
    )
    return render_subcommand_guide(guide)
