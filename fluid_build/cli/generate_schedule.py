# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid generate schedule`` subcommand.

Generates schedule/orchestration artifacts (Airflow DAGs, Dagster pipelines,
Prefect flows) from a FLUID contract.  The scheduler engine is auto-detected
from ``orchestration.engine`` in the contract.

Builds that declare a trigger (``execution.trigger``: a cron, datasets, or
both) get Airflow DAGs whose every task runs one build through ``fluid apply
--mode amend-and-build --build-id <id> --yes`` (see
:mod:`fluid_build.schedulers.airflow.fluid_apply`). Builds that
``builds[].dependencies`` link share one DAG, each task after the builds it
depends on. That is the rendering whenever the engine is Airflow and the
contract declares no explicit ``orchestration.tasks``; with no
``orchestration.engine`` at all, such a contract defaults to Airflow. The DAG
reads the project directory from ``$FLUID_PROJECT_DIR`` on the worker and
appends the contract's path relative to the current directory (or
``--contract-path``).

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
        Builds with an execution.trigger (type schedule, dataset or
        schedule_and_dataset) and no explicit orchestration.tasks get
        Airflow DAGs (the default engine) that run each build through
        fluid apply --mode amend-and-build. Builds that builds[].dependencies
        link run in one DAG, in dependency order; each task declares the
        assets its build writes as outlets.

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

  # A DAG per scheduled build that runs `fluid apply --env aws ...`; the
  # Airflow worker sets FLUID_PROJECT_DIR to the product checkout
  fluid generate schedule contracts/orders/contract.fluid.yaml --env aws

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
    p.add_argument(
        "--output", "-o", help="Output directory (default: ./dags or ./pipelines or ./flows)"
    )
    p.add_argument("--scheduler", help="Override scheduler engine (airflow, dagster, prefect)")
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing output directory")
    p.add_argument(
        "--env",
        help=(
            "Environment overlay to apply (dev/test/prod). For a fluid-apply Airflow "
            "DAG it is also the --env every scheduled run passes to fluid apply."
        ),
    )
    p.add_argument(
        "--contract-path",
        dest="dag_contract_path",
        default=None,
        help=(
            "Contract path, relative to $FLUID_PROJECT_DIR on the Airflow worker, that a "
            "fluid-apply DAG runs (default: the contract argument relative to the "
            "current directory)."
        ),
    )
    p.add_argument(
        "--dag-id",
        dest="dag_id",
        default=None,
        help="Override the DAG id of an Airflow DAG rendered from an 'orchestration' section "
        "(default: the contract id)",
    )
    p.add_argument(
        "--schedule",
        dest="dag_schedule",
        default=None,
        help="Override the schedule (cron or an Airflow keyword) of an Airflow DAG rendered "
        "from an 'orchestration' section",
    )
    p.add_argument(
        "--list",
        dest="list_schedulers",
        action="store_true",
        help="List available schedulers and exit",
    )
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

        contract = load_contract_with_overlay(
            str(contract_path), getattr(args, "env", None), logger
        )

        # --- Resolve scheduler ---
        from fluid_build.schedulers.airflow import fluid_apply

        scheduler_name = getattr(args, "scheduler", None)
        if not scheduler_name:
            orchestration = contract.get("orchestration", {})
            scheduler_name = orchestration.get("engine", "")
        if not scheduler_name and fluid_apply.has_scheduled_builds(contract):
            # A build that declares a trigger has said WHEN it runs. The
            # artifact is inert until schedule-sync pushes it to a scheduler
            # the operator names, so defaulting WHO renders it commits nothing.
            scheduler_name = fluid_apply.DEFAULT_ENGINE

        # A contract whose every build runs on its own platform (a Flink job, a
        # stream fluid apply keeps running, or a build whose trigger is
        # continuous) has nothing to schedule, which is not an error: unless it
        # hand-declares orchestration tasks, or names a scheduler that does not
        # exist.
        if fluid_apply.runs_only_outside_schedulers(contract) and not (
            fluid_apply.has_explicit_tasks(contract)
        ):
            from fluid_build.schedulers import has_scheduler

            if not scheduler_name or has_scheduler(scheduler_name):
                cprint(
                    "No build needs a DAG: every build runs outside forge-cli (a Flink "
                    "job, for example), as a stream that fluid apply keeps running, or "
                    "continuously (trigger.type: continuous), so there is nothing to "
                    "schedule. No files written."
                )
                return 0

        if not scheduler_name:
            cprint(
                "No scheduler engine specified.\n"
                "Set orchestration.engine in your contract, or use --scheduler.\n\n"
                "Available schedulers: fluid generate schedule --list"
            )
            return 1

        if fluid_apply.uses_fluid_apply_dags(contract, engine=scheduler_name):
            if getattr(args, "dag_id", None) or getattr(args, "dag_schedule", None):
                raise CLIError(
                    2,
                    "generate_schedule_override_not_supported",
                    {
                        "hint": "--dag-id and --schedule override the one DAG of an "
                        "'orchestration' section; a scheduled build gets its DAG id and "
                        "schedule from its own trigger"
                    },
                )
            try:
                files = fluid_apply.render_fluid_apply_dags(
                    contract,
                    env=getattr(args, "dag_env", None) or getattr(args, "env", None),
                    contract_path=_dag_contract_path(args, contract_path),
                )
            except fluid_apply.ScheduleRenderError as exc:
                raise CLIError(
                    2, "generate_schedule_invalid_schedule", {"error": str(exc)}
                ) from exc
            return _write_files(args, files, scheduler_name)

        # Synthesize orchestration from builds when the contract lacks one.
        if not contract.get("orchestration") and contract.get("builds"):
            from fluid_build.schedulers.synthesis import synthesize_orchestration_from_builds

            provider = contract.get("provider", "")
            synthesized = synthesize_orchestration_from_builds(
                contract,
                scheduler_name,
                provider=provider,
            )
            if synthesized:
                contract = {**contract, "orchestration": synthesized}
                if getattr(args, "verbose", False):
                    info(
                        logger,
                        f"Synthesized orchestration from {len(synthesized.get('tasks', []))} build steps",
                    )

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
        from fluid_build.schedulers.airflow._config import resolve_provider_config

        provider_config: Dict[str, Any] = resolve_provider_config(provider, contract)
        if getattr(args, "dag_id", None):
            provider_config["dag_id"] = args.dag_id
        if getattr(args, "dag_schedule", None):
            provider_config["schedule"] = args.dag_schedule

        # --- Generate ---
        files = scheduler.generate(
            contract,
            provider=provider,
            provider_config=provider_config,
        )

        return _write_files(args, files, scheduler_name)

    except CLIError as e:
        error(logger, f"{e.event}: {e.context}")
        return e.exit_code
    except Exception as e:
        error(logger, f"Error generating schedule artifacts: {e}")
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        return 1


def _write_files(args: Any, files: Dict[str, str], scheduler_name: str) -> int:
    """Write *files* under the resolved output directory; return the exit code."""
    if not files:
        cprint("No files generated.")
        return 0

    output_dir = _resolve_output_dir(args, scheduler_name)

    if output_dir.exists() and not getattr(args, "overwrite", False):
        if any(output_dir.iterdir()):
            cprint(
                f"Output directory '{output_dir}' already exists and is not empty.\n"
                f"Use --overwrite to replace existing files."
            )
            return 1

    output_dir.mkdir(parents=True, exist_ok=True)
    for rel_path, content in sorted(files.items()):
        file_path = output_dir / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")

    _print_summary(output_dir, files, scheduler_name)
    return 0


def _dag_contract_path(args: Any, contract_path: Path) -> str:
    """The contract path a ``fluid apply`` DAG appends to ``$FLUID_PROJECT_DIR``.

    ``--contract-path`` wins. Otherwise the contract argument, relative to the
    current directory, which is taken to be the project directory.
    """
    explicit = getattr(args, "dag_contract_path", None)
    if explicit:
        return str(explicit)
    try:
        return contract_path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        raise CLIError(
            2,
            "generate_schedule_contract_outside_project",
            {
                "contract": str(contract_path),
                "cwd": str(Path.cwd()),
                "hint": (
                    "run from the project directory, or pass --contract-path with the "
                    "contract's path relative to it"
                ),
            },
        ) from None


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

    raise CLIError(
        1,
        "no_contract_found",
        {
            "cwd": str(Path.cwd()),
            "hint": "Specify a contract path or run from a directory with contract.fluid.yaml",
        },
    )


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
        platforms = (
            ", ".join(scheduler.supported_platforms)
            if scheduler and scheduler.supported_platforms
            else "all"
        )
        cprint(f"  {name:12s} platforms: {platforms}")

    cprint("\nUsage: fluid generate schedule [contract.fluid.yaml]")
    cprint("Scheduler is auto-detected from orchestration.engine in the contract.")
    return 0


def _print_summary(output_dir: Path, files: Dict[str, str], scheduler_name: str) -> None:
    """Print a clean summary of generated files."""
    cprint(f"\nGenerated {len(files)} files ({scheduler_name} scheduler):\n")

    for rel_path in sorted(files.keys()):
        cprint(f"  {output_dir / rel_path}")

    cprint("\nTip: To regenerate after editing the contract: fluid generate schedule")
