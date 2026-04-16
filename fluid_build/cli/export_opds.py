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
import logging
import os

from ._common import CLIError, load_contract_with_overlay, write_json
from ._logging import info

COMMAND = "export-opds"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(COMMAND, help="Export FLUID → OPDS JSON")
    p.add_argument("contract", help="contract.fluid.yaml")
    p.add_argument("--env", help="overlay env")
    p.add_argument("--out", default="runtime/exports/product.opds.json", help="Output path")
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    try:
        c = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
        try:
            from fluid_build.providers.odps.odps import OdpsProvider

            export = OdpsProvider.to_odps(c)
        except Exception:
            export = {
                "specVersion": "1.0",
                "id": c.get("id"),
                "title": c.get("name"),
                "owner": c.get("metadata", {}).get("owner", {}),
                "domain": c.get("domain"),
                "exposes": c.get("exposes", []),
            }
        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        write_json(args.out, export)
        info(logger, "export_opds_ok", out=args.out)
        return 0
    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "export_opds_failed", {"error": str(e)})
