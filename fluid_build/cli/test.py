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

from fluid_build.severity import is_error as _is_error_severity
from fluid_build.severity import is_warning as _is_warning_severity

LOG = logging.getLogger("fluid.cli.test")
COMMAND = "test"

# Row outcomes for the results table. ``SKIP`` exists because a check
# that never ran must not render as a green tick — ``--no-data`` used to
# print "✅ Resource exists" for a table that does not exist.
_PASS, _WARN, _FAIL, _SKIP = "pass", "warn", "fail", "skip"

_ROW_ICON = {
    _PASS: "[green]✅[/green]",
    _WARN: "[yellow]⚠️[/yellow]",
    _FAIL: "[red]❌[/red]",
    _SKIP: "[dim]—[/dim]",
}


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

    # --- quality engine ---
    p.add_argument(
        "--engine",
        choices=["native", "soda"],
        default="native",
        help=(
            "Quality-check engine. 'native' (default) runs forge's built-in "
            "quality gates. 'soda' generates SodaCL from the contract quality "
            "block and shells out to a locally-installed `soda scan`."
        ),
    )
    p.add_argument(
        "--datasource",
        help=(
            "Soda data-source name (required when --engine soda is set). "
            "Must match a datasource configured in your Soda configuration.yml."
        ),
    )
    p.add_argument(
        "--soda-config",
        help="Path to Soda configuration.yml (defaults to Soda's auto-discovery)",
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

    # Soda engine dispatch — generates SodaCL from the contract's quality
    # block and shells out to the user's installed `soda` binary. Skipped
    # when --engine native (the default).
    if getattr(args, "engine", "native") == "soda":
        return _run_soda_engine(args, contract_path, logger)

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

    console = Console(file=open(output_file, "w", encoding="utf-8") if output_file else sys.stdout)

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
    rows = _build_check_rows(report)

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=4)
    table.add_column("Result", width=6)
    table.add_column("Check", min_width=22)
    table.add_column("Details", overflow="fold")

    for idx, (status, name, detail) in enumerate(rows, 1):
        table.add_row(str(idx), _ROW_ICON[status], name, detail)

    console.print(table)

    # ── summary line ──
    # Counted from what the table actually shows. The old footer printed
    # "N check(s) passed" for every row that was not a hard error, so a
    # run whose CRITICAL data-quality rule failed still announced
    # "✅ 8 check(s) passed".
    n_passed = sum(1 for status, _, _ in rows if status == _PASS)
    n_warned = sum(1 for status, _, _ in rows if status == _WARN)
    n_failed = sum(1 for status, _, _ in rows if status == _FAIL)
    n_skipped = sum(1 for status, _, _ in rows if status == _SKIP)

    total_errors = len(report.get_errors())
    total_warnings = len(report.get_warnings())

    parts = [f"{n_passed} passed"]
    if n_warned:
        parts.append(f"{n_warned} warned")
    if n_failed:
        parts.append(f"{n_failed} failed")
    if n_skipped:
        parts.append(f"{n_skipped} skipped")
    parts.append(f"{total_errors} error(s)")
    parts.append(f"{total_warnings} warning(s)")
    parts.append(f"{report.duration:.2f}s")
    body = "  |  ".join(parts)

    if passed:
        console.print(f"\n[bold green]\u2705 {body}[/bold green]")
    else:
        console.print(f"\n[bold red]\u274c {body}[/bold red]")

    if getattr(report, "checks", None):
        rules_passed = sum(1 for c in report.checks if c.passed)
        console.print(f"[dim]Data-quality rules: {rules_passed}/{len(report.checks)} passed[/dim]")

    if output_file:
        cprint(f"Report saved to: {output_file}")


def _build_check_rows(report) -> List[tuple]:
    """Build ``(status, name, details)`` rows for the results table.

    Split out of the renderer so each row's outcome is a value that can
    be counted (and unit-tested) rather than a decision made inline
    while printing.
    """
    issues = report.issues
    data_checked = getattr(report, "data_checks_performed", True)
    rows: List[tuple] = []

    def _errs(*categories: str) -> List:
        return [i for i in issues if i.category in categories and _is_error_severity(i.severity)]

    def _warns(*categories: str) -> List:
        return [i for i in issues if i.category in categories and _is_warning_severity(i.severity)]

    # Schema syntax
    schema_errors = _errs("schema")
    if schema_errors:
        rows.append((_FAIL, "Schema syntax", "; ".join(e.message for e in schema_errors)))
    else:
        rows.append((_PASS, "Schema syntax", "Valid"))

    # Provider connectivity
    # The connection probe runs in provider detection, so it happens
    # under --no-data too.
    conn_issues = [i for i in issues if i.category == "connection"]
    if conn_issues:
        rows.append((_FAIL, "Provider connection", "; ".join(e.message for e in conn_issues)))
    else:
        rows.append((_PASS, "Provider connection", "OK"))

    # Binding / platform
    bind_errors = _errs("binding")
    bind_warns = _warns("binding")
    if bind_errors:
        rows.append((_FAIL, "Binding configuration", "; ".join(e.message for e in bind_errors)))
    elif bind_warns:
        rows.append((_WARN, "Binding configuration", "; ".join(e.message for e in bind_warns)))
    else:
        rows.append((_PASS, "Binding configuration", "OK"))

    # Live-resource checks. Under --no-data none of these ran, so they
    # are reported as skipped instead of asserted to have passed — a
    # green "Resource exists" for a table nobody queried is a false claim.
    missing = [i for i in issues if i.category == "missing_resource"]
    if missing:
        rows.append((_FAIL, "Resource exists", "; ".join(e.message for e in missing)))
    elif not data_checked:
        rows.append((_SKIP, "Resource exists", "not checked (--no-data)"))
    else:
        rows.append(
            (
                _PASS,
                "Resource exists",
                f"{max(report.exposes_validated, 1)} exposed resource(s) found",
            )
        )

    field_cats = ("missing_field", "type_mismatch", "mode_mismatch", "extra_field")
    field_errors = _errs(*field_cats)
    field_warns = _warns(*field_cats)
    # Columns present in the table but absent from the contract are
    # reported at ``info`` — the contract's own fields all matched, so
    # this row is a pass. Counting them as "N warning(s)" contradicted
    # the footer, which (correctly) counted zero warnings.
    field_infos = [
        i
        for i in issues
        if i.category in field_cats
        and not _is_error_severity(i.severity)
        and not _is_warning_severity(i.severity)
    ]
    if field_errors:
        rows.append(
            (
                _FAIL,
                "Schema fields",
                "{n} error(s): {msgs}".format(
                    n=len(field_errors),
                    msgs="; ".join(e.message for e in field_errors[:3]),
                ),
            )
        )
    elif not data_checked:
        rows.append((_SKIP, "Schema fields", "not checked (--no-data)"))
    elif missing:
        # No table, no columns to compare — "All fields match" here would
        # assert a comparison that never happened.
        rows.append((_SKIP, "Schema fields", "not checked (resource missing)"))
    elif field_warns:
        rows.append((_WARN, "Schema fields", f"{len(field_warns)} warning(s)"))
    elif field_infos:
        rows.append(
            (
                _PASS,
                "Schema fields",
                f"All contract fields match ({len(field_infos)} extra column(s) in the table)",
            )
        )
    else:
        rows.append((_PASS, "Schema fields", "All fields match"))

    row_cats = ("empty_table", "row_count_below_threshold")
    row_errors = _errs(*row_cats)
    row_warns = _warns(*row_cats)
    if row_errors:
        rows.append((_FAIL, "Row count / SLA", "; ".join(e.message for e in row_errors)))
    elif row_warns:
        rows.append((_WARN, "Row count / SLA", "; ".join(e.message for e in row_warns)))
    elif not data_checked:
        rows.append((_SKIP, "Row count / SLA", "not checked (--no-data)"))
    elif missing:
        rows.append((_SKIP, "Row count / SLA", "not checked (resource missing)"))
    else:
        rows.append((_PASS, "Row count / SLA", "OK"))

    # Quality tests
    quality_issues = [i for i in issues if i.category == "quality"]
    q_errors = _errs("quality")
    q_warns = _warns("quality")
    q_infos = [
        i
        for i in quality_issues
        if not _is_error_severity(i.severity) and not _is_warning_severity(i.severity)
    ]
    executed = [c for c in getattr(report, "checks", []) if c.category == "quality"]
    if q_errors:
        detail = "; ".join(e.message for e in q_errors)
        if q_warns:
            # Don't let a hard failure hide the non-gating rules that
            # also failed — they are still failed rules.
            detail += f"  (+{len(q_warns)} non-gating rule failure(s))"
        rows.append((_FAIL, "Quality tests", detail))
    elif q_warns:
        rows.append((_WARN, "Quality tests", "; ".join(e.message for e in q_warns)))
    elif executed:
        rows.append((_PASS, "Quality tests", f"{len(executed)} rule(s) passed"))
    elif not data_checked:
        rows.append((_SKIP, "Quality tests", "not checked (--no-data)"))
    elif q_infos:
        rows.append((_SKIP, "Quality tests", "; ".join(i.message for i in q_infos)))
    else:
        rows.append((_SKIP, "Quality tests", "no data-quality rules declared"))

    # Metadata / governance
    meta_issues = [i for i in issues if i.category == "metadata"]
    m_errors = _errs("metadata")
    if m_errors:
        rows.append((_FAIL, "Metadata / governance", "; ".join(e.message for e in m_errors)))
    elif meta_issues:
        rows.append((_WARN, "Metadata / governance", f"{len(meta_issues)} info/warning(s)"))
    else:
        rows.append((_PASS, "Metadata / governance", "Complete"))

    # Drift (only rendered when drift detection actually reported)
    drift_issues = [i for i in issues if i.category == "drift"]
    if drift_issues:
        rows.append((_WARN, "Drift detection", "; ".join(e.message for e in drift_issues)))

    return rows


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
        with open(output_file, "w", encoding="utf-8") as f:
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
        # Per-check outcomes — the passing checks too. ``issues`` only
        # ever describes failures, so a consumer reading it alone cannot
        # tell "ran and passed" from "never ran".
        "checks": [
            {
                "name": c.name,
                "category": c.category,
                "passed": c.passed,
                "severity": c.severity,
                "message": c.message,
                "expected": c.expected,
                "actual": c.actual,
            }
            for c in getattr(report, "checks", [])
        ],
    }
    text = json.dumps(data, indent=2)
    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(text)
        cprint(f"Report saved to: {output_file}")
    else:
        # Print raw so it's pipe-friendly
        sys.stdout.write(text + "\n")


# ======================================================================
# Output – JUnit XML
# ======================================================================


def _output_junit(report, output_file: Optional[str] = None) -> None:
    """Emit JUnit XML for CI/CD systems (Jenkins, GitHub Actions, etc.).

    Granularity matters here: folding every failing data-quality rule
    into a single ``<testcase name="quality">`` (and omitting the
    passing rules entirely) meant a CI dashboard showed one red test
    however many rules failed, and no evidence at all of the rules that
    passed. Each executed DQ rule now gets its own test case.
    """
    ts = ET.Element("testsuite")
    ts.set("name", f"fluid-test:{report.contract_id}")
    ts.set("errors", "0")
    ts.set("time", f"{report.duration:.3f}")
    ts.set("timestamp", report.validation_time.isoformat())

    classname = f"fluid.test.{report.contract_id}"
    total = 0
    failures = 0

    # ── one testcase per executed data-quality rule ──
    rule_checks = [c for c in getattr(report, "checks", []) if c.category == "quality"]
    for check in rule_checks:
        total += 1
        tc = ET.SubElement(ts, "testcase")
        tc.set("classname", f"{classname}.quality")
        tc.set("name", check.name)
        tc.set("time", "0.000")
        if check.passed:
            continue
        failures += 1
        fail = ET.SubElement(tc, "failure")
        fail.set("message", check.message)
        fail.set("type", f"DataQualityFailure[{check.severity}]")
        body = [f"[{check.severity.upper()}] {check.message}"]
        if check.expected is not None:
            body.append(f"  expected: {check.expected}")
        if check.actual is not None:
            body.append(f"  actual:   {check.actual}")
        fail.text = "\n".join(body)

    # ── one testcase per structural check category ──
    categories_seen: Dict[str, List] = {}
    for issue in report.issues:
        categories_seen.setdefault(issue.category, []).append(issue)

    structural_categories = [
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
    if rule_checks:
        # The per-rule cases above already carry every rule outcome; a
        # roll-up ``quality`` case on top would double-count them.
        structural_categories.remove("quality")

    extra_categories = [c for c in categories_seen if c not in structural_categories]
    if rule_checks and "quality" in extra_categories:
        extra_categories.remove("quality")

    for cat in structural_categories + extra_categories:
        total += 1
        tc = ET.SubElement(ts, "testcase")
        tc.set("classname", classname)
        tc.set("name", cat)
        tc.set("time", f"{report.duration / max(len(structural_categories), 1):.3f}")

        issues_in_cat = categories_seen.get(cat, [])
        errors_in_cat = [i for i in issues_in_cat if _is_error_severity(i.severity)]
        warns_in_cat = [i for i in issues_in_cat if _is_warning_severity(i.severity)]

        if errors_in_cat:
            failures += 1
            fail = ET.SubElement(tc, "failure")
            fail.set("message", "; ".join(i.message for i in errors_in_cat))
            fail.set("type", "AssertionError")
            body_lines = []
            for i in errors_in_cat:
                body_lines.append(f"[{i.severity.upper()}] {i.message}")
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

    ts.set("tests", str(total))
    ts.set("failures", str(failures))

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


# ======================================================================
# Soda engine
# ======================================================================


def _run_soda_engine(
    args: argparse.Namespace,
    contract_path: Path,
    logger: logging.Logger,
) -> int:
    """Render SodaCL from the contract and run ``soda scan``.

    Required user inputs:
        --datasource <name>   Soda datasource configured in their configuration.yml
        --soda-config <path>  Optional path to that configuration.yml

    The function:
      1. Loads the contract (with --env overlay if set).
      2. Renders SodaCL from exposes[].contract.dq.rules[].
      3. Writes the SodaCL to a temp file.
      4. Resolves the `soda` binary (env var → $PATH → fail loud).
      5. Shells out to `soda scan`.
      6. Parses results and prints a summary in the requested format.

    Returns 0 only when every declared rule was mapped to a SodaCL check
    *and* every check passed. Any rule the exporter could not express, and
    any scan whose outcome we cannot account for, exits 1 — a quality gate
    that did not run must never read as green.
    """
    import tempfile

    datasource = getattr(args, "datasource", None)
    if not datasource:
        console_error(
            "--datasource is required when --engine soda is set. "
            "Use the name of a datasource defined in your Soda configuration.yml."
        )
        return 1

    from ..build_runners.soda import SodaNotInstalled, resolve_soda_executable, run_soda_scan
    from ..exporters.sodacl import render_sodacl_document
    from ._common import load_contract_with_overlay

    try:
        contract = load_contract_with_overlay(
            str(contract_path), getattr(args, "env", None), logger
        )
    except Exception as exc:
        console_error(f"Failed to load contract: {exc}")
        return 1

    rendering = render_sodacl_document(contract)

    if rendering.declared == 0:
        # Genuinely nothing declared. Matches what `--engine native` does with
        # a rule-less contract; the message names the exact keys so "nothing
        # to check" is actionable rather than mysterious.
        warning(
            "No data-quality rules declared on any expose "
            "(looked in exposes[].contract.dq.rules[] and "
            "exposes[].contract.quality[]) — nothing to check via Soda."
        )
        return 0

    if rendering.unmapped:
        # Loud, itemised, and always fatal. Silently dropping a declared gate
        # is the failure this engine used to ship: it rendered an empty
        # document for every schema-valid contract and exited 0.
        _report_unmapped(rendering.unmapped)

    if not rendering.has_checks:
        console_error(
            f"None of the {rendering.declared} declared data-quality rule(s) "
            "could be expressed as SodaCL — no scan was run and nothing was "
            "checked."
        )
        return 1

    # Soda needs the YAML on disk — write to a temp file so the scan
    # invocation is self-contained.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sodacl.yml", delete=False, encoding="utf-8"
    ) as f:
        f.write(rendering.text)
        sodacl_path = f.name

    try:
        soda_bin = resolve_soda_executable()
    except SodaNotInstalled as exc:
        console_error(str(exc))
        os.unlink(sodacl_path)
        return 1

    try:
        result = run_soda_scan(
            sodacl_path,
            datasource=datasource,
            config_path=getattr(args, "soda_config", None),
            executable=soda_bin,
        )
    finally:
        # Always clean up the temp SodaCL file. The Soda binary has already
        # consumed it by this point.
        try:
            os.unlink(sodacl_path)
        except OSError:
            pass

    # We emitted N checks; if the runner could not account for a single one,
    # we do not know what happened and must not print PASS. `soda scan`
    # exiting 0 with unparseable stdout would otherwise render as
    # "PASS | passed: 0, failed: 0" — checks that never ran, reported green.
    accounted = (
        result.checks_passed
        + result.checks_failed
        + result.checks_warned
        + result.checks_not_evaluated
    )
    unaccounted = result.ok and accounted == 0
    if unaccounted:
        console_error(
            f"soda scan returned {result.return_code} but reported no check "
            f"outcomes for the {len(rendering.mapped)} check(s) we sent — "
            "refusing to report a pass we cannot substantiate. Run `soda scan "
            "-d <datasource> <file>` directly to see its output."
        )
        if result.raw_stderr:
            from fluid_build.observability.secret_redactor import redact_secret_text

            cprint(f"   soda stderr: {redact_secret_text(result.raw_stderr.strip()[:500])}")

    # Render in the requested format (re-use the existing --output flag).
    output_format = getattr(args, "output", "text")
    output_file = getattr(args, "output_file", None)

    if output_format == "json":
        _emit_soda_json(result, datasource, output_file, rendering)
    elif output_format == "junit":
        _emit_soda_junit(result, datasource, output_file, rendering)
    else:
        _emit_soda_text(result, datasource, rendering)

    if rendering.unmapped or unaccounted:
        return 1
    return 0 if result.ok else 1


def _report_unmapped(unmapped) -> None:
    """Print every declared-but-unrun rule, with the reason, to stderr."""
    console_error(
        f"{len(unmapped)} declared data-quality rule(s) have no SodaCL "
        "equivalent and were NOT checked:"
    )
    for rule in unmapped:
        console_error(f"   - {rule.describe()}")


def _emit_soda_json(result, datasource: str, output_file: Optional[str], rendering=None) -> None:
    """Write the soda result as the canonical JSON envelope.

    ``unmapped_rules`` is part of the envelope, not a terminal-only nicety:
    a consumer reading ``ok: true`` alone must still be able to see that
    rules were declared and never executed.
    """
    unmapped = list(getattr(rendering, "unmapped", []) or [])
    data = {
        "engine": "soda",
        "datasource": datasource,
        "return_code": result.return_code,
        "checks_passed": result.checks_passed,
        "checks_failed": result.checks_failed,
        "checks_warned": result.checks_warned,
        "checks_not_evaluated": result.checks_not_evaluated,
        "failed_check_names": result.failed_check_names,
        "rules_declared": getattr(rendering, "declared", None),
        "rules_mapped": list(getattr(rendering, "mapped", []) or []),
        "unmapped_rules": [
            {
                "expose": u.expose,
                "rule_id": u.rule_id,
                "rule_type": u.rule_type,
                "reason": u.reason,
            }
            for u in unmapped
        ],
        # A scan whose checks all passed is still not a pass overall when a
        # declared rule never ran.
        "ok": result.ok and not unmapped,
    }
    text = json.dumps(data, indent=2)
    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        cprint(f"Report saved to: {output_file}")
    else:
        sys.stdout.write(text + "\n")


def _emit_soda_junit(result, datasource: str, output_file: Optional[str], rendering=None) -> None:
    """Write the soda result as JUnit XML for CI/CD systems.

    One ``<testsuite>`` containing one ``<testcase>`` per check.
    Passed checks emit an empty case; failed checks emit a ``<failure>``
    with the check expression and any captured stderr (secret-redacted).

    Declared rules with no SodaCL equivalent get their own failing test case
    — a CI dashboard must not show all-green for a gate that never ran.
    """
    from fluid_build.observability.secret_redactor import redact_secret_text

    unmapped = list(getattr(rendering, "unmapped", []) or [])
    total = (
        result.checks_passed
        + result.checks_failed
        + result.checks_warned
        + result.checks_not_evaluated
        + len(unmapped)
    )
    ts = ET.Element("testsuite")
    ts.set("name", f"fluid-test-soda:{datasource}")
    ts.set("tests", str(total))
    ts.set("failures", str(result.checks_failed + len(unmapped)))
    ts.set("errors", "0")
    # Soda's "not evaluated" is a check that never ran; JUnit's closest
    # honest label is "skipped", not a silent omission from the suite.
    ts.set("skipped", str(result.checks_not_evaluated))

    for rule in unmapped:
        tc = ET.SubElement(ts, "testcase")
        tc.set("classname", f"soda.{datasource}.unmapped")
        tc.set("name", f"{rule.expose}.{rule.rule_id}")
        fail = ET.SubElement(tc, "failure")
        fail.set("type", "UnmappedQualityRule")
        fail.set("message", f"rule not executed: {rule.reason}")
        fail.text = rule.describe()

    # Emit a passed test case for the suite overall when we have no
    # per-check granularity from Soda's stdout (older Soda versions don't
    # surface individual passed-check names).
    if result.checks_passed and not result.failed_check_names:
        tc = ET.SubElement(ts, "testcase")
        tc.set("classname", f"soda.{datasource}")
        tc.set("name", f"{result.checks_passed} check(s) passed")

    for name in result.failed_check_names:
        tc = ET.SubElement(ts, "testcase")
        tc.set("classname", f"soda.{datasource}")
        tc.set("name", name)
        fail = ET.SubElement(tc, "failure")
        fail.set("type", "SodaCheckFailure")
        fail.set("message", name)
        if result.raw_stderr:
            fail.text = redact_secret_text(result.raw_stderr.strip()[:2000])

    if not result.ok and not result.failed_check_names:
        # The runner couldn't pull per-check names but the suite failed —
        # emit a single failure case so CI dashboards still surface it.
        tc = ET.SubElement(ts, "testcase")
        tc.set("classname", f"soda.{datasource}")
        tc.set("name", "soda scan failed")
        fail = ET.SubElement(tc, "failure")
        fail.set("type", "SodaScanFailure")
        fail.set("message", f"return_code={result.return_code}")
        if result.raw_stderr:
            fail.text = redact_secret_text(result.raw_stderr.strip()[:2000])

    if output_file:
        ET.ElementTree(ts).write(output_file, encoding="unicode", xml_declaration=True)
        cprint(f"JUnit XML saved to: {output_file}")
    else:
        ET.indent(ts)
        sys.stdout.write('<?xml version="1.0" ?>\n' + ET.tostring(ts, encoding="unicode") + "\n")


def _emit_soda_text(result, datasource: str, rendering=None) -> None:
    """Human-readable terminal output for the soda engine."""
    unmapped = list(getattr(rendering, "unmapped", []) or [])
    # An all-green scan is still not a PASS when a declared rule never ran.
    icon = "PASS" if (result.ok and not unmapped) else "FAIL"
    cprint(f"{icon}  fluid test --engine soda  |  datasource: {datasource}")
    counts = (
        f"   passed: {result.checks_passed}, "
        f"failed: {result.checks_failed}, "
        f"warned: {result.checks_warned}"
    )
    if result.checks_not_evaluated:
        counts += f", not evaluated by soda: {result.checks_not_evaluated}"
    if unmapped:
        counts += f", not executed: {len(unmapped)}"
    cprint(counts)
    if result.failed_check_names:
        cprint("   failed checks:")
        for name in result.failed_check_names:
            cprint(f"     - {name}")
    if not result.ok and result.raw_stderr:
        # Soda's stderr can include credential-bearing connection
        # strings (DSNs with embedded passwords, BigQuery service-
        # account JSON). Route through the project-wide redactor
        # before showing it to the operator.
        from fluid_build.observability.secret_redactor import redact_secret_text

        redacted = redact_secret_text(result.raw_stderr.strip()[:500])
        cprint(f"   soda stderr: {redacted}")
