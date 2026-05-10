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

"""``fluid apply`` plan-display + report-generation helpers.

Lifted from ``cli/apply.py`` (host file was 1530 LOC). ~240 LOC of
post-execution renderers:

* :func:`_display_execution_plan`, :func:`_confirm_execution`,
  :func:`_display_dry_run_summary` — Rich UI for the operator.
* :func:`_generate_final_report`, :func:`_generate_html_report`,
  :func:`_generate_json_report`, :func:`_generate_markdown_report`
  — file emitters.
* :func:`_send_notifications`, :func:`_export_metrics` — observability
  hooks.

``apply.py`` re-imports each at module top so existing test patches
that target ``fluid_build.cli.apply.<helper>`` keep resolving.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from rich.table import Table

from fluid_build.cli.orchestration import ExecutionContext, ExecutionPlan


# ── Indirection accessors ───────────────────────────────────────────────
# Tests patch ``fluid_build.cli.apply.cprint`` /
# ``fluid_build.cli.apply.RICH_AVAILABLE``. Resolve those via the host
# module so the patches flow through to the extracted helpers.
def _host():
    from fluid_build.cli import apply as _apply

    return _apply


def cprint(*args, **kwargs):
    return _host().cprint(*args, **kwargs)


def success(*args, **kwargs):
    return _host().success(*args, **kwargs)


def warning(*args, **kwargs):
    return _host().warning(*args, **kwargs)


# Bare-name lookups inside functions (``LOAD_GLOBAL``) don't trigger
# module ``__getattr__``, so we can't rely on PEP 562 for
# ``RICH_AVAILABLE``. Instead, we expose a ``RICH_AVAILABLE`` *property*
# via a wrapper class — accessing the bare name resolves the property,
# which delegates to the host module so test patches flow through.
class _RichAvailableProxy:
    def __bool__(self) -> bool:
        return bool(getattr(_host(), "RICH_AVAILABLE", False))

    def __eq__(self, other) -> bool:
        return bool(self) == bool(other)


RICH_AVAILABLE = _RichAvailableProxy()


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
            <p>Status: {"✅ Success" if execution_result.get("success") else "❌ Failed"}</p>
        </div>
        
        <div class="metrics">
            <div class="metric">
                <h3>Total Actions</h3>
                <p>{execution_result.get("metrics", {}).get("total_actions", 0)}</p>
            </div>
            <div class="metric">
                <h3>Successful</h3>
                <p>{execution_result.get("metrics", {}).get("successful_actions", 0)}</p>
            </div>
            <div class="metric">
                <h3>Failed</h3>
                <p>{execution_result.get("metrics", {}).get("failed_actions", 0)}</p>
            </div>
            <div class="metric">
                <h3>Duration</h3>
                <p>{execution_result.get("metrics", {}).get("total_duration_seconds", 0):.2f}s</p>
            </div>
        </div>
        
        <h2>Phase Details</h2>
    """

    for phase in execution_result.get("phases", []):
        status_class = "success" if phase.get("status") == "success" else "failed"
        html_content += f"""
        <div class="phase {status_class}">
            <h3>{phase.get("phase", "Unknown").title()}</h3>
            <p>Status: {phase.get("status", "unknown")}</p>
            <p>Actions: {phase.get("action_count", 0)}</p>
            <p>Duration: {phase.get("duration", 0):.2f}s</p>
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
- **Status**: {status_icon} {"Success" if execution_result.get("success") else "Failed"}
- **Contract**: {context.plan.contract_path}
- **Environment**: {context.plan.environment or "default"}
- **Duration**: {execution_result.get("metrics", {}).get("total_duration_seconds", 0):.2f} seconds

## Metrics
| Metric | Value |
|--------|-------|
| Total Actions | {execution_result.get("metrics", {}).get("total_actions", 0)} |
| Successful | {execution_result.get("metrics", {}).get("successful_actions", 0)} |
| Failed | {execution_result.get("metrics", {}).get("failed_actions", 0)} |
| Skipped | {execution_result.get("metrics", {}).get("skipped_actions", 0)} |

## Phase Details
"""

    for phase in execution_result.get("phases", []):
        phase_icon = "✅" if phase.get("status") == "success" else "❌"
        markdown_content += f"""
### {phase_icon} {phase.get("phase", "Unknown").title()}
- **Status**: {phase.get("status", "unknown")}
- **Actions**: {phase.get("action_count", 0)}
- **Duration**: {phase.get("duration", 0):.2f}s
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
