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
FLUID Plan Command - Unified Version

Supports both FLUID 0.5.7 and 0.7.1+ contracts with automatic version detection.
Uses ProviderActionParser for 0.7.1+ (with dependency resolution) and legacy
provider.plan() for 0.5.7.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict

from fluid_build.cli.console import cprint, warning

from ._common import (
    CLIError,
    build_provider,
    load_contract_with_overlay,
    resolve_provider_from_contract,
)
from ._logging import info, warn


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

    # Serialize to string with consistent formatting
    new_content = json.dumps(obj, indent=2, sort_keys=True)  # sort_keys for determinism

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
    """Register unified plan command supporting both 0.5.7 and 0.7.1+"""
    p = subparsers.add_parser(
        COMMAND,
        help="Generate execution plan from FLUID contract",
        description="""
Generate an execution plan from a FLUID data product contract.

UNIFIED SUPPORT for FLUID 0.5.7 and 0.7.1+:
• Automatically detects contract version
• 0.7.1+: Uses ProviderActionParser with dependency resolution
• 0.5.7: Uses legacy provider.plan() method
• Gracefully handles both explicit and inferred actions

The plan shows the sequence of operations needed to build and deploy
the data product, including infrastructure provisioning, data transformations,
access grants, and orchestration tasks.
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
    p.add_argument("contract", help="path to contract.fluid.yaml file")
    p.add_argument("--env", help="environment overlay (dev, staging, prod)")
    p.add_argument(
        "--out",
        "--output",
        dest="out",
        default="runtime/plan.json",
        help="output file for the execution plan (default: runtime/plan.json)",
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
        const="runtime/plan.html",
        default=None,
        help="generate HTML visualization (default path: runtime/plan.html)",
    )
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    """
    Main entry point with automatic version detection and routing.

    Supports both FLUID 0.7.1+ (provider actions) and 0.5.7 (legacy) contracts.
    """
    try:
        # Load contract with environment overlay
        contract = load_contract_with_overlay(args.contract, getattr(args, "env", None), logger)
        fluid_version = contract.get("fluidVersion", "0.5.7")

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
                warn(logger, "html_render_skipped", message="plan visualizer not available")

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
            "sdk_not_available",
            message="fluid-provider-sdk not installed — skipping action validation",
        )
        return

    raw_actions = plan.get("actions") or []
    if not raw_actions:
        info(logger, "validate_actions_skip", message="No actions to validate")
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
    version = contract.get("fluidVersion", "0.5.7")
    if _parse_semver(version) >= (0, 7, 0) and PROVIDER_ACTIONS_AVAILABLE:
        return True

    return False


def _plan_with_provider_actions(
    contract: Dict[str, Any], args, logger: logging.Logger
) -> Dict[str, Any]:
    """
    Generate plan using FLUID 0.7.1+ ProviderActionParser.

    Handles both:
    - Explicit providerActions array (0.7.1+)
    - Inferred actions from exposes/builds (0.5.7 compatibility)

    Returns plan dict with actions, dependencies, and execution order.
    """
    if not PROVIDER_ACTIONS_AVAILABLE:
        raise CLIError(
            1,
            "provider_actions_not_available",
            context={"message": "ProviderActionParser not available. Install 0.7.1 dependencies."},
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
            message="No actions could be parsed or inferred from contract",
        )
        return {
            "format_version": contract.get("fluidVersion", "0.7.1"),
            "generated_at": time.time(),
            "contract": {
                "id": contract.get("id"),
                "name": contract.get("name")
                or contract.get("metadata", {}).get("name")
                or "Unknown",
                "version": contract.get("fluidVersion", "0.7.1"),
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

    # Convert to plan format
    plan_actions = []
    for i, action in enumerate(ordered):
        plan_actions.append(
            {
                "step": i + 1,
                "action_id": action.action_id,
                "action_type": action.action_type.value,
                "provider": action.provider,
                "params": action.params,
                "depends_on": action.depends_on,
                "description": action.description
                or f"{action.action_type.value} on {action.provider}",
            }
        )

    return {
        "format_version": contract.get("fluidVersion", "0.7.1"),
        "generated_at": time.time(),
        "contract": {
            "id": contract.get("id"),
            "name": contract.get("name") or contract.get("metadata", {}).get("name") or "Unknown",
            "version": contract.get("fluidVersion", "0.7.1"),
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
    """
    Generate plan using legacy provider.plan() method (0.5.7).

    Falls back to provider-specific planning for older contracts.
    Provider is resolved from: --provider flag > contract binding.platform > FLUID_PROVIDER env.
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
            message="Provider does not implement plan() method. Using basic fallback.",
        )
        actions = [
            {"op": "ensure_dataset", "description": "Create dataset/database"},
            {"op": "ensure_table", "description": "Create tables/schemas"},
        ]

    # --- Lifecycle hooks: post_plan ---
    actions = run_post_plan(provider, actions, logger)

    return {
        "format_version": "0.5.7",
        "generated_at": time.time(),
        "contract": {
            "id": contract.get("id"),
            "name": contract.get("name") or contract.get("metadata", {}).get("name") or "Unknown",
            "version": contract.get("fluidVersion", "0.5.7"),
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

    cprint(f"\n{'='*60}")
    cprint("FLUID Execution Plan")
    cprint(f"{'='*60}")
    cprint(f"Contract: {name}")
    cprint(f"Version: {version}")
    cprint(f"Total Actions: {total}")
    cprint(f"{'='*60}\n")

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
