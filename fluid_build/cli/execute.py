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

# fluid_build/cli/execute.py
"""
FLUID Execute Command - Declarative Build Execution

Executes build jobs defined in FLUID contracts. Reads execution triggers
(manual, schedule) and runs the specified scripts accordingly.

Supports:
- Manual execution with iteration counts (free tier compatible)
- Scheduled execution (requires Cloud Composer/Scheduler)
- Build filtering by ID
- Dry-run mode
- Parallel execution
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fluid_build.cli.console import cprint, success
from fluid_build.cli.console import error as console_error

from ._common import CLIError, load_contract_with_overlay

LOG = logging.getLogger("fluid.cli.execute")

COMMAND = "execute"


def register(sp: argparse._SubParsersAction) -> None:
    """Register the execute command with the CLI (deprecated — use apply --build)"""
    p = sp.add_parser(
        "execute",
        help=argparse.SUPPRESS,  # hidden — deprecated, use 'apply --build'
        description="""
Execute build jobs defined in a FLUID contract.

This command reads the contract's execution configuration and runs the
specified build scripts according to their trigger settings.

Examples:
  # Execute all builds in contract
  fluid execute contract.fluid.yaml

  # Execute specific build by ID
  fluid execute contract.fluid.yaml --build bitcoin_price_ingestion

  # Dry-run to see what would execute
  fluid execute contract.fluid.yaml --dry-run

  # Execute with delay between iterations
  fluid execute contract.fluid.yaml --delay 5

Trigger Types:
  manual   - Run N times when invoked (free tier compatible)
             Specify iterations in contract: trigger.iterations
  
  schedule - Requires Cloud Composer/Scheduler (paid tier)
             Shows warning and skips execution
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    p.add_argument("contract", help="Path to FLUID contract YAML file")

    p.add_argument(
        "--build",
        "--build-id",
        dest="build_id",
        help="Execute specific build by ID (default: all builds)",
    )

    p.add_argument(
        "--dry-run", action="store_true", help="Show what would be executed without running"
    )

    p.add_argument(
        "--delay", type=int, default=2, help="Seconds to wait between iterations (default: 2)"
    )

    p.add_argument(
        "--no-output", action="store_true", help="Suppress build script output (show summary only)"
    )

    p.add_argument("--fail-fast", action="store_true", help="Stop execution on first failure")

    p.add_argument("--env", help="Environment overlay file")

    p.set_defaults(func=run)


def resolve_script_path(contract_path: Path, build: Dict[str, Any]) -> Optional[Path]:
    """Resolve the script path for a build"""
    repository = build.get("repository", "./")
    properties = build.get("properties", {})
    model = properties.get("model", "ingest")

    # Try .py extension first
    script_path = contract_path.parent / repository / f"{model}.py"
    if script_path.exists():
        return script_path

    # Try without extension
    script_path = contract_path.parent / repository / model
    if script_path.exists():
        return script_path

    return None


def execute_build(
    build: Dict[str, Any],
    script_path: Path,
    contract_dir: Path,
    dry_run: bool = False,
    delay: int = 2,
    no_output: bool = False,
    fail_fast: bool = False,
) -> int:
    """Execute a single build"""
    build_id = build.get("id", "unknown")
    execution = build.get("execution", {})
    trigger = execution.get("trigger", {})
    trigger_type = trigger.get("type", "manual")

    cprint(f"\n{'=' * 80}")
    cprint(f"📋 Build: {build_id}")
    cprint(f"   Script: {script_path}")
    cprint(f"   Trigger: {trigger_type}")

    if trigger_type == "manual":
        iterations = trigger.get("iterations", 1)
        # Support both delaySeconds (schema-friendly) and delay (legacy)
        delay_from_contract = trigger.get("delaySeconds", trigger.get("delay"))
        if delay_from_contract is not None:
            delay = delay_from_contract

        cprint(f"   Iterations: {iterations}")
        if delay > 0:
            cprint(f"   Delay: {delay}s between runs")

        if dry_run:
            cprint(f"   🔍 [DRY RUN] Would execute {iterations} time(s)")
            cprint(f"{'=' * 80}")
            return 0

        cprint(f"{'=' * 80}\n")

        successful_runs = 0
        failed_runs = 0

        for i in range(iterations):
            cprint(f"🚀 Run {i+1}/{iterations} - {datetime.now().strftime('%H:%M:%S')}")
            cprint("-" * 80)

            start_time = time.time()

            # Use virtual environment Python if available, otherwise system Python
            python_executable = sys.executable
            venv_path = os.environ.get("VIRTUAL_ENV")
            if venv_path:
                venv_python = Path(venv_path) / "bin" / "python3"
                if venv_python.exists():
                    python_executable = str(venv_python)

            try:
                result = subprocess.run(
                    [python_executable, str(script_path)],
                    cwd=contract_dir,
                    capture_output=no_output,
                    text=True,
                )

                duration = time.time() - start_time

                if result.returncode == 0:
                    successful_runs += 1
                    success(f"Run {i+1} completed successfully ({duration:.2f}s)")
                else:
                    failed_runs += 1
                    console_error(f"Run {i+1} failed with exit code {result.returncode}")

                    if no_output and result.stderr:
                        cprint(f"Error output:\n{result.stderr}")

                    if fail_fast:
                        cprint("\n⚠️  Stopping execution (--fail-fast enabled)")
                        return 1

            except Exception as e:
                failed_runs += 1
                console_error(f"Run {i+1} failed with exception: {e}")
                if fail_fast:
                    return 1

            cprint("-" * 80)

            # Delay between iterations (except last)
            if i < iterations - 1 and delay > 0:
                cprint(f"⏳ Waiting {delay}s before next run...\n")
                time.sleep(delay)

        cprint(f"\n{'=' * 80}")
        cprint(f"📊 Execution Summary for {build_id}:")
        cprint(f"   Total runs: {iterations}")
        cprint(f"   ✅ Successful: {successful_runs}")
        cprint(f"   ❌ Failed: {failed_runs}")
        cprint(f"{'=' * 80}")

        return 0 if failed_runs == 0 else 1

    elif trigger_type == "schedule":
        cron = trigger.get("cron", "")
        cprint(f"   Cron: {cron}")
        cprint("   ⚠️  Scheduled execution requires Cloud Composer/Scheduler (paid tier)")
        cprint("   💡 For free tier, use trigger.type: manual with iterations")
        cprint(f"{'=' * 80}")
        return 0

    else:
        cprint(f"   ❌ Unknown trigger type: {trigger_type}")
        cprint("   Supported types: manual, schedule")
        cprint(f"{'=' * 80}")
        return 1


def run(args: argparse.Namespace, logger: logging.Logger, *, _from_apply: bool = False) -> int:
    """Execute builds from FLUID contract.

    Note: This command is deprecated. Use 'fluid apply --build <id>' instead.
    """
    global LOG
    LOG = logger
    if not _from_apply:
        cprint("Note: 'fluid execute' is deprecated. Use 'fluid apply --build <id>' instead.\n")

    contract_path = Path(args.contract)

    if not contract_path.exists():
        raise CLIError(1, "contract_not_found", {"path": str(contract_path)})

    # Load contract using shared infrastructure (overlays now work!)
    LOG.info(f"Loading contract: {contract_path}")
    try:
        contract = load_contract_with_overlay(
            str(contract_path), getattr(args, "env", None), logger
        )
    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "contract_load_failed", {"path": str(contract_path), "error": str(e)})

    builds = contract.get("builds", [])

    if not builds:
        LOG.warning("No builds defined in contract")
        return 0

    # Filter builds if specific ID requested
    if args.build_id:
        builds = [b for b in builds if b.get("id") == args.build_id]
        if not builds:
            LOG.error(f"Build not found: {args.build_id}")
            return 1

    cprint(f"\n{'=' * 80}")
    cprint("🚀 FLUID Execute - Build Execution")
    cprint(f"{'=' * 80}")
    cprint(f"Contract: {contract_path}")
    cprint(f"Builds: {len(builds)}")
    if args.dry_run:
        cprint("Mode: DRY RUN")
    cprint(f"{'=' * 80}")

    total_executed = 0
    total_failed = 0
    total_skipped = 0

    for build in builds:
        build_id = build.get("id", "unknown")

        # Resolve script path
        script_path = resolve_script_path(contract_path, build)

        if not script_path:
            repository = build.get("repository", "./")
            properties = build.get("properties", {})
            model = properties.get("model", "ingest")
            expected = contract_path.parent / repository / f"{model}.py"

            cprint(f"\n⚠️  Build '{build_id}' - Script not found: {expected}")
            total_skipped += 1
            continue

        # Execute build
        result = execute_build(
            build,
            script_path,
            contract_path.parent,
            dry_run=args.dry_run,
            delay=args.delay,
            no_output=args.no_output,
            fail_fast=args.fail_fast,
        )

        if result == 0:
            total_executed += 1
        else:
            total_failed += 1
            if args.fail_fast:
                break

    # Final summary
    cprint(f"\n{'=' * 80}")
    cprint("📈 Overall Summary")
    cprint(f"{'=' * 80}")
    cprint(f"Total builds: {len(builds)}")
    success(f"Executed: {total_executed}")
    console_error(f"Failed: {total_failed}")
    cprint(f"⏭️  Skipped: {total_skipped}")
    cprint(f"{'=' * 80}\n")

    return 0 if total_failed == 0 else 1
