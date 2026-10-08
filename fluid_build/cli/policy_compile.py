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
import re
from typing import Any, List

from ._common import CLIError, load_contract_with_overlay, write_json
from ._logging import info, warn

COMMAND = "policy-compile"


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    """Populate a pre-created parser with the policy-compile args.

    Shared between the legacy ``fluid policy-compile`` top-level
    command and the new ``fluid policy compile`` subcommand so the
    argument surface stays single-sourced.
    """
    parser.add_argument("contract", help="contract.fluid.yaml")
    parser.add_argument("--env", help="overlay env")
    parser.add_argument("--out", default="runtime/policy/bindings.json", help="bindings path")
    parser.set_defaults(cmd=COMMAND, func=run)


def register(subparsers: argparse._SubParsersAction):
    """Register the legacy top-level ``fluid policy-compile`` command.

    New code should prefer ``fluid policy compile``; this is a
    deprecation-aliased surface kept for one release.
    """
    p = subparsers.add_parser(COMMAND, help="Compile accessPolicy → provider IAM bindings")
    _add_arguments(p)


#: The values ``compile_policy`` reads as a mapping or a list. A wrong type at
#: one of these paths is one the compiler can fail on. Any other schema
#: error (an extra key on a grant, a wrong type under ``metadata``) is not
#: named: it did not make the compiler fail.
_COMPILER_READ_PATHS = re.compile(
    r"accessPolicy(?:\.grants(?:\[\d+\](?:\.permissions)?)?)?"
    r"|exposes\[\d+\]\.binding(?:\.location)?"
)


def _compiler_schema_errors(contract: Any) -> List[str]:
    """The schema type errors ``fluid validate`` reports where the compiler reads.

    ``policy compile`` does not schema-validate, so a grant or binding of the
    wrong type reaches the compiler and fails there as a Python error that
    names neither the value nor the fix. Read only after the compiler failed;
    ``[]`` when the schema finds no such error or cannot be run.
    """
    try:
        from fluid_build.schema_manager import FluidSchemaManager

        result = FluidSchemaManager().validate_contract(contract, offline_only=True)
    except Exception:  # the compiler's own error is reported instead
        return []
    # An error starts with its path: ``accessPolicy.grants[0]: ...``,
    # ``exposes[0].binding: ...``.
    found = []
    for error in result.errors:
        path, _, message = error.partition(": ")
        if _COMPILER_READ_PATHS.fullmatch(path) and " is not of type " in message:
            found.append(error)
    return found


def _compile_failed(contract: Any, exc: Exception) -> CLIError:
    """The ``policy_compiler_crashed`` error for a crash inside ``compile_policy``."""
    schema_errors = _compiler_schema_errors(contract)
    if schema_errors:
        error = "policy compile failed on contract values of the wrong type: " + "; ".join(
            schema_errors
        )
    else:
        error = f"policy compiler failed: {type(exc).__name__}: {exc}"
    return CLIError(1, "policy_compiler_crashed", {"error": error})


def run(args, logger: logging.Logger) -> int:
    import traceback

    try:
        c = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
        try:
            from fluid_build.policy.compiler import NO_GRANTS, compile_policy

            bindings, warnings = compile_policy(c)
        except Exception as e:
            # Fail closed. This used to write an empty bindings list and exit
            # 0, so a crash read as "no grants to enforce" to the next stage.
            # Nothing is written: a bindings file from an earlier run stays as
            # it was, and the non-zero exit stops the pipeline before apply.
            logger.debug(traceback.format_exc())
            raise _compile_failed(c, e) from e
        out_dir = os.path.dirname(args.out)
        if out_dir:  # Only create dir if path has a directory component
            os.makedirs(out_dir, exist_ok=True)
        write_json(args.out, {"bindings": bindings, "warnings": warnings})
        # A warning can be the only sign that a grant compiled to nothing (an
        # Iceberg table in a catalog the compiler cannot grant on), so it goes
        # to the console, not only into the file. Warnings keep exit 0. A
        # contract with no grants leaves nothing unenforced: not a WARNING.
        for warning in warnings:
            if warning != NO_GRANTS:
                warn(logger, "policy_compile_warning", warning=warning, out=args.out)
        info(logger, "policy_compiled", out=args.out, warnings=len(warnings))
        return 0
    except CLIError:
        raise
    except Exception as e:
        logger.error(f"Outer exception: {e}")
        logger.error(traceback.format_exc())
        raise CLIError(1, "policy_compile_failed", {"error": str(e)})
