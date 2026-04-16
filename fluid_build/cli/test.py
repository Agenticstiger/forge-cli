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
FLUID Test Command — Live Data Contract Testing

Connects to actual deployed resources and validates that they match the FLUID
contract specification.  Inspired by the Data Contract CLI's ``test`` command
but integrated with Fluid's multi-provider ecosystem.

Checks performed:
- Contract schema syntax validation
- Provider connectivity
- Schema comparison (fields, types, nullability)
- Row-count / SLA thresholds
- Quality tests declared in the contract
- Metadata / governance completeness
- Drift detection (optional)

Output formats:
- Rich terminal table (default)
- Plain text
- JSON
- JUnit XML (for CI/CD integration)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

from fluid_build.cli.console import cprint, success, warning
from fluid_build.cli.console import error as console_error

# Rich imports (optional)
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

LOG = logging.getLogger("fluid.cli.test")
COMMAND = "test"


# ======================================================================
# Registration
# ======================================================================


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``fluid test`` command."""
    p = subparsers.add_parser(
        COMMAND,
        help="Test a FLUID contract against live data (schema, quality, SLAs)",
        description="""
        Connects to actual deployed resources and validates that they match
        the FLUID contract specification.

        Performs comprehensive checks:
        - Schema comparison (fields, types, nullability)
        - Row-count / SLA thresholds (freshness, completeness)
        - Quality tests declared in the contract
        - Metadata and governance completeness
        - Optional drift detection against historical results

        Supports all FLUID providers: gcp, snowflake, aws, local.
        """,
        epilog="""
Examples:
  # Test contract against live resources
  fluid test contract.fluid.yaml

  # Test with a specific environment overlay
  fluid test contract.fluid.yaml --env prod

  # Override detected provider
  fluid test contract.fluid.yaml --provider snowflake

  # Output JUnit XML for CI/CD
  fluid test contract.fluid.yaml --output junit --output-file results.xml

  # JSON output piped to jq
  fluid test contract.fluid.yaml --output json | jq '.summary'

  # Strict mode — warnings fail the build
  fluid test contract.fluid.yaml --strict

  # Enable drift detection
  fluid test contract.fluid.yaml --check-drift
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # --- positional ---
    p.add_argument(
        "contract",
        help="Path to contract.fluid.(yaml|json)",
    )

    # --- optional ---
    p.add_argument("--env", help="Environment overlay (dev/test/prod)")
    p.add_argument("--provider", help="Override provider platform (gcp, snowflake, aws, local)")
    p.add_argument("--project", help="Override project/account ID")
    p.add_argument("--region", help="Override region/location")

    p.add_argument(
        "--server",
        help="Provider connection string or identifier (e.g. Snowflake account locator)",
    )

    p.add_argument(
        "--strict",
        action="store_true",
        default=False,
        help="Treat warnings as errors (exit 1 on any warning)",
    )

    p.add_argument(
        "--no-data",
        action="store_true",
        default=False,
        help="Skip live data validation (structure-only checks)",
    )

    # --- output ---
    p.add_argument(
        "--output",
        choices=["text", "json", "junit"],
        default="text",
        help="Output format (default: text)",
    )
    p.add_argument(
        "--output-file",
        help="Write report to file instead of stdout",
    )

    # --- caching ---
    p.add_argument(
        "--no-cache",
        dest="cache",
        action="store_false",
        default=True,
        help="Disable schema caching",
    )
    p.add_argument(
        "--cache-ttl", type=int, default=3600, help="Cache TTL in seconds (default: 3600)"
    )
    p.add_argument("--cache-clear", action="store_true", help="Clear cache before running")

    # --- drift ---
    p.add_argument(
        "--check-drift", action="store_true", help="Detect validation drift vs. historical results"
    )

    # --- publish test results ---
    p.add_argument(
        "--publish",
        metavar="URL",
        help=(
            "Publish test results to Data Mesh Manager / Entropy Data. "
            "URL should be the test-results endpoint, e.g. "
            "https://api.entropy-data.com/api/test-results"
        ),
    )

    p.set_defaults(cmd=COMMAND, func=run)


# ======================================================================
# Entry point
# ======================================================================


def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Execute ``fluid test``."""
    contract_path = Path(args.contract)
    if not contract_path.exists():
        console_error(f"Contract file not found: {contract_path}")
        return 1

    # Import ContractValidator lazily to avoid circular deps
    from .contract_validation import ContractValidator

    validator = ContractValidator(
        contract_path=contract_path,
        env=getattr(args, "env", None),
        provider_name=getattr(args, "provider", None),
        project=getattr(args, "project", None),
        region=getattr(args, "region", None),
        strict=getattr(args, "strict", False),
        check_data=not getattr(args, "no_data", False),
        use_cache=getattr(args, "cache", True),
        cache_ttl=getattr(args, "cache_ttl", 3600),
        cache_clear=getattr(args, "cache_clear", False),
        track_history=True,
        check_drift=getattr(args, "check_drift", False),
        server=getattr(args, "server", None),
        logger=logger,
    )

    try:
        report = validator.validate()
    except Exception as e:
        console_error(f"Test failed: {e}")
        LOG.exception("test_error")
        return 1

    # --- output ---
    output_format = getattr(args, "output", "text")
    output_file = getattr(args, "output_file", None)

    if output_format == "json":
        _output_json(report, output_file)
    elif output_format == "junit":
        _output_junit(report, output_file)
    else:
        _output_rich(report, output_file)

    # --- publish test results to Data Mesh Manager ---
    publish_url = getattr(args, "publish", None)
    if publish_url:
        _publish_results(report, publish_url, logger)

    # --- exit code ---
    if not report.is_valid():
        return 1
    if getattr(args, "strict", False) and report.get_warnings():
        console_error("Test failed: warnings treated as errors (--strict)")
        return 1
    return 0


# ======================================================================
# Output – Rich table (inspired by DCCLI)
# ======================================================================


def _output_rich(report, output_file: Optional[str] = None) -> None:
    """Render a Data-Contract-CLI-style test results table."""
    if not RICH_AVAILABLE:
        _output_plain(report, output_file)
        return

    console = Console(file=open(output_file, "w") if output_file else sys.stdout)

    # ── header panel ──
    passed = report.is_valid()
    icon = "\u2705" if passed else "\u274c"
    border = "green" if passed else "red"

    header_lines = [
        f"[bold]{icon} Data Contract Test: {report.contract_id}[/bold]",
        f"Version {report.contract_version}  |  Provider: {_detect_provider_label(report)}",
        "Duration: {dur:.2f}s  |  {dt}".format(
            dur=report.duration,
            dt=report.validation_time.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    ]
    console.print(Panel("\n".join(header_lines), border_style=border, title="fluid test"))

    # ── check-by-check table ──
    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=4)
    table.add_column("Result", width=6)
    table.add_column("Check", min_width=22)
    table.add_column("Details", overflow="fold")

    # Build rows from issues + implicit passes
    check_no = 0

    # Schema syntax
    check_no += 1
    schema_errors = [i for i in report.issues if i.category == "schema" and i.severity == "error"]
    if schema_errors:
        table.add_row(
            str(check_no),
            "[red]\u274c[/red]",
            "Schema syntax",
            "; ".join(e.message for e in schema_errors),
        )
    else:
        table.add_row(str(check_no), "[green]\u2705[/green]", "Schema syntax", "Valid")

    # Provider connectivity
    check_no += 1
    conn_errors = [i for i in report.issues if i.category == "connection"]
    if conn_errors:
        table.add_row(
            str(check_no),
            "[red]\u274c[/red]",
            "Provider connection",
            "; ".join(e.message for e in conn_errors),
        )
    else:
        table.add_row(str(check_no), "[green]\u2705[/green]", "Provider connection", "OK")

    # Binding / platform
    check_no += 1
    bind_errors = [i for i in report.issues if i.category == "binding" and i.severity == "error"]
    bind_warns = [i for i in report.issues if i.category == "binding" and i.severity == "warning"]
    if bind_errors:
        table.add_row(
            str(check_no),
            "[red]\u274c[/red]",
            "Binding configuration",
            "; ".join(e.message for e in bind_errors),
        )
    elif bind_warns:
        table.add_row(
            str(check_no),
            "[yellow]\u26a0\ufe0f[/yellow]",
            "Binding configuration",
            "; ".join(e.message for e in bind_warns),
        )
    else:
        table.add_row(str(check_no), "[green]\u2705[/green]", "Binding configuration", "OK")

    # Missing resources
    check_no += 1
    missing = [i for i in report.issues if i.category == "missing_resource"]
    if missing:
        table.add_row(
            str(check_no),
            "[red]\u274c[/red]",
            "Resource exists",
            "; ".join(e.message for e in missing),
        )
    else:
        table.add_row(
            str(check_no),
            "[green]\u2705[/green]",
            "Resource exists",
            f"{max(report.exposes_validated, 1)} exposed resource(s) found",
        )

    # Field-level issues
    check_no += 1
    field_issues = [
        i
        for i in report.issues
        if i.category in ("missing_field", "type_mismatch", "mode_mismatch", "extra_field")
    ]
    field_errors = [i for i in field_issues if i.severity == "error"]
    field_warns = [i for i in field_issues if i.severity in ("warning", "info")]
    if field_errors:
        table.add_row(
            str(check_no),
            "[red]\u274c[/red]",
            "Schema fields",
            "{n} error(s): {msgs}".format(
                n=len(field_errors),
                msgs="; ".join(e.message for e in field_errors[:3]),
            ),
        )
    elif field_warns:
        table.add_row(
            str(check_no),
            "[yellow]\u26a0\ufe0f[/yellow]",
            "Schema fields",
            f"{len(field_warns)} warning(s)",
        )
    else:
        table.add_row(str(check_no), "[green]\u2705[/green]", "Schema fields", "All fields match")

    # Empty-table / row-count
    check_no += 1
    row_issues = [
        i for i in report.issues if i.category in ("empty_table", "row_count_below_threshold")
    ]
    row_errors = [i for i in row_issues if i.severity == "error"]
    row_warns = [i for i in row_issues if i.severity == "warning"]
    if row_errors:
        table.add_row(
            str(check_no),
            "[red]\u274c[/red]",
            "Row count / SLA",
            "; ".join(e.message for e in row_errors),
        )
    elif row_warns:
        table.add_row(
            str(check_no),
            "[yellow]\u26a0\ufe0f[/yellow]",
            "Row count / SLA",
            "; ".join(e.message for e in row_warns),
        )
    else:
        table.add_row(str(check_no), "[green]\u2705[/green]", "Row count / SLA", "OK")

    # Quality tests
    check_no += 1
    quality_issues = [i for i in report.issues if i.category == "quality"]
    q_errors = [i for i in quality_issues if i.severity == "error"]
    if q_errors:
        table.add_row(
            str(check_no),
            "[red]\u274c[/red]",
            "Quality tests",
            "; ".join(e.message for e in q_errors),
        )
    elif quality_issues:
        table.add_row(
            str(check_no),
            "[yellow]\u26a0\ufe0f[/yellow]",
            "Quality tests",
            "; ".join(e.message for e in quality_issues),
        )
    else:
        table.add_row(str(check_no), "[green]\u2705[/green]", "Quality tests", "Passed")

    # Metadata / governance
    check_no += 1
    meta_issues = [i for i in report.issues if i.category == "metadata"]
    m_errors = [i for i in meta_issues if i.severity == "error"]
    if m_errors:
        table.add_row(
            str(check_no),
            "[red]\u274c[/red]",
            "Metadata / governance",
            "; ".join(e.message for e in m_errors),
        )
    elif meta_issues:
        table.add_row(
            str(check_no),
            "[yellow]\u26a0\ufe0f[/yellow]",
            "Metadata / governance",
            f"{len(meta_issues)} info/warning(s)",
        )
    else:
        table.add_row(str(check_no), "[green]\u2705[/green]", "Metadata / governance", "Complete")

    # Drift
    drift_issues = [i for i in report.issues if i.category == "drift"]
    if drift_issues:
        check_no += 1
        table.add_row(
            str(check_no),
            "[yellow]\u26a0\ufe0f[/yellow]",
            "Drift detection",
            "; ".join(e.message for e in drift_issues),
        )

    console.print(table)

    # ── summary line ──
    total_errors = len(report.get_errors())
    total_warnings = len(report.get_warnings())
    if passed:
        console.print(
            f"\n[bold green]\u2705 {check_no - total_errors} check(s) passed[/bold green]"
            f"  |  {total_warnings} warning(s)  |  {report.duration:.2f}s"
        )
    else:
        console.print(
            f"\n[bold red]\u274c {total_errors} error(s)[/bold red]"
            f"  |  {total_warnings} warning(s)  |  {check_no - total_errors} passed  |  {report.duration:.2f}s"
        )

    if output_file:
        cprint(f"Report saved to: {output_file}")


def _output_plain(report, output_file: Optional[str] = None) -> None:
    """Fallback plain-text output when Rich is not installed."""
    lines: List[str] = []
    passed = report.is_valid()
    icon = "PASS" if passed else "FAIL"

    lines.append("=" * 60)
    lines.append(f"fluid test  |  {icon}  |  {report.contract_id}")
    lines.append(f"Version {report.contract_version}  |  Duration: {report.duration:.2f}s")
    lines.append("=" * 60)

    for idx, issue in enumerate(report.issues, 1):
        sev = issue.severity.upper()
        lines.append(f"  [{sev}] {issue.category}: {issue.message}")
        if issue.suggestion:
            lines.append(f"         -> {issue.suggestion}")

    lines.append("-" * 60)
    lines.append(
        f"{len(report.get_errors())} error(s), {len(report.get_warnings())} warning(s), {report.duration:.2f}s"
    )

    text = "\n".join(lines)
    if output_file:
        with open(output_file, "w") as f:
            f.write(text)
        cprint(f"Report saved to: {output_file}")
    else:
        cprint(text)


# ======================================================================
# Output – JSON
# ======================================================================


def _output_json(report, output_file: Optional[str] = None) -> None:
    """Emit machine-readable JSON report."""
    data = {
        "contract_path": report.contract_path,
        "contract_id": report.contract_id,
        "contract_version": report.contract_version,
        "validation_time": report.validation_time.isoformat(),
        "duration": round(report.duration, 3),
        "is_valid": report.is_valid(),
        "summary": {
            "exposes_validated": report.exposes_validated,
            "consumes_validated": report.consumes_validated,
            "checks_passed": report.checks_passed,
            "checks_failed": report.checks_failed,
            "errors": len(report.get_errors()),
            "warnings": len(report.get_warnings()),
        },
        "issues": [
            {
                "severity": i.severity,
                "category": i.category,
                "message": i.message,
                "path": i.path,
                "expected": i.expected,
                "actual": i.actual,
                "suggestion": i.suggestion,
            }
            for i in report.issues
        ],
    }
    text = json.dumps(data, indent=2)
    if output_file:
        with open(output_file, "w") as f:
            f.write(text)
        cprint(f"Report saved to: {output_file}")
    else:
        # Print raw so it's pipe-friendly
        sys.stdout.write(text + "\n")


# ======================================================================
# Output – JUnit XML
# ======================================================================


def _output_junit(report, output_file: Optional[str] = None) -> None:
    """Emit JUnit XML for CI/CD systems (Jenkins, GitHub Actions, etc.)."""
    ts = ET.Element("testsuite")
    ts.set("name", f"fluid-test:{report.contract_id}")
    ts.set("tests", str(report.checks_passed + report.checks_failed))
    ts.set("failures", str(report.checks_failed))
    ts.set("errors", "0")
    ts.set("time", f"{report.duration:.3f}")
    ts.set("timestamp", report.validation_time.isoformat())

    # Group issues by category for test-case granularity
    categories_seen: Dict[str, List] = {}
    for issue in report.issues:
        categories_seen.setdefault(issue.category, []).append(issue)

    # Emit one <testcase> per category
    all_categories = [
        "schema",
        "connection",
        "binding",
        "missing_resource",
        "missing_field",
        "type_mismatch",
        "mode_mismatch",
        "extra_field",
        "empty_table",
        "row_count_below_threshold",
        "quality",
        "metadata",
        "drift",
    ]
    for cat in all_categories:
        tc = ET.SubElement(ts, "testcase")
        tc.set("classname", f"fluid.test.{report.contract_id}")
        tc.set("name", cat)
        tc.set("time", f"{report.duration / max(len(all_categories), 1):.3f}")

        issues_in_cat = categories_seen.get(cat, [])
        errors_in_cat = [i for i in issues_in_cat if i.severity == "error"]
        warns_in_cat = [i for i in issues_in_cat if i.severity == "warning"]

        if errors_in_cat:
            fail = ET.SubElement(tc, "failure")
            fail.set("message", "; ".join(i.message for i in errors_in_cat))
            fail.set("type", "AssertionError")
            body_lines = []
            for i in errors_in_cat:
                body_lines.append(f"[ERROR] {i.message}")
                if i.expected is not None:
                    body_lines.append(f"  expected: {i.expected}")
                if i.actual is not None:
                    body_lines.append(f"  actual:   {i.actual}")
                if i.suggestion:
                    body_lines.append(f"  hint:     {i.suggestion}")
            fail.text = "\n".join(body_lines)
        elif warns_in_cat:
            # JUnit doesn't have "warning" — emit as system-out
            so = ET.SubElement(tc, "system-out")
            so.text = "\n".join(f"[WARN] {i.message}" for i in warns_in_cat)

    # Also add any categories we haven't covered
    for cat, issues_list in categories_seen.items():
        if cat not in all_categories:
            tc = ET.SubElement(ts, "testcase")
            tc.set("classname", f"fluid.test.{report.contract_id}")
            tc.set("name", cat)
            errors_in_cat = [i for i in issues_list if i.severity == "error"]
            if errors_in_cat:
                fail = ET.SubElement(tc, "failure")
                fail.set("message", "; ".join(i.message for i in errors_in_cat))
                fail.set("type", "AssertionError")

    tree = ET.ElementTree(ts)

    if output_file:
        tree.write(output_file, encoding="unicode", xml_declaration=True)
        cprint(f"JUnit XML saved to: {output_file}")
    else:
        ET.indent(ts)
        xml_str = ET.tostring(ts, encoding="unicode")
        sys.stdout.write('<?xml version="1.0" ?>\n' + xml_str + "\n")


# ======================================================================
# Helpers
# ======================================================================


def _detect_provider_label(report) -> str:
    """Best-effort provider label from report metadata."""
    _LABELS = {
        "gcp": "gcp (BigQuery)",
        "snowflake": "snowflake",
        "aws": "aws (Glue/Athena)",
        "local": "local (DuckDB)",
    }
    name = getattr(report, "provider_name", None)
    if name:
        return _LABELS.get(name, name)
    return "auto-detected"


# ======================================================================
# Publish – Data Mesh Manager / Entropy Data
# ======================================================================


def _publish_results(
    report,
    publish_url: str,
    logger: logging.Logger,
) -> None:
    """POST test results to a Data Mesh Manager / Entropy Data endpoint.

    Compatible with ``POST /api/test-results`` as used by DCCLI's
    ``--publish`` flag.
    """
    from fluid_build.providers.datamesh_manager.datamesh_manager import (
        DataMeshManagerProvider,
    )

    api_key = os.getenv("DMM_API_KEY", "")
    if not api_key:
        warning(f"DMM_API_KEY not set — skipping publish to {publish_url}")
        return

    try:
        provider = DataMeshManagerProvider(api_key=api_key, logger=logger)
        result = provider.publish_test_results(report, publish_url=publish_url)
        success(
            "Test results published to {} (HTTP {})".format(
                publish_url, result.get("status_code", "?")
            )
        )
    except Exception as exc:
        console_error(f"Failed to publish test results: {exc}")
        LOG.exception("publish_test_results_error")
