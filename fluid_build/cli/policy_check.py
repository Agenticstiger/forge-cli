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
FLUID Policy Check Command

Validates contracts against schema-driven policy declarations.
Enforces governance rules defined in the FLUID 0.5.7 schema.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fluid_build.cli.console import cprint, success
from fluid_build.cli.console import error as console_error

try:
    import time

    from rich import box
    from rich.align import Align
    from rich.box import DOUBLE, HEAVY, ROUNDED
    from rich.columns import Columns
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
    from rich.table import Table
    from rich.text import Text
    from rich.tree import Tree

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

from ..policy.schema_engine import (
    PolicyCategory,
    PolicyEnforcementResult,
    PolicySeverity,
    SchemaBasedPolicyEngine,
)
from ._common import CLIError, load_contract_with_overlay

logger = logging.getLogger(__name__)

COMMAND = "policy-check"


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register policy-check command"""
    parser = subparsers.add_parser(
        "policy-check",
        help="Validate contract against schema-driven policies",
        description="Enforce governance policies declared in FLUID contracts",
    )

    parser.add_argument("contract", help="Path to FLUID contract file (contract.fluid.yaml)")

    parser.add_argument("--env", help="Environment overlay (dev, staging, prod)")

    parser.add_argument(
        "--strict", action="store_true", help="Treat warnings as errors (fail on any violation)"
    )

    parser.add_argument(
        "--category",
        choices=["sensitivity", "access_control", "data_quality", "lifecycle", "schema_evolution"],
        help="Check only specific policy category",
    )

    parser.add_argument("--output", "-o", help="Output file for policy report (JSON)")

    parser.add_argument(
        "--format",
        choices=["rich", "text", "json"],
        default="rich" if RICH_AVAILABLE else "text",
        help="Output format",
    )

    parser.add_argument(
        "--show-passed", action="store_true", help="Show passed checks (default: only violations)"
    )

    parser.set_defaults(func=run)


def run(args: argparse.Namespace, logger_instance: logging.Logger) -> int:
    """Execute policy check command"""
    global logger
    logger = logger_instance

    try:
        if RICH_AVAILABLE and args.format == "rich":
            console = Console()

            # Sexy loading animation
            with Progress(
                SpinnerColumn("dots"), TextColumn("[bold blue]{task.description}"), console=console
            ) as progress:
                task = progress.add_task(f"🔍 Loading contract: {args.contract}", total=None)
                contract_path = Path(args.contract)

                if not contract_path.exists():
                    raise CLIError(
                        1,
                        "file_not_found",
                        context={"message": f"Contract not found: {args.contract}"},
                    )

                contract = load_contract_with_overlay(str(contract_path), args.env, logger)
                progress.update(task, description=f"✅ Loaded: {contract.get('id', 'unknown')}")
                time.sleep(0.3)  # Smooth transition

                progress.remove_task(task)
                task = progress.add_task("🛡️  Initializing policy engine", total=None)
                engine = SchemaBasedPolicyEngine(contract)
                time.sleep(0.2)
                progress.update(task, description="⚡ Running comprehensive policy checks")
                result = engine.enforce_all()
                time.sleep(0.2)
                progress.update(
                    task, description=f"✨ Analysis complete: {result.calculate_score()}/100"
                )
                time.sleep(0.3)
        else:
            # Fallback without animations
            logger.info(f"Loading contract: {args.contract}")
            contract_path = Path(args.contract)

            if not contract_path.exists():
                raise CLIError(
                    1, "file_not_found", context={"message": f"Contract not found: {args.contract}"}
                )

            contract = load_contract_with_overlay(str(contract_path), args.env, logger)
            logger.info("Running policy enforcement")
            engine = SchemaBasedPolicyEngine(contract)
            result = engine.enforce_all()

        # Filter by category if specified
        if args.category:
            category = PolicyCategory(args.category)
            result.violations = result.get_by_category(category)

        # Output results
        if args.format == "json":
            output_json(result, args.output)
        elif args.format == "rich" and RICH_AVAILABLE:
            output_rich(result, contract, args.show_passed, args.strict)
        else:
            output_text(result, contract, args.show_passed, args.strict)

        # Return exit code
        if not result.is_compliant():
            logger.warning("Policy compliance check FAILED")
            return 1

        if args.strict and result.violations:
            logger.warning("Policy check FAILED (strict mode)")
            return 1

        logger.info("Policy compliance check PASSED")
        return 0

    except CLIError as e:
        logger.error(f"Policy check error: {e.message}")
        return e.exit_code
    except Exception as e:
        logger.exception("Unexpected error during policy check")
        console_error(f"Policy check failed: {e}")
        return 1


def output_rich(
    result: PolicyEnforcementResult, contract: Dict[str, Any], show_passed: bool, strict: bool
) -> None:
    """Output results using Rich formatting with modern, sexy design"""
    console = Console()

    # Stunning header with gradient-like effect
    contract_id = contract.get("id", "unknown")
    score = result.calculate_score()

    # Dynamic score styling
    if score >= 95:
        score_emoji = "🏆"
        score_color = "bright_green"
        grade = "EXCEPTIONAL"
    elif score >= 90:
        score_emoji = "✨"
        score_color = "green"
        grade = "EXCELLENT"
    elif score >= 80:
        score_emoji = "👍"
        score_color = "blue"
        grade = "GOOD"
    elif score >= 70:
        score_emoji = "⚠️"
        score_color = "yellow"
        grade = "FAIR"
    elif score >= 50:
        score_emoji = "🔶"
        score_color = "orange"
        grade = "NEEDS WORK"
    else:
        score_emoji = "🚨"
        score_color = "red"
        grade = "CRITICAL"

    # Create stunning header
    header = Table.grid(padding=1)
    header.add_column(style="cyan", justify="right")
    header.add_column(style="white")

    header.add_row("🛡️  Contract:", f"[bold]{contract_id}[/bold]")
    header.add_row(
        "📊 Policy Score:",
        f"[{score_color} bold]{score_emoji} {score}/100[/{score_color} bold] [dim]({grade})[/dim]",
    )
    header.add_row("✅ Checks Passed:", f"[green]{result.checks_passed}[/green]")
    header.add_row("❌ Checks Failed:", f"[red]{result.checks_failed}[/red]")

    console.print()
    console.print(
        Panel(
            Align.center(header),
            title="[bold blue]🔍 FLUID Policy Governance Report[/bold blue]",
            border_style=score_color,
            box=ROUNDED,
            padding=(1, 2),
        )
    )
    console.print()

    # Sexy category tree visualization
    categories = {cat: result.get_by_category(cat) for cat in PolicyCategory}

    # Category icons and colors
    category_meta = {
        PolicyCategory.SENSITIVITY: {
            "icon": "🔒",
            "name": "Data Sensitivity & Privacy",
            "color": "magenta",
        },
        PolicyCategory.ACCESS_CONTROL: {
            "icon": "🛡️",
            "name": "Access Control & Authorization",
            "color": "blue",
        },
        PolicyCategory.DATA_QUALITY: {
            "icon": "📊",
            "name": "Data Quality Standards",
            "color": "cyan",
        },
        PolicyCategory.LIFECYCLE: {"icon": "🔄", "name": "Lifecycle Management", "color": "yellow"},
        PolicyCategory.SCHEMA_EVOLUTION: {
            "icon": "⚡",
            "name": "Schema Evolution Control",
            "color": "green",
        },
    }

    for category, violations in categories.items():
        meta = category_meta[category]

        # Count by severity
        critical = len([v for v in violations if v.severity == PolicySeverity.CRITICAL])
        errors = len([v for v in violations if v.severity == PolicySeverity.ERROR])
        warnings = len([v for v in violations if v.severity == PolicySeverity.WARNING])
        len(violations)

        # Determine status
        if critical > 0:
            status_icon = "🚨"
            status_color = "red"
            status_text = "CRITICAL"
        elif errors > 0:
            status_icon = "⚠️"
            status_color = "yellow"
            status_text = "NEEDS ATTENTION"
        elif warnings > 0:
            status_icon = "💡"
            status_color = "yellow"
            status_text = "ADVISORY"
        else:
            status_icon = "✅"
            status_color = "green"
            status_text = "COMPLIANT"

        # Create tree for this category
        if violations or show_passed:
            tree = Tree(
                f"[{status_color} bold]{status_icon} {meta['icon']} {meta['name']}[/{status_color} bold] [dim]({status_text})[/dim]",
                guide_style=meta["color"],
            )

            if violations:
                # Group by severity
                for severity in [
                    PolicySeverity.CRITICAL,
                    PolicySeverity.ERROR,
                    PolicySeverity.WARNING,
                    PolicySeverity.INFO,
                ]:
                    severity_violations = [v for v in violations if v.severity == severity]
                    if not severity_violations:
                        continue

                    severity_icons = {
                        PolicySeverity.CRITICAL: "🚨",
                        PolicySeverity.ERROR: "❌",
                        PolicySeverity.WARNING: "⚠️",
                        PolicySeverity.INFO: "ℹ️",
                    }
                    severity_colors = {
                        PolicySeverity.CRITICAL: "red",
                        PolicySeverity.ERROR: "red",
                        PolicySeverity.WARNING: "yellow",
                        PolicySeverity.INFO: "blue",
                    }

                    severity_node = tree.add(
                        f"[{severity_colors[severity]}]{severity_icons[severity]} {severity.value.upper()} ({len(severity_violations)})[/{severity_colors[severity]}]"
                    )

                    for v in severity_violations:
                        # Build location badge
                        location_parts = []
                        if v.expose_id:
                            location_parts.append(f"[cyan]{v.expose_id}[/cyan]")
                        if v.field:
                            location_parts.append(f"[blue]{v.field}[/blue]")
                        if v.rule_id:
                            location_parts.append(f"[yellow]{v.rule_id}[/yellow]")

                        location_badge = " → ".join(location_parts) if location_parts else ""

                        violation_node = severity_node.add(f"[white]{v.message}[/white]")
                        if location_badge:
                            violation_node.add(f"📍 {location_badge}")
                        if v.remediation:
                            violation_node.add(f"[dim]💡 Remediation: {v.remediation}[/dim]")
            else:
                passed_count = _estimate_passed_checks(result, category)
                tree.add(f"[green]✓ All {passed_count} checks passed[/green]")

            console.print(tree)
            console.print()
        else:
            # Compact view for clean categories
            passed_count = _estimate_passed_checks(result, category)
            console.print(
                f"[{status_color}]{status_icon}[/{status_color}] "
                f"[bold {meta['color']}]{meta['icon']} {meta['name']}[/bold {meta['color']}] "
                f"[dim]({passed_count} checks passed)[/dim]"
            )

    console.print()

    # Stunning summary with visual score bar
    blocking = result.get_blocking_violations()

    # Create visual score bar
    score_bar = _create_score_bar(score)

    # Summary table
    summary_table = Table.grid(padding=(0, 2))
    summary_table.add_column(style="dim", justify="right")
    summary_table.add_column()

    summary_table.add_row("Compliance Score:", score_bar)
    summary_table.add_row("Checks Passed:", f"[green]✓ {result.checks_passed}[/green]")
    summary_table.add_row("Checks Failed:", f"[red]✗ {result.checks_failed}[/red]")

    if blocking:
        summary_table.add_row("Blocking Issues:", f"[red bold]🚨 {len(blocking)}[/red bold]")

    if len(result.violations) > len(blocking):
        non_blocking = len(result.violations) - len(blocking)
        summary_table.add_row("Advisory Issues:", f"[yellow]💡 {non_blocking}[/yellow]")

    console.print(
        Panel(
            Align.center(summary_table),
            title="[bold]📈 Policy Compliance Summary[/bold]",
            border_style=score_color,
            box=ROUNDED,
        )
    )
    console.print()

    # Final verdict with style
    if result.is_compliant():
        verdict_panel = Panel(
            Align.center(
                "[bold green]✅ PASSED[/bold green]\n[dim]Contract meets all governance requirements[/dim]"
            ),
            border_style="green",
            box=HEAVY,
        )
    else:
        action_text = f"[red]🚨 {len(blocking)} BLOCKING issue(s) must be resolved[/red]"
        if strict and result.violations:
            action_text += "\n[yellow]⚠️  Strict mode: All violations must be fixed[/yellow]"

        verdict_panel = Panel(
            Align.center(f"[bold red]❌ FAILED[/bold red]\n{action_text}"),
            border_style="red",
            box=HEAVY,
        )

    console.print(verdict_panel)


def output_text(
    result: PolicyEnforcementResult, contract: Dict[str, Any], show_passed: bool, strict: bool
) -> None:
    """Output results in plain text"""
    contract_id = contract.get("id", "unknown")
    score = result.calculate_score()

    cprint("\n📋 Schema-Based Policy Validation")
    cprint(f"Contract: {contract_id}")
    cprint(f"Score: {score}/100\n")
    cprint("=" * 60)

    # Group by category
    categories = {cat: result.get_by_category(cat) for cat in PolicyCategory}

    for category, violations in categories.items():
        category_name = category.value.replace("_", " ").title()

        if violations:
            cprint(f"\n❌ {category_name} ({len(violations)} issues)")
            for v in violations:
                location = []
                if v.expose_id:
                    location.append(f"expose: {v.expose_id}")
                if v.field:
                    location.append(f"field: {v.field}")

                loc_str = f" [{', '.join(location)}]" if location else ""
                cprint(f"  {v.severity.value.upper()}: {v.message}{loc_str}")

                if v.remediation:
                    cprint(f"    💡 {v.remediation}")
        else:
            cprint(f"\n✅ {category_name}")

    # Summary
    cprint("\n" + "=" * 60)
    cprint(f"Checks Passed: {result.checks_passed}")
    cprint(f"Checks Failed: {result.checks_failed}")
    cprint(f"Total Violations: {len(result.violations)}")
    cprint(f"Blocking Issues: {len(result.get_blocking_violations())}")
    cprint(f"Policy Score: {score}/100\n")

    if result.is_compliant():
        success("Contract is policy compliant")
    else:
        console_error("Contract has policy violations")


def output_json(result: PolicyEnforcementResult, output_file: Optional[str] = None) -> None:
    """Output results in JSON format"""
    output = result.to_dict()

    json_str = json.dumps(output, indent=2)

    if output_file:
        with open(output_file, "w") as f:
            f.write(json_str)
        success(f"Policy report saved to: {output_file}")
    else:
        cprint(json_str)


def _estimate_passed_checks(result: PolicyEnforcementResult, category: PolicyCategory) -> int:
    """Estimate number of passed checks for a category"""
    # This is a rough estimate since we don't track passes by category
    violations = len(result.get_by_category(category))
    total_for_category = result.checks_passed // len(PolicyCategory)
    return max(0, total_for_category - violations)


def _create_score_bar(score: int) -> str:
    """Create a visual score bar with gradient colors"""
    # Create a visual bar representation
    bar_length = 30
    filled = int((score / 100) * bar_length)

    # Color segments based on score ranges
    if score >= 90:
        bar_color = "bright_green"
        emoji = "🏆"
    elif score >= 80:
        bar_color = "green"
        emoji = "✨"
    elif score >= 70:
        bar_color = "blue"
        emoji = "👍"
    elif score >= 50:
        bar_color = "yellow"
        emoji = "⚠️"
    else:
        bar_color = "red"
        emoji = "🚨"

    filled_bar = "█" * filled
    empty_bar = "░" * (bar_length - filled)

    return f"[{bar_color}]{filled_bar}[/{bar_color}][dim]{empty_bar}[/dim] [{bar_color} bold]{emoji} {score}/100[/{bar_color} bold]"
