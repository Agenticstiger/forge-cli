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

"""``fluid forge`` post-run CI/CD pipeline auto-scaffolder.

Lifted from ``cli/forge_modes.py`` (the host file was 2459 LOC; the
CI scaffolding block is a coherent ~400 LOC self-contained group). The
two entry points consumers care about are:

* :func:`scaffold_ci_pipeline` — top-level: resolve provider +
  complexity, run drift detection, write the ci-state.json record.
* :func:`resolve_ci_choice` — the precedence-ordered resolver
  (kill-switch → ``--no-ci`` → ``--ci <provider>`` → recorded
  ci-state → memory → menu → silent skip).

Tests that previously imported these from ``fluid_build.cli.forge_modes``
keep working: ``forge_modes.py`` re-exports each at module top.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# Choices + alias map. These are the source of truth — the host
# module re-exports them under the same names so legacy imports keep
# resolving.

_CI_PROVIDER_CHOICES = [
    {"label": "GitHub Actions", "value": "github_actions"},
    {"label": "GitLab CI", "value": "gitlab_ci"},
    {"label": "Azure DevOps", "value": "azure_devops"},
    {"label": "Jenkins", "value": "jenkins"},
    {"label": "Bitbucket Pipelines", "value": "bitbucket"},
    {"label": "CircleCI", "value": "circleci"},
    {"label": "Tekton", "value": "tekton"},
    {"label": "None (skip)", "value": "none"},
]

_CI_COMPLEXITY_CHOICES = [
    {"label": "Basic — validate → apply", "value": "basic"},
    {"label": "Standard — full workflow with tests", "value": "standard"},
    {"label": "Advanced — multi-env, approvals, security", "value": "advanced"},
    {"label": "Enterprise — full governance and compliance", "value": "enterprise"},
]

_CI_COMPLEXITY_VALUES = {c["value"] for c in _CI_COMPLEXITY_CHOICES}
_CI_PROVIDER_VALUES = {c["value"] for c in _CI_PROVIDER_CHOICES if c["value"] != "none"}
_CI_PROVIDER_ALIASES = {
    "circle_ci": "circleci",
    "circleci": "circleci",
}


def normalize_ci_provider(value: Optional[str]) -> Optional[str]:
    """Map legacy/provider aliases to the CLI-facing CI provider names."""
    if value is None:
        return None
    return _CI_PROVIDER_ALIASES.get(value, value)


def ci_killswitch_enabled() -> bool:
    """Return True if the env kill switch disables CI auto-scaffolding."""
    return os.environ.get("FLUID_FORGE_AUTO_CI", "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }


def prompt_ci_menu(
    console: Any,
    ask_dialog_question_fn: Callable[[Any, Any], Any],
    *,
    memory_default: Optional[str],
    complexity_default: str,
) -> tuple[Optional[str], str]:
    """Interactive CI provider + complexity menu.

    Returns ``(provider, complexity)``; ``provider`` is ``None`` when
    the user selects *None (skip)* or the prompt is cancelled.
    """
    # Lazy import to avoid cycles through forge_modes → interview → dialogs.
    from fluid_build.cli.forge_copilot_interview import InterviewQuestion

    if console:
        try:
            console.print("\n[bold blue]🛠  CI/CD pipeline[/bold blue]")
            if memory_default:
                console.print(f"[dim]Default from your preferences: {memory_default}[/dim]")
        except Exception:  # noqa: BLE001
            pass

    effective_default = memory_default if memory_default in _CI_PROVIDER_VALUES else None

    provider_question = InterviewQuestion(
        id="ci_provider",
        field="ci_provider",
        prompt="Generate a CI/CD pipeline now? Pick a provider (or 'none' to skip).",
        type="choice",
        choices=list(_CI_PROVIDER_CHOICES),
        required=False,
        allow_skip=True,
        default=effective_default,
    )
    provider_result = ask_dialog_question_fn(console, provider_question)
    provider_value = getattr(provider_result, "value", None)
    if not provider_value or provider_value == "none":
        return (None, complexity_default)

    complexity_question = InterviewQuestion(
        id="ci_complexity",
        field="ci_complexity",
        prompt="Pipeline complexity?",
        type="choice",
        choices=list(_CI_COMPLEXITY_CHOICES),
        required=False,
        allow_skip=True,
        default=complexity_default if complexity_default in _CI_COMPLEXITY_VALUES else "standard",
    )
    complexity_result = ask_dialog_question_fn(console, complexity_question)
    resolved_complexity = getattr(complexity_result, "value", None) or complexity_default
    if resolved_complexity not in _CI_COMPLEXITY_VALUES:
        resolved_complexity = "standard"
    return (provider_value, resolved_complexity)


def resolve_ci_choice(
    args: Any,
    context: Dict[str, Any],
    *,
    is_interactive: bool,
    ask_dialog_question_fn: Callable[[Any, Any], Any],
    get_cli_arg_fn: Callable[[Any, str, Any], Any],
    console: Any = None,
) -> tuple[Optional[str], str]:
    """Resolve (provider, complexity) for the auto-CI hook.

    Precedence (highest first):
        1. ``FLUID_FORGE_AUTO_CI=0`` env kill switch
        2. ``--no-ci`` or ``--ci none``
        3. ``--ci <provider>`` explicit value
        4. ``--ci ask`` → force interactive menu
        5. Recorded ci-state (from slice 7) — auto-selects the same
           provider that produced the committed CI files so re-runs on
           any teammate's machine refresh them without prompting.  Set
           by the caller in ``context["ci_provider"]`` /
           ``context["ci_complexity"]``.  ci-state beats personal
           memory because it is product-scoped (committed next to the
           CI files it describes).
        6. Personal memory default → used only as preselection in the
           interactive menu
        7. Interactive menu (no preselection)
        8. Non-interactive with a recorded ci-state provider → use it
        9. Non-interactive with no flag and no ci-state → silently skip
    """
    # 1. Kill switch
    if ci_killswitch_enabled():
        return (None, "standard")

    # 2. Explicit "no CI"
    if get_cli_arg_fn(args, "no_ci", False):
        return (None, "standard")

    raw_complexity = (
        get_cli_arg_fn(args, "ci_complexity", None) or context.get("ci_complexity") or "standard"
    )
    complexity = raw_complexity if raw_complexity in _CI_COMPLEXITY_VALUES else "standard"

    ci_flag = normalize_ci_provider(get_cli_arg_fn(args, "ci", None))

    # 2b. Explicit "none" sentinel from --ci
    if ci_flag == "none":
        return (None, complexity)

    # 3. Explicit provider value (not the "ask" sentinel)
    if ci_flag and ci_flag != "ask":
        if ci_flag in _CI_PROVIDER_VALUES:
            return (ci_flag, complexity)
        # Unknown provider — warn and skip rather than crash.
        if console:
            try:
                console.print(
                    f"[yellow]Unknown CI provider: {ci_flag!r}. Skipping auto-scaffold.[/yellow]"
                )
            except Exception:  # noqa: BLE001
                pass
        return (None, complexity)

    # 4/5/6/7. Interactive path
    if is_interactive:
        memory_default = context.get("ci_provider")
        if memory_default not in _CI_PROVIDER_VALUES:
            memory_default = None
        return prompt_ci_menu(
            console,
            ask_dialog_question_fn,
            memory_default=memory_default,
            complexity_default=complexity,
        )

    # 8. Non-interactive with a recorded ci-state provider → use it.
    recorded_provider = normalize_ci_provider(context.get("ci_provider"))
    if recorded_provider in _CI_PROVIDER_VALUES:
        return (recorded_provider, complexity)

    # 9. Non-interactive with no flag and no ci-state → silent skip
    return (None, complexity)


def scaffold_ci_pipeline(
    args: Any,
    target_dir: Path,
    context: Dict[str, Any],
    console: Any,
    *,
    ask_dialog_question_fn: Callable[[Any, Any], Any],
    get_cli_arg_fn: Callable[[Any, str, Any], Any],
    dry_run: bool,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve the CI choice and invoke ``PipelineTemplateGenerator``.

    Returns ``(provider, complexity)`` when files were generated (or
    planned in dry-run), otherwise ``(None, None)``.
    """
    is_interactive = not bool(get_cli_arg_fn(args, "non_interactive", False))

    # Slice 8: a committed ci-state.json in the target dir records the
    # provider/complexity that last produced the committed CI files.
    # Thread its values into context BEFORE resolve_ci_choice runs so
    # the recorded choice beats personal memory.
    try:
        from fluid_build.cli.artifact_ci_state import load_ci_state

        recorded = load_ci_state(target_dir)
    except Exception:  # noqa: BLE001 — ci-state read is best-effort
        recorded = None

    if recorded is not None:
        context = dict(context)
        context["ci_provider"] = normalize_ci_provider(recorded.provider)
        context["ci_complexity"] = recorded.complexity

    provider, complexity = resolve_ci_choice(
        args,
        context,
        is_interactive=is_interactive,
        ask_dialog_question_fn=ask_dialog_question_fn,
        get_cli_arg_fn=get_cli_arg_fn,
        console=console,
    )

    if not provider:
        explicit_skip = (
            get_cli_arg_fn(args, "no_ci", False) or get_cli_arg_fn(args, "ci", None) == "none"
        )
        if explicit_skip and console:
            try:
                console.print("[dim]CI scaffolding skipped.[/dim]")
            except Exception:  # noqa: BLE001
                pass
        return (None, None)

    try:
        from fluid_build.cli.pipeline_generator import (
            build_pipeline_config,
            write_pipeline_files,
        )
        from fluid_build.forge.core.pipeline_templates import PipelineTemplateGenerator
    except ImportError as exc:
        if console:
            try:
                console.print(f"[yellow]Could not load pipeline generator: {exc}[/yellow]")
            except Exception:  # noqa: BLE001
                pass
        return (None, None)

    try:
        config = build_pipeline_config(provider=provider, complexity=complexity)
        files = PipelineTemplateGenerator().generate_pipeline(config)
    except Exception as exc:  # noqa: BLE001
        if console:
            try:
                console.print(f"[yellow]CI generation failed: {exc}[/yellow]")
            except Exception:  # noqa: BLE001
                pass
        return (None, None)

    # Drift-aware collision check (slice 8).
    try:
        from fluid_build.cli.artifact_ci_state import (
            classify_ci_drift,
            load_ci_state,
        )

        state = load_ci_state(target_dir)
        drift = classify_ci_drift(target_dir, files, state=state)
    except Exception as exc:  # noqa: BLE001 — drift detection is best-effort
        state = None
        drift = None
        if console:
            try:
                console.print(f"[dim]ci-state drift check skipped: {exc}[/dim]")
            except Exception:  # noqa: BLE001
                pass

    if drift is not None and not dry_run:
        if drift.drifted:
            if console:
                try:
                    console.print(
                        f"\n[yellow]Skipping CI scaffold — hand-edited files would be overwritten: "
                        f"{', '.join(drift.drifted)}[/yellow]"
                    )
                    console.print(
                        "[dim]Delete the files or run 'fluid generate-pipeline --output-dir .' to regenerate explicitly.[/dim]"
                    )
                except Exception:  # noqa: BLE001
                    pass
            return (None, None)

        if drift.missing_from_state:
            if console:
                try:
                    console.print(
                        f"\n[yellow]Skipping CI scaffold — existing files would be overwritten: "
                        f"{', '.join(drift.missing_from_state)}[/yellow]"
                    )
                    console.print(
                        "[dim]No ci-state.json recorded these — delete them or run 'fluid generate-pipeline --output-dir .' to regenerate.[/dim]"
                    )
                except Exception:  # noqa: BLE001
                    pass
            return (None, None)

        if drift.pristine and console:
            try:
                console.print(
                    f"[dim]CI already up to date ({len(drift.pristine)} file(s)); regenerating cleanly.[/dim]"
                )
            except Exception:  # noqa: BLE001
                pass

    if console:
        try:
            console.print(
                f"\n[bold blue]🛠  Generating {provider} pipeline ({complexity})…[/bold blue]"
            )
        except Exception:  # noqa: BLE001
            pass

    try:
        command_str = f"fluid forge --ci {provider} --ci-complexity {complexity}"
        try:
            from fluid_build import __version__ as tool_version
        except Exception:  # pragma: no cover — defensive
            tool_version = ""
        written_paths = write_pipeline_files(
            files,
            target_dir,
            dry_run=dry_run,
            console=console,
            command=command_str,
            tool_version=str(tool_version),
        )
    except OSError as exc:
        if console:
            try:
                console.print(f"[yellow]Could not write CI files: {exc}[/yellow]")
            except Exception:  # noqa: BLE001
                pass
        return (None, None)

    # Emit ci-state.json so other machines can detect drift.
    if not dry_run:
        try:
            from fluid_build.cli.artifact_ci_state import (
                build_ci_state_payload,
                write_ci_state,
            )

            doc = build_ci_state_payload(
                provider=provider,
                complexity=complexity,
                environments=list(getattr(config, "environments", []) or []),
                options={
                    "enable_approvals": bool(getattr(config, "enable_approvals", False)),
                    "enable_security_scan": bool(getattr(config, "enable_security_scan", True)),
                    "enable_marketplace_publishing": bool(
                        getattr(config, "enable_marketplace_publishing", False)
                    ),
                },
                written_files=written_paths,
                product_root=target_dir,
                body_contents=files,
            )
            write_ci_state(
                doc,
                target_dir,
                command=command_str,
                tool_version=str(tool_version),
            )
        except Exception as exc:  # noqa: BLE001 — ci-state write is best-effort
            if console:
                try:
                    console.print(f"[dim]ci-state not written: {exc}[/dim]")
                except Exception:  # noqa: BLE001
                    pass

    if console and not dry_run:
        try:
            console.print(
                "[dim]Tip: run 'fluid generate-pipeline --help' to regenerate later.[/dim]"
            )
        except Exception:  # noqa: BLE001
            pass

    return (provider, complexity)


__all__ = [
    "_CI_COMPLEXITY_CHOICES",
    "_CI_COMPLEXITY_VALUES",
    "_CI_PROVIDER_ALIASES",
    "_CI_PROVIDER_CHOICES",
    "_CI_PROVIDER_VALUES",
    "ci_killswitch_enabled",
    "normalize_ci_provider",
    "prompt_ci_menu",
    "resolve_ci_choice",
    "scaffold_ci_pipeline",
]
