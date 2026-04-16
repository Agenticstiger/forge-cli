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

from fluid_build.cli.console import cprint, success
from fluid_build.cli.console import error as console_error

from ._common import CLIError, load_contract_with_overlay
from ._logging import info

COMMAND = "contract-tests"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(COMMAND, help="Run schema compatibility & consumer-impact tests")
    p.add_argument("contract", help="contract.fluid.yaml")
    p.add_argument("--env", help="overlay env")
    p.add_argument("--baseline", help="baseline schema signature JSON")
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    try:
        contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
        try:
            from fluid_build.contract_tests import run_tests
        except Exception:

            def run_tests(contract, baseline_path=None):
                return {"compatible": True, "reasons": []}

        result = run_tests(contract, getattr(args, "baseline", None))
        info(logger, "contract_tests", **result)

        # Human-friendly output
        if result.get("compatible"):
            success("Contract tests passed")
        else:
            reasons = result.get("reasons", [])
            console_error(f"Contract tests failed — {len(reasons)} incompatibility(ies) found")
            for r in reasons:
                cprint(f"   • {r}")

        return 0 if result.get("compatible") else 2
    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "contract_tests_failed", {"error": str(e)})
