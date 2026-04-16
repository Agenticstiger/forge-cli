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

from ._common import CLIError, read_json
from ._logging import info

COMMAND = "viz-plan"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(
        COMMAND,
        help=argparse.SUPPRESS,  # hidden from help — deprecated
    )
    p.add_argument("plan", help="runtime/plan.json")
    p.add_argument("--out", default="runtime/plan.html", help="HTML path")
    p.set_defaults(cmd=COMMAND, func=run)


def render_plan_html(plan_path: str, out_html: str, logger: logging.Logger) -> None:
    data = read_json(plan_path)
    actions = data.get("actions", [])
    html = f"""<!doctype html>
    <html><head><meta charset='utf-8'><title>FLUID Plan</title>
    <style>body{{font-family:Menlo,monospace;padding:16px;background:#0b1020;color:#e5e7eb;}}
    pre{{background:#0a0f1d;padding:12px;border-radius:8px;border:1px solid #1f2937;overflow:auto;}}
    .card{{border:1px solid #374151;border-radius:10px;padding:12px;margin:10px 0;background:#0f172a;}}
    .pill{{display:inline-block;padding:2px 8px;border:1px solid #334155;border-radius:9999px;margin-right:4px;}}
    </style></head><body>
    <h1>FLUID Plan</h1>
    <p>Actions: {len(actions)}</p>
    <div class='card'><pre>{json.dumps(actions, indent=2)}</pre></div>
    </body></html>"""
    os.makedirs(os.path.dirname(out_html), exist_ok=True)
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    info(logger, "viz_plan_ok", out=out_html, actions=len(actions))


def run(args, logger: logging.Logger) -> int:
    from fluid_build.cli.console import cprint

    cprint("Note: 'fluid viz-plan' is deprecated. Use 'fluid plan --html' instead.\n")
    try:
        render_plan_html(args.plan, args.out, logger)
        return 0
    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "viz_plan_failed", {"error": str(e)})
