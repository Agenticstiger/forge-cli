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

"""Plain-Python script runner.

Launches non-dbt builds via ``subprocess.run([python, script.py])``.
Honours the contract's ``execution.trigger.{type,iterations,delaySeconds}``.
Used by ``build_runners.base.run_builds_from_args`` when ``is_dbt_build``
returns False.
"""

from __future__ import annotations

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

from .._path_safety import confine_to_workspace

LOG = logging.getLogger("fluid.build_runners.python")


def resolve_script_path(contract_path: Path, build: Dict[str, Any]) -> Optional[Path]:
    """Resolve the script path for a build, confined to the contract's workspace.

    Tries ``<repository>/<model>.py`` first, then ``<repository>/<model>``
    (no extension). Returns ``None`` if neither exists, or if the resolved
    path escapes the contract's parent directory (path-traversal guard).
    ``model`` defaults to ``"ingest"``.
    """
    repository = build.get("repository", "./")
    properties = build.get("properties", {})
    model = properties.get("model", "ingest")
    build_id = str(build.get("id", "unknown"))
    workspace_root = contract_path.parent

    # Try .py extension first
    script_path = contract_path.parent / repository / f"{model}.py"
    if script_path.exists():
        return confine_to_workspace(
            script_path, workspace_root, build_id=build_id, kind="python", logger=LOG
        )

    # Try without extension
    script_path = contract_path.parent / repository / model
    if script_path.exists():
        return confine_to_workspace(
            script_path, workspace_root, build_id=build_id, kind="python", logger=LOG
        )

    return None


def execute_build(
    build: Dict[str, Any],
    script_path: Path,
    contract_dir: Path,
    dry_run: bool = False,
    delay: int = 2,
    no_output: bool = False,
    fail_fast: bool = False,
    force_run: bool = False,
) -> int:
    """Execute a single build"""
    build_id = build.get("id", "unknown")
    execution = build.get("execution", {})
    trigger = execution.get("trigger", {})
    trigger_type = trigger.get("type", "manual")

    cprint(f"\n{'=' * 80}")
    cprint(f"📋 Build: {build_id}")
    cprint(f"   Script: {script_path}")
    trigger_label = trigger_type
    if trigger_type == "schedule" and force_run:
        trigger_label = "schedule (manual apply override)"
    cprint(f"   Trigger: {trigger_label}")

    if trigger_type == "manual" or (trigger_type == "schedule" and force_run):
        iterations = 1 if trigger_type == "schedule" and force_run else trigger.get("iterations", 1)
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
