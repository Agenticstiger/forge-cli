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

"""Shared base for build_runners: constants, helpers, and the top-level
``run_builds_from_args`` dispatcher.

This module is the runtime-execution counterpart of
``fluid_build.engines.*`` (which generates dbt/SQL project files at
``fluid generate speed-transformation`` time). See
``fluid_build/engines/__init__.py`` for the generation-side framework.

Migrated from ``fluid_build/cli/execute.py`` as part of the 11-stage
pipeline split. The deprecated ``fluid execute`` subcommand was removed
alongside; ``cli/apply.py`` calls :func:`run_builds_from_args` directly.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict

from fluid_build.cli._common import CLIError, load_contract_with_overlay
from fluid_build.cli.console import cprint, success
from fluid_build.cli.console import error as console_error

LOG = logging.getLogger("fluid.build_runners")

# ``{{ env.NAME }}`` placeholder substitution — resolved against
# ``os.environ`` at run time. Used by both the dbt profile generator and
# the dbt command builder to expand author-supplied vars/resources
# references.
ENV_PLACEHOLDER_RE = re.compile(r"\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# Used by the dbt command-log renderer to decide whether the value of an
# ``-e KEY=VALUE`` pair should be redacted. A structural hack: if the KEY
# looks sensitive (password/token/etc.), the value is replaced with
# ``<redacted>`` before being printed. Keep this in sync with the module
# that owns the renderer (``build_runners.dbt.runner._render_command_for_log``).
SENSITIVE_ENV_KEY_RE = re.compile(
    r"(?i)(password|passphrase|secret|token|api[_-]?key|private[_-]?key|credential|auth)"
)


def _resolve_env_placeholders(value: Any) -> Any:
    """Resolve ``{{ env.NAME }}`` placeholders against the current environment.

    Recurses into lists and dicts. Non-string/list/dict values are returned
    unchanged. A missing env var resolves to the empty string (not ``None``)
    so downstream YAML dumps and command-line args don't carry a literal
    ``None`` token.
    """
    if isinstance(value, str):
        return ENV_PLACEHOLDER_RE.sub(lambda m: os.getenv(m.group(1), ""), value)
    if isinstance(value, list):
        return [_resolve_env_placeholders(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_env_placeholders(item) for key, item in value.items()}
    return value


def is_dbt_build(build: Dict[str, Any]) -> bool:
    """Return True when a build should execute as a dbt project.

    Accepts ``dbt`` plus any ``dbt-<adapter>`` variant (``dbt-bigquery``,
    ``dbt-snowflake``, ``dbt-redshift``, ``dbt-postgres``, ``dbt-duckdb``, …).
    The adapter is selected by dbt itself from ``profiles.yml``; the engine
    name just routes the build into the dbt execute path here.
    """
    engine = (build.get("engine") or "").strip().lower()
    return engine == "dbt" or engine.startswith("dbt-")


def run_builds_from_args(
    args: argparse.Namespace,
    logger: logging.Logger,
    *,
    force_run: bool = False,
) -> int:
    """Execute builds from a FLUID contract.

    Loads the contract, filters by ``args.build_id`` if present, and
    dispatches each build to the dbt or python engine via
    :func:`is_dbt_build`. Returns 0 if no build failed, 1 otherwise.

    ``force_run=True`` (the default when called from ``fluid apply --build``)
    forces scheduled builds to run once — normally the scheduler owns the
    run, but apply may legitimately kick off a one-shot refresh.
    """
    # Deferred imports to avoid circular import at module-load time:
    # base.py -> python.runner -> base.py (for _resolve_env_placeholders).
    from .dbt.runner import execute_dbt_build, resolve_dbt_project_path
    from .python.runner import execute_build, resolve_script_path

    global LOG
    LOG = logger

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
    cprint("🚀 FLUID Build Runner")
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

        if is_dbt_build(build):
            project_dir = resolve_dbt_project_path(contract_path, build)
            if not project_dir:
                repository = build.get("repository", "./")
                expected = (contract_path.parent / repository / "dbt_project.yml").resolve()
                cprint(f"\n⚠️  Build '{build_id}' - dbt project not found: {expected}")
                total_skipped += 1
                continue

            result = execute_dbt_build(
                build,
                project_dir,
                contract_path.parent,
                dry_run=args.dry_run,
                delay=args.delay,
                no_output=args.no_output,
                fail_fast=args.fail_fast,
                force_run=force_run,
            )
        else:
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
                force_run=force_run,
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
