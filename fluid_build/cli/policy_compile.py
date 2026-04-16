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

COMMAND = "policy-compile"


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(COMMAND, help="Compile accessPolicy → provider IAM bindings")
    p.add_argument("contract", help="contract.fluid.yaml")
    p.add_argument("--env", help="overlay env")
    p.add_argument("--out", default="runtime/policy/bindings.json", help="bindings path")
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    import traceback

    try:
        c = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
        try:
            from fluid_build.policy.compiler import compile_policy

            bindings, warnings = compile_policy(c)
        except Exception as e:
            logger.error(f"Policy compiler error: {e}")
            logger.error(traceback.format_exc())
            bindings, warnings = {"bindings": []}, [f"policy compiler failed: {str(e)}"]
        out_dir = os.path.dirname(args.out)
        if out_dir:  # Only create dir if path has a directory component
            os.makedirs(out_dir, exist_ok=True)
        write_json(args.out, {"bindings": bindings, "warnings": warnings})
        info(logger, "policy_compiled", out=args.out, warnings=len(warnings))
        return 0
    except CLIError:
        raise
    except Exception as e:
        logger.error(f"Outer exception: {e}")
        logger.error(traceback.format_exc())
        raise CLIError(1, "policy_compile_failed", {"error": str(e)})
