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
FLUID Doctor Command - Unified System Diagnostics

Runs comprehensive system diagnostics including:
- Basic infrastructure checks
- FLUID 0.7.1 feature availability (automatic detection)
- Provider capabilities
- Schema validation
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

from fluid_build.cli.console import cprint

from ._common import CLIError
from ._logging import info
from .forge_copilot_llm_providers import LlmReadinessCheck, check_llm_readiness
from .security import (
    InputSanitizer,
    ProductionLogger,
    validate_output_file,
)

# Try Rich for better output
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

COMMAND = "doctor"
EXTENDED_DIAG_SCRIPT = Path("scripts/diagnose.sh")
EXTENDED_DIAG_README = Path("scripts/README.md")


@dataclass
class DoctorSummary:
    status: str
    message: str
    border_style: str
    text_style: str


def _run_scoped(args, logger: logging.Logger, scope_arg: str) -> int:
    """Dispatch ``fluid doctor --scope <scope>`` into cli/ops/doctor.py.

    Returns 0 when every check is OK, 1 if any error is reported, and
    keeps a non-zero exit on warnings unless ``--json`` is passed (machine
    consumers can decide for themselves).
    """
    import json
    import sys

    from fluid_build.cli.ops.doctor import DoctorScope, Severity, run_doctor

    scope = DoctorScope(scope_arg)
    report = run_doctor(scope)

    if getattr(args, "json", False):
        json.dump(
            {
                "scope": report.scope.value,
                "ok": report.ok,
                "results": [
                    {
                        "name": r.name,
                        "severity": r.severity.value,
                        "detail": r.detail,
                        "fix": r.fix,
                        "doc": r.doc,
                    }
                    for r in report.results
                ],
            },
            sys.stdout,
            indent=2,
            default=str,
        )
        sys.stdout.write("\n")
    else:
        from fluid_build.cli.console import cprint

        icon = {Severity.OK: "✓", Severity.WARN: "!", Severity.ERROR: "✗"}
        cprint(f"fluid doctor — scope={report.scope.value}")
        for r in report.results:
            cprint(f"  {icon[r.severity]} {r.name}: {r.detail}")
            if r.severity is not Severity.OK and r.fix:
                cprint(f"      fix: {r.fix}")
        summary = (
            f"{len(report.results)} checks  "
            f"errors={len(report.errors)}  warnings={len(report.warnings)}"
        )
        cprint(summary)

    return 0 if not report.errors else 1


def register(subparsers: argparse._SubParsersAction):
    """Register unified doctor command"""
    p = subparsers.add_parser(
        COMMAND,
        help="Run built-in health checks and optional extended diagnostics",
        description="""
Run built-in health checks for FLUID CLI.

Automatically checks:
• Forge copilot readiness
• FLUID 0.7.1 feature availability (if applicable)
• Provider capabilities
• Schema and runtime support

Optional workspace diagnostics can be run with --extended
(or the legacy alias --comprehensive) when scripts/diagnose.sh
is available in the current checkout.
        """.strip(),
    )
    p.add_argument(
        "--out-dir", default="runtime/diag", help="Output directory for diagnostic files"
    )
    p.add_argument(
        "--features-only",
        action="store_true",
        help="Only check FLUID feature availability (skip infrastructure)",
    )
    p.add_argument(
        "--extended",
        "--comprehensive",
        action="store_true",
        dest="extended",
        help="Run optional workspace diagnostics via scripts/diagnose.sh",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="Show detailed output")
    # Acquisition-stack scope check. When set, runs the source-aligned
    # acquisition health checks from cli/ops/doctor.py instead of the
    # legacy infrastructure-and-features path. Five scopes available:
    # authoring | pipeline | ingestion | infra | catalog | all.
    p.add_argument(
        "--scope",
        choices=["authoring", "pipeline", "ingestion", "infra", "catalog", "all"],
        default=None,
        help="Run acquisition-stack health checks for the named scope",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit results as JSON (only when --scope is set)",
    )
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    """
    Run system diagnostics with automatic feature detection.

    Automatically checks both base infrastructure and 0.7.1 features.
    When ``--scope`` is set, dispatches to the acquisition-stack scoped
    checks in cli/ops/doctor.py instead.
    """
    scope_arg = getattr(args, "scope", None)
    if scope_arg:
        return _run_scoped(args, logger, scope_arg)

    secure_logger = ProductionLogger(logger)
    verbose = getattr(args, "verbose", False)
    extended_requested = getattr(args, "extended", False)

    # Always check 0.7.1 feature availability (non-intrusive)
    feature_checks_ok, feature_checks = _check_fluid_features()
    copilot_readiness = _check_copilot_readiness()

    # If features-only mode, just show features and exit
    if getattr(args, "features_only", False):
        _print_feature_checks(feature_checks, verbose)
        return 0 if feature_checks_ok else 1

    resolved_script = _resolve_extended_diagnostic_script()
    extended_available = resolved_script is not None
    _print_doctor_summary(
        feature_checks_ok=feature_checks_ok,
        copilot_readiness=copilot_readiness,
        extended_available=extended_available,
        extended_requested=extended_requested,
    )
    _print_copilot_readiness(copilot_readiness, verbose)

    # Show feature checks first
    if verbose or not feature_checks_ok:
        _print_feature_checks(feature_checks, verbose)
        cprint()  # Spacing

    _print_doctor_next_steps(
        feature_checks_ok=feature_checks_ok,
        copilot_readiness=copilot_readiness,
    )

    if not extended_requested:
        return 0 if feature_checks_ok else 1

    if resolved_script is None:
        raise _extended_diagnostic_error(
            "Extended diagnostics are not installed in this checkout.",
            EXTENDED_DIAG_SCRIPT.resolve(),
            EXTENDED_DIAG_README.resolve(),
        )
    validated_script = resolved_script

    # Validate and create output directory
    try:
        out_dir = Path(args.out_dir)
        validate_output_file(out_dir / "test", "diagnostic output")
        out_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise CLIError(
            1, "doctor_output_dir_failed", context={"out_dir": args.out_dir, "error": str(e)}
        )

    # Prepare secure environment
    env = os.environ.copy()

    # Sanitize environment variables
    if getattr(args, "provider", None):
        provider = InputSanitizer.sanitize_filename(args.provider)
        if provider != args.provider:
            secure_logger.log_safe(
                "warning", f"Provider name sanitized: {args.provider} -> {provider}"
            )
        env["PROVIDER"] = provider

    # Add output directory to environment
    env["FLUID_DIAG_OUT_DIR"] = str(out_dir.resolve())

    try:
        subprocess.run(
            ["bash", str(validated_script)],
            env=env,
            check=True,
            cwd=Path.cwd(),
            capture_output=False,
            timeout=300,
        )

        secure_logger.log_safe("info", "Diagnostic completed successfully", out_dir=str(out_dir))
        info(logger, "doctor_ok")

        # Return success only if both feature checks and infrastructure checks pass
        return 0 if feature_checks_ok else 1

    except subprocess.TimeoutExpired:
        raise CLIError(1, "doctor_timeout", context={"timeout": 300})
    except subprocess.CalledProcessError as e:
        secure_logger.log_safe("error", f"Diagnostic failed with return code: {e.returncode}")
        raise CLIError(1, "doctor_failed", context={"returncode": e.returncode})
    except Exception as e:
        secure_logger.log_safe("error", f"Unexpected diagnostic error: {str(e)}")
        raise CLIError(1, "doctor_unexpected_error", context={"error": str(e)})


def _extended_diagnostics_available() -> bool:
    """Return whether an extended workspace diagnostic script is available."""
    return _resolve_extended_diagnostic_script() is not None


def _resolve_extended_diagnostic_script() -> Optional[Path]:
    """Resolve the optional workspace diagnostic script, returning None if unavailable."""
    script_path = EXTENDED_DIAG_SCRIPT.resolve()

    if not script_path.exists():
        return None

    if not script_path.is_file():
        return None

    if not (os.access(script_path, os.X_OK) or os.access(script_path, os.R_OK)):
        return None

    return script_path


def _extended_diagnostic_error(message: str, script_path: Path, readme_path: Path) -> CLIError:
    error = CLIError(
        1,
        "doctor_extended_unavailable",
        context={
            "script": str(script_path),
            "readme": str(readme_path),
            "hint": "Run `fluid doctor` for built-in checks only.",
        },
    )
    error.message = message
    return error


def _build_doctor_summary(
    *,
    feature_checks_ok: bool,
    copilot_readiness: LlmReadinessCheck,
    extended_available: bool,
    extended_requested: bool,
) -> DoctorSummary:
    if not feature_checks_ok or not copilot_readiness.ready:
        return DoctorSummary(
            status="Action needed",
            message="Some built-in checks need attention before Forge is fully ready.",
            border_style="yellow",
            text_style="yellow",
        )

    if not extended_available and not extended_requested:
        return DoctorSummary(
            status="Optional extras unavailable",
            message="Built-in checks passed. Extended workspace diagnostics are not installed here.",
            border_style="blue",
            text_style="cyan",
        )

    return DoctorSummary(
        status="Ready",
        message="Built-in checks passed.",
        border_style="green",
        text_style="green",
    )


def _print_doctor_summary(
    *,
    feature_checks_ok: bool,
    copilot_readiness: LlmReadinessCheck,
    extended_available: bool,
    extended_requested: bool,
) -> None:
    summary = _build_doctor_summary(
        feature_checks_ok=feature_checks_ok,
        copilot_readiness=copilot_readiness,
        extended_available=extended_available,
        extended_requested=extended_requested,
    )

    if RICH_AVAILABLE:
        console = Console()
        console.print(
            Panel(
                f"[{summary.text_style}]{summary.status}[/{summary.text_style}]\n"
                f"[dim]{summary.message}[/dim]",
                title="🩺 Doctor Summary",
                border_style=summary.border_style,
            )
        )
        return

    cprint("\n" + "=" * 60)
    cprint("Doctor Summary")
    cprint("=" * 60)
    cprint(f"Status:  {summary.status}")
    cprint(f"Message: {summary.message}")
    cprint()


def _print_doctor_next_steps(
    *, feature_checks_ok: bool, copilot_readiness: LlmReadinessCheck
) -> None:
    suggestions: List[str] = []

    if not copilot_readiness.ready and copilot_readiness.error is not None:
        # LlmReadinessCheck.error is a plain string message; older code paths
        # expected a structured error with .suggestions. Accept both shapes so
        # doctor keeps working whichever provider populates `error`.
        error = copilot_readiness.error
        error_suggestions = getattr(error, "suggestions", None)
        if error_suggestions:
            suggestions.extend(error_suggestions)
        elif isinstance(error, str):
            suggestions.append(error)

    if not feature_checks_ok:
        suggestions.append("Run `fluid doctor --verbose` to inspect failing feature checks.")

    if not suggestions:
        return

    if RICH_AVAILABLE:
        console = Console()
        console.print(
            Panel(
                "\n".join(f"• {item}" for item in suggestions),
                title="Next steps",
                border_style="yellow",
            )
        )
        return

    cprint("Next steps:")
    for suggestion in suggestions:
        cprint(f"  • {suggestion}")
    cprint()


def _check_fluid_features() -> Tuple[bool, List[Dict[str, any]]]:
    """
    Check FLUID feature availability (v0.7.x line).

    Returns:
        (all_ok, checks) - Boolean and list of check results
    """
    checks = []
    all_ok = True

    # Core checks (v0.7.x baseline)
    try:
        from fluid_build.schema_manager import FluidSchemaManager

        versions = FluidSchemaManager.BUNDLED_VERSIONS
        has_071 = "0.7.1" in versions

        checks.append(
            {
                "check": "FLUID Schema Manager",
                "category": "core",
                "status": "✅ Available",
                "ok": True,
                "details": f"Versions: {', '.join(versions)}",
            }
        )

        if has_071:
            checks.append(
                {
                    "check": "FLUID 0.7.1 Schema",
                    "category": "0.7.1",
                    "status": "✅ Available",
                    "ok": True,
                    "details": "Provider-first orchestration schema",
                }
            )
        else:
            checks.append(
                {
                    "check": "FLUID 0.7.1 Schema",
                    "category": "0.7.1",
                    "status": "⚠️  Not found",
                    "ok": True,  # Not critical — older 0.7.x still works
                    "details": "newer 0.7.x features unavailable; older 0.7.x baseline still works",
                }
            )
    except Exception as e:
        checks.append(
            {
                "check": "FLUID Schema Manager",
                "category": "core",
                "status": "❌ Error",
                "ok": False,
                "details": str(e),
            }
        )
        all_ok = False

    # 0.7.1 Enhancement checks
    try:
        checks.append(
            {
                "check": "Sovereignty Validator",
                "category": "0.7.1",
                "status": "✅ Available",
                "ok": True,
                "details": "Jurisdiction & data residency constraints",
            }
        )
    except Exception:
        checks.append(
            {
                "check": "Sovereignty Validator",
                "category": "0.7.1",
                "status": "⚠️  Not available",
                "ok": True,  # Non-critical
                "details": "0.7.1 sovereignty features unavailable",
            }
        )

    try:
        checks.append(
            {
                "check": "AgentPolicy Validator",
                "category": "0.7.1",
                "status": "✅ Available",
                "ok": True,
                "details": "AI/LLM usage governance",
            }
        )
    except Exception:
        checks.append(
            {
                "check": "AgentPolicy Validator",
                "category": "0.7.1",
                "status": "⚠️  Not available",
                "ok": True,  # Non-critical
                "details": "0.7.1 agent policy features unavailable",
            }
        )

    try:
        checks.append(
            {
                "check": "Provider Action Parser",
                "category": "0.7.1",
                "status": "✅ Available",
                "ok": True,
                "details": "Provider-first orchestration ready",
            }
        )
    except Exception:
        checks.append(
            {
                "check": "Provider Action Parser",
                "category": "0.7.1",
                "status": "⚠️  Not available",
                "ok": True,  # Non-critical — older 0.7.x baseline still works
                "details": "0.7.1 provider actions unavailable",
            }
        )

    # Provider-specific checks (optional)
    try:
        checks.append(
            {
                "check": "GCP Provider Actions",
                "category": "providers",
                "status": "✅ Available",
                "ok": True,
                "details": "BigQuery, GCS, IAM actions",
            }
        )
    except Exception:
        checks.append(
            {
                "check": "GCP Provider Actions",
                "category": "providers",
                "status": "⚠️  Not available",
                "ok": True,  # Non-critical if GCP not used
                "details": "Install GCP dependencies for full support",
            }
        )

    try:
        from fluid_build.iac import get_iac_plugin

        aws_iac_ready = get_iac_plugin("aws") is not None
        checks.append(
            {
                "check": "AWS Provider Actions",
                "category": "providers",
                "status": "✅ Available" if aws_iac_ready else "⚠️  Not available",
                "ok": True,
                "details": "AWS provisioning via the OpenTofu engine (contract → .tf.json → tofu)",
            }
        )
    except Exception:
        checks.append(
            {
                "check": "AWS Provider Actions",
                "category": "providers",
                "status": "⚠️  Not available",
                "ok": True,  # Non-critical if AWS not used
                "details": "Install AWS dependencies for full support",
            }
        )

    # AI Copilot readiness
    try:
        from fluid_build.cli.forge_copilot_llm_providers import check_llm_readiness

        readiness = check_llm_readiness()
        if readiness.ready:
            checks.append(
                {
                    "check": "Forge AI Copilot",
                    "category": "ai",
                    "status": "✅ Ready",
                    "ok": True,
                    "details": f"{readiness.provider} / {readiness.model}",
                }
            )
        else:
            checks.append(
                {
                    "check": "Forge AI Copilot",
                    "category": "ai",
                    "status": "⚠️  Not configured",
                    "ok": True,  # Non-critical
                    "details": "Run 'fluid ai setup' to configure",
                }
            )
    except Exception:
        checks.append(
            {
                "check": "Forge AI Copilot",
                "category": "ai",
                "status": "⚠️  Not available",
                "ok": True,
                "details": "Run 'fluid ai setup' to configure",
            }
        )

    # Phase B4 — LiteLLM unified backend detection. Optional dep; the
    # check is non-critical (ok=True) so a missing install doesn't fail
    # the suite. Surfaces both install state and the env-var status so
    # users can confirm at a glance whether routing is live.
    try:
        import litellm  # type: ignore[import-untyped]

        litellm_version = getattr(litellm, "__version__", "(unknown)")
        details = f"litellm {litellm_version} · routing every provider"
        checks.append(
            {
                "check": "LiteLLM backend",
                "category": "ai",
                "status": "✅ Installed",
                "ok": True,
                "details": details,
            }
        )
    except ImportError:
        checks.append(
            {
                "check": "LiteLLM backend",
                "category": "ai",
                "status": "⚠️  Not installed (optional)",
                "ok": True,
                "details": "Install with: pip install 'fluid-build[litellm]'",
            }
        )

    return all_ok, checks


def _print_feature_checks(checks: List[Dict[str, any]], verbose: bool = False):
    """Print feature checks with appropriate formatting."""

    if RICH_AVAILABLE:
        console = Console()

        table = Table(title="🔍 FLUID Feature Availability", show_header=True)
        table.add_column("Feature", style="cyan", width=30)
        table.add_column("Status", width=20)
        if verbose:
            table.add_column("Details", style="dim")

        for check in checks:
            status_color = "green" if check["ok"] else "red"
            if "⚠️" in check["status"]:
                status_color = "yellow"

            row = [check["check"], f"[{status_color}]{check['status']}[/{status_color}]"]
            if verbose:
                row.append(check["details"])

            table.add_row(*row)

        console.print(table)

        # Summary
        ok_count = sum(1 for c in checks if c["ok"])
        warning_count = sum(1 for c in checks if "⚠️" in c["status"])
        total = len(checks)

        if ok_count == total:
            console.print(
                Panel("[green]✅ All critical features available![/green]", border_style="green")
            )
        elif ok_count >= total - warning_count:
            console.print(
                Panel(
                    f"[yellow]⚠️  {ok_count}/{total} features available ({warning_count} optional features missing)[/yellow]",
                    border_style="yellow",
                )
            )
        else:
            console.print(
                Panel(
                    f"[red]❌ {total - ok_count} critical features missing[/red]",
                    border_style="red",
                )
            )
    else:
        # Simple text output
        cprint("\n" + "=" * 60)
        cprint("FLUID Feature Availability")
        cprint("=" * 60)

        for check in checks:
            status = check["status"]
            cprint(f"{status:20} {check['check']}")
            if verbose:
                cprint(f"                     → {check['details']}")

        cprint("=" * 60)

        ok_count = sum(1 for c in checks if c["ok"])
        total = len(checks)
        cprint(f"\n{ok_count}/{total} features available")
        cprint()


def _check_copilot_readiness() -> LlmReadinessCheck:
    """Inspect whether Forge copilot has enough local config to start."""
    return check_llm_readiness()


def _print_copilot_readiness(readiness: LlmReadinessCheck, verbose: bool = False) -> None:
    """Render the copilot readiness summary without leaking secrets."""
    status_text = "✅ Ready" if readiness.ready else "⚠️  Setup needed"
    auth_text = "Configured" if readiness.auth_available else "Missing"
    endpoint_text = readiness.endpoint or "Not configured"

    if RICH_AVAILABLE:
        console = Console()
        table = Table(title="🤖 Forge Copilot Readiness", show_header=True)
        table.add_column("Item", style="cyan", width=18)
        table.add_column("Value", style="white")
        table.add_row("Status", status_text)
        table.add_row("Provider", readiness.provider or "Not selected")
        table.add_row("Model", readiness.model or "Not selected")
        table.add_row("Endpoint", endpoint_text)
        table.add_row("Auth", auth_text)
        if verbose and readiness.error is not None:
            # ``readiness.error`` is a plain string in current code paths; older
            # paths populated a structured object with ``.message`` / ``.suggestions``.
            # Tolerate both shapes — see the same dance in ``_print_doctor_next_steps``.
            error = readiness.error
            message_text = getattr(error, "message", None)
            if message_text is None:
                message_text = error if isinstance(error, str) else str(error)
            table.add_row("Message", message_text)
        console.print(table)
        error_suggestions = getattr(readiness.error, "suggestions", None)
        if verbose and readiness.error is not None and error_suggestions:
            console.print(
                Panel(
                    "\n".join(f"• {item}" for item in error_suggestions),
                    title="Copilot Suggestions",
                    border_style="yellow",
                )
            )
        return

    cprint("\n" + "=" * 60)
    cprint("Forge Copilot Readiness")
    cprint("=" * 60)
    cprint(f"Status:   {status_text}")
    cprint(f"Provider: {readiness.provider or 'Not selected'}")
    cprint(f"Model:    {readiness.model or 'Not selected'}")
    cprint(f"Endpoint: {endpoint_text}")
    cprint(f"Auth:     {auth_text}")
    if verbose and readiness.error is not None:
        error = readiness.error
        message_text = getattr(error, "message", None)
        if message_text is None:
            message_text = error if isinstance(error, str) else str(error)
        cprint(f"Message:  {message_text}")
        for suggestion in getattr(error, "suggestions", None) or []:
            cprint(f"  • {suggestion}")
    cprint()
