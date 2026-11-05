# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Top-level `fluid roadmap` command."""

from __future__ import annotations

import argparse
import logging
from importlib import resources

from fluid_build.cli.console import cprint

COMMAND = "roadmap"


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(COMMAND, help="Show the staged forge roadmap")
    parser.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    text = resources.files("fluid_build.copilot").joinpath("roadmap.md").read_text(encoding="utf-8")
    cprint(text.rstrip())
    return 0
