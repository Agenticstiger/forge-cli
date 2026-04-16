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

"""Deprecated — use ``fluid plan --html`` instead.

This module is kept for backward compatibility only.  ``fluid preview``
now delegates to ``fluid plan --html``.
"""

from __future__ import annotations

import argparse
import logging

from fluid_build.cli.console import cprint

COMMAND = "preview"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(
        COMMAND,
        help=argparse.SUPPRESS,  # hidden from help — deprecated
    )
    p.add_argument("contract", help="contract.fluid.yaml")
    p.add_argument("--env", help="overlay env")
    p.add_argument("--out", default="runtime/plan.json", help="plan path")
    p.add_argument("--html", default="runtime/plan.html", help="HTML report")
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    cprint("Note: 'fluid preview' is deprecated. Use 'fluid plan --html' instead.\n")

    # Translate preview args to plan args
    args.html_output = args.html
    args.verbose = False
    args.validate_actions = False
    args.estimate_cost = False
    args.check_sovereignty = False
    args.provider = None
    args.project = None
    args.region = None

    from .plan import run as plan_run

    return plan_run(args, logger)
