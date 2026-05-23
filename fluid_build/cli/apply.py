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
- Infrastructure provisioning (OpenTofu engine, cloud resources)
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
from typing import Any, Dict, List, Optional

from fluid_build.cli.console import cprint, success, warning
from fluid_build.observability.tracing import traced_stage as _traced_stage

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
# Plugin apply-time hooks (entry-point group)
# ==========================================


def _run_apply_hooks(
    contract: Dict[str, Any],
    contract_dir: Path,
    logger: logging.Logger,
    *,
    force: bool = False,
) -> int:
    """Invoke any plugin-registered apply-time hooks.

    External packages can register a hook by declaring an entry-point in
    their ``pyproject.toml``::

        [project.entry-points."fluid_build.apply_hooks"]
        my-hook = "my_pkg.hooks:verify_something"

    The referenced callable is invoked as
    ``hook(contract_dir, contract, errors_list)`` and may append messages
    to ``errors_list`` to indicate apply-time invariants that have been
    violated (e.g. scaffold bundle digest drift).

    **Trust model.** The hook receives a ``copy.deepcopy`` of the contract,
    not the live reference, so a buggy or malicious hook cannot mutate the
    contract the rest of apply will consume. Plugin code is otherwise
    uncontained (no sandboxing, no timeout, runs in-process); see
    ``SECURITY.md`` for the full plugin trust statement.

    Returns 0 if all hooks passed (or ``force`` was set), non-zero
    otherwise. Plugin exceptions are caught and reported as errors so a
    buggy hook can't crash ``fluid apply`` itself.
    """
    import copy

    from fluid_build.observability.secret_redactor import redact_secret_text

    try:
        import importlib.metadata as _md

        try:
            eps = _md.entry_points(group="fluid_build.apply_hooks")
        except TypeError:
            eps = _md.entry_points().get("fluid_build.apply_hooks", [])
    except Exception as e:
        logger.warning("apply hook discovery failed: %s", redact_secret_text(str(e)))
        return 0

    errors: List[str] = []
    for ep in eps:
        # Defense-in-depth: each hook gets its own deep copy. A hook can
        # observe the contract freely but cannot poison the data structure
        # the rest of apply or other hooks rely on.
        hook_contract = copy.deepcopy(contract)
        try:
            hook = ep.load()
            hook(contract_dir, hook_contract, errors)
        except Exception as e:
            # Pre-redact the exception message — the SecretRedactingFilter
            # only scrubs args bound to ``password=%s``-style template
            # tokens, but plugin exception messages are free-form text
            # that may embed credential-shaped substrings anywhere. We
            # apply ``redact_secret_text`` directly so the error is safe
            # both for the log line below and for any reporter consuming
            # the errors list.
            errors.append(redact_secret_text(f"apply hook {ep.name!r} raised: {e}"))

    if not errors:
        return 0

    if force:
        for err in errors:
            logger.warning("apply hook drift ignored (--force-pattern-drift): %s", err)
        return 0

    for err in errors:
        logger.error("apply hook: %s", err)
    return 1


def _gate_contract_for_apply(contract: Dict[str, Any], logger: logging.Logger) -> None:
    """Reject pre-0.7 + schema-invalid contracts before any DDL.

    Thin wrapper over ``cli/plan.py::_gate_contract_for_plan_or_apply`` so
    the pre-0.7 rejection and JSON-schema validation logic lives in exactly
    one place (DRY). ``fluid apply`` calls this on the contract it is about
    to apply — whether loaded from a ``.fluid.yaml`` or extracted from a
    pre-built ``plan.json`` — so an end-of-life or structurally-broken
    contract never reaches a provider with exit 0.

    Raises:
        CLIError: ``contract_version_unsupported`` for pre-0.7 contracts,
            ``apply_contract_invalid`` for schema-invalid contracts.
    """
    from fluid_build.cli.plan import _gate_contract_for_plan_or_apply

    _gate_contract_for_plan_or_apply(contract, logger, command="apply")


# ==========================================
# CLI Command Registration & Execution
# ==========================================


def register(subparsers: argparse._SubParsersAction):
    """Register the apply command with comprehensive options"""
    p = subparsers.add_parser(
        COMMAND,
        help="Apply a plan or contract against providers with full orchestration",
        # 3 examples + doc link. Long-form flag reference lives in the
        # docs page.
        epilog=(
            "  fluid apply                                    # CWD contract\n"
            "  fluid apply contract.fluid.yaml --env prod --yes\n"
            "  fluid apply contract.fluid.yaml --dry-run --verbose\n\n"
            "Docs: https://github.com/open-data-protocol/fluid/blob/main/docs/apply.md"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Core arguments
    p.add_argument(
        "contract",
        nargs="?",
        default=None,
        help=(
            "Path to contract.fluid.yaml or execution plan JSON file. "
            "When omitted, auto-finds ``contract.fluid.yaml`` in the "
            "current directory."
        ),
    )
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
        default=None,  # resolved by parse_mode; None = amend
        help=(
            "DDL/DML strategy: dry-run | create-only | amend (default) | "
            "amend-and-build | replace | replace-and-build. See docs/apply.md "
            "for the full matrix."
        ),
    )
    mode_group.add_argument(
        "--allow-data-loss",
        action="store_true",
        default=False,
        help=(
            "Required for replace modes when env != dev or target has rows. "
            "Pre-replace snapshot enables ``fluid rollback``."
        ),
    )
    mode_group.add_argument(
        "--bundle",
        default=None,
        help=(
            "Path to the .tgz bundle this plan.json was generated against. "
            "When the plan carries a bundleDigest it is re-verified against "
            "this bundle before any DDL; auto-discovered from a sibling "
            ".tgz when omitted."
        ),
    )
    # SECURITY: the plan-binding gate and the federation upstream-digest
    # gate are distinct trust domains — a single ``--no-verify-digest``
    # waiver for both was a security finding. Each now has its own
    # narrowly-scoped escape hatch; both log at WARNING for audit.
    mode_group.add_argument(
        "--no-verify-plan-binding",
        action="store_true",
        default=False,
        help="Skip plan/bundle digest verification (DR escape hatch). Logged at WARNING for audit.",
    )
    mode_group.add_argument(
        "--no-verify-federation",
        action="store_true",
        default=False,
        help=(
            "Skip the federated-consumes upstream-digest gate (DR escape "
            "hatch). Logged at WARNING for audit."
        ),
    )

    # Execution control
    execution_group = p.add_argument_group("Execution Control")
    execution_group.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    execution_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Render plan without applying (alias for --mode dry-run)",
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
    safety_group.add_argument(
        "--force-pattern-drift",
        action="store_true",
        help=(
            "Override apply-time plugin hooks that detect drift (e.g. a "
            "scaffold-bundle digest mismatch). Use with care — drift normally "
            "means the inputs have changed and a fresh generate is needed."
        ),
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
        "--build-id",
        dest="build_id",
        help=(
            "Filter build execution to a specific build job by ID "
            "(from the contract's ``builds[]``). Combine with "
            "``--mode amend-and-build`` (additive) or "
            "``--mode replace-and-build`` (destructive). When unset "
            "and the mode requires builds, every build runs."
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
    # ``--provider`` MUST be registered explicitly: without it, argparse
    # abbreviation-matching silently folds ``--provider local`` into
    # ``--provider-config`` (its only registered ``--provider*`` sibling),
    # populating ``provider_config`` with a provider name. Harmless until
    # ``--provider-config`` became a path validated against the filesystem.
    advanced_group.add_argument(
        "--provider", help="Override provider name (default: from contract binding)"
    )
    advanced_group.add_argument(
        "--project", help="Override project/account (default: from contract)"
    )
    advanced_group.add_argument(
        "--region", help="Override region/location (default: from contract)"
    )
    advanced_group.add_argument(
        "--provider-config", help="Path to provider-specific configuration file"
    )
    advanced_group.add_argument(
        "--state-backend",
        help="OpenTofu remote state backend for cloud apply "
        "(s3://bucket/key or gcs://bucket/prefix)",
    )

    p.set_defaults(cmd=COMMAND, func=run)


def _actions_from_source(
    src: str,
    env: str | None,
    provider,
    logger: logging.Logger,
    *,
    mode: Optional[str] = None,
):
    """
    Extract actions from source (supports 0.7.1 provider actions).

    For providers with a plan() method (like AwsProvider, GcpProvider), delegate
    to the provider's planner which generates service-level actions the provider
    can dispatch (e.g. s3.ensure_bucket, glue.ensure_table).

    For other providers or when no planner is available, fall back to the 0.7.1
    ProviderActionParser which infers high-level actions (provisionDataset, etc.).

    The optional ``mode`` argument is forwarded to provider planners that
    accept it so destructive modes (``replace`` / ``replace-and-build``)
    can emit CREATE OR REPLACE + a pre-flight CLONE snapshot. Providers
    that don't accept ``mode`` (older signatures) fall back to the
    mode-less call.
    """
    if src.endswith(".json"):
        # Load pre-generated execution plan.
        data = read_json(src)
        # When the plan embeds the contract AND a destructive mode is
        # in play, re-translate via the provider's native planner so
        # the actions are CTAS-shaped + carry the pre-flight CLONE.
        # Without this, the abstract ``provisionDataset`` / ``scheduleTask``
        # actions baked into plan.json would dispatch via the abstract
        # handler which only emits ``INSERT INTO`` regardless of mode.
        embedded_contract = data.get("contract") if isinstance(data.get("contract"), dict) else None
        is_destructive = (mode or "").lower() in ("replace", "replace-and-build")
        if (
            embedded_contract
            and is_destructive
            and hasattr(provider, "plan")
            and callable(getattr(provider, "plan", None))
        ):
            try:
                native_actions = provider.plan(embedded_contract, mode=mode)
            except TypeError:
                native_actions = None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "plan_retranslation_failed: falling back to recorded actions (%s)",
                    exc,
                )
                native_actions = None
            if native_actions:
                logger.info(
                    "plan_retranslated_for_mode mode=%s actions=%d",
                    mode,
                    len(native_actions),
                )
                return native_actions
        return data.get("actions", [])

    # Load contract
    contract = load_contract_with_overlay(src, env, logger)

    # Prefer provider.plan() when available — it generates service-level
    # actions (s3.*, glue.*, athena.*) that the provider's apply() can dispatch.
    if hasattr(provider, "plan") and callable(getattr(provider, "plan", None)):
        try:
            try:
                actions = provider.plan(contract, mode=mode)
            except TypeError:
                # Provider's plan() doesn't accept ``mode``; fall back so
                # older providers still work.
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
                f"Parsed {len(provider_actions)} provider actions (schema {latest_version})"
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


def _resolve_bundle_path(plan_data: Dict[str, Any], args, logger: logging.Logger):
    """Locate the .tgz bundle a plan.json was generated against.

    Returns ``None`` when the plan declares no ``bundleDigest`` (a raw
    plan with no bundle to pin). When a ``bundleDigest`` IS present and no
    bundle can be located, this still returns ``None`` — and
    ``verify_plan_binding`` then fails closed with ``bundle-missing``.
    """
    if not plan_data.get("bundleDigest"):
        return None
    explicit = getattr(args, "bundle", None)
    if explicit:
        return Path(explicit)
    # Auto-discover a single sibling bundle next to the plan.json.
    plan_dir = Path(args.contract).resolve().parent
    candidates = sorted(plan_dir.glob("*.tgz")) + sorted(plan_dir.glob("*.tar.gz"))
    if len(candidates) == 1:
        logger.info("Auto-discovered bundle for plan-binding: %s", candidates[0])
        return candidates[0]
    if len(candidates) > 1:
        logger.warning(
            "Multiple .tgz bundles next to %s — pass --bundle to disambiguate.",
            args.contract,
        )
    return None


def _verify_plan_digests(
    plan_data: Dict[str, Any], args, logger: logging.Logger, *, bundle_path=None
) -> None:
    """Enforce the stage-7 plan-binding gate.

    Cryptographic "apply consumes exact plan" guarantee: before any DDL runs,
    recompute ``planDigest`` over the plan body and compare against the
    value stored in ``plan.json``. When the plan carries a non-empty
    ``bundleDigest``, the bundle is also re-verified — a missing bundle
    fails closed (``bundle-missing``) rather than silently skipping.
    Mismatch → ``CLIError`` with a stable ``event`` field so CI logs can
    classify the failure.

    ``--no-verify-plan-binding`` waives the check for legitimate
    emergencies (bundle unreachable during DR). The waiver is logged at
    WARNING level so audit trails show the operator made the call.

    Plans without a ``planDigest`` field are treated as tamper signals —
    legitimate plans always carry one. This catches both (a) plans produced
    by an older fluid version and (b) plans that had the digest stripped.
    """
    if getattr(args, "no_verify_plan_binding", False):
        logger.warning(
            "--no-verify-plan-binding: plan-binding verification was SKIPPED. "
            "This is an emergency escape hatch; the apply may be running "
            "against a tampered or stale plan. Make sure this is recorded "
            "in the change log."
        )
        return

    # Local import so plan_digest's own import of bundle/tarfile only loads
    # when verification actually happens (keeps cold-path tests fast).
    from ..forge.core.plan_digest import PlanBindingError, verify_plan_binding

    try:
        verify_plan_binding(plan_data, bundle_path=bundle_path)
    except PlanBindingError as exc:
        # ``exc.kind`` is either ``bundle-mismatch`` or ``plan-tamper``.
        # Surface it as the stable event field so CI log parsers can match.
        raise CLIError(
            1,
            f"apply_plan_digest_{exc.kind.replace('-', '_')}",
            context={"kind": exc.kind, "error": str(exc)},
        )


@_traced_stage("apply")
def run(args, logger: logging.Logger) -> int:
    """
    Main execution function for the apply command

    This is the heart of the FLUID platform - the orchestration engine that
    transforms declarative contracts into deployed data products.
    """
    # Bare ``fluid apply`` auto-finds ``contract.fluid.yaml`` in CWD.
    from fluid_build.cli._common import auto_find_contract

    if not auto_find_contract(args):
        from fluid_build.cli._common import CLIError as _CE

        raise _CE(
            1,
            "contract_required",
            {
                "message": (
                    "No contract path supplied and no ``contract.fluid.yaml`` "
                    "found in the current directory."
                )
            },
        )

    # F1 / F6: validate every operator-supplied path argument (traversal,
    # forbidden system paths, symlink) before any of them reach
    # ``read_json`` / ``load_contract_with_overlay`` / ``open()``. The
    # positional argument may be a contract OR a pre-built ``.json``
    # plan; ``--bundle`` is a ``.tgz``; ``--report`` / ``--state-file``
    # are write targets; ``--provider-config`` is an input file.
    from fluid_build.cli.security import validate_cli_path

    args.contract = str(validate_cli_path(args.contract, mode="read", file_type="contract"))
    if getattr(args, "bundle", None):
        args.bundle = str(validate_cli_path(args.bundle, mode="read", file_type="bundle"))
    if getattr(args, "provider_config", None):
        args.provider_config = str(
            validate_cli_path(args.provider_config, mode="read", file_type="provider config")
        )
    if getattr(args, "report", None):
        args.report = str(
            validate_cli_path(args.report, mode="write", must_exist=False, file_type="report")
        )
    if getattr(args, "state_file", None):
        args.state_file = validate_cli_path(
            args.state_file, mode="write", must_exist=False, file_type="state file"
        )

    start_time = time.time()
    execution_id = f"fluid_apply_{int(time.time())}_{os.getpid()}"

    # Cross-CLI run-id correlation — read or create the workspace's
    # shared id so OTel spans emitted here group with the upstream
    # bundle / plan stages and the downstream verify / publish stages.
    # See ``observability/run_id.py`` for the resolution order.
    from fluid_build.observability.run_id import get_or_create_run_id

    run_id = get_or_create_run_id()

    # Mirror verify/publish: hydrate os.environ from project dotenv +
    # FLUID_SECRETS_FILE before anything that reads SNOWFLAKE_*, DMM_*, etc.
    # Must happen before the --build branch delegates to execute.run, since
    # the dbt subprocess launched there reads os.environ for its profile.
    hydrate_dotenv(Path.cwd(), environment=getattr(args, "env", None))

    # --- Resolve apply mode (11-stage pipeline stage 7) ---
    # ``--mode`` is canonical (amend / amend-and-build / replace /
    # replace-and-build / dry-run / create-only). Default is ``amend``.
    from fluid_build.forge.core.apply_modes import (
        ApplyMode,
        check_data_loss_gate,
        is_dry_run,
        needs_build,
        parse_mode,
    )

    try:
        resolved_mode = parse_mode(getattr(args, "mode", None))
    except ValueError as exc:
        raise CLIError(1, "apply_mode_invalid", {"error": str(exc)})

    resolved_build_id = getattr(args, "build_id", None)

    # ``--dry-run`` flag still supported as a CLI ergonomic alias for
    # ``--mode dry-run``; the canonical form is the mode value. Normalize
    # the two so the downstream code sees one signal.
    #
    # Stomp the resolved value back onto ``args.dry_run`` so the rest of
    # the apply path (which checks ``args.dry_run`` at the gate sites)
    # honours ``--mode dry-run`` too. Without this, ``--mode dry-run``
    # was passing through the dry-run gate and reaching the provider's
    # apply() — for the Snowflake provider that meant attempting to
    # connect with credentials it didn't have.
    effective_dry_run = bool(getattr(args, "dry_run", False)) or is_dry_run(resolved_mode)
    args.dry_run = effective_dry_run

    # Log operation start — include resolved mode + run_id so
    # observability surfaces both. ``run_id`` correlates this apply
    # stage's spans with the upstream bundle / plan stages and the
    # downstream verify / publish stages.
    log_operation_start(
        logger,
        "apply_contract",
        execution_id=execution_id,
        run_id=run_id,
        source=args.contract,
        env=args.env,
        dry_run=effective_dry_run,
        mode=resolved_mode.value,
    )

    # Apply-engine resolution is automatic and per-provider — no user
    # switch. The cloud providers compile the contract to `.tf.json` and
    # delegate to `tofu`; `local` keeps the native path below.
    from fluid_build.cli._apply_opentofu_engine import (
        apply_via_opentofu,
        resolve_apply_engine,
    )

    if resolve_apply_engine(args, logger) == "opentofu":
        rc = apply_via_opentofu(args, logger)
        # The OpenTofu engine provisions infrastructure only. Build-augmented
        # modes (amend-and-build / replace-and-build) still need their build
        # phase to run — mirror the native dispatch below so ``--build`` is
        # not silently dropped on the cloud-provider path.
        if rc == 0 and needs_build(resolved_mode):
            args.build_id = resolved_build_id
            from fluid_build.build_runners import run_builds_from_args

            return run_builds_from_args(args, logger, force_run=True)
        return rc

    try:
        # --- Build execution mode (absorbed from legacy 'fluid execute') ---
        # ``--mode amend-and-build`` / ``--mode replace-and-build`` delegate
        # to build_runners. Three paths in:
        #   1. Legacy ``--build <id>`` (auto-upgrades to amend-and-build).
        #   2. ``--mode amend-and-build --build <id>`` (explicit pair).
        #   3. ``--mode amend-and-build`` alone — the runner runs every
        #      build in the contract. Previously this branch required
        #      ``resolved_build_id`` to be set, which made bare
        #      ``--mode amend-and-build`` a silent no-op when the user
        #      forgot ``--build``. Now we delegate unconditionally for
        #      build-augmented modes; ``run_builds_from_args`` handles
        #      the "no filter → run all builds" case cleanly.
        # force_run=True mirrors the historical ``_from_apply=True`` semantic.
        if needs_build(resolved_mode):
            # Forward the resolved build_id (may be None — that's fine,
            # the runner iterates all builds when unfiltered).
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
            # (catches tampering between stages 6 and 7) AND, when the plan
            # carries a non-empty ``bundleDigest``, the bundle it was bound
            # to (from --bundle or an auto-discovered sibling .tgz). A
            # non-empty bundleDigest with no bundle available fails closed.
            # ``--no-verify-plan-binding`` waives the gate for emergencies.
            _verify_plan_digests(
                plan_data,
                args,
                logger,
                bundle_path=_resolve_bundle_path(plan_data, args, logger),
            )

            # SECURITY (TOCTOU): re-verification snapshot.
            # ``_verify_plan_digests`` just proved the planDigest over
            # ``plan_data`` as loaded. Capture a deep copy of that
            # exact, attested structure NOW, before any downstream code
            # runs. ``verified_plan_data`` is the frozen reference: it
            # is what the digest covered and nothing mutates it.
            # Provider dispatch derives the contract from this copy
            # (see ``contract = verified_plan_data.get("contract")``
            # below), so the structure that actually drives DDL is
            # provably the one that was digest-checked — not a sibling
            # alias that could have been swapped between verify and
            # use. Operator-supplied ``--config-override`` is still
            # applied afterwards (that is an explicit apply-time input,
            # not plan tampering), but it mutates a child of this
            # verified copy, never the loaded ``plan_data``.
            import copy as _copy

            verified_plan_data = _copy.deepcopy(plan_data)
            plan_data = verified_plan_data

            # --- Plan/apply mode-mismatch gate ---
            # ``fluid plan x.yaml --output p.json`` records the mode it
            # was generated for (None = mode-unaware). When the operator
            # then runs ``fluid apply p.json --mode X``, the recorded
            # mode must match (or be unrecorded for the additive
            # default). Otherwise we'd silently run an additive apply
            # against a plan generated for replace, or vice-versa.
            recorded_mode = plan_data.get("mode")
            requested_mode_value = resolved_mode.value if resolved_mode is not None else None
            # Normalize: treat ``amend`` and ``None`` as compatible
            # (default; mode-unaware plans applied with default mode).
            _amend_aliases = {None, "amend"}
            requested_norm = (
                None if requested_mode_value in _amend_aliases else requested_mode_value
            )
            recorded_norm = None if recorded_mode in _amend_aliases else recorded_mode
            if requested_norm != recorded_norm:
                raise CLIError(
                    1,
                    "apply_plan_mode_mismatch",
                    {
                        "plan_mode": recorded_mode,
                        "requested_mode": requested_mode_value,
                        "hint": (
                            "the plan was generated for "
                            f"mode={recorded_mode!r} but apply requested "
                            f"mode={requested_mode_value!r}. Re-run "
                            f"``fluid plan <contract> --mode {requested_mode_value}`` "
                            "to produce a mode-aware plan, or change "
                            "``--mode`` on apply to match."
                        ),
                    },
                )

            contract = plan_data.get("contract", {})

            # --- Pre-apply contract gate (embedded-contract path) ---------
            # A pre-built plan.json embeds the full contract. The plan-binding
            # digest gate above proves the plan wasn't tampered with, but a
            # plan minted from a pre-0.7 / schema-invalid contract by an
            # older fluid (before the plan-time gate existed) could still
            # reach apply. Re-run the same gates on the embedded contract so
            # apply never executes DDL for an unsupported / broken contract.
            if isinstance(contract, dict) and contract:
                _gate_contract_for_apply(contract, logger)

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

            # --- Pre-apply contract gate ----------------------------------
            # ``fluid apply`` used to apply ANY contract — including a
            # pre-0.7 end-of-life contract or a structurally-broken one —
            # with exit 0. Run the same gates ``fluid validate`` runs
            # (pre-0.7 rejection + JSON-schema validation) BEFORE any DDL.
            # A failure raises ``CLIError`` → non-zero exit, no apply.
            _gate_contract_for_apply(contract, logger)

            # Run plugin-registered apply-time hooks (entry-point group
            # ``fluid_build.apply_hooks``). Plugins can use these to verify
            # apply-time invariants — e.g. scaffold bundle digest drift,
            # lockfile freshness. ``--force-pattern-drift`` overrides any
            # reported drift.
            _hook_rc = _run_apply_hooks(
                contract,
                Path(args.contract).resolve().parent,
                logger,
                force=bool(getattr(args, "force_pattern_drift", False)),
            )
            if _hook_rc != 0:
                logger.error(
                    "apply aborted by an apply-time plugin hook. "
                    "Pass --force-pattern-drift to override."
                )
                return _hook_rc

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

        # --- Cross-mesh federation digest gate (stage-7 apply gate) ---
        # When ``consumes[]`` declares an ``upstreamWorkspace``, the
        # federation validator fetches the live upstream digest and
        # compares against the pinned ``upstreamDigest``. Drift produces
        # a typed ``FederatedConsumeViolation`` per drifted row and we
        # abort apply before any DDL — same loud-failure posture as the
        # plan-binding gate. ``--no-verify-federation`` is the DR escape
        # hatch (logged at WARNING).
        if not getattr(args, "no_verify_federation", False):
            try:
                from fluid_build.forge.federation import validate_federated_consumes

                fed_violations = validate_federated_consumes(contract, workspace_root=Path.cwd())
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug(
                    "federation_validate_skipped: err=%s — manifest absent or "
                    "unreachable; treating as no-op",
                    exc,
                )
                fed_violations = []
            if fed_violations:
                # Build a stable, machine-parseable error payload that
                # mirrors PlanBindingError's contract so CI templates can
                # match both gates with one regex.
                first = fed_violations[0]
                raise CLIError(
                    1,
                    "apply_consumes_drift",
                    {
                        "kind": "upstream-mismatch",
                        "violations": [
                            {
                                "consume_index": v.consume_index,
                                "upstream_workspace_id": v.upstream_workspace_id,
                                "upstream_product_id": v.upstream_product_id,
                                "expected_digest": v.expected_digest,
                                "actual_digest": v.actual_digest,
                                "reason": v.reason,
                            }
                            for v in fed_violations
                        ],
                        "first_violation": first.reason,
                    },
                )
        else:
            logger.warning(
                "--no-verify-federation: federation digest gate was SKIPPED. "
                "Federated consumes[] entries with drifted upstreams will "
                "apply against stale data. Make sure this is recorded in "
                "the change log."
            )

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

            # Explicit CLI flags override the contract-derived values.
            if getattr(args, "provider", None):
                provider_name = args.provider
            if getattr(args, "project", None):
                project = args.project
            if getattr(args, "region", None):
                region = args.region

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

            # Get actions from contract — pass the resolved apply mode so
            # destructive modes (replace / replace-and-build) trigger
            # CREATE OR REPLACE TABLE + pre-flight CLONE snapshot in
            # the provider's planner.
            actions = _actions_from_source(
                args.contract,
                args.env,
                provider,
                logger,
                mode=resolved_mode.value if resolved_mode is not None else None,
            )

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

            # Rollback-state writer: when the plan contained
            # ``rollback_snapshot`` markers (emitted for destructive
            # modes by per-provider planners) AND the apply succeeded,
            # append the snapshot metadata to ``.fluid/rollback-state.json``
            # so ``fluid rollback`` can find them. Best-effort: a
            # writer failure does not abort the apply.
            if success:
                try:
                    from fluid_build.cli._rollback_writer import (
                        write_snapshots_for_apply,
                    )

                    write_snapshots_for_apply(
                        actions,
                        contract=contract,
                        env=getattr(args, "env", None),
                        provider=getattr(provider, "name", None) or provider_name or "unknown",
                        workspace_root=Path.cwd(),
                        logger=logger,
                        results=(
                            result.get("results")
                            if isinstance(result.get("results"), list)
                            else None
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("rollback_state_writer_skipped: %s", exc, exc_info=True)

            # Three outcomes — success_with_outputs (green), success_no_outputs
            # (yellow warning, render the misconfigured-contract case),
            # failure (red).
            output_count = 0
            if isinstance(result.get("results"), list):
                output_count = sum(
                    len(r.get("written", [])) for r in result["results"] if r.get("status") == "ok"
                )
            # ``no_outputs`` fires only when ``applied == 0 AND
            # output_count == 0`` so two legitimate "0 local files"
            # cases stay green:
            #   * Cloud applies (snowflake / bigquery / aws) where
            #     materialisation is in the cloud catalog.
            #   * Acquisition builds where the engine runner wrote
            #     parquet under its own path (apply.py doesn't see those
            #     writes via ``result["results"]``).
            applied_count = (
                int(result.get("applied", 0)) if isinstance(result.get("applied"), int) else 0
            )
            no_outputs = success and output_count == 0 and applied_count == 0

            # Show results
            if RICH_AVAILABLE:
                console = Console()
                if success and not no_outputs:
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

                    if output_count > 0:
                        summary_table.add_row("Files Generated", str(output_count))

                    console.print(summary_table)

                    # Show output files
                    if "results" in result:
                        for r in result["results"]:
                            if r.get("status") == "ok" and "written" in r:
                                for path in r["written"]:
                                    console.print(f"  📁 [cyan]{path}[/cyan]")
                elif no_outputs:
                    # Honest "ran but did nothing useful" panel.
                    console.print("\n[yellow]⚠️  Apply ran but produced no output files.[/yellow]")
                    if "applied" in result:
                        console.print(f"  [dim]Actions applied: {result['applied']}[/dim]")
                    console.print(
                        "  [dim]This usually means the contract uses an "
                        "engine the active provider doesn't yet materialise "
                        "(e.g. dlt acquisition on the local provider). "
                        "Try a different provider with `--provider <name>` "
                        "or fix the contract's engine choice.[/dim]"
                    )
                else:
                    error_msg = result.get("error", "Unknown error")
                    console.print(f"\n[red]❌ Deployment failed: {error_msg}[/red]")

                    # Show individual action errors
                    if "results" in result:
                        console.print("\n[bold]Action Errors:[/bold]")
                        for i, r in enumerate(result["results"]):
                            if r.get("status") == "error":
                                console.print(
                                    f"  {i + 1}. [red]✗[/red] {r.get('op', 'unknown')}: {r.get('error', 'no details')}"
                                )
            else:
                if success and not no_outputs:
                    logger.info("✅ Data product deployed successfully")
                    if "applied" in result:
                        logger.info(f"Applied {result['applied']} action(s)")
                elif no_outputs:
                    logger.warning("⚠️  Apply ran but produced no output files.")
                    if "applied" in result:
                        logger.info(f"Actions applied: {result['applied']}")
                else:
                    error_msg = result.get("error", "Unknown error")
                    logger.error(f"❌ Deployment failed: {error_msg}")

            total_time = time.time() - start_time
            # Drop the always-green "Execution completed" footer when the
            # run had no output — a hidden retort that contradicted the
            # warning panel above. Surface the duration via the summary
            # table only.
            if success and not no_outputs:
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
        <p>Status: {"Success" if success else "Failed"}</p>
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

        # Execute the plan. The user-facing "🚀 Executing actions..."
        # message lands later via the progress UI; this breadcrumb is
        # for the structured log only (DEBUG).
        logger.debug("Starting data product deployment orchestration")

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
        # Let typed user errors bubble straight to main() for the rich
        # five-field Panel render — wrapping them in CLIError loses the
        # structured shape the catalog provides.
        from fluid_build.cli._errors import FluidUserError as _FUE

        if isinstance(e, _FUE):
            raise
        logger.error(f"💥 Unexpected error during execution: {e}")
        if args.debug:
            import traceback

            logger.error(traceback.format_exc())
        raise CLIError(1, "apply_execution_failed", {"error": str(e)})


# Plan-display + report-generation helpers — physically extracted to
# ``cli/_apply_reports.py``. ~240 LOC of post-execution renderers
# lifted without behaviour change. Re-exported here so existing test
# patches on ``fluid_build.cli.apply.<helper>`` flow through to the
# moved functions via the module-attribute-access indirection.
from fluid_build.cli._apply_reports import (  # noqa: E402,F401
    _confirm_execution,
    _display_dry_run_summary,
    _display_execution_plan,
    _export_metrics,
    _generate_final_report,
    _generate_html_report,
    _generate_json_report,
    _generate_markdown_report,
    _send_notifications,
)
