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

"""``fluid ship`` — the canonical "validate → bundle → plan → apply" macro.

UX hardening pass — operators consistently asked for "one command
that runs the happy path." Today the 11-stage pipeline requires
three to four manual invocations
(``validate → bundle → plan → apply``) which is fine for CI scripts
but painful for daily authoring. ``fluid ship`` chains the canonical
subset under one ``--yes`` confirmation, fails LOUD on the first
stage that breaks (so the operator sees exactly which one), and
relays the failing stage's exit code to the shell.

The macro subprocesses each stage's existing CLI entry point, so
flag handling stays consistent with the standalone invocation
(``fluid validate``, etc.) and any flag a stage accepts works
unchanged. The ~50ms-per-stage spawn cost is negligible against the
seconds-per-stage runtime; the decoupling is worth it.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from typing import List

from ._common import CLIError, auto_find_contract

COMMAND = "ship"


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        COMMAND,
        help="Run validate → bundle → plan → apply in sequence (the happy path)",
        description=(
            "Runs the canonical 4-stage happy path with one command:\n\n"
            "  validate → bundle → plan → apply\n\n"
            "Stops at the first non-zero exit so you see exactly which "
            "stage broke. ``--yes`` propagates to ``apply``. ``--env`` "
            "propagates to plan + apply. ``--strict`` propagates to "
            "validate."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  fluid ship                          # CWD contract, no env\n"
            "  fluid ship contract.fluid.yaml --env dev --yes\n"
            "  fluid ship --skip-bundle --env prod # skip the bundle stage\n"
        ),
    )
    p.add_argument(
        "contract",
        nargs="?",
        default=None,
        help=(
            "Path to contract.fluid.yaml. When omitted, auto-finds it in CWD "
            "(matches validate / plan / bundle / apply ergonomics)."
        ),
    )
    p.add_argument(
        "--env",
        default=None,
        help="Environment overlay (dev, staging, prod). Passed to plan + apply.",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Pass --strict to validate (fail on warnings).",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help=("Skip apply's interactive confirmation. Required for " "non-interactive / CI usage."),
    )
    p.add_argument(
        "--skip-bundle",
        action="store_true",
        help="Skip the bundle stage (useful when the contract is already bundled).",
    )
    p.add_argument(
        "--skip-plan",
        action="store_true",
        help="Skip the plan stage and go straight from validate → apply.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Pass --dry-run to apply so the run stops before writes.",
    )
    p.set_defaults(cmd=COMMAND, func=run)


def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Run validate → bundle → plan → apply in sequence.

    Each stage subprocesses ``fluid <stage>`` so flag handling
    stays consistent with the standalone invocations and any future
    stage flag works unchanged.
    """
    if not auto_find_contract(args):
        raise CLIError(
            1,
            "contract_required",
            {
                "message": (
                    "No contract path supplied and no contract.fluid.yaml "
                    "in the current directory."
                )
            },
        )

    contract = args.contract
    env = args.env
    strict = bool(args.strict)
    yes = bool(args.yes)
    skip_bundle = bool(args.skip_bundle)
    skip_plan = bool(args.skip_plan)
    dry_run = bool(args.dry_run)

    # Plan the stage sequence + per-stage argv. Each entry is
    # (stage_name, argv-tail) where the tail is appended to
    # ``[fluid, <stage_name>]``.
    sequence: List[tuple] = [
        (
            "validate",
            "Schema + provider rules",
            [contract] + (["--strict"] if strict else []),
        ),
    ]
    if not skip_bundle:
        sequence.append(
            (
                "bundle",
                "Resolve fragments + freeze digest",
                [contract, "--format", "tgz"],
            )
        )
    if not skip_plan:
        plan_argv = [contract]
        if env:
            plan_argv += ["--env", env]
        sequence.append(("plan", "Plan execution against the provider", plan_argv))
    apply_argv = [contract]
    if env:
        apply_argv += ["--env", env]
    if yes:
        apply_argv += ["--yes"]
    if dry_run:
        apply_argv += ["--dry-run"]
    sequence.append(("apply", "Deploy", apply_argv))

    logger.info("🚢 fluid ship — running %d stage(s)", len(sequence))
    for stage_name, _desc, _ in sequence:
        logger.info("  ▸ %s", stage_name)

    fluid_bin = _resolve_fluid_bin()
    for stage_name, stage_desc, stage_argv in sequence:
        rc = _run_stage(fluid_bin, stage_name, stage_argv, logger)
        if rc != 0:
            logger.error(
                "❌ Ship aborted at stage %r (%s) — exit code %d. "
                "Re-run that stage manually to see the full error: "
                "fluid %s %s",
                stage_name,
                stage_desc,
                rc,
                stage_name,
                " ".join(stage_argv),
            )
            return rc

    logger.info("🎉 Ship complete — all %d stage(s) passed", len(sequence))
    return 0


def _resolve_fluid_bin() -> List[str]:
    """Resolve the ``fluid`` entry-point invocation for subprocesses.

    Prefer ``sys.executable -m fluid_build.cli`` over the bare
    ``fluid`` shim because:
    - The user might be running an editable install where ``fluid``
      isn't on PATH (e.g. via ``python -m fluid_build.cli forge ...``).
    - ``sys.executable`` guarantees the same Python the macro is
      running in, avoiding venv mismatches.
    """
    return [sys.executable, "-m", "fluid_build.cli"]


def _run_stage(fluid_bin: List[str], stage: str, argv: List[str], logger: logging.Logger) -> int:
    """Subprocess one stage; return its exit code. Stream output through."""
    cmd = fluid_bin + [stage] + argv
    logger.info("─" * 70)
    logger.info("[ship %s] $ fluid %s %s", stage, stage, " ".join(argv))
    logger.info("─" * 70)
    try:
        result = subprocess.run(cmd, check=False)
    except FileNotFoundError as exc:
        logger.error("ship_subprocess_failed: %s", exc)
        return 127
    return result.returncode
