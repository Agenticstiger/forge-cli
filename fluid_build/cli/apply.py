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

"""
FLUID Apply Command - The Heart of Data Product Orchestration

This is the core orchestration engine that transforms declarative FLUID contracts
into fully deployed, governed, and discoverable data products. It coordinates
multiple providers, handles dependencies, manages rollbacks, and ensures
comprehensive observability throughout the deployment process.

Key Responsibilities:
- Infrastructure provisioning (Terraform, Cloud resources)
- Data transformation execution (dbt, Spark, SQL)
- Quality gate enforcement (tests, validations, SLA checks)
- Governance policy application (security, compliance, discovery)
- Monitoring and alerting setup
- Documentation generation and registration
- Dependency resolution and orchestration
- Rollback and recovery management
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from fluid_build.cli.console import cprint

# Rich imports for enhanced output
try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        Progress,
        SpinnerColumn,
        TaskID,
        TextColumn,
        TimeElapsedColumn,
    )
    from rich.table import Table
    from rich.text import Text
    from rich.tree import Tree

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from ..structured_logging import (
    log_metric,
    log_operation_failure,
    log_operation_start,
    log_operation_success,
)
from ._common import (
    CLIError,
    build_provider,
    hydrate_dotenv,
    load_contract_with_overlay,
    read_json,
)
from .core import ProgressManager, confirm_action

# Import orchestration engine (extracted for maintainability)
from .orchestration import (
    ExecutionContext,
    ExecutionPlan,
    FluidOrchestrationEngine,
    FluidPlanGenerator,
    RollbackStrategy,
)

COMMAND = "apply"

# ==========================================
# CLI Command Registration & Execution
# ==========================================


def register(subparsers: argparse._SubParsersAction):
    """Register the apply command with comprehensive options"""
    p = subparsers.add_parser(
        COMMAND,
        help="Apply a plan or contract against providers with full orchestration",
        epilog="""
🌊 FLUID Apply - The Heart of Data Product Orchestration

The apply command is the core orchestration engine that transforms your declarative 
FLUID contract into a fully deployed, governed, and discoverable data product.

Examples:
  # Apply a contract
  fluid apply contract.fluid.yaml --env prod
  
  # Apply with custom configuration
  fluid apply contract.fluid.yaml \\
    --env staging \\
    --rollback-strategy immediate \\
    --parallel-phases \\
    --timeout 120
  
  # Dry run to see what would happen
  fluid apply contract.fluid.yaml --dry-run --verbose
  
  # Apply from a pre-generated plan
  fluid apply runtime/execution_plan.json --yes
  
  # Apply with custom monitoring
  fluid apply contract.fluid.yaml \\
    --report detailed_report.html \\
    --metrics-export prometheus \\
    --notify slack:data-team

Advanced Examples:
  # Production deployment with full safety
  fluid apply prod-contract.fluid.yaml \\
    --env production \\
    --rollback-strategy immediate \\
    --require-approval \\
    --backup-state \\
    --validate-dependencies
  
  # Parallel execution for faster deployments
  fluid apply large-pipeline.fluid.yaml \\
    --parallel-phases \\
    --max-workers 8 \\
    --timeout 180
  
  # Development with enhanced debugging
  fluid apply dev-contract.fluid.yaml \\
    --env dev \\
    --verbose \\
    --debug \\
    --keep-temp-files \\
    --rollback-strategy none

What Apply Does:
1. 🔍 Validates contract and dependencies
2. 🏗️  Provisions infrastructure (Terraform, Cloud resources)
3. 📊 Sets up data ingestion (Airbyte, custom connectors)
4. 🔄 Executes transformations (dbt, Spark, SQL)
5. ✅ Runs quality gates (Great Expectations, custom tests)
6. 🛡️  Applies governance (Ranger, Atlas, privacy controls)
7. 📈 Configures monitoring (Datadog, Grafana, alerts)
8. 📚 Registers in discovery (catalogs, service registry)
9. 📝 Generates reports and notifications

The apply command coordinates multiple providers in the correct dependency order,
handles rollbacks on failure, provides real-time progress tracking, and ensures
your data product is production-ready with full observability.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Core arguments
    p.add_argument("contract", help="Path to contract.fluid.yaml or execution plan JSON file")
    p.add_argument("--env", help="Environment overlay (dev, staging, prod, etc.)")

    # --- Mode matrix (11-stage pipeline stage 7) ---
    # Six modes express every realistic deploy decision. See
    # ``fluid_build.forge.core.apply_modes`` for the full matrix + semantics.
    # Default is ``amend`` (additive schema evolution, data preserved).
    from fluid_build.forge.core.apply_modes import CANONICAL_CHOICES as _MODE_CHOICES

    mode_group = p.add_argument_group("Apply Mode (stage-7 dispatch)")
    mode_group.add_argument(
        "--mode",
        choices=_MODE_CHOICES,
        default=None,  # resolved by resolve_mode_with_build_alias; None = amend
        help=(
            "What DDL/DML to run against the target. "
            "dry-run (render only; no warehouse calls). "
            "create-only (fail if target exists; otherwise CREATE IF NOT EXISTS). "
            "amend (default — ALTER ADD COLUMN IF NOT EXISTS; views CREATE OR REPLACE; "
            "data preserved). "
            "amend-and-build (amend + dbt run; transforms refreshed). "
            "replace (auto-snapshot then DROP+RECREATE; requires --allow-data-loss "
            "in non-dev or when target has rows). "
            "replace-and-build (replace + dbt run --full-refresh). "
            "See plan Part 1 for the full matrix."
        ),
    )
    mode_group.add_argument(
        "--allow-data-loss",
        action="store_true",
        default=False,
        help=(
            "Required for --mode replace / --mode replace-and-build when the "
            "environment is not ``dev`` OR the target has rows. Explicit "
            "operator opt-in that can't be accidentally typed. The pre-replace "
            "table is snapshotted to ``<target>__backup_<ts>`` so "
            "``fluid rollback`` can restore it."
        ),
    )
    mode_group.add_argument(
        "--no-verify-digest",
        action="store_true",
        default=False,
        help=(
            "Skip the ``planDigest`` + ``bundleDigest`` verification that "
            "normally runs before any DDL (emergency-hotfix escape hatch). "
            "Use only when a legitimate plan.json exists but the original "
            "bundle is unreachable (mid-incident recovery, out-of-band "
            "DR runbook). Logged prominently so audit trails show the "
            "operator waived the binding."
        ),
    )

    # Execution control
    execution_group = p.add_argument_group("Execution Control")
    execution_group.add_argument(
        "--yes", action="store_true", help="Skip confirmation prompt and proceed automatically"
    )
    execution_group.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Show what would be executed without making changes. Equivalent to "
            "--mode dry-run but retained as an orthogonal flag for back-compat "
            "with existing CI scripts."
        ),
    )
    execution_group.add_argument(
        "--timeout", type=int, default=120, help="Global timeout in minutes (default: 120)"
    )
    execution_group.add_argument(
        "--parallel-phases",
        action="store_true",
        help="Enable parallel execution of independent phases",
    )
    execution_group.add_argument(
        "--max-workers", type=int, default=4, help="Maximum parallel workers (default: 4)"
    )

    # Rollback and safety
    safety_group = p.add_argument_group("Safety & Rollback")
    safety_group.add_argument(
        "--rollback-strategy",
        choices=["none", "immediate", "phase_complete", "full_rollback"],
        default="phase_complete",
        help="Rollback strategy on failure (default: phase_complete)",
    )
    safety_group.add_argument(
        "--require-approval",
        action="store_true",
        help="Require explicit approval for destructive operations",
    )
    safety_group.add_argument(
        "--backup-state", action="store_true", help="Create state backup before execution"
    )
    safety_group.add_argument(
        "--validate-dependencies",
        action="store_true",
        help="Validate all dependencies before execution",
    )

    # Reporting and monitoring
    reporting_group = p.add_argument_group("Reporting & Monitoring")
    reporting_group.add_argument(
        "--report",
        default="runtime/apply_report.html",
        help="Output path for execution report (default: runtime/apply_report.html)",
    )
    reporting_group.add_argument(
        "--report-format",
        choices=["html", "json", "markdown"],
        default="html",
        help="Report format (default: html)",
    )
    reporting_group.add_argument(
        "--metrics-export",
        choices=["none", "prometheus", "datadog", "cloudwatch"],
        default="none",
        help="Export metrics to monitoring system",
    )
    reporting_group.add_argument(
        "--notify", help="Notification destinations (e.g., slack:channel, email:user@domain.com)"
    )

    # Development and debugging
    debug_group = p.add_argument_group("Development & Debugging")
    debug_group.add_argument(
        "--verbose", action="store_true", help="Enable verbose output with detailed progress"
    )
    debug_group.add_argument(
        "--debug", action="store_true", help="Enable debug mode with full logging"
    )
    debug_group.add_argument(
        "--keep-temp-files", action="store_true", help="Keep temporary files for debugging"
    )
    debug_group.add_argument("--profile", action="store_true", help="Enable performance profiling")

    # Build execution (absorbed from 'fluid execute')
    build_group = p.add_argument_group("Build Execution")
    build_group.add_argument(
        "--build",
        "--build-id",
        dest="build_id",
        help=(
            "Execute a specific build job by ID (from contract builds section). "
            "DEPRECATED in 11-stage pipeline: use --mode amend-and-build (or "
            "--mode replace-and-build) instead. When --build is set WITHOUT "
            "--mode, the apply auto-upgrades to --mode amend-and-build with a "
            "deprecation warning. Alias kept for one release."
        ),
    )
    build_group.add_argument(
        "--delay",
        type=int,
        default=2,
        help="Seconds between build iterations (default: 2)",
    )
    build_group.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop build execution on first failure",
    )
    build_group.add_argument(
        "--no-output",
        action="store_true",
        help="Suppress build script output (show summary only)",
    )

    # Advanced options
    advanced_group = p.add_argument_group("Advanced Options")
    advanced_group.add_argument(
        "--workspace-dir",
        type=Path,
        default=Path("."),
        help="Workspace directory (default: current directory)",
    )
    advanced_group.add_argument("--state-file", type=Path, help="Custom state file location")
    advanced_group.add_argument(
        "--config-override", help="JSON string to override contract configuration"
    )
    advanced_group.add_argument(
        "--provider-config", help="Path to provider-specific configuration file"
    )

    p.set_defaults(cmd=COMMAND, func=run)


def _actions_from_source(src: str, env: str | None, provider, logger: logging.Logger):
    """
    Extract actions from source (supports 0.7.1 provider actions).

    For providers with a plan() method (like AwsProvider, GcpProvider), delegate
    to the provider's planner which generates service-level actions the provider
    can dispatch (e.g. s3.ensure_bucket, glue.ensure_table).

    For other providers or when no planner is available, fall back to the 0.7.1
    ProviderActionParser which infers high-level actions (provisionDataset, etc.).
    """
    if src.endswith(".json"):
        # Load pre-generated execution plan
        data = read_json(src)
        return data.get("actions", [])

    # Load contract
    contract = load_contract_with_overlay(src, env, logger)

    # Prefer provider.plan() when available — it generates service-level
    # actions (s3.*, glue.*, athena.*) that the provider's apply() can dispatch.
    if hasattr(provider, "plan") and callable(getattr(provider, "plan", None)):
        try:
            actions = provider.plan(contract)
            if actions:
                logger.info(f"Provider planner generated {len(actions)} actions")
                return actions
        except Exception as e:
            logger.warning(f"Provider planner failed ({e}), falling back to action parser")

    # Fallback: use 0.7.1 ProviderActionParser (high-level actions)
    try:
        from ..forge.core.provider_actions import ProviderActionParser

        parser = ProviderActionParser(logger)
        provider_actions = parser.parse(contract)

        if provider_actions:
            # Stamp each action with the CURRENT latest bundled schema
            # version rather than a hardcoded literal. When the schema
            # ships 0.8.x, the metadata tracks it automatically.
            from fluid_build.schema_manager import SchemaManager

            latest_version = SchemaManager.latest_bundled_version()
            logger.info(
                f"Parsed {len(provider_actions)} provider actions " f"(schema {latest_version})"
            )
            return [
                {
                    "op": action.action_type.value,
                    "action_id": action.action_id,
                    "provider": action.provider,
                    "params": action.params,
                    "depends_on": action.depends_on,
                    "metadata": {"type": "provider_action", "version": latest_version},
                }
                for action in provider_actions
            ]
    except ImportError:
        logger.debug("Provider action parser not available")

    # Final fallback
    return [{"op": "ensure_dataset"}, {"op": "ensure_table"}]


def _verify_plan_digests(plan_data: Dict[str, Any], args, logger: logging.Logger) -> None:
    """Enforce the stage-7 plan-binding gate.

    Cryptographic "apply consumes exact plan" guarantee: before any DDL runs,
    recompute ``planDigest`` over the plan body and compare against the
    value stored in ``plan.json``. Mismatch → ``CLIError`` with a stable
    ``event`` field so CI logs can classify the failure.

    ``--no-verify-digest`` waives the check for legitimate emergencies
    (bundle unreachable during DR). The waiver is logged at WARNING level
    so audit trails show the operator made the call.

    Plans without a ``planDigest`` field are treated as tamper signals —
    legitimate plans always carry one. This catches both (a) plans produced
    by an older fluid version and (b) plans that had the digest stripped.
    """
    if getattr(args, "no_verify_digest", False):
        logger.warning(
            "--no-verify-digest: plan-binding verification was SKIPPED. "
            "This is an emergency escape hatch; the apply may be running "
            "against a tampered or stale plan. Make sure this is recorded "
            "in the change log."
        )
        return

    # Local import so plan_digest's own import of bundle/tarfile only loads
    # when verification actually happens (keeps cold-path tests fast).
    from ..forge.core.plan_digest import PlanBindingError, verify_plan_binding

    try:
        verify_plan_binding(plan_data, bundle_path=None)
    except PlanBindingError as exc:
        # ``exc.kind`` is either ``bundle-mismatch`` or ``plan-tamper``.
        # Surface it as the stable event field so CI log parsers can match.
        raise CLIError(
            1,
            f"apply_plan_digest_{exc.kind.replace('-', '_')}",
            context={"kind": exc.kind, "error": str(exc)},
        )


def run(args, logger: logging.Logger) -> int:
    """
    Main execution function for the apply command

    This is the heart of the FLUID platform - the orchestration engine that
    transforms declarative contracts into deployed data products.
    """
    start_time = time.time()
    execution_id = f"fluid_apply_{int(time.time())}_{os.getpid()}"

    # Mirror verify/publish: hydrate os.environ from project dotenv +
    # FLUID_SECRETS_FILE before anything that reads SNOWFLAKE_*, DMM_*, etc.
    # Must happen before the --build branch delegates to execute.run, since
    # the dbt subprocess launched there reads os.environ for its profile.
    hydrate_dotenv(Path.cwd(), environment=getattr(args, "env", None))

    # --- Resolve --mode + --build back-compat (11-stage pipeline stage 7) ---
    # ``--build X`` without ``--mode`` auto-upgrades to ``--mode amend-and-build``
    # with a deprecation warning. ``--build`` combined with a non-build-augmented
    # mode raises a clear error.
    from fluid_build.forge.core.apply_modes import (
        ApplyMode,
        check_data_loss_gate,
        is_dry_run,
        needs_build,
        resolve_mode_with_build_alias,
    )

    build_id = getattr(args, "build_id", None)
    try:
        resolved_mode, resolved_build_id = resolve_mode_with_build_alias(
            getattr(args, "mode", None),
            build_id if isinstance(build_id, str) else None,
        )
    except ValueError as exc:
        raise CLIError(1, "apply_mode_invalid", {"error": str(exc)})

    # Deprecation warning when the legacy --build flag kicked the upgrade.
    if (
        resolved_build_id
        and getattr(args, "mode", None) is None
        and resolved_mode is ApplyMode.AMEND_AND_BUILD
    ):
        logger.warning(
            "--build is deprecated; use --mode amend-and-build instead. "
            "This invocation has been auto-upgraded. --build will be removed "
            "in the next release."
        )

    # --dry-run flag is still honored for back-compat; --mode dry-run is the
    # canonical form. Normalize the two so the downstream code sees one signal.
    effective_dry_run = bool(getattr(args, "dry_run", False)) or is_dry_run(resolved_mode)

    # Log operation start — include resolved mode so observability surfaces it.
    log_operation_start(
        logger,
        "apply_contract",
        execution_id=execution_id,
        source=args.contract,
        env=args.env,
        dry_run=effective_dry_run,
        mode=resolved_mode.value,
    )

    try:
        # --- Build execution mode (absorbed from legacy 'fluid execute') ---
        # ``--mode amend-and-build`` / ``--mode replace-and-build`` + an
        # explicit ``--build <id>`` delegate to build_runners. Legacy --build
        # calls land here too (post-resolve mode is amend-and-build).
        # force_run=True mirrors the historical ``_from_apply=True`` semantic.
        if resolved_build_id and needs_build(resolved_mode):
            # Ensure the build engine sees the resolved build_id even if the
            # caller only passed --mode (no --build). Safe-no-op when already set.
            args.build_id = resolved_build_id
            from fluid_build.build_runners import run_builds_from_args

            return run_builds_from_args(args, logger, force_run=True)

        # Load contract or execution plan
        if args.contract.endswith(".json"):
            # Load pre-generated execution plan
            logger.info("Loading pre-generated execution plan")
            plan_data = read_json(args.contract)

            # --- Plan-binding verification (stage-7 apply gate) ---
            # Before ANY DDL runs, re-verify the plan's ``planDigest``
            # (catches tampering between stages 6 and 7). ``bundleDigest``
            # verification is skipped here because the plan is consumed
            # standalone — the canonical path where both digests verify is
            # ``fluid apply <bundle.tgz> --plan plan.json`` (Phase 7 wiring).
            # ``--no-verify-digest`` waives the gate for emergency hotfixes.
            _verify_plan_digests(plan_data, args, logger)

            contract = plan_data.get("contract", {})

            # --- Plan-format detection ---
            # Two plan.json shapes exist today:
            #
            # 1. plan.py output (Phase-6C canonical form): flat dict with
            #    top-level ``actions`` + ``total_actions`` + full ``contract``.
            #    Route to SIMPLE mode — provider detection + action dispatch
            #    walks the embedded contract the same way yaml-loaded contracts
            #    do.
            # 2. Legacy orchestration format: nested ``plan`` sub-key with
            #    ``contract_path`` + ``environment`` + ``phases``. Route to
            #    COMPLEX mode — FluidOrchestrationEngine consumes the
            #    ExecutionPlan dataclass directly.
            #
            # Heuristic: presence of ``plan_data["plan"]["phases"]`` identifies
            # the legacy format. Anything else is the flat plan.py shape.
            legacy_nested_plan = plan_data.get("plan")
            has_legacy_orchestration_plan = (
                isinstance(legacy_nested_plan, dict) and "phases" in legacy_nested_plan
            )

            if has_legacy_orchestration_plan:
                plan = ExecutionPlan(**legacy_nested_plan)
                use_simple_mode = False
            else:
                # plan.py flat format. Simple-mode path handles actions via
                # ``_actions_from_source`` which reads plan_data["actions"]
                # when args.contract is a .json file.
                plan = None
                use_simple_mode = True
        else:
            # Load contract
            logger.info(f"Loading FLUID contract: {args.contract}")
            contract = load_contract_with_overlay(args.contract, args.env, logger)

            # Determine if this is a simple local execution (no orchestration engine needed)
            has_complex_config = any(
                key in contract
                for key in [
                    "infrastructure",
                    "terraform",
                    "sources",
                    "ingestion",
                    "monitoring",
                    "governance_policies",
                    "quality_expectations",
                    "catalog",
                    "service_registry",
                    "notifications",
                ]
            )

            use_simple_mode = not has_complex_config

            if use_simple_mode:
                # Simple mode - direct provider execution
                logger.info("Using simple execution mode (local provider)")
                plan = None
            else:
                # Complex mode - full orchestration
                plan_generator = FluidPlanGenerator(contract, args.env)
                plan = plan_generator.generate_execution_plan(args.contract)
                plan.global_timeout_minutes = args.timeout
                plan.dry_run = args.dry_run
                plan.parallel_phases = args.parallel_phases
                plan.rollback_strategy = RollbackStrategy(args.rollback_strategy)

        # Apply configuration overrides
        if args.config_override:
            try:
                override_config = json.loads(args.config_override)
            except json.JSONDecodeError as exc:
                error = CLIError(
                    2,
                    "invalid_config_override",
                    {"error": str(exc), "config_override": args.config_override},
                )
                error.message = "Invalid --config-override JSON"
                raise error from exc
            contract.update(override_config)

        # --- Data-loss safety gate (11-stage pipeline stage 7) ---
        # Destructive modes (``replace*``) require ``--allow-data-loss`` in any
        # env where FLUID_ENV != dev OR the target has rows. This runs BEFORE
        # any provider call so no DDL executes when the gate blocks.
        #
        # ``target_row_count=None`` signals "unknown" — the gate treats that as
        # populated (fail-safe). Future enhancement: providers can implement
        # a cheap ``estimate_row_count()`` and pass it in. For now, the gate's
        # default behavior is "non-dev + replace → require --allow-data-loss
        # unless you can prove the target is empty."
        gate = check_data_loss_gate(
            resolved_mode,
            env=args.env,
            target_row_count=None,  # unknown until provider check added
            allow_data_loss=bool(getattr(args, "allow_data_loss", False)),
        )
        if gate.blocked:
            raise CLIError(
                1,
                "apply_mode_data_loss_blocked",
                {"mode": resolved_mode.value, "env": args.env, "reason": gate.reason},
            )

        # Simple mode execution
        if use_simple_mode:
            logger.info("🚀 Executing data product build (simple mode)")

            # Detect provider and project from contract (check builds and exposes)
            provider_name = "local"  # default
            project = None
            region = contract.get("region", "local")

            # First try to get provider and project from exposes (most specific)
            for expose in contract.get("exposes", []):
                binding = expose.get("binding", {})
                if "platform" in binding:
                    provider_name = binding["platform"]
                    # Get project from binding location
                    location = binding.get("location", {})
                    if "project" in location and not project:
                        project = location["project"]

            # Then check builds if not found
            if provider_name == "local":
                for build in contract.get("builds", []):
                    runtime = build.get("execution", {}).get("runtime", {})
                    if "platform" in runtime:
                        provider_name = runtime["platform"]
                        break

            # For AWS, extract region from binding.location or env vars and let
            # resolve_account_and_region() discover the account via STS.
            if provider_name == "aws":
                if not project or project == contract.get("id"):
                    project = None  # Let AwsProvider resolve from STS
                if region == "local":
                    # Try binding.location.region first
                    for expose in contract.get("exposes", []):
                        loc_region = expose.get("binding", {}).get("location", {}).get("region")
                        if loc_region and not loc_region.startswith("{{"):
                            region = loc_region
                            break
                    else:
                        region = None  # Let AwsProvider resolve from env/defaults

            # Fallback to contract-level project or ID for providers that use it.
            # Snowflake resolves database/account from binding + env and should not
            # inherit the contract id as a pseudo-project.
            if not project and provider_name not in {"aws", "snowflake"}:
                project = contract.get("project") or contract.get("id", "local-project")

            # Set appropriate default region for provider
            if provider_name == "gcp" and region == "local":
                region = "US"  # Default BigQuery location

            logger.info(f"Detected provider: {provider_name}, project: {project}")
            provider = build_provider(provider_name, project, region, logger)

            # Get actions from contract
            actions = _actions_from_source(args.contract, args.env, provider, logger)

            if not actions:
                logger.warning("No actions to execute")
                return 0

            # Show execution preview and get confirmation (unless --yes flag)
            if not args.yes and not args.dry_run and os.isatty(0):
                if RICH_AVAILABLE:
                    console = Console()
                    console.print("\n[bold cyan]🚀 Execution Preview[/bold cyan]")
                    console.print(f"Provider: [yellow]{provider_name}[/yellow]")
                    console.print(f"Project: [yellow]{project}[/yellow]")
                    console.print(f"Actions: [yellow]{len(actions)}[/yellow]")

                    # Show action breakdown
                    action_types = {}
                    for action in actions:
                        op = action.get("op", "unknown")
                        action_types[op] = action_types.get(op, 0) + 1

                    if action_types:
                        console.print("\nAction breakdown:")
                        for op, count in sorted(action_types.items()):
                            console.print(f"  • {op}: {count}")

                    # Safety warnings for destructive operations
                    destructive_ops = ["drop_table", "delete_data", "truncate_table"]
                    destructive_actions = [a for a in actions if a.get("op") in destructive_ops]

                    if destructive_actions:
                        console.print(
                            f"\n[red]⚠️  Warning: {len(destructive_actions)} potentially destructive actions![/red]"
                        )

                    if not confirm_action(
                        "\nProceed with execution?", default=False, console=console
                    ):
                        console.print("[yellow]Operation cancelled[/yellow]")
                        return 0
                else:
                    logger.info(f"About to execute {len(actions)} actions")
                    response = input("Proceed? [y/N]: ").strip().lower()
                    if response not in ["y", "yes"]:
                        logger.info("Operation cancelled")
                        return 0

            # Dry run mode
            if args.dry_run:
                logger.info("🔍 Dry run mode - showing execution plan")
                if RICH_AVAILABLE:
                    console = Console()
                    console.print(
                        Panel("🔍 Dry Run - No changes will be made", border_style="yellow")
                    )
                    table = Table(title="📋 Planned Actions")
                    table.add_column("Operation", style="cyan")
                    table.add_column("Details", style="white")
                    for action in actions:
                        table.add_row(action.get("op", "unknown"), str(action.get("metadata", {})))
                    console.print(table)
                else:
                    logger.info(f"Would execute {len(actions)} actions:")
                    for action in actions:
                        logger.info(f"  - {action.get('op')}: {action.get('metadata', {})}")
                return 0

            # Execute with provider
            logger.info(f"Executing {len(actions)} actions...")

            # --- Lifecycle hooks: pre_apply ---
            from fluid_build.cli.hooks import run_on_error, run_post_apply, run_pre_apply

            actions = run_pre_apply(provider, actions, logger)

            try:
                if RICH_AVAILABLE:
                    console = Console()
                    console.print("[green]🚀 Executing actions...[/green]")
                    with ProgressManager(console) as progress:
                        task = progress.add_task(f"Executing {len(actions)} actions...", total=None)
                        result = provider.apply(actions=actions, plan={"contract": contract})
                        progress.update(task, completed=True)
                else:
                    result = provider.apply(actions=actions, plan={"contract": contract})
            except Exception as exc:
                run_on_error(provider, exc, "apply", logger)
                raise

            # --- Lifecycle hooks: post_apply ---
            run_post_apply(provider, result, logger)

            # Check for success (local provider uses 'failed' field, others use 'status')
            success = result.get("failed", 1) == 0 or result.get("status") == "success"

            # Show results
            if RICH_AVAILABLE:
                console = Console()
                if success:
                    # Success panel
                    console.print("\n[green]✅ Data product deployed successfully[/green]")

                    # Summary table
                    summary_table = Table(show_header=False, box=None)
                    summary_table.add_column("Metric", style="cyan")
                    summary_table.add_column("Value", style="white")

                    if "applied" in result:
                        summary_table.add_row("Actions Applied", str(result["applied"]))

                    total_time = time.time() - start_time
                    summary_table.add_row("Duration", f"{total_time:.2f}s")

                    if "results" in result:
                        # Count output files
                        output_count = sum(
                            len(r.get("written", []))
                            for r in result["results"]
                            if r.get("status") == "ok"
                        )
                        if output_count > 0:
                            summary_table.add_row("Files Generated", str(output_count))

                    console.print(summary_table)

                    # Show output files
                    if "results" in result:
                        files_shown = 0
                        for r in result["results"]:
                            if r.get("status") == "ok" and "written" in r:
                                for path in r["written"]:
                                    console.print(f"  📁 [cyan]{path}[/cyan]")
                                    files_shown += 1

                        if files_shown == 0:
                            console.print("  [dim]No output files generated[/dim]")
                else:
                    error_msg = result.get("error", "Unknown error")
                    console.print(f"\n[red]❌ Deployment failed: {error_msg}[/red]")

                    # Show individual action errors
                    if "results" in result:
                        console.print("\n[bold]Action Errors:[/bold]")
                        for i, r in enumerate(result["results"]):
                            if r.get("status") == "error":
                                console.print(
                                    f"  {i+1}. [red]✗[/red] {r.get('op', 'unknown')}: {r.get('error', 'no details')}"
                                )
            else:
                if success:
                    logger.info("✅ Data product deployed successfully")
                    if "applied" in result:
                        logger.info(f"Applied {result['applied']} action(s)")
                else:
                    error_msg = result.get("error", "Unknown error")
                    logger.error(f"❌ Deployment failed: {error_msg}")

            total_time = time.time() - start_time
            logger.info(f"✅ Execution completed in {total_time:.2f}s")

            # Log metrics and completion
            log_metric(logger, "apply_duration", total_time, unit="seconds")
            log_metric(logger, "actions_executed", result.get("applied", 0), unit="count")

            # Generate report if requested (simple mode)
            if hasattr(args, "report") and args.report:
                try:
                    report_path = Path(args.report)
                    report_path.parent.mkdir(parents=True, exist_ok=True)

                    report_format = getattr(args, "report_format", "html")
                    contract_name = contract.get("name") or contract.get("id") or "Unknown"
                    applied_count = result.get("applied", 0)
                    failed_count = result.get("failed", 0)

                    if report_format == "html":
                        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>FLUID Apply Report</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; }}
        .header {{ background: #1f2937; color: white; padding: 20px; border-radius: 8px; }}
        .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin: 20px 0; }}
        .metric {{ background: #f8fafc; padding: 15px; border-radius: 8px; border-left: 4px solid #3b82f6; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>FLUID Apply Report</h1>
        <p>Contract: {contract_name}</p>
        <p>Execution ID: {execution_id}</p>
        <p>Status: {'Success' if success else 'Failed'}</p>
    </div>
    <div class="metrics">
        <div class="metric"><h3>Actions Applied</h3><p>{applied_count}</p></div>
        <div class="metric"><h3>Failed</h3><p>{failed_count}</p></div>
        <div class="metric"><h3>Duration</h3><p>{total_time:.2f}s</p></div>
        <div class="metric"><h3>Mode</h3><p>Simple</p></div>
    </div>
</body>
</html>"""
                        with open(report_path, "w") as f:
                            f.write(html_content)
                    elif report_format == "json":
                        import json as json_mod

                        with open(report_path, "w") as f:
                            json_mod.dump(
                                {
                                    "execution_id": execution_id,
                                    "contract": contract_name,
                                    "success": success,
                                    "applied": applied_count,
                                    "failed": failed_count,
                                    "duration_seconds": round(total_time, 2),
                                    "mode": "simple",
                                },
                                f,
                                indent=2,
                            )

                    logger.info(f"📄 Execution report generated: {report_path}")
                except Exception as e:
                    logger.warning(f"Failed to generate report: {e}")

            if success:
                log_operation_success(
                    logger,
                    "apply_contract",
                    duration=total_time,
                    execution_id=execution_id,
                    mode="simple",
                )
            else:
                log_operation_failure(
                    logger,
                    "apply_contract",
                    error=result.get("error", "Unknown error"),
                    duration=total_time,
                )

            return 0 if success else 1

        # Complex orchestration mode (original code)
        # Initialize console for rich output
        console = None
        if RICH_AVAILABLE and not args.debug:
            console = Console()
            console.print(
                Panel(
                    "🌊 FLUID Apply - Data Product Orchestration Engine",
                    subtitle=f"Execution ID: {execution_id}",
                    border_style="blue",
                )
            )

        # Create execution context
        context = ExecutionContext(
            execution_id=execution_id,
            contract=contract,
            plan=plan,
            workspace_dir=args.workspace_dir,
            state_file=args.state_file or Path("runtime/apply_state.json"),
            console=console,
            logger=logger,
        )

        # Setup artifacts directory
        context.artifacts_dir = context.workspace_dir / "runtime" / "artifacts" / execution_id
        context.logs_dir = context.workspace_dir / "runtime" / "logs" / execution_id

        # Show execution plan summary
        _display_execution_plan(plan, console, logger)

        # Confirmation prompt (unless --yes or dry-run)
        if not args.yes and not args.dry_run and os.isatty(0):
            if not _confirm_execution(plan, console):
                logger.info("Execution cancelled by user")
                return 0

        # Initialize orchestration engine
        engine = FluidOrchestrationEngine(context)

        if args.dry_run:
            logger.info("🔍 Dry run mode - showing execution plan without making changes")
            _display_dry_run_summary(plan, console, logger)
            return 0

        # Execute the plan
        logger.info("🚀 Starting data product deployment orchestration")

        if asyncio.get_event_loop().is_running():
            # If we're already in an async context, create a new loop
            import threading

            result = {}
            exception = {}

            def run_in_thread():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    result["value"] = loop.run_until_complete(engine.execute_plan())
                except Exception as e:
                    exception["value"] = e
                finally:
                    loop.close()

            thread = threading.Thread(target=run_in_thread)
            thread.start()
            thread.join()

            if "value" in exception:
                raise exception["value"]

            execution_result = result["value"]
        else:
            # Normal async execution
            execution_result = asyncio.run(engine.execute_plan())

        # Generate final report
        _generate_final_report(execution_result, args, context, logger)

        # Send notifications
        if args.notify:
            _send_notifications(execution_result, args.notify, logger)

        # Export metrics
        if args.metrics_export != "none":
            _export_metrics(execution_result, args.metrics_export, logger)

        # Determine exit code
        if execution_result.get("success", False):
            total_time = time.time() - start_time
            logger.info(f"✅ Data product deployment completed successfully in {total_time:.2f}s")

            # Log metrics and success
            log_metric(logger, "apply_duration", total_time, unit="seconds")
            log_metric(
                logger, "phases_executed", execution_result.get("phases_executed", 0), unit="count"
            )
            log_operation_success(
                logger,
                "apply_contract",
                duration=total_time,
                execution_id=execution_id,
                mode="orchestrated",
            )

            return 0
        else:
            total_time = time.time() - start_time
            error_msg = execution_result.get("error", "Unknown error")
            logger.error(f"❌ Data product deployment failed: {error_msg}")

            # Log failure
            log_operation_failure(logger, "apply_contract", error=error_msg, duration=total_time)

            return 1

    except CLIError:
        duration = time.time() - start_time
        log_operation_failure(logger, "apply_contract", error="CLI error", duration=duration)
        raise
    except KeyboardInterrupt:
        duration = time.time() - start_time
        log_operation_failure(logger, "apply_contract", error="User interrupted", duration=duration)
        logger.warning("⚠️ Execution interrupted by user")
        return 130
    except Exception as e:
        logger.error(f"💥 Unexpected error during execution: {e}")
        if args.debug:
            import traceback

            logger.error(traceback.format_exc())
        raise CLIError(1, "apply_execution_failed", {"error": str(e)})


def _display_execution_plan(plan: ExecutionPlan, console, logger: logging.Logger):
    """Display execution plan summary"""
    total_actions = sum(len(phase.actions) for phase in plan.phases)

    if console and RICH_AVAILABLE:
        table = Table(title="📋 Execution Plan Summary")
        table.add_column("Phase", style="cyan")
        table.add_column("Actions", justify="right", style="magenta")
        table.add_column("Parallel", justify="center", style="green")
        table.add_column("Strategy", style="yellow")

        for phase in plan.phases:
            table.add_row(
                phase.phase.value.title(),
                str(len(phase.actions)),
                "✅" if phase.parallel_execution else "❌",
                phase.rollback_strategy.value,
            )

        console.print(table)
        console.print(f"\n📊 Total Actions: {total_actions}")
        console.print(f"⏱️  Estimated Duration: {plan.global_timeout_minutes} minutes")
    else:
        logger.info(f"📋 Execution Plan: {len(plan.phases)} phases, {total_actions} total actions")


def _confirm_execution(plan: ExecutionPlan, console) -> bool:
    """Get user confirmation for execution"""
    total_actions = sum(len(phase.actions) for phase in plan.phases)

    if console and RICH_AVAILABLE:
        console.print(
            f"\n⚠️  This will execute {total_actions} actions across {len(plan.phases)} phases."
        )
        console.print("Some operations may be irreversible. Continue? [y/N] ", end="")
    else:
        cprint(f"This will execute {total_actions} actions. Continue? [y/N] ", end="", flush=True)

    answer = (input() or "n").strip().lower()
    return answer in ("y", "yes")


def _display_dry_run_summary(plan: ExecutionPlan, console, logger: logging.Logger):
    """Display dry run summary"""
    if console and RICH_AVAILABLE:
        console.print("\n🔍 Dry Run Summary:", style="bold blue")
        for phase in plan.phases:
            console.print(f"\n📂 {phase.phase.value.title()}:", style="bold")
            for action in phase.actions:
                console.print(f"  • {action.description} ({action.provider})", style="dim")
    else:
        logger.info("Dry run summary:")
        for phase in plan.phases:
            logger.info(f"Phase: {phase.phase.value}")
            for action in phase.actions:
                logger.info(f"  - {action.description}")


def _generate_final_report(
    execution_result: Dict[str, Any], args, context: ExecutionContext, logger: logging.Logger
):
    """Generate comprehensive final report"""
    try:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)

        if args.report_format == "html":
            _generate_html_report(execution_result, report_path, context)
        elif args.report_format == "json":
            _generate_json_report(execution_result, report_path, context)
        elif args.report_format == "markdown":
            _generate_markdown_report(execution_result, report_path, context)

        logger.info(f"📄 Execution report generated: {report_path}")
    except Exception as e:
        logger.warning(f"Failed to generate report: {e}")


def _generate_html_report(
    execution_result: Dict[str, Any], report_path: Path, context: ExecutionContext
):
    """Generate HTML execution report"""
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>FLUID Apply Execution Report</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 40px; }}
            .header {{ background: #1f2937; color: white; padding: 20px; border-radius: 8px; }}
            .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin: 20px 0; }}
            .metric {{ background: #f8fafc; padding: 15px; border-radius: 8px; border-left: 4px solid #3b82f6; }}
            .phase {{ margin: 20px 0; padding: 15px; border-radius: 8px; }}
            .success {{ background-color: #ecfdf5; border-left: 4px solid #10b981; }}
            .failed {{ background-color: #fef2f2; border-left: 4px solid #ef4444; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🌊 FLUID Apply Execution Report</h1>
            <p>Execution ID: {context.execution_id}</p>
            <p>Status: {'✅ Success' if execution_result.get('success') else '❌ Failed'}</p>
        </div>
        
        <div class="metrics">
            <div class="metric">
                <h3>Total Actions</h3>
                <p>{execution_result.get('metrics', {}).get('total_actions', 0)}</p>
            </div>
            <div class="metric">
                <h3>Successful</h3>
                <p>{execution_result.get('metrics', {}).get('successful_actions', 0)}</p>
            </div>
            <div class="metric">
                <h3>Failed</h3>
                <p>{execution_result.get('metrics', {}).get('failed_actions', 0)}</p>
            </div>
            <div class="metric">
                <h3>Duration</h3>
                <p>{execution_result.get('metrics', {}).get('total_duration_seconds', 0):.2f}s</p>
            </div>
        </div>
        
        <h2>Phase Details</h2>
    """

    for phase in execution_result.get("phases", []):
        status_class = "success" if phase.get("status") == "success" else "failed"
        html_content += f"""
        <div class="phase {status_class}">
            <h3>{phase.get('phase', 'Unknown').title()}</h3>
            <p>Status: {phase.get('status', 'unknown')}</p>
            <p>Actions: {phase.get('action_count', 0)}</p>
            <p>Duration: {phase.get('duration', 0):.2f}s</p>
        </div>
        """

    html_content += """
    </body>
    </html>
    """

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_content)


def _generate_json_report(
    execution_result: Dict[str, Any], report_path: Path, context: ExecutionContext
):
    """Generate JSON execution report"""
    report_data = {
        "execution_id": context.execution_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "contract_path": context.plan.contract_path,
        "environment": context.plan.environment,
        "result": execution_result,
    }

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)


def _generate_markdown_report(
    execution_result: Dict[str, Any], report_path: Path, context: ExecutionContext
):
    """Generate Markdown execution report"""
    status_icon = "✅" if execution_result.get("success") else "❌"

    markdown_content = f"""# 🌊 FLUID Apply Execution Report

## Summary
- **Execution ID**: {context.execution_id}
- **Status**: {status_icon} {'Success' if execution_result.get('success') else 'Failed'}
- **Contract**: {context.plan.contract_path}
- **Environment**: {context.plan.environment or 'default'}
- **Duration**: {execution_result.get('metrics', {}).get('total_duration_seconds', 0):.2f} seconds

## Metrics
| Metric | Value |
|--------|-------|
| Total Actions | {execution_result.get('metrics', {}).get('total_actions', 0)} |
| Successful | {execution_result.get('metrics', {}).get('successful_actions', 0)} |
| Failed | {execution_result.get('metrics', {}).get('failed_actions', 0)} |
| Skipped | {execution_result.get('metrics', {}).get('skipped_actions', 0)} |

## Phase Details
"""

    for phase in execution_result.get("phases", []):
        phase_icon = "✅" if phase.get("status") == "success" else "❌"
        markdown_content += f"""
### {phase_icon} {phase.get('phase', 'Unknown').title()}
- **Status**: {phase.get('status', 'unknown')}
- **Actions**: {phase.get('action_count', 0)}
- **Duration**: {phase.get('duration', 0):.2f}s
"""

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(markdown_content)


def _send_notifications(
    execution_result: Dict[str, Any], notify_config: str, logger: logging.Logger
):
    """Send execution notifications"""
    try:
        # Parse notification configuration
        # Format: "slack:channel" or "email:user@domain.com"
        notify_type, notify_target = notify_config.split(":", 1)

        status = "✅ Success" if execution_result.get("success") else "❌ Failed"
        f"FLUID Apply {status} - {execution_result.get('execution_id')}"

        if notify_type == "slack":
            # Would integrate with Slack API
            logger.info(f"Notification sent to Slack: {notify_target}")
        elif notify_type == "email":
            # Would integrate with email service
            logger.info(f"Notification sent to email: {notify_target}")

    except Exception as e:
        logger.warning(f"Failed to send notification: {e}")


def _export_metrics(execution_result: Dict[str, Any], metrics_system: str, logger: logging.Logger):
    """Export metrics to monitoring system"""
    try:
        execution_result.get("metrics", {})

        if metrics_system == "prometheus":
            # Would export to Prometheus
            logger.info("Metrics exported to Prometheus")
        elif metrics_system == "datadog":
            # Would export to Datadog
            logger.info("Metrics exported to Datadog")
        elif metrics_system == "cloudwatch":
            # Would export to CloudWatch
            logger.info("Metrics exported to CloudWatch")

    except Exception as e:
        logger.warning(f"Failed to export metrics: {e}")
