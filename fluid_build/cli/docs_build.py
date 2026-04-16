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
import glob
import json
import logging
import os

from ._common import CLIError
from ._logging import info

COMMAND = "docs"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(COMMAND, help="Generate static docs for products")
    p.add_argument("--src", default="products", help="Root to scan for contracts")
    p.add_argument("--out", default="docs", help="Docs folder")
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    try:
        os.makedirs(args.out, exist_ok=True)
        index = []
        for path in glob.glob(f"{args.src}/**/contract.fluid.*", recursive=True):
            index.append({"path": path})
        with open(os.path.join(args.out, "index.json"), "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2)
        info(
            logger, "docs_index_written", out=os.path.join(args.out, "index.json"), count=len(index)
        )
        return 0
    except Exception as e:
        raise CLIError(1, "docs_failed", {"error": str(e)})
