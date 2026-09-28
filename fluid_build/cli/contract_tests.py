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

"""``fluid contract-tests``: compare a contract's exposed schemas with a baseline.

The comparison is :mod:`fluid_build.tools.contract_tests`. Without a baseline
there is nothing to compare, and the command says it was skipped rather than
that anything passed.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from fluid_build.cli.console import cprint, success, warning
from fluid_build.cli.console import error as console_error

from ._common import CLIError, load_contract_with_overlay
from ._logging import info

COMMAND = "contract-tests"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(
        COMMAND, help="Check a contract's exposed schemas against a saved baseline"
    )
    p.add_argument("contract", help="contract.fluid.yaml")
    p.add_argument("--env", help="overlay env")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--baseline", help="baseline schema signature JSON to compare against")
    mode.add_argument(
        "--write-baseline",
        metavar="PATH",
        help="write the contract's schema signature to PATH as a new baseline, and exit",
    )
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    try:
        contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
        # No fallback: an import that fails here is a broken install, and a
        # stand-in that answers "compatible" is how this command once passed
        # every contract without running a test.
        from fluid_build.tools.contract_tests import run_tests, write_baseline

        out = getattr(args, "write_baseline", None)
        if out:
            write_baseline(contract, out)
            info(logger, "contract_tests_baseline_written", path=out)
            success(f"Baseline written to {out}")
            return 0

        baseline = getattr(args, "baseline", None)
        if not baseline:
            info(logger, "contract_tests_skipped", reason="no_baseline")
            warning(
                "Contract tests skipped: no --baseline to compare against. "
                f"Create one with: fluid contract-tests {args.contract} "
                "--write-baseline baseline.schema.json"
            )
            return 0
        if not Path(baseline).is_file():
            raise CLIError(1, "contract_tests_baseline_missing", {"baseline": baseline})
        try:
            result = run_tests(contract, baseline)
        except ValueError as e:
            raise CLIError(
                1, "contract_tests_bad_baseline", {"baseline": baseline, "error": str(e)}
            ) from e
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
