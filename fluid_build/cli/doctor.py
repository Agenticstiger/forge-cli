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
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from fluid_build.cli.console import cprint

from ._common import CLIError
from ._logging import info
from .security import (
    InputSanitizer,
    ProductionLogger,
    validate_output_file,
)

if TYPE_CHECKING:  # resolve annotation names for ruff/type-checkers only
    from .forge_copilot_llm_providers import LlmReadinessCheck

# NOTE: ``forge_copilot_llm_providers`` pulls in ``httpx`` (a heavy dependency
# via the AI runtime). ``check_llm_readiness`` is imported lazily at its use
# sites below so it stays off the ``fluid --help`` / ``build_parser()`` cold
# path — ``register`` only needs argparse. Annotations referencing
# ``LlmReadinessCheck`` are safe at module scope because
# ``from __future__ import annotations`` keeps them as lazy strings.

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


# H9 — curated kill-switch catalog surfaced via ``fluid doctor --env``.
#
# Borrow-before-build:
# - ``aws configure list`` → table of NAME / VALUE / TYPE / LOCATION
#   (https://docs.aws.amazon.com/cli/latest/reference/configure/list.html)
# - ``gcloud config list`` → table of property / value
# - ``gh config list`` → property / value list of CLI-recognised settings
# - ``terraform env`` (now ``terraform workspace``) → curated set of
#   env-var knobs documented in
#   https://developer.hashicorp.com/terraform/cli/config/environment-variables
#
# Pattern adopted: one row per recognised FLUID_* runtime knob, columns
# = name / current value / default behavior / one-line description. The
# value column reads from ``os.environ`` at call time. Unset values
# render as ``(unset)`` so the user can tell at a glance which knobs are
# active in the current shell.
#
# Listed knobs are the operator-facing kill switches (UX audit H9
# called out 8 originally; we ship a small superset for cost caps /
# backend selectors that are commonly asked about).
#
# Entry shape: ``(env_var, default_behavior, description)``.
ENV_KILL_SWITCHES: List[Tuple[str, str, str]] = [
    # — Forge UX kill switches (the 8 UX audit H9 explicitly called out) —
    (
        "FLUID_FORGE_NO_PICKER",
        "picker shown on TTY",
        "Skip the 5-mode picker on bare `fluid forge`",
    ),
    (
        "FLUID_FORGE_NO_PREVIEW",
        "pre-write preview shown",
        "Suppress the pre-write preview panel + confirm prompt",
    ),
    (
        "FLUID_FORGE_NO_WELCOME",
        "welcome scan rendered",
        "Suppress the welcome scan panel",
    ),
    (
        "FLUID_FORGE_NO_STREAMING_PREVIEW",
        "streaming preview on",
        "Disable the live contract-growth panel during the interview",
    ),
    (
        "FLUID_FORGE_OFFLINE",
        "online (LLM path available)",
        "Set =1 to force the local no-network guided interview (like `--offline`)",
    ),
    (
        "FLUID_COPILOT_JUDGE",
        "judge stage runs (=1)",
        "Set =0 to skip the post-forge LLM judge stage",
    ),
    (
        "FLUID_COPILOT_ENRICHMENT",
        "enrichment stage runs (=1)",
        "Set =0 to skip the post-forge LLM enrichment stage",
    ),
    (
        "FLUID_JUDGE_SELF_CRITIQUE",
        "judge self-critique on (=1)",
        "Set =0 to disable the judge's self-critique pass",
    ),
    (
        "FLUID_COPILOT_CHECKPOINT",
        "checkpointing on (file store)",
        "Set =0 to route checkpoints to the null store (no on-disk receipts)",
    ),
    # — High-traffic operational knobs surfaced for the same reason —
    (
        "FLUID_LLM_BACKEND",
        "native per-provider backends",
        "Set to `litellm` to route every LLM call through the unified backend",
    ),
    (
        "FLUID_COST_LIMIT_USD",
        "no global cap",
        "Per-run global LLM cost ceiling (USD)",
    ),
    (
        "FLUID_COST_LIMIT_USD_PER_RUN",
        "no per-run cap",
        "Per-run cost ceiling shown in the progress prefix (USD)",
    ),
    (
        "FLUID_COST_LIMIT_USD_PER_PRODUCT",
        "no per-product cap",
        "Per-product cost ceiling, enforced inside the agent coordinator (USD)",
    ),
    (
        "FLUID_INTERVIEW_LEGACY",
        "world-class interview",
        "Set =1 to revert to the legacy bootstrap interview",
    ),
    (
        "FLUID_RUN_ID",
        "auto-generated per run",
        "Pre-seed or override the cross-stage run-id",
    ),
    (
        "FLUID_TOFU_TIMEOUT_SECONDS",
        "1800s default",
        "Per-`tofu` invocation wall-clock cap",
    ),
    # — Keyless coding-agent providers (claude-code / codex / cursor / kiro) —
    (
        "FLUID_FORGE_AGENT",
        "no agent preselected",
        "Select a keyless coding-agent provider (claude-code/codex/cursor/kiro)",
    ),
    (
        "FLUID_FORGE_AGENT_MODE",
        "envelope",
        "Coding-agent drive mode: `envelope` (default) or `agentic`",
    ),
    (
        "FLUID_FORGE_AGENT_TIMEOUT_SECONDS",
        "LLM timeout (120s)",
        "Per-agent-CLI invocation wall-clock cap",
    ),
    (
        "FLUID_FORGE_AGENT_CWD",
        "scratch tempdir",
        "Working dir for the agent CLI (default avoids loading project CLAUDE.md/AGENTS.md)",
    ),
    # — Agent network-egress tools (opt-in, default OFF) —
    (
        "FLUID_AGENT_WEB_TOOLS",
        "web_search/web_fetch hidden",
        "Set =1 to expose the SSRF-safe web_search + web_fetch agent tools",
    ),
    # — Telemetry consent (opt-in, default OFF) —
    (
        "FLUID_TELEMETRY",
        "OFF (opt-in via ~/.fluid/config.yaml telemetry.enabled)",
        "Set =1 to enable anonymous UX telemetry, =0 to force off (overrides config)",
    ),
    (
        "DO_NOT_TRACK",
        "unset (telemetry OFF by default anyway)",
        "Universal kill switch — when set, all telemetry is disabled (wins over everything)",
    ),
]


def _resolve_env_state(name: str) -> Tuple[str, str]:
    """Return ``(value, source)`` for a kill switch.

    ``source`` is ``"env"`` when the operator set the value, ``"default"``
    when the variable is absent (in which case ``value`` is rendered as
    ``(unset)``). Mirrors the LOCATION column from ``aws configure list``.
    """
    raw = os.environ.get(name)
    if raw is None:
        return ("(unset)", "default")
    if raw == "":
        return ('""', "env")
    return (raw, "env")


def _telemetry_state_line() -> str:
    """One-line resolved telemetry state for the --env footer.

    Shows the *effective* decision after applying the full precedence
    ladder (DO_NOT_TRACK > FLUID_TELEMETRY > persisted config > default),
    which a per-env-var table can't convey on its own.
    """
    try:
        from fluid_build.cli._telemetry_consent import describe_state

        st = describe_state()
        effective = "ON" if st["enabled"] else "OFF"
        if st["do_not_track"]:
            why = "DO_NOT_TRACK set"
        elif st["env_override"] is not None:
            why = f"FLUID_TELEMETRY={st['env_override']!r}"
        elif st["persisted"] is not None:
            why = f"~/.fluid/config.yaml telemetry.enabled={st['persisted']}"
        else:
            why = "default (opt-in, not yet enabled)"
        return f"Telemetry: {effective} — {why}"
    except Exception:  # noqa: BLE001
        return "Telemetry: OFF — default (gate unavailable)"


def _run_env_listing(args, logger: logging.Logger) -> int:
    """Render the kill-switch catalog as a table or JSON.

    Output shape borrows from ``aws configure list``: NAME / VALUE /
    SOURCE / DESCRIPTION. Adds a DEFAULT column because FLUID's knobs
    are mostly boolean toggles where the "off" behavior is the more
    important piece of information to convey.
    """
    import json
    import sys

    rows: List[Dict[str, str]] = []
    for env_var, default_behavior, description in ENV_KILL_SWITCHES:
        value, source = _resolve_env_state(env_var)
        rows.append(
            {
                "name": env_var,
                "value": value,
                "source": source,
                "default": default_behavior,
                "description": description,
            }
        )

    if getattr(args, "json", False):
        try:
            from fluid_build.cli._telemetry_consent import describe_state

            telemetry_state = describe_state()
        except Exception:  # noqa: BLE001
            telemetry_state = {"enabled": False, "default": False}
        json.dump({"env": rows, "telemetry": telemetry_state}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if RICH_AVAILABLE:
        console = Console()
        table = Table(
            title="FLUID runtime kill switches",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Env var", style="cyan", no_wrap=True)
        table.add_column("Current value", style="white")
        table.add_column("Source", style="dim")
        table.add_column("Default behavior", style="green")
        table.add_column("Description")
        for row in rows:
            value_style = "yellow" if row["source"] == "env" else "dim"
            table.add_row(
                row["name"],
                f"[{value_style}]{row['value']}[/{value_style}]",
                row["source"],
                row["default"],
                row["description"],
            )
        console.print(table)
        console.print(
            "[dim]Tip: source values are 'env' when the operator set them, "
            "'default' when unset.[/dim]"
        )
        console.print(f"[bold]{_telemetry_state_line()}[/bold]")
        return 0

    # Plain-text fallback.
    cprint("FLUID runtime kill switches")
    cprint("=" * 60)
    for row in rows:
        cprint(f"  {row['name']}")
        cprint(f"    value:       {row['value']} ({row['source']})")
        cprint(f"    default:     {row['default']}")
        cprint(f"    description: {row['description']}")
        cprint()
    cprint(_telemetry_state_line())
    return 0


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

Use --env to see the recognised FLUID_* runtime kill switches
and their current values.
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
        help=(
            "Emit results as JSON. With --scope/--env the matching surface "
            "is serialised; otherwise emits the store-backend section "
            "(MEMORY-E2E-A finding #55)."
        ),
    )
    # H9 — list every recognised FLUID_* env-var kill switch with its
    # current value + default + one-line description. Borrows the table
    # shape from ``aws configure list`` (NAME / VALUE / TYPE / LOCATION):
    # see https://docs.aws.amazon.com/cli/latest/reference/configure/list.html
    # and ``gcloud config list`` / ``gh config list`` for the same idea.
    # The previous gap (UX audit finding H9): every kill switch was
    # documented only in CLAUDE.md and source docstrings, never on the
    # CLI itself.
    p.add_argument(
        "--env",
        action="store_true",
        help="List recognised FLUID_* runtime kill switches with current values + defaults",
    )
    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    """
    Run system diagnostics with automatic feature detection.

    Automatically checks both base infrastructure and 0.7.1 features.
    When ``--scope`` is set, dispatches to the acquisition-stack scoped
    checks in cli/ops/doctor.py instead.
    When ``--env`` is set, dispatches to the kill-switch catalog
    listing.
    """
    if getattr(args, "env", False):
        return _run_env_listing(args, logger)

    scope_arg = getattr(args, "scope", None)
    if scope_arg:
        return _run_scoped(args, logger, scope_arg)

    secure_logger = ProductionLogger(logger)
    verbose = getattr(args, "verbose", False)
    extended_requested = getattr(args, "extended", False)

    # Always check 0.7.1 feature availability (non-intrusive)
    feature_checks_ok, feature_checks = _check_fluid_features()
    copilot_readiness = _check_copilot_readiness()
    # MEMORY-E2E-A finding #55: surface the active memory-store backend
    # so operators can tell at a glance which one is wired (file /
    # sqlite / postgres / vector). Inspection is read-only, swallows
    # backend resolution errors, and never costs the happy path more
    # than a few ms.
    store_backend_status = _inspect_store_backend()

    # If features-only mode, just show features and exit
    if getattr(args, "features_only", False):
        _print_feature_checks(feature_checks, verbose)
        return 0 if feature_checks_ok else 1

    # --json on the default path emits the structured payload (currently
    # just the store backend section — the rest of the default output is
    # narrative and stays human-readable). Mirrors the --env / --scope
    # JSON contracts so machine consumers have ONE shape per surface.
    if getattr(args, "json", False):
        import json as _json
        import sys as _sys

        _json.dump(
            {"store_backend": store_backend_status},
            _sys.stdout,
            indent=2,
            default=str,
        )
        _sys.stdout.write("\n")
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
    _print_store_backend_status(store_backend_status, verbose)

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

    # Third-party LLM provider plugins (entry-point group fluid_build.llm_providers).
    # Reads entry-point NAMES only (never loads/executes plugin code) so a health
    # check can't be a side-channel to run a third party's __init__. Only surfaced
    # when at least one is installed, to avoid noise. Fail-safe: any error is
    # swallowed so doctor never crashes on a bad plugin.
    try:
        from fluid_build.plugin_manager import LLM_PROVIDER_GROUP_KEY, installed_plugins

        entries = installed_plugins(LLM_PROVIDER_GROUP_KEY).get(LLM_PROVIDER_GROUP_KEY, [])
        allowed = [e["name"] for e in entries if e.get("allowed")]
        blocked = len(entries) - len(allowed)
        if entries:
            detail = f"{', '.join(allowed) or '(all blocked)'} — use with --llm-provider"
            if blocked:
                detail += f"; {blocked} blocked by FLUID_PLUGINS_BLOCKLIST/ALLOWLIST"
            checks.append(
                {
                    "check": "LLM provider plugins",
                    "category": "ai",
                    "status": f"✅ {len(allowed)} installed",
                    "ok": True,
                    "details": detail,
                }
            )
    except Exception:  # noqa: BLE001 - never break doctor on plugin discovery
        pass

    # Agent web tools (web_search / web_fetch) — opt-in via
    # FLUID_AGENT_WEB_TOOLS. Non-critical; surfaces whether the two
    # network-egress tools are exposed and which search provider (if any)
    # is configured, so operators can confirm at a glance.
    try:
        from fluid_build.cli.forge_web_tools import _select_search_provider, is_enabled

        if is_enabled():
            provider = _select_search_provider(os.environ)
            search_state = (
                f"web_search provider: {provider}"
                if provider
                else "web_search: no provider key (set TAVILY_API_KEY/BRAVE_API_KEY)"
            )
            checks.append(
                {
                    "check": "Agent Web Tools",
                    "category": "ai",
                    "status": "✅ Enabled",
                    "ok": True,
                    "details": f"web_fetch (SSRF-safe) + {search_state}",
                }
            )
        else:
            checks.append(
                {
                    "check": "Agent Web Tools",
                    "category": "ai",
                    "status": "⚪ Disabled (opt-in)",
                    "ok": True,
                    "details": "Set FLUID_AGENT_WEB_TOOLS=1 to expose web_search + web_fetch",
                }
            )
    except Exception:  # noqa: BLE001 — non-critical status line
        checks.append(
            {
                "check": "Agent Web Tools",
                "category": "ai",
                "status": "⚪ Disabled (opt-in)",
                "ok": True,
                "details": "Set FLUID_AGENT_WEB_TOOLS=1 to expose web_search + web_fetch",
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
    from .forge_copilot_llm_providers import check_llm_readiness

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


# ---------------------------------------------------------------------
# Store backend inspection (MEMORY-E2E-A finding #55)
# ---------------------------------------------------------------------
#
# Until this slice, ``fluid doctor`` told operators about kill switches
# and Copilot readiness but stayed silent on which memory-store backend
# was actually wired. Operators exporting ``FLUID_STORE_BACKEND=postgres``
# had no easy way to confirm the DSN parsed, the schema initialised,
# and the writes would land where they expected. The inspector below
# resolves the active backend exactly once, probes connectivity in a
# bounded, non-blocking way, and renders a compact "Store Backend"
# section alongside the existing Copilot Readiness panel.
#
# Read-only by construction — every backend variant either skips the
# probe entirely or wraps it in try/except so a malformed DSN doesn't
# crash ``fluid doctor``.


def _inspect_store_backend() -> Dict[str, str]:
    """Return a dict describing the active store backend.

    Keys are stable so the ``--json`` shape is documented and the
    ``_print_store_backend_status`` renderer can rely on them.
    Connection probes are bounded (2 s for Postgres) and any failure
    flows back as ``ok=False`` with a ``status`` message — never an
    exception.
    """
    backend_raw = os.environ.get("FLUID_STORE_BACKEND")
    backend = (backend_raw or "file").strip().lower() or "file"

    info: Dict[str, str] = {
        "env": backend_raw if backend_raw is not None else "(unset)",
        "backend": backend,
        # Resolved fields filled in by the per-backend branches below.
        "class": "",
        "location": "",
        "status": "",
        "schema_version": "",
        "ok": "true",
    }

    if backend in {"null", "none", "0", "disabled"}:
        info["class"] = "NullBackend"
        info["location"] = "(no persistence)"
        info["status"] = "active (no-op backend)"
        return info

    if backend == "file":
        path = Path(
            os.environ.get("FLUID_STORE_ROOT") or (Path.home() / ".fluid" / "store")
        ).expanduser()
        info["class"] = "FileBackend"
        info["location"] = str(path)
        if path.exists() and path.is_dir() and os.access(path, os.R_OK | os.W_OK):
            info["status"] = "ready (path readable + writable)"
        elif path.exists():
            info["status"] = "path exists but not readable+writable"
            info["ok"] = "false"
        else:
            info["status"] = "path missing (will auto-create on first write)"
        return info

    if backend == "sqlite":
        path = Path(
            os.environ.get("FLUID_STORE_PATH")
            or (Path.home() / ".fluid" / "store" / "store.sqlite3")
        ).expanduser()
        info["class"] = "SqliteBackend"
        info["location"] = str(path)
        if not path.exists():
            info["status"] = "file missing (will auto-create on first write)"
            return info
        if not os.access(path, os.R_OK):
            info["status"] = "file present but not readable"
            info["ok"] = "false"
            return info
        # Read-only probe via PRAGMA user_version. We open with a
        # dedicated short-lived connection so we don't disturb whatever
        # state the main runtime may hold.
        try:
            import sqlite3

            conn = sqlite3.connect(str(path), timeout=2.0)
            try:
                cur = conn.execute("PRAGMA user_version")
                row = cur.fetchone()
                info["schema_version"] = str(row[0]) if row else "0"
                info["status"] = "reachable (sqlite3 PRAGMA user_version probe ok)"
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            info["status"] = f"probe failed: {exc.__class__.__name__}"
            info["ok"] = "false"
        return info

    if backend == "postgres":
        dsn = (os.environ.get("FLUID_STORE_DSN") or "").strip()
        info["class"] = "PostgresBackend"
        if not dsn:
            info["location"] = "(FLUID_STORE_DSN unset)"
            info["status"] = "DSN missing — PostgresBackend cannot connect"
            info["ok"] = "false"
            return info
        # Use the same redactor the backend uses so the displayed DSN
        # never leaks the password.
        try:
            from fluid_build.copilot.store.backends.postgres import _redact_dsn

            info["location"] = _redact_dsn(dsn)
        except Exception:  # noqa: BLE001
            info["location"] = "postgresql://***"
        try:
            import psycopg  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001
            info["status"] = f"psycopg not installed: {exc.__class__.__name__}"
            info["ok"] = "false"
            return info
        # Ping with a hard wall-clock cap so a hung Postgres doesn't
        # hang ``fluid doctor``. ``connect_timeout`` is documented at
        # https://www.postgresql.org/docs/current/libpq-connect.html
        # and is honoured by libpq/psycopg.
        try:
            conn = psycopg.connect(dsn, connect_timeout=2)
            try:
                with conn.cursor() as cur:
                    cur.execute("select 1")
                    cur.fetchone()
                    # Schema version probe: read SERVER_VERSION_NUM as a
                    # proxy (the storage layer doesn't carry its own
                    # versioned migration table yet — auto-CREATE TABLE
                    # IF NOT EXISTS handles evolution today).
                    cur.execute("show server_version_num")
                    row = cur.fetchone()
                    if row:
                        info["schema_version"] = f"pg server={row[0]}"
                info["status"] = "reachable (select 1 round-trip ok)"
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            info["status"] = f"connect failed: {exc.__class__.__name__}"
            info["ok"] = "false"
        return info

    if backend == "vector":
        backing = (os.environ.get("FLUID_STORE_VECTOR_BACKING") or "file").strip().lower()
        info["class"] = "VectorBackend"
        info["location"] = f"backing={backing or 'file'}"
        info["status"] = "wraps backing store"
        return info

    # Unknown backend selector — ``resolve_store`` would raise; surface
    # the message instead of crashing the doctor output.
    info["class"] = "(unknown)"
    info["location"] = ""
    info["status"] = f"unrecognised FLUID_STORE_BACKEND={backend!r}"
    info["ok"] = "false"
    return info


def _print_store_backend_status(info: Dict[str, str], verbose: bool = False) -> None:
    """Render the inspected store-backend dict.

    Mirrors the Copilot Readiness panel's layout so the two sections
    share the same visual rhythm. The ``info["ok"]`` string is the
    canonical pass/fail signal — anything other than ``"true"`` lights
    the warning icon.
    """
    ok_flag = info.get("ok", "true") == "true"
    status_icon = "✅ Active" if ok_flag else "⚠️  Action needed"

    if RICH_AVAILABLE:
        console = Console()
        table = Table(title="📦 Memory Store Backend", show_header=True)
        table.add_column("Item", style="cyan", width=18)
        table.add_column("Value", style="white")
        table.add_row("Status", status_icon)
        table.add_row("FLUID_STORE_BACKEND", info.get("env", "(unset)"))
        table.add_row("Backend class", info.get("class", ""))
        table.add_row("Location", info.get("location", ""))
        if info.get("schema_version"):
            table.add_row("Schema version", info["schema_version"])
        table.add_row("Probe", info.get("status", ""))
        console.print(table)
        return

    cprint("\n" + "=" * 60)
    cprint("Memory Store Backend")
    cprint("=" * 60)
    cprint(f"Status:               {status_icon}")
    cprint(f"FLUID_STORE_BACKEND:  {info.get('env', '(unset)')}")
    cprint(f"Backend class:        {info.get('class', '')}")
    cprint(f"Location:             {info.get('location', '')}")
    if info.get("schema_version"):
        cprint(f"Schema version:       {info['schema_version']}")
    cprint(f"Probe:                {info.get('status', '')}")
    cprint()
