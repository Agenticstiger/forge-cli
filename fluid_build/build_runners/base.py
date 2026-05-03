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


# Engines that ship as acquisition runners under build_runners/<engine>/.
ACQUISITION_ENGINES = frozenset(
    {"duckdb", "airbyte", "meltano", "dlt", "kafka-connect", "debezium"}
)


def _declared_capabilities_for_engine(engine: str):
    """Return the set of capabilities the named engine declares.

    Returns None when we can't import the runner (so the caller skips the
    capability gate rather than failing loudly on an unrelated import
    issue). The lazy import keeps fluid CLI startup fast.
    """
    runner_modpaths = {
        "duckdb": ("fluid_build.build_runners.duckdb.runner", "DuckdbRunner"),
        "dlt": ("fluid_build.build_runners.dlt.runner", "DltRunner"),
        "meltano": ("fluid_build.build_runners.meltano.runner", "MeltanoRunner"),
        "airbyte": ("fluid_build.build_runners.airbyte.runner", "AirbyteRunner"),
        "kafka-connect": (
            "fluid_build.build_runners.kafka_connect.runner",
            "KafkaConnectRunner",
        ),
        "debezium": ("fluid_build.build_runners.debezium.runner", "DebeziumRunner"),
    }
    target = runner_modpaths.get(engine)
    if not target:
        return None
    try:
        import importlib

        mod = importlib.import_module(target[0])
        cls = getattr(mod, target[1], None)
        if cls is None:
            return None
        decl = getattr(cls, "declared_capabilities", None)
        if decl is None:
            return None
        return [str(c.value if hasattr(c, "value") else c) for c in decl]
    except Exception:  # noqa: BLE001
        return None


def is_acquisition_build(build: Dict[str, Any]) -> bool:
    """Return True for builds with ``pattern: acquisition`` AND a known runner."""
    if (build.get("pattern") or "").strip().lower() != "acquisition":
        return False
    engine = (build.get("engine") or "").strip().lower()
    return engine in ACQUISITION_ENGINES


# Acquisition-engine registry: ``engine name → (module path, callable name)``.
# Adding a new engine is one entry here + a runner module — no edits to
# the dispatcher's switch chain. Modules are imported lazily so a missing
# optional extra (e.g. ``dlt`` not installed) doesn't abort the import
# of this whole module.
_ACQUISITION_RUNNER_REGISTRY: Dict[str, tuple] = {
    "duckdb": (".duckdb.runner", "execute_duckdb_build"),
    "dlt": (".dlt.runner", "execute_dlt_build"),
    "meltano": (".meltano.runner", "execute_meltano_build"),
    "airbyte": (".airbyte.runner", "execute_airbyte_build"),
    "kafka-connect": (".kafka_connect.runner", "execute_kafka_connect_build"),
    "debezium": (".debezium.runner", "execute_debezium_build"),
}


def register_acquisition_runner(engine: str, *, module_path: str, function_name: str) -> None:
    """Register a custom acquisition-engine runner.

    Third-party packages that ship their own runner can call this at
    import time to extend the dispatch table without monkey-patching.

    ``module_path`` is the dotted import path; ``function_name`` is
    the callable resolved from that module. The function must accept
    the same kwargs as the built-in runners
    (``build, contract, contract_dir, *, dry_run, sample_rows``).
    """
    _ACQUISITION_RUNNER_REGISTRY[engine.lower()] = (module_path, function_name)


def _execute_acquisition_build(
    build: Dict[str, Any],
    contract: Dict[str, Any],
    contract_dir: Path,
    *,
    dry_run: bool,
    sample_rows: Any = None,
) -> int:
    """Dispatch an acquisition build to its runner.

    Looks up the engine in ``_ACQUISITION_RUNNER_REGISTRY`` and invokes
    the registered callable. Capability negotiation runs first so a
    contract asking for an unsupported capability fails with the rich
    ``CapabilityMismatchError`` instead of running the wrong shape.
    """
    engine = (build.get("engine") or "").strip().lower()

    # Capability negotiation: each runner declares the capabilities it
    # supports via ``declared_capabilities``. If the build asks for one
    # the runner doesn't declare, raise the typed catalog error so the
    # user sees the five-field Panel pointing to a runner that does.
    asked = [str(c) for c in (build.get("capabilities") or [])]
    if asked:
        declared = _declared_capabilities_for_engine(engine)
        if declared is not None and not set(asked).issubset(set(declared)):
            from fluid_build.cli._errors import CapabilityMismatchError

            raise CapabilityMismatchError.for_runner(
                runner_name=engine,
                asked=asked,
                declared=list(declared),
            )

    entry = _ACQUISITION_RUNNER_REGISTRY.get(engine)
    if entry is None:
        LOG.error(
            "acquisition.engine_not_implemented engine=%s build=%s",
            engine,
            build.get("id"),
        )
        return 1

    module_path, function_name = entry
    import importlib

    module = importlib.import_module(module_path, package="fluid_build.build_runners")
    runner_fn = getattr(module, function_name)
    return runner_fn(
        build,
        contract,
        contract_dir,
        dry_run=dry_run,
        sample_rows=sample_rows,
    )


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

        if is_acquisition_build(build):
            sample_rows = getattr(args, "sample_rows", None)
            result = _execute_acquisition_build(
                build,
                contract,
                contract_path.parent,
                dry_run=args.dry_run,
                sample_rows=sample_rows,
            )
            if result == 0:
                total_executed += 1
            else:
                total_failed += 1
                if args.fail_fast:
                    break
            continue

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
                # Forward the apply mode so destructive modes (replace
                # / replace-and-build) append ``--full-refresh`` to dbt.
                apply_mode=getattr(args, "mode", None),
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
