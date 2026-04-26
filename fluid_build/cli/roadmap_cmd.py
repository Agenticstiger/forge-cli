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
