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

"""FLUID Plan Command (v0.7.x).

Reads a v0.7.x contract and produces an execution plan via the
:class:`ProviderActionParser` (provider actions with dependency
resolution). Pre-0.7 contracts (0.4.0, 0.5.x) are no longer supported.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List

from fluid_build.cli.console import cprint, warning

# 11-stage pipeline: stage-6 → stage-7 cryptographic plan-binding. ``plan.json``
# carries ``bundleDigest`` (pins input bundle) + ``planDigest`` (catches
# plan-file tampering). ``fluid apply`` re-verifies both before any DDL.
from ..forge.core.plan_digest import coerce_keys_to_str, inject_digests, is_bundle_path
from ..observability.tracing import traced_stage as _traced_stage
from ._common import (
    CLIError,
    build_provider,
    load_contract_with_overlay,
    resolve_provider_from_contract,
)
from ._logging import error, info, warn

# Path-B scheduling engines. When ``orchestration.engine`` matches one of
# these, plan.py invokes the provider-native scheduler planner so the
# scheduling resources (EventBridge rules, Snowflake tasks, MWAA env) land
# inside ``plan.json`` and get applied alongside DDL in stage 7. Path-A
# engines (airflow/prefect/dagster) go through ``fluid generate schedule``
# + ``fluid schedule-sync`` and do not need plan-time action emission.
_PATH_B_ENGINES = {"eventbridge", "snowflake_tasks", "mwaa", "step-functions"}


def _parse_semver(v: str) -> tuple:
    """Parse a semver string into a comparable tuple of ints."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", v)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return (0, 0, 0)


# Try to import 0.7.1 provider action support
try:
    from ..forge.core.provider_actions import ProviderActionParser

    PROVIDER_ACTIONS_AVAILABLE = True
except ImportError:
    PROVIDER_ACTIONS_AVAILABLE = False

# Try Rich for better output
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

COMMAND = "plan"


def _default_fluid_version() -> str:
    """Return the latest bundled FLUID schema version (dynamic lookup).

    Used as the fallback when a contract doesn't declare ``fluidVersion``
    — instead of hardcoding a number that goes stale every release, we
    ask ``SchemaManager`` which version is the newest bundled schema on
    disk. When we ship 0.8.x, the fallback tracks it automatically.

    Lazy-imported so ``plan.py``'s module load doesn't pull the full
    schema_manager graph for ``--help`` invocations.
    """
    from fluid_build.schema_manager import SchemaManager

    return SchemaManager.latest_bundled_version()


def write_json_idempotent(path: str, obj: Any) -> None:
    """
    Idempotent file write - only writes if content changed.

    This follows declarative infrastructure principles:
    - Same input → same output (deterministic)
    - No unnecessary filesystem changes
    - Preserves timestamps if content unchanged
    - Better for CI/CD (doesn't trigger unnecessary rebuilds)
    """
    # Ensure directory exists (like mkdir -p)
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)

    # Serialize to string with consistent formatting. Coerce non-str dict
    # keys to str first: a contract with a YAML magic-word key (on/off/
    # yes/no) yields a Python bool key, and ``sort_keys=True`` cannot
    # order a mixed bool/str key set (TypeError). See plan_digest.py.
    new_content = json.dumps(
        coerce_keys_to_str(obj), indent=2, sort_keys=True
    )  # sort_keys for determinism

    # Check if file exists and content is identical
    if Path(path).exists():
        try:
            with open(path, encoding="utf-8") as f:
                existing_content = f.read()

            if existing_content == new_content:
                # Content unchanged - skip write (preserves timestamp)
                return
        except OSError:
            # If we can't read the file, proceed with write
            pass

    # Write only if new or changed
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)


def register(subparsers: argparse._SubParsersAction):
    """Register the plan command (v0.7.x)."""
    p = subparsers.add_parser(
        COMMAND,
        help="Generate execution plan from FLUID contract",
        description="""
Generate an execution plan from a FLUID v0.7.x data product contract.

Uses :class:`ProviderActionParser` with dependency resolution to walk
``builds[]`` / ``exposes[]`` and emit ordered actions per provider.
Pre-0.7 contracts (0.4.0, 0.5.x) are no longer supported.

The plan shows the sequence of operations needed to build and deploy
the data product, including infrastructure provisioning, data
transformations, access grants, and orchestration tasks.
        """.strip(),
        epilog="""Examples:
  # Plan a contract (generates execution plan)
  fluid plan contract.fluid.yaml
  fluid plan contract.fluid.yaml --verbose

  # Custom output locations
  fluid plan contract.fluid.yaml --out my-plan.json
  fluid plan contract.fluid.yaml --env prod --out production-plan.json

  # Environment-specific planning
  fluid plan contract.fluid.yaml --env staging
  fluid plan contract.fluid.yaml --env test --out test-plan.json

  # Verbose output with detailed action information
  fluid plan contract.fluid.yaml --verbose""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "contract",
        nargs="?",
        default=None,
        help=(
            "Path to the contract.fluid.yaml file. When omitted, "
            "auto-finds it in the current directory."
        ),
    )
    p.add_argument("--env", help="environment overlay (dev, staging, prod)")
    p.add_argument(
        "--mode",
        default=None,
        help=(
            "Apply mode the plan is being generated FOR. Stamped into "
            "plan.json so a subsequent ``fluid apply plan.json --mode X`` "
            "can detect a mismatch and refuse rather than running an "
            "additive apply when the operator asked for replace. "
            "Choices: amend (default) | amend-and-build | replace | "
            "replace-and-build | dry-run | create-only. When unset, the "
            "plan is generated mode-less and apply must use the same."
        ),
    )
    p.add_argument(
        "--out",
        "--output",
        dest="out",
        default="plan.json",
        help="output file for the execution plan (default: plan.json in the current directory)",
    )
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="show detailed action information with Rich formatting",
    )
    p.add_argument(
        "--validate-actions",
        action="store_true",
        help="validate generated actions against ProviderAction schema (SDK)",
    )
    p.add_argument(
        "--estimate-cost",
        action="store_true",
        help="ask provider to estimate cost of planned actions",
    )
    p.add_argument(
        "--check-sovereignty",
        action="store_true",
        help="ask provider to check data sovereignty constraints",
    )
    p.add_argument("--provider", help="override provider name (default: from contract)")
    p.add_argument("--project", help="override project/account (default: from contract)")
    p.add_argument("--region", help="override region/location (default: from contract)")
    p.add_argument(
        "--html",
        dest="html_output",
        nargs="?",
        const="plan.html",
        default=None,
        help="generate HTML visualization (default path: plan.html in the current directory)",
    )
    p.set_defaults(cmd=COMMAND, func=run)


@_traced_stage("plan")
def run(args, logger: logging.Logger) -> int:
    """
    Main entry point — accepts a v0.7.x contract path or pre-built
    ``plan.json``, dispatches to ``ProviderActionParser`` for the
    canonical action graph, and writes the merged plan + binding
    digest to ``plan.json`` in the current directory (or the path
    given by ``--out``).
    """
    try:
        # UX hardening — accept bare ``fluid plan`` when CWD has a contract.
        from fluid_build.cli._common import auto_find_contract

        if not auto_find_contract(args):
            raise CLIError(
                1,
                "contract_required",
                {
                    "message": (
                        "No contract path supplied and no ``contract.fluid.yaml`` "
                        "found in the current directory."
                    )
                },
            )

        # F1 / F6: validate the operator-supplied contract/plan path
        # (traversal, forbidden system paths, symlink) before it reaches
        # the loader. ``.tgz`` bundles and ``.json`` plans are accepted.
        from fluid_build.cli.security import validate_cli_path

        args.contract = str(validate_cli_path(args.contract, mode="read", file_type="contract"))

        # F1: validate the ``--out`` plan write target.
        if getattr(args, "out", None):
            args.out = str(
                validate_cli_path(args.out, mode="write", must_exist=False, file_type="output")
            )

        # F1: validate the ``--html`` write target when set.
        if getattr(args, "html_output", None):
            args.html_output = str(
                validate_cli_path(
                    args.html_output, mode="write", must_exist=False, file_type="HTML report"
                )
            )

        # Load contract with environment overlay
        contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
        fluid_version = contract.get("fluidVersion", _default_fluid_version())

        # --- Pre-plan contract gate ---------------------------------------
        # ``fluid plan`` used to plan ANY contract — including a pre-0.7
        # end-of-life contract or a structurally-broken one — and emit a
        # signed ``plan.json`` with exit 0. That let an invalid contract
        # slip into stage-7 apply. Run the same gates ``fluid validate``
        # runs (pre-0.7 rejection + JSON-schema validation) BEFORE any
        # plan generation or write. A failure raises ``CLIError`` →
        # non-zero exit, no plan.json written.
        _gate_contract_for_plan_or_apply(contract, logger, command="plan")

        info(
            logger,
            "plan_start",
            contract=args.contract,
            version=fluid_version,
            env=getattr(args, "env", None),
        )

        # Version-aware routing
        if _should_use_provider_actions(contract, logger):
            plan = _plan_with_provider_actions(contract, args, logger)
        else:
            plan = _plan_legacy(contract, args, logger)

        # --- Path-B scheduling (stage 6 ⇒ stage 7 pipeline) -----------------
        # When ``orchestration.engine`` selects a provider-native scheduler
        # (EventBridge / Snowflake Tasks / MWAA / Step Functions), merge the
        # schedule actions into ``plan["actions"]`` so stage-7 apply creates
        # them alongside DDL. Path-A engines (airflow/prefect/dagster) emit
        # DAG files via ``fluid generate schedule`` — handled in stage 3, not
        # here — so schedule wiring is a no-op for them.
        schedule_actions = _plan_schedule_actions(contract, args, logger)
        if schedule_actions:
            existing = plan.get("actions") or []
            plan["actions"] = list(existing) + schedule_actions
            plan["total_actions"] = len(plan["actions"])

        # --- Packaging truthfulness (RFC-packaging-modes.md file 8) ---------
        # plan.json is the digest-bound artifact the human reviews, and
        # untouched it would list container-*creation* actions for containers
        # the emit path (correctly) refuses to own under `packaging.mode:
        # shared` — an approved plan claiming it will create a pool it must
        # never own. Drop those actions and stamp the effective ownership so
        # the approver reads it off the plan instead of recomputing the
        # two-level precedence.
        #
        # Runs BEFORE inject_digests so planDigest covers both. One
        # chokepoint here rather than per-provider, so it holds for the
        # abstract-op path and the native planner path alike. A contract with
        # no `packaging` block is returned untouched — no new key, no digest
        # churn. Function-local import: `iac` pulls every provider plugin and
        # must stay off the `fluid --help` cold path.
        from fluid_build.iac.plan_packaging import apply_packaging_to_plan

        plan = apply_packaging_to_plan(plan, contract)

        # --- Plan-binding digests (stage 6 ⇒ stage 7 pipeline) --------------
        # ``bundleDigest`` pins the input bundle when the contract is a tgz;
        # ``planDigest`` catches tampering of plan.json between stages 6 and
        # 7. Both are verified by ``fluid apply`` before any DDL runs.
        bundle_path: Path | None = Path(args.contract) if is_bundle_path(args.contract) else None
        try:
            plan = inject_digests(plan, bundle_path=bundle_path)
        except FileNotFoundError as exc:
            raise CLIError(
                1,
                "plan_bundle_missing",
                context={"bundle": str(bundle_path), "error": str(exc)},
            )
        except ValueError as exc:
            # ``read_bundle_digest`` raises ValueError on malformed MANIFEST
            # or broken tarball. Surface as a dedicated event so CI logs
            # don't blur bundle tamper with generic planner failure.
            raise CLIError(
                1,
                "plan_bundle_invalid",
                context={"bundle": str(bundle_path), "error": str(exc)},
            )

        # Write plan to file (idempotent - only if changed)
        write_json_idempotent(args.out, plan)

        # Validate actions against SDK schema if requested
        if getattr(args, "validate_actions", False):
            _validate_plan_actions(plan, logger)

        # Display plan
        if RICH_AVAILABLE and getattr(args, "verbose", False):
            _display_plan_rich(plan, contract)
        else:
            _display_plan_simple(plan, logger, output_path=args.out)

        # --- Advanced hooks: cost estimation & sovereignty checking ---
        if getattr(args, "estimate_cost", False) or getattr(args, "check_sovereignty", False):
            from fluid_build.cli.hooks import run_estimate_cost, run_validate_sovereignty

            # Build provider for hook invocation
            provider_flag = getattr(args, "provider", None)
            project_flag = getattr(args, "project", None)
            region_flag = getattr(args, "region", None)
            if not provider_flag:
                provider_flag, loc = resolve_provider_from_contract(contract)
                if not project_flag:
                    project_flag = loc.get("project")
                if not region_flag:
                    region_flag = loc.get("region")
            try:
                hook_provider = build_provider(provider_flag, project_flag, region_flag, logger)
            except Exception:
                hook_provider = None

            actions_list = plan.get("actions", [])

            if getattr(args, "estimate_cost", False) and hook_provider:
                estimate = run_estimate_cost(hook_provider, actions_list, logger)
                if estimate is not None:
                    cprint(
                        f"\nEstimated cost: ${estimate.monthly:.2f}/month"
                        f" + ${estimate.one_time:.2f} one-time"
                        f" ({estimate.currency})"
                    )
                    if estimate.notes:
                        cprint(f"  Note: {estimate.notes}")
                    plan["cost_estimate"] = estimate.to_dict()
                    # Re-inject digests — cost_estimate was added AFTER the
                    # first inject, so planDigest no longer covers the plan
                    # body. Recompute to keep the stage-7 tamper gate honest.
                    # ``force=True``: the plan is already bound from the
                    # injection above; this is a deliberate, in-process
                    # re-bind over a mutation we just made ourselves, so we
                    # opt past inject_digests' overwrite guard (J7).
                    plan = inject_digests(plan, bundle_path=bundle_path, force=True)
                    write_json_idempotent(args.out, plan)
                else:
                    cprint("\nCost estimation: not supported by this provider")

            if getattr(args, "check_sovereignty", False) and hook_provider:
                violations = run_validate_sovereignty(hook_provider, contract, logger)
                if violations:
                    cprint(f"\nSovereignty check: {len(violations)} violation(s)")
                    for v in violations:
                        cprint(f"  - {v}")
                else:
                    cprint("\nSovereignty check: PASS")

        # --- HTML visualization (--html flag, absorbs preview/viz-plan) ---
        html_path = getattr(args, "html_output", None)
        if html_path:
            try:
                from .viz_plan import render_plan_html

                render_plan_html(args.out, html_path, logger)
                cprint(f"HTML report: {html_path}")
            except Exception:
                warn(logger, "plan visualizer not available", event="html_render_skipped")

        info(
            logger,
            "plan_success",
            output=args.out,
            actions=plan.get("total_actions", 0),
            version=fluid_version,
        )

        return 0

    except CLIError:
        raise
    except Exception as e:
        raise CLIError(1, "planner_failed", context={"error": str(e)})


def _gate_contract_for_plan_or_apply(
    contract: Dict[str, Any], logger: logging.Logger, *, command: str
) -> None:
    """Reject pre-0.7 + schema-invalid contracts before plan/apply.

    Two gates, run on the loaded contract dict BEFORE any plan generation
    or DDL:

    1. **Pre-0.7 rejection.** Shares ``validate.py::_reject_pre_07_contract``
       so a 0.4.x / 0.5.x / 0.6.x contract is rejected here exactly as
       ``fluid validate`` rejects it (raises ``CLIError`` with the
       ``contract_version_unsupported`` event).
    2. **JSON-schema validation.** Runs ``FluidSchemaManager.validate_contract``
       (the same validation ``fluid validate`` runs) so a structurally
       broken contract — missing ``id``, unknown keys, wrong types —
       never reaches a signed ``plan.json`` or a provider apply with
       exit 0. A schema-invalid contract raises ``CLIError`` →
       non-zero exit; no success plan is written.

    Raises:
        CLIError: ``contract_version_unsupported`` for pre-0.7 contracts,
            or ``local_plan_validation_failed`` / ``apply_contract_invalid``
            for schema-invalid contracts.
    """
    # Gate 1 — pre-0.7 rejection (shared with ``fluid validate``).
    from fluid_build.cli.validate import _reject_pre_07_contract

    _reject_pre_07_contract(contract)

    # Gate 2 — JSON-schema validation. Auto-detects the contract's own
    # ``fluidVersion`` (offline, bundled schemas) — identical to the
    # ``fluid validate`` default path.
    from fluid_build.schema_manager import FluidSchemaManager

    try:
        result = FluidSchemaManager().validate_contract(contract, offline_only=True)
    except Exception as exc:  # pragma: no cover — defensive
        raise CLIError(
            1,
            "schema_validation_error",
            context={
                "command": command,
                "error": str(exc),
                "message": (
                    f"``fluid {command}`` could not schema-validate the "
                    "contract before proceeding."
                ),
            },
        )

    if not result.is_valid:
        errors = list(result.errors)
        for err in errors:
            error(logger, "contract_schema_error", command=command, detail=err)
        event = "local_plan_validation_failed" if command == "plan" else "apply_contract_invalid"
        raise CLIError(
            1,
            event,
            context={
                "command": command,
                "error_count": len(errors),
                "errors": errors,
                "message": (
                    f"Contract validation failed with {len(errors)} error(s); "
                    f"``fluid {command}`` will not proceed on a schema-invalid "
                    f"contract. Run ``fluid validate`` for the full report."
                ),
            },
        )


def _validate_plan_actions(plan: Dict[str, Any], logger: logging.Logger) -> None:
    """Run SDK ``validate_actions()`` over the plan's action list.

    Converts raw action dicts to ``ProviderAction`` instances, validates,
    and prints results.  Non-fatal — logs warnings but does NOT raise.
    """
    try:
        from fluid_provider_sdk import ProviderAction, validate_actions
    except ImportError:
        warn(
            logger,
            "fluid-provider-sdk not installed — skipping action validation",
            event="sdk_not_available",
        )
        return

    raw_actions = plan.get("actions") or []
    if not raw_actions:
        info(logger, "No actions to validate", event="validate_actions_skip")
        return

    typed: list = []
    for raw in raw_actions:
        try:
            typed.append(ProviderAction.from_dict(raw))
        except Exception as exc:
            warn(logger, "action_parse_error", action=raw.get("op", "?"), error=str(exc))

    errors = validate_actions(typed)
    if errors:
        cprint(f"\n⚠  Action validation found {len(errors)} issue(s):")
        for err in errors:
            cprint(f"   • {err}")
    else:
        cprint(f"\n✓  All {len(typed)} actions pass schema validation")


def _plan_schedule_actions(
    contract: Dict[str, Any], args, logger: logging.Logger
) -> List[Dict[str, Any]]:
    """Return Path-B scheduling actions for the contract, or ``[]``.

    Path B (provider-native scheduling) puts schedule resources into
    ``plan.json`` so stage-7 apply creates them alongside DDL. Today only
    the AWS provider's ``SchedulePlanner`` is wired up; Snowflake-Tasks
    lands when the Snowflake planner gains a schedule method. Other engines
    (airflow/prefect/dagster — Path A) emit DAG files at stage 3 and get
    synced at stage 11; they return ``[]`` here.

    The helper is defensive by design — any import/runtime failure
    degrades gracefully to "no schedule actions" with a warning. A
    plan-time failure should never block apply for a contract whose
    author didn't need schedule wiring at all.
    """
    orchestration = contract.get("orchestration") or {}
    engine = str(orchestration.get("engine") or "").strip().lower()
    if engine not in _PATH_B_ENGINES:
        return []

    # Today only AWS has a SchedulePlanner. Snowflake Tasks path is in
    # scope but not yet implemented — log + skip so the plan still emits.
    if engine == "snowflake_tasks":
        warn(
            logger,
            "schedule_planner_not_implemented",
            engine=engine,
            detail="Snowflake Tasks scheduling is not yet wired into plan.py",
        )
        return []

    try:
        from ..providers.aws.plan.schedule import SchedulePlanner
    except ImportError:
        warn(
            logger,
            "schedule_planner_unavailable",
            engine=engine,
            detail="AWS schedule planner not importable — skipping schedule actions",
        )
        return []

    # Resolve account + region for the planner. Fall back to placeholders
    # so plan-time rendering works even without AWS creds; apply re-resolves
    # against STS at execution time.
    account_id = (
        getattr(args, "project", None)
        or contract.get("project")
        or "000000000000"  # placeholder; apply resolves via STS
    )
    # Region resolution order (most-specific → least-specific):
    #   1. --region CLI flag
    #   2. contract.region (top-level declaration)
    #   3. contract.binding.location.region (per-expose binding)
    #   4. FLUID_DEFAULT_REGION env (project-wide override)
    #   5. AWS_REGION / AWS_DEFAULT_REGION env (standard boto3 chain)
    #   6. ``eu-west-1`` as a neutral EU default (GDPR-friendly; Dublin
    #      is within the EEA). us-east-1 was the prior default but made
    #      EU-first workflows hostile to inspect; operators who want us-east
    #      export AWS_REGION=us-east-1 and get the old behaviour.
    region_from_contract = contract.get("region")
    if not region_from_contract:
        for _expose in contract.get("exposes", []) or []:
            _loc = (_expose.get("binding") or {}).get("location") or {}
            if _loc.get("region"):
                region_from_contract = _loc["region"]
                break
    region = (
        getattr(args, "region", None)
        or region_from_contract
        or os.environ.get("FLUID_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "eu-west-1"
    )

    try:
        planner = SchedulePlanner(account_id=str(account_id), region=str(region), logger=logger)
        actions = planner.plan_schedule_actions(contract)
    except Exception as exc:
        # Non-fatal — the contract will still apply, but operators need to
        # set up schedules manually. Surfacing via warn() (not error()) is
        # intentional: plan doesn't fail because schedule planning fails.
        warn(
            logger,
            "schedule_planner_failed",
            engine=engine,
            error=str(exc),
            detail="Schedule planner raised; schedule actions omitted from plan",
        )
        return []

    return actions or []


def _should_use_provider_actions(contract: Dict[str, Any], logger: logging.Logger) -> bool:
    """
    Determine if we should use ProviderActionParser (0.7.1+) or legacy flow.

    Returns True if:
    - Explicit providerActions array exists, OR
    - Version is 0.7.0+ and parser is available
    """
    # Check for explicit provider actions
    if "providerActions" in contract:
        return True

    # Check version and parser availability
    version = contract.get("fluidVersion", _default_fluid_version())
    if _parse_semver(version) >= (0, 7, 0) and PROVIDER_ACTIONS_AVAILABLE:
        return True

    return False


def _plan_with_provider_actions(
    contract: Dict[str, Any], args, logger: logging.Logger
) -> Dict[str, Any]:
    """Generate plan using the v0.7.x ``ProviderActionParser``.

    Handles both:
    - Explicit ``providerActions[]`` arrays (when present)
    - Inferred actions from ``exposes[]`` / ``builds[]`` (the common path)

    Returns plan dict with actions, dependencies, and execution order.
    """
    if not PROVIDER_ACTIONS_AVAILABLE:
        raise CLIError(
            1,
            "provider_actions_not_available",
            context={"message": "ProviderActionParser not available. Install 0.7.x dependencies."},
        )

    parser = ProviderActionParser(logger)

    # Parse provider actions (handles both explicit and inferred)
    actions = parser.parse(contract)

    info(logger, "provider_actions_parsed", count=len(actions))

    if not actions:
        warn(
            logger,
            "no_actions_generated",
            contract_id=contract.get("id"),
            detail="No actions could be parsed or inferred from contract",
        )
        return {
            "format_version": contract.get("fluidVersion", _default_fluid_version()),
            "generated_at": time.time(),
            # Stage-7 apply needs the FULL contract to dispatch (provider
            # platform, binding.location, exposes, builds). Stripping it
            # here broke the canonical ``fluid plan → fluid apply plan.json``
            # flow because apply couldn't resolve the provider. Keep the
            # stripped metadata as ``contract_metadata`` for consumers that
            # only want identity (viz-plan, audit logs).
            "contract": contract,
            "contract_metadata": {
                "id": contract.get("id"),
                "name": contract.get("name")
                or contract.get("metadata", {}).get("name")
                or "Unknown",
                "version": contract.get("fluidVersion", _default_fluid_version()),
                # Absolute path of the source contract file. Used by the
                # build runner (run_builds_from_args) to anchor relative
                # paths like ``repository: ../../reference-assets/...``
                # when invoked via ``fluid apply <plan>.json --mode
                # amend-and-build``. Without this, the runner would anchor
                # against runtime/plan.json's parent dir and resolve
                # relatives wrong.
                "source_path": (
                    str(Path(args.contract).resolve()) if getattr(args, "contract", None) else None
                ),
            },
            "actions": [],
            "total_actions": 0,
        }

    # Build dependency graph
    graph = parser.build_dependency_graph(actions)

    # Check for circular dependencies
    if graph.get("has_cycles"):
        raise CLIError(1, "dependency_cycle_detected", context={"cycles": graph.get("cycles", [])})

    # Get execution order (topological sort)
    execution_levels = parser.get_execution_order(actions)

    # Flatten to ordered list
    ordered = []
    for level in execution_levels:
        for action_id in level:
            for action in actions:
                if action.action_id == action_id:
                    ordered.append(action)
                    break

    # Convert to plan format.
    # Emit BOTH ``op`` AND ``action_type`` for each action:
    #   - ``op`` is what ``fluid apply``'s provider dispatcher reads
    #     (see cli/apply.py::_actions_from_source which emits op=action.action_type.value
    #     on the yaml-contract → provider path). stage-7 apply fails loud
    #     with "Action missing required 'op' field" if omitted.
    #   - ``action_type`` is preserved for display/viz tooling
    #     (plan.py::_display_plan_*, viz_provider_actions.py) that still
    #     keys on action_type first (with op fallback). Dropping it would
    #     silently change plan.html labels.
    # Both hold the same string — ``action.action_type.value`` — so this
    # is just schema surface, not extra data.
    plan_actions = []
    for i, action in enumerate(ordered):
        op_value = action.action_type.value
        plan_actions.append(
            {
                "step": i + 1,
                "action_id": action.action_id,
                "op": op_value,
                "action_type": op_value,
                "provider": action.provider,
                "params": action.params,
                "depends_on": action.depends_on,
                "description": action.description or f"{op_value} on {action.provider}",
            }
        )

    return {
        "format_version": contract.get("fluidVersion", _default_fluid_version()),
        "generated_at": time.time(),
        # ``mode`` records the apply mode the operator generated this
        # plan FOR (None = mode-unaware plan). ``fluid apply plan.json
        # --mode X`` uses this to detect a mismatch and refuse rather
        # than silently running an additive apply when the operator
        # asked for replace.
        "mode": getattr(args, "mode", None),
        # Embed full contract so stage-7 apply can resolve provider,
        # binding, exposes, builds without re-reading the source file.
        # ``contract_metadata`` preserved for identity-only consumers.
        "contract": contract,
        "contract_metadata": {
            "id": contract.get("id"),
            "name": contract.get("name") or contract.get("metadata", {}).get("name") or "Unknown",
            "version": contract.get("fluidVersion", _default_fluid_version()),
            # See ``source_path`` rationale at the actions=[] branch above.
            "source_path": (
                str(Path(args.contract).resolve()) if getattr(args, "contract", None) else None
            ),
        },
        "actions": plan_actions,
        "total_actions": len(plan_actions),
        "has_dependencies": any(a["depends_on"] for a in plan_actions),
        "dependency_graph": {
            "nodes": [a["action_id"] for a in plan_actions],
            "edges": [(a["action_id"], dep) for a in plan_actions for dep in a["depends_on"]],
        },
    }


def _plan_legacy(contract: Dict[str, Any], args, logger: logging.Logger) -> Dict[str, Any]:
    """Generate plan via the provider's ``plan()`` method directly.

    Used for v0.7.x contracts whose providers haven't migrated to
    ``providerActions[]`` yet (Snowflake / GCP / AWS still emit their
    plans through this path). The function name is preserved for
    git-blame continuity but the "legacy" framing is gone — this is
    the standard plan path for class-based providers.

    Provider is resolved from: --provider flag > contract
    binding.platform > FLUID_PROVIDER env.
    """
    provider_flag = getattr(args, "provider", None)
    project_flag = getattr(args, "project", None)
    region_flag = getattr(args, "region", None)

    # If no --provider flag, read it from the contract schema
    if not provider_flag:
        contract_provider, contract_location = resolve_provider_from_contract(contract)
        provider_flag = contract_provider or None
        if not project_flag:
            project_flag = contract_location.get("project")
        if not region_flag:
            region_flag = contract_location.get("region")

    provider = build_provider(provider_flag, project_flag, region_flag, logger)

    # --- Lifecycle hooks: pre_plan ---
    from fluid_build.cli.hooks import run_on_error, run_post_plan, run_pre_plan

    contract = run_pre_plan(provider, contract, logger)

    if hasattr(provider, "plan"):
        try:
            actions = provider.plan(contract)
        except Exception as exc:
            run_on_error(provider, exc, "plan", logger)
            raise
    else:
        # Ultimate fallback - basic stub actions
        warn(
            logger,
            "provider_plan_not_implemented",
            provider=type(provider).__name__,
            detail="Provider does not implement plan() method. Using basic fallback.",
        )
        actions = [
            {"op": "ensure_dataset", "description": "Create dataset/database"},
            {"op": "ensure_table", "description": "Create tables/schemas"},
        ]

    # --- Lifecycle hooks: post_plan ---
    actions = run_post_plan(provider, actions, logger)

    return {
        "format_version": contract.get("fluidVersion", _default_fluid_version()),
        "generated_at": time.time(),
        # Embed full contract (see rationale in _plan_with_provider_actions).
        "contract": contract,
        "contract_metadata": {
            "id": contract.get("id"),
            "name": contract.get("name") or contract.get("metadata", {}).get("name") or "Unknown",
            "version": contract.get("fluidVersion", _default_fluid_version()),
            # See ``source_path`` rationale in _plan_with_provider_actions.
            "source_path": (
                str(Path(args.contract).resolve()) if getattr(args, "contract", None) else None
            ),
        },
        "actions": actions,
        "total_actions": len(actions),
    }


def _display_plan_rich(plan: Dict[str, Any], contract: Dict[str, Any]):
    """Display plan with Rich formatting (verbose mode)."""
    console = Console()

    # Header
    console.print(
        Panel.fit(
            f"[bold cyan]FLUID Execution Plan[/bold cyan]\n"
            f"Contract: {contract.get('name') or contract.get('metadata', {}).get('name') or 'Unknown'}\n"
            f"Version: {plan.get('contract', {}).get('version', plan.get('format_version', 'Unknown'))}\n"
            f"Total Actions: {plan['total_actions']}",
            border_style="cyan",
        )
    )

    # Actions table
    table = Table(title="Execution Steps", show_header=True, header_style="bold magenta")
    table.add_column("Step", style="dim", width=6)
    table.add_column("Action ID", style="cyan")
    table.add_column("Type", style="green")
    table.add_column("Provider", style="yellow")
    table.add_column("Dependencies", style="blue")

    for action in plan["actions"]:
        table.add_row(
            str(action.get("step", "?")),
            action.get("action_id", action.get("op", "unknown")),
            action.get("action_type", action.get("op", "unknown")),
            action.get("provider", "N/A"),
            ", ".join(action.get("depends_on", [])) or "None",
        )

    console.print(table)

    # Dependency graph info
    if plan.get("has_dependencies"):
        console.print(
            "\n[yellow]⚠️  This plan has dependencies. Actions will execute in dependency order.[/yellow]"
        )


def _display_plan_simple(plan: Dict[str, Any], logger: logging.Logger, output_path: str = None):
    """Display plan with simple text output."""
    version = plan.get("contract", {}).get("version", plan.get("format_version", "Unknown"))
    name = plan.get("contract", {}).get("name") or "Unknown"
    total = plan["total_actions"]

    cprint(f"\n{'=' * 60}")
    cprint("FLUID Execution Plan")
    cprint(f"{'=' * 60}")
    cprint(f"Contract: {name}")
    cprint(f"Version: {version}")
    cprint(f"Total Actions: {total}")
    cprint(f"{'=' * 60}\n")

    if total > 0:
        for action in plan["actions"]:
            step = action.get("step", "?")
            action_id = action.get("action_id", action.get("op", "unknown"))
            action_type = action.get("action_type", action.get("op", "unknown"))
            deps = action.get("depends_on", [])

            cprint(f"{step}. {action_id} ({action_type})")
            if deps:
                cprint(f"   → Depends on: {', '.join(deps)}")

        cprint(f"\n✅ Plan saved to: {output_path or 'output file'}")
    else:
        warning("No actions generated")

    cprint()
