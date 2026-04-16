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
    sp = p.add_subparsers(dest="verb", required=True)
    sp.add_parser("list", help="Show current context")
    g = sp.add_parser("set", help="Set a key")
    g.add_argument("key", choices=["provider", "project", "region"], help="Key")
    g.add_argument("value", help="Value")
    sp.add_parser("get", help="Get a key").add_argument("key")
    p.set_defaults(cmd=COMMAND, func=run)


def register_context_alias(subparsers: argparse._SubParsersAction):
    """Register hidden 'context' alias for backward compatibility."""
    import argparse as _argparse

    p = subparsers.add_parser("context", help=_argparse.SUPPRESS)
    sp = p.add_subparsers(dest="verb", required=True)
    sp.add_parser("list", help="Show current context")
    g = sp.add_parser("set", help="Set a key")
    g.add_argument("key", choices=["provider", "project", "region"], help="Key")
    g.add_argument("value", help="Value")
    sp.add_parser("get", help="Get a key").add_argument("key")
    p.set_defaults(cmd="context", func=run)


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
        verb = args.verb
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
