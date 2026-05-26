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

"""CLI command ``fluid generate`` — unified entry point for artifact generation.

Subcommands:
    fluid generate transformation         Generate transformation artifacts (dbt, SQL, ...)
    fluid generate schedule               Generate schedule artifacts (Airflow, Dagster, Prefect)
    fluid generate ci                     Generate CI/CD pipelines (GitHub Actions, GitLab CI)
    fluid generate standard               Export to data product standards
                                          (Bitol ODPS v1.0.0 = default; ODCS; LF/ODPI v4.1
                                          opt-in via ``--format odps-v4.1``)
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from fluid_build.cli.console import cprint

COMMAND = "generate"


def register(subparsers: argparse._SubParsersAction):
    """Register the generate command with its subcommands."""
    p = subparsers.add_parser(
        COMMAND,
        help="Generate artifacts from FLUID contract",
        description="""
        Unified artifact generation from FLUID contracts.

        Subcommands:
          speed-transformation   Generate transformation engine artifacts (dbt, SQL, etc.)
          transformation         Friendly alias for speed-transformation
          dbt                    Alias for transformation when generating dbt projects
          schedule               Generate schedule/orchestration artifacts (Airflow, Dagster, Prefect)
          ci                     Generate CI/CD pipelines (GitHub Actions, GitLab CI)
          standard               Export to data product standards
                                 (Bitol ODPS v1.0.0 = default ``--format odps``;
                                 ODCS; LF/ODPI v4.1 opt-in ``--format odps-v4.1``)
          artifacts              Stage-3 fanout: bundle → ODCS, Bitol ODPS,
                                 LF/ODPI ODPS v4.1, schedule, policies

        When called without a subcommand, shows available subcommands.
        """,
        epilog="""
Examples:
  # Generate transformation artifacts
  fluid generate transformation

  # Generate schedule artifacts
  fluid generate schedule

  # Generate CI/CD pipeline
  fluid generate ci --system github

  # Export to Bitol ODPS (the center-stage, default export)
  fluid generate standard contract.fluid.yaml --format odps

  # Export to LF/ODPI ODPS v4.1 (opt-in)
  fluid generate standard contract.fluid.yaml --format odps-v4.1

  # List available engines/schedulers/formats
  fluid generate transformation --list
  fluid generate schedule --list
  fluid generate standard --list
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Register subcommands
    sub = p.add_subparsers(dest="generate_sub", help="Generation target")

    from . import (
        generate_artifacts,
        generate_ci,
        generate_dbt_tests,
        generate_schedule,
        generate_speed_transformation,
        generate_standard,
    )

    generate_speed_transformation.register_subcommand(sub)
    generate_schedule.register_subcommand(sub)
    generate_ci.register_subcommand(sub)
    generate_standard.register_subcommand(sub)
    generate_artifacts.register_subcommand(sub)
    generate_dbt_tests.register_subcommand(sub)

    # Default handler (backward compat: no subcommand → transformation)
    p.set_defaults(cmd=COMMAND, func=run)

    # Legacy flags so bare `fluid generate --list` still works
    p.add_argument("--output", "-o", help=argparse.SUPPRESS)
    p.add_argument("--build-index", type=int, default=0, help=argparse.SUPPRESS)
    p.add_argument("--overwrite", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--env", help=argparse.SUPPRESS)
    p.add_argument("--list", dest="list_engines", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--verbose", "-v", action="store_true", help=argparse.SUPPRESS)


def run(args: Any, logger: logging.Logger) -> int:
    """Route to the appropriate subcommand."""
    sub = getattr(args, "generate_sub", None)

    if sub in {"speed-transformation", "transformation", "dbt"}:
        from . import generate_speed_transformation

        return generate_speed_transformation.run(args, logger)

    if sub == "schedule":
        from . import generate_schedule

        return generate_schedule.run(args, logger)

    if sub == "ci":
        from . import generate_ci

        return generate_ci.run(args, logger)

    if sub == "standard":
        from . import generate_standard

        return generate_standard.run(args, logger)

    if sub == "artifacts":
        from . import generate_artifacts

        return generate_artifacts.run(args, logger)

    # No subcommand specified — default to speed-transformation.
    if sub is None:
        # Check if user passed --list
        if getattr(args, "list_engines", False):
            from . import generate_speed_transformation

            return generate_speed_transformation.run(args, logger)

        # Bare `fluid generate` — show help
        cprint("Usage: fluid generate <subcommand>\n")
        cprint("Subcommands:")
        cprint("  transformation         Generate transformation artifacts (dbt, SQL, etc.)")
        cprint("  schedule               Generate schedule artifacts (Airflow, Dagster, Prefect)")
        cprint("  ci                     Generate CI/CD pipelines (GitHub Actions, GitLab CI)")
        cprint(
            "  standard               Export to standards (Bitol ODPS v1.0.0 = default; "
            "ODCS; LF/ODPI v4.1 opt-in via --format odps-v4.1)"
        )
        cprint("")
        cprint("Examples:")
        cprint("  fluid generate transformation")
        cprint("  fluid generate schedule")
        cprint("  fluid generate ci --system github")
        cprint("  fluid generate standard contract.fluid.yaml --format odps          # Bitol ODPS")
        cprint(
            "  fluid generate standard contract.fluid.yaml --format odps-v4.1     # LF/ODPI v4.1"
        )
        return 0

    cprint(f"Unknown subcommand: {sub}")
    return 1
