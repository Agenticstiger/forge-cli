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

"""``fluid generate schedule`` subcommand.

Generates schedule/orchestration artifacts (Airflow DAGs, Dagster pipelines,
Prefect flows) from a FLUID contract.  The scheduler engine is auto-detected
from ``orchestration.engine`` in the contract.

Usage:
    fluid generate schedule                                    # discover contract + scheduler
    fluid generate schedule contract.fluid.yaml                # specify contract
    fluid generate schedule contract.fluid.yaml -o dags/       # specify output dir
    fluid generate schedule --list                             # show available schedulers
    fluid generate schedule --scheduler dagster                # override scheduler
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Dict

from fluid_build.cli.console import cprint

from ._common import CLIError, load_contract_with_overlay
from ._logging import error, info

SUBCOMMAND = "schedule"


def register_subcommand(subparsers: argparse._SubParsersAction):
    """Register the schedule subcommand under ``fluid generate``."""
    p = subparsers.add_parser(
        SUBCOMMAND,
        help="Generate schedule artifacts (Airflow DAG, Dagster pipeline, Prefect flow)",
        description="""
        Generate schedule/orchestration artifacts from a FLUID contract.
        The scheduler engine is auto-detected from orchestration.engine.

        Supports Airflow (generates DAG files), Dagster (generates pipeline
        definitions), and Prefect (generates flow definitions).
        """,
        epilog="""
Examples:
  # Auto-discover contract and scheduler
  fluid generate schedule

  # Specify contract path
  fluid generate schedule contract.fluid.yaml

  # Specify output directory
  fluid generate schedule contract.fluid.yaml -o dags/

  # Override scheduler engine
  fluid generate schedule --scheduler dagster

  # List available schedulers
  fluid generate schedule --list
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    p.add_argument(
        "contract",
        nargs="?",
        default=None,
        help="Path to FLUID contract file (default: discover contract.fluid.yaml in CWD)",
    )
    p.add_argument("--output", "-o", help="Output directory (default: ./dags or ./pipelines or ./flows)")
    p.add_argument("--scheduler", help="Override scheduler engine (airflow, dagster, prefect)")
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing output directory")
    p.add_argument("--env", help="Environment overlay to apply (dev/test/prod)")
    p.add_argument("--list", dest="list_schedulers", action="store_true", help="List available schedulers and exit")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    p.set_defaults(generate_sub=SUBCOMMAND, func=run)


def run(args: Any, logger: logging.Logger) -> int:
    """Execute schedule artifact generation."""
    try:
        # --- List mode ---
        if getattr(args, "list_schedulers", False):
            return _list_schedulers()

        # --- Load contract ---
        contract_path = _resolve_contract_path(args)
        if getattr(args, "verbose", False):
            info(logger, f"Loading contract from {contract_path}")

        contract = load_contract_with_overlay(str(contract_path), getattr(args, "env", None), logger)

        # --- Resolve scheduler ---
        scheduler_name = getattr(args, "scheduler", None)
        if not scheduler_name:
            orchestration = contract.get("orchestration", {})
            scheduler_name = orchestration.get("engine", "")

        if not scheduler_name:
            cprint(
                "No scheduler engine specified.\n"
                "Set orchestration.engine in your contract, or use --scheduler.\n\n"
                "Available schedulers: fluid generate schedule --list"
            )
            return 1

        # Synthesize orchestration from builds when the contract lacks one.
        if not contract.get("orchestration") and contract.get("builds"):
            from fluid_build.schedulers.synthesis import synthesize_orchestration_from_builds

            provider = contract.get("provider", "")
            synthesized = synthesize_orchestration_from_builds(
                contract, scheduler_name, provider=provider,
            )
            if synthesized:
                contract = {**contract, "orchestration": synthesized}
                if getattr(args, "verbose", False):
                    info(logger, f"Synthesized orchestration from {len(synthesized.get('tasks', []))} build steps")

        # --- Look up scheduler ---
        from fluid_build.schedulers import get_scheduler, has_scheduler, list_schedulers

        if not has_scheduler(scheduler_name):
            cprint(
                f"No scheduler available for '{scheduler_name}'.\n"
                f"Available schedulers: {', '.join(list_schedulers())}\n\n"
                f"Use --scheduler to specify one, or set orchestration.engine in your contract."
            )
            return 1

        scheduler = get_scheduler(scheduler_name)
        if scheduler is None:
            return 1

        if getattr(args, "verbose", False):
            info(logger, f"Using scheduler: {scheduler_name}")

        # --- Validate ---
        issues = scheduler.validate(contract)
        errors = [i for i in issues if i.severity.value == "error"]
        if errors:
            for issue in errors:
                error(logger, str(issue))
            return 1

        for issue in issues:
            if issue.severity.value == "warning":
                cprint(f"Warning: {issue}")

        # --- Resolve provider ---
        provider = contract.get("provider", "")
        provider_config: Dict[str, Any] = {}
        # Extract provider-specific config from contract metadata
        metadata = contract.get("metadata", {})
        if provider == "gcp":
            provider_config = {
                "project": metadata.get("gcp_project", "my-project"),
                "region": metadata.get("gcp_region", "us-central1"),
            }
        elif provider == "aws":
            provider_config = {
                "region": metadata.get("aws_region", "us-east-1"),
                "account_id": metadata.get("aws_account_id", ""),
            }
        elif provider == "snowflake":
            provider_config = {
                "connection_id": metadata.get("snowflake_connection_id", "snowflake_default"),
            }

        # --- Generate ---
        files = scheduler.generate(
            contract,
            provider=provider,
            provider_config=provider_config,
        )

        if not files:
            cprint("No files generated.")
            return 0

        # --- Resolve output directory ---
        output_dir = _resolve_output_dir(args, scheduler_name)

        if output_dir.exists() and not getattr(args, "overwrite", False):
            if any(output_dir.iterdir()):
                cprint(
                    f"Output directory '{output_dir}' already exists and is not empty.\n"
                    f"Use --overwrite to replace existing files."
                )
                return 1

        # --- Write files ---
        output_dir.mkdir(parents=True, exist_ok=True)
        for rel_path, content in sorted(files.items()):
            file_path = output_dir / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")

        # --- Summary ---
        _print_summary(output_dir, files, scheduler_name)
        return 0

    except CLIError as e:
        error(logger, f"{e.event}: {e.context}")
        return e.exit_code
    except Exception as e:
        error(logger, f"Error generating schedule artifacts: {e}")
        if getattr(args, "verbose", False):
            import traceback
            traceback.print_exc()
        return 1


def _resolve_contract_path(args: Any) -> Path:
    """Find the contract file."""
    if args.contract:
        path = Path(args.contract)
        if not path.exists():
            raise CLIError(1, "contract_not_found", {"path": str(path)})
        return path

    for name in ("contract.fluid.yaml", "contract.fluid.json"):
        candidate = Path.cwd() / name
        if candidate.exists():
            return candidate

    raise CLIError(1, "no_contract_found", {
        "cwd": str(Path.cwd()),
        "hint": "Specify a contract path or run from a directory with contract.fluid.yaml",
    })


def _resolve_output_dir(args: Any, scheduler_name: str) -> Path:
    """Determine the output directory."""
    if getattr(args, "output", None):
        return Path(args.output)

    # Default directories by scheduler type
    defaults = {
        "airflow": "dags",
        "dagster": "pipelines",
        "prefect": "flows",
    }
    return Path.cwd() / defaults.get(scheduler_name, "schedules")


def _list_schedulers() -> int:
    """List available schedule engines."""
    from fluid_build.schedulers import list_schedulers

    schedulers = list_schedulers()
    if not schedulers:
        cprint("No schedule engines registered.")
        return 0

    cprint("Available schedule engines:\n")
    for name in schedulers:
        from fluid_build.schedulers import get_scheduler
        scheduler = get_scheduler(name)
        platforms = ", ".join(scheduler.supported_platforms) if scheduler and scheduler.supported_platforms else "all"
        cprint(f"  {name:12s} platforms: {platforms}")

    cprint("\nUsage: fluid generate schedule [contract.fluid.yaml]")
    cprint("Scheduler is auto-detected from orchestration.engine in the contract.")
    return 0


def _print_summary(output_dir: Path, files: Dict[str, str], scheduler_name: str) -> None:
    """Print a clean summary of generated files."""
    cprint(f"\nGenerated {len(files)} files ({scheduler_name} scheduler):\n")

    for rel_path in sorted(files.keys()):
        cprint(f"  {output_dir / rel_path}")

    cprint(f"\nTip: To regenerate after editing the contract: fluid generate schedule")
