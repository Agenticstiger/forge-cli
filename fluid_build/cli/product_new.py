# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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


#: The skeleton ``run`` writes. Its ``fluidVersion`` is stamped there, from the
#: newest stable bundled schema: a literal goes stale every release, and this
#: module-level dict cannot read the schema at import time without dragging
#: jsonschema onto the --help path.
SAMPLE = {
    "kind": "DataProduct",
    "id": "gold.customer360_v1",
    "name": "Customer 360",
    "domain": "Customer",
    "metadata": {
        "layer": "Gold",
        "productType": "CDP",
        "owner": {"team": "Data", "email": "owner@example.com"},
    },
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
        from fluid_build.schema_manager import FluidSchemaManager

        contract = {
            "fluidVersion": FluidSchemaManager.latest_bundled_version(),
            **SAMPLE,
            "id": pid,
            "name": pid.split(".")[-1],
        }
        path = os.path.join(base, "contract.fluid.json")
        atomic_write(path, json.dumps(contract, indent=2))
        info(logger, "product_new_written", path=path)
        return 0
    except Exception as e:
        raise CLIError(1, "product_new_failed", {"error": str(e)})
