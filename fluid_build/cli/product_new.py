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

from ._common import CLIError
from ._io import atomic_write
from ._logging import info

COMMAND = "product-new"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(COMMAND, help="Bootstrap a new product skeleton")
    p.add_argument("--id", required=True, help="Product id, e.g., gold.customer360_v1")
    p.add_argument("--out-dir", default="products", help="Where to create files")
    p.set_defaults(cmd=COMMAND, func=run)


SAMPLE = {
    "fluidVersion": "0.5.7",
    "kind": "DataProduct",
    "id": "gold.customer360_v1",
    "name": "Customer 360",
    "domain": "Customer",
    "metadata": {"layer": "Gold", "owner": {"team": "Data", "email": "owner@example.com"}},
    "consumes": [],
    "builds": [
        {
            "id": "main_build",
            "pattern": "hybrid-reference",
            "engine": "dbt",
            "repository": "./models",
            "properties": {"model": "customer360_v1"},
            "execution": {"trigger": {"type": "schedule", "cron": "15 2 * * *"}},
        }
    ],
    "exposes": [],
}


def run(args, logger: logging.Logger) -> int:
    try:
        pid = args.id
        base = os.path.join(args.out_dir, pid.replace(".", "_"))
        os.makedirs(base, exist_ok=True)
        SAMPLE["id"] = pid
        SAMPLE["name"] = pid.split(".")[-1]
        path = os.path.join(base, "contract.fluid.json")
        atomic_write(path, json.dumps(SAMPLE, indent=2))
        info(logger, "product_new_written", path=path)
        return 0
    except Exception as e:
        raise CLIError(1, "product_new_failed", {"error": str(e)})
