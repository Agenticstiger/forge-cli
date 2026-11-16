# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""
Deprecated alias of ``fluid generate schedule --scheduler airflow``.

Usage:
    fluid generate-airflow contract.yaml -o dags/

Both commands render a contract's DAGs through one code path
(``fluid_build.schedulers.airflow``): a DAG per scheduled build, or one DAG for
a hand-written ``orchestration`` section. The command used to carry two
generators of its own (a provider-action DAG of placeholder ``echo`` tasks, and
a hand-off to ``fluid export``) that disagreed with each other and with
``generate schedule``; it now only forwards.
"""

import argparse
import logging
import shutil
import tempfile
from pathlib import Path

from ._common import CLIError
from ._logging import error, warn

COMMAND = "generate-airflow"
SUCCESSOR = "fluid generate schedule --scheduler airflow"


def register(subparsers: argparse._SubParsersAction):
    """Register the generate-airflow command."""
    p = subparsers.add_parser(
        COMMAND,
        help=f"Deprecated: use '{SUCCESSOR}'",
        description=f"""
        Deprecated alias of '{SUCCESSOR}'.

        Generates the Airflow DAGs for a FLUID contract: one per build that
        declares a cron trigger, or one for a hand-written 'orchestration'
        section. Output is identical to the successor command.
        """,
        epilog="""
Examples:
  # Generate DAGs into ./dags
  fluid generate-airflow contract.fluid.yaml

  # Specify the output directory
  fluid generate-airflow contract.yaml -o dags/

  # Write the one DAG file to a path of your choosing
  fluid generate-airflow contract.yaml -o dags/sales_pipeline.py
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    p.add_argument("contract", help="Path to FLUID contract file (YAML or JSON)")
    p.add_argument(
        "--output",
        "-o",
        help="Output directory (default: ./dags), or a .py path when the contract renders one DAG",
    )
    p.add_argument("--env", help="Environment overlay to apply (dev/test/prod)")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    """Forward to ``fluid generate schedule --scheduler airflow``."""
    warn(logger, f"'generate-airflow' is deprecated. Use '{SUCCESSOR}' instead.")
    try:
        from . import generate_schedule

        output = getattr(args, "output", None)
        to_file = bool(output) and Path(output).suffix == ".py"
        scratch = Path(tempfile.mkdtemp(prefix="fluid-dag-")) if to_file else None
        try:
            code = generate_schedule.run(
                argparse.Namespace(
                    contract=args.contract,
                    output=str(scratch) if scratch else output,
                    scheduler="airflow",
                    overwrite=bool(scratch),
                    env=getattr(args, "env", None),
                    dag_env=None,
                    dag_contract_path=None,
                    list_schedulers=False,
                    verbose=getattr(args, "verbose", False),
                ),
                logger,
            )
            if code == 0 and scratch is not None:
                dags = sorted(scratch.rglob("*.py"))
                if len(dags) != 1:
                    raise CLIError(
                        2,
                        "generate_airflow_output_not_a_file",
                        {
                            "output": output,
                            "dags": len(dags),
                            "hint": f"this contract renders {len(dags)} DAG files; "
                            "pass an output directory to -o",
                        },
                    )
                Path(output).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dags[0]), output)
            return code
        finally:
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
    except CLIError as e:
        error(logger, f"❌ {e.event}: {e.context}")
        return e.exit_code
