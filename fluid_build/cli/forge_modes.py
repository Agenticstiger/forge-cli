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

"""Mode handlers for `fluid forge`."""

from __future__ import annotations

__all__ = [
    "run_ai_copilot_mode",
    "run_domain_agent_mode",
    "run_guided_mode",
    "run_template_mode",
]  # Note: domain_agent/template modes are deprecated but kept for backward compat


import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from fluid_build.cli.console import cprint, success, warning
from fluid_build.cli.console import error as console_error
from fluid_build.cli.forge_copilot_interview import (
    InterviewQuestion,
    run_adaptive_copilot_interview,
)
from fluid_build.cli.forge_copilot_llm_providers import (
    PROVIDER_DISPLAY_NAMES,
    CopilotGenerationError,
    LlmConfig,
    build_llm_run_plan,
    check_llm_readiness,
    detect_provider_from_api_key,
    get_catalog_default,
    get_llm_provider,
    resolve_ollama_model,
    save_api_key_to_keyring,
)
from fluid_build.cli.forge_copilot_taxonomy import normalize_copilot_context
from fluid_build.cli.forge_dialogs import (
    ask_confirmation,
    ask_dialog_question,
    ask_secret_text,
    print_dialog_status,
)
from fluid_build.cli.forge_ui import (
    print_assumptions_panel,
    print_copilot_intro_panel,
    print_copilot_recovery_panel,
    print_free_tier_guide,
    print_welcome_panel,
)

try:
    from rich.console import Console
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised via non-Rich fallbacks
    Console = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]
    RICH_AVAILABLE = False


def _create_session_llm_config(
    console: Any,
    *,
    default_provider: str = "gemini",
    ask_dialog_question_fn: Callable[[Any, Any], Any] = ask_dialog_question,
    ask_secret_text_fn: Callable[..., Optional[str]] = ask_secret_text,
) -> Optional[LlmConfig]:
    """Collect a session-only LLM configuration via an API-key-first flow.

    The user pastes an API key and the provider is auto-detected from its
    format.  If detection fails, a short follow-up asks which provider the
    key belongs to.  Model and endpoint always use sensible provider defaults
    so the user never has to type them.
    """
    if console:
        console.print(
            "[dim]Paste an API key for OpenAI, Anthropic (Claude), or Google Gemini "
            "and I'll detect the provider automatically.\n"
            "Type [bold]ollama[/bold] if you want to use a local model instead.[/dim]"
        )

    api_key = ask_secret_text_fn(
        console,
        "API key for OpenAI / Anthropic / Gemini (or 'ollama')",
        required=True,
    )

    raw = (api_key or "").strip().lower()

    # --- Ollama shortcut ---
    if raw in ("ollama", "local", "ollama/local", "ollama (local)"):
        provider = get_llm_provider("ollama")
        model = resolve_ollama_model(os.environ)
        endpoint = provider.default_endpoint(model, os.environ)
        print_dialog_status(
            console,
            status="success",
            message=f"Using Ollama -- {model}",
        )
        return LlmConfig(provider="ollama", model=model, endpoint=endpoint, api_key=None)

    if not api_key:
        if console:
            print_dialog_status(
                console,
                status="error",
                message="A hosted provider needs an API key for this run.",
                detail="You can choose a different mode or try Ollama for a local setup.",
            )
        return None

    # --- Auto-detect provider from key format ---
    detected = detect_provider_from_api_key(api_key)
    if detected:
        provider_name = detected
    else:
        # Key format not recognised — ask which provider
        provider_question = InterviewQuestion(
            id="llm_provider",
            field="llm_provider",
            prompt="Which provider is this key for?",
            type="choice",
            choices=[
                {"label": "Google Gemini", "value": "gemini"},
                {"label": "OpenAI", "value": "openai"},
                {"label": "Anthropic (Claude)", "value": "anthropic"},
            ],
            required=True,
            allow_skip=False,
            default=default_provider,
        )
        selection = ask_dialog_question_fn(console, provider_question)
        provider_name = str(selection.value or default_provider or "gemini").strip().lower()

    provider = get_llm_provider(provider_name)
    model = get_catalog_default(provider_name) or provider.default_model
    endpoint = provider.default_endpoint(model, os.environ)
    display = PROVIDER_DISPLAY_NAMES.get(provider_name, provider_name)

    if console:
        print_dialog_status(
            console,
            status="success",
            message=f"Detected {display} -- using {model}",
        )

    # Persist the key in the OS keychain so future runs resolve silently.
    if api_key and provider_name != "ollama":
        saved = save_api_key_to_keyring(provider_name, api_key)
        if console:
            if saved:
                print_dialog_status(
                    console,
                    status="info",
                    message="Key saved to your system keychain for future runs.",
                    detail="Use 'fluid ai setup --clear' to reset later.",
                )
            else:
                print_dialog_status(
                    console,
                    status="warning",
                    message="Could not save key to keychain (keyring unavailable).",
                    detail="Set an env var like OPENAI_API_KEY for persistence, or re-run setup next time.",
                )

    return LlmConfig(provider=provider.name, model=model, endpoint=endpoint, api_key=api_key)


def _choose_recovery_mode(
    console: Any,
    *,
    fallback_mode_choices: Sequence[Mapping[str, str]],
    ask_dialog_question_fn: Callable[[Any, Any], Any] = ask_dialog_question,
) -> Optional[str]:
    """Ask the user which non-copilot mode to use instead."""
    if not fallback_mode_choices:
        return None
    print_welcome_panel(console)
    question = InterviewQuestion(
        id="fallback_mode",
        field="fallback_mode",
        prompt="Which creation mode would you like to use instead?",
        type="choice",
        choices=list(fallback_mode_choices),
        required=False,
        allow_skip=True,
        default=str(fallback_mode_choices[0].get("value") or ""),
    )
    selection = ask_dialog_question_fn(console, question)
    return str(selection.value or fallback_mode_choices[0].get("value") or "").strip() or None


def _handle_copilot_recovery(
    *,
    args: Any,
    console: Any,
    error: CopilotGenerationError,
    llm_readiness_fn: Callable[[Any], Any],
    route_mode_fn: Optional[Callable[[str], int]],
    fallback_mode_choices: Sequence[Mapping[str, str]],
    ask_dialog_question_fn: Callable[[Any, Any], Any],
    ask_secret_text_fn: Callable[..., Optional[str]],
) -> Dict[str, Any] | int:
    """Offer session-only setup first, then alternate modes if the user declines."""
    if console:
        print_copilot_recovery_panel(
            console,
            message=error.message,
            suggestions=error.suggestions,
        )
        print_free_tier_guide(console)

    wants_setup = ask_confirmation(
        console,
        "Set up AI for this run now?",
        default=True,
        title="Copilot Setup Needed",
        preview=(
            "Forge needs a working LLM to power copilot.\n"
            "You can fix this permanently by setting an env var:\n"
            "  export OPENAI_API_KEY=sk-...       (OpenAI)\n"
            "  export ANTHROPIC_API_KEY=sk-ant-... (Claude)\n"
            "  export GEMINI_API_KEY=AIza...       (Gemini)\n\n"
            "Or choose Yes and I'll ask for a key just for this run."
        ),
        border_style="yellow",
    )
    if wants_setup:
        default_provider = getattr(llm_readiness_fn(), "provider", "gemini") or "gemini"
        llm_config = _create_session_llm_config(
            console,
            default_provider=default_provider,
            ask_dialog_question_fn=ask_dialog_question_fn,
            ask_secret_text_fn=ask_secret_text_fn,
        )
        if llm_config:
            if console:
                print_dialog_status(
                    console,
                    status="success",
                    message=f"{llm_config.provider.title()} is configured for this run.",
                    detail="Continuing into AI Copilot.",
                )
            return {"llm_config": llm_config}

    selected_mode = _choose_recovery_mode(
        console,
        fallback_mode_choices=fallback_mode_choices,
        ask_dialog_question_fn=ask_dialog_question_fn,
    )
    if selected_mode and route_mode_fn:
        return route_mode_fn(selected_mode)

    if console:
        print_dialog_status(
            console,
            status="error",
            message=error.message,
            detail="Copilot setup was skipped and no alternate mode was selected.",
        )
    return 1


def _print_discovery_summary(console: Any, discovery: Any) -> None:
    """Show a one-liner about what the discovery scanner found."""
    if not console:
        return
    samples = len(getattr(discovery, "sample_files", None) or [])
    sqls = len(getattr(discovery, "sql_files", None) or [])
    contracts = len(getattr(discovery, "existing_contracts", None) or [])
    parts: List[str] = []
    if samples:
        parts.append(f"{samples} data file{'s' if samples != 1 else ''}")
    if sqls:
        parts.append(f"{sqls} SQL file{'s' if sqls != 1 else ''}")
    if contracts:
        parts.append(f"{contracts} existing contract{'s' if contracts != 1 else ''}")
    if parts:
        console.print(
            f"[dim]Data: found {', '.join(parts)} -- schemas will guide contract generation[/dim]"
        )
    else:
        _print_discovery_hint(console)


def _print_mode_awareness(console: Any) -> None:
    """Show what mode forge is in and list alternatives.

    Displayed only when the user ran ``fluid forge`` without ``--mode``
    inside an existing workspace, so they know other modes exist.
    """
    if not console:
        return
    try:
        from rich.panel import Panel  # noqa: F811 — local import for optional dep

        console.print(
            Panel(
                "[bold]Forge — Add a data product[/bold]\n\n"
                "Mode: [cyan]AI Copilot[/cyan] [dim](default)[/dim]\n\n"
                "[dim]Other modes available:[/dim]\n"
                "  [cyan]fluid forge --mode template[/cyan]   "
                "[dim]← from a pre-built template[/dim]\n"
                "  [cyan]fluid forge --mode agent[/cyan]      "
                "[dim]← domain expert (finance, healthcare)[/dim]\n"
                "  [cyan]fluid forge --blank[/cyan]           "
                "[dim]← empty contract[/dim]",
                border_style="bright_magenta",
            )
        )
    except ImportError:
        pass


def _load_industry_skills(ws_root: Any, context: Dict[str, Any], console: Any) -> None:
    """Read ``.fluid/skills.yaml`` and inject industry knowledge into *context*.

    Slice UX-J: prefer the pre-compiled ``skills.compiled.json`` via
    :func:`forge_copilot_skills_cache.load_compiled_skills`.  The
    compiled form is ~10-20x smaller and memoized per-process, so
    subsequent forge runs in the same process skip YAML parsing
    entirely.  The raw ``skills.yaml`` is still loaded for fields
    that the compiled form drops (``canonical_model.primary``,
    ``industry.name``) — those are used locally for context seeding
    but not sent to the LLM.
    """
    try:
        from pathlib import Path

        import yaml

        skills_path = Path(ws_root) / ".fluid" / "skills.yaml"
        if not skills_path.exists():
            return

        with skills_path.open() as f:
            skills = yaml.safe_load(f)
        if not skills:
            return

        context["industry_skills"] = skills

        # Slice UX-J: inject the compiled payload for prompt builders.
        # If the compiled file exists and is cached, this is a <1ms
        # dict lookup; otherwise it compiles on-the-fly from the raw
        # YAML we already loaded.
        try:
            from fluid_build.cli.forge_copilot_skills_cache import load_compiled_skills

            compiled = load_compiled_skills(Path(ws_root))
            if compiled:
                context["compiled_skills"] = compiled
        except Exception:  # noqa: BLE001
            pass  # Compiled cache is best-effort.

        # Pre-fill domain and canonical model from skills (don't overwrite).
        cm = skills.get("canonical_model", {})
        if cm.get("primary") and "canonical_model" not in context:
            context["canonical_model"] = cm["primary"]
        ind = skills.get("industry", {})
        if ind.get("name") and "domain" not in context:
            context["domain"] = ind["name"]

        if console and ind.get("label"):
            console.print(
                f"[dim]Industry skills: {ind['label']}"
                f"{' — ' + cm.get('label', '') if cm.get('label') else ''}[/dim]\n"
            )
    except Exception:  # noqa: BLE001
        pass  # Skills are optional — never block on them.


def _apply_workspace_defaults(context: Dict[str, Any], console: Any) -> None:
    """Read ``fluid.workspace.yaml`` and inject shared defaults into *context*."""
    try:
        from fluid_build.cli.workspace_config import (
            discover_workspace_products,
            find_workspace_root,
            load_workspace_config,
        )

        ws_root = find_workspace_root()
        if ws_root is None:
            return
        ws = load_workspace_config(ws_root)
        if ws.is_empty:
            return

        # Show existing products.
        products = discover_workspace_products(ws_root)
        if products and console:
            console.print(
                f"[dim]📂 Workspace: [bold]{ws.name or ws_root.name}[/bold] "
                f"({len(products)} product{'s' if len(products) != 1 else ''})[/dim]"
            )
            for p in products[:8]:
                parts = [p.name]
                if p.expose_count:
                    parts.append(f"{p.expose_count} expose{'s' if p.expose_count != 1 else ''}")
                if p.provider:
                    parts.append(f"provider: {p.provider}")
                console.print(f"[dim]  • {', '.join(parts)}[/dim]")
            console.print()

        # Inject defaults (don't overwrite explicit values).
        if ws.domain and "domain" not in context:
            context["domain"] = ws.domain
        if ws.provider and "provider" not in context:
            context["provider"] = ws.provider
        if ws.owner_team and "owner_team" not in context:
            context["owner_team"] = ws.owner_team

        if console and (ws.domain or ws.provider or ws.owner_team):
            parts = []
            if ws.domain:
                parts.append(f"domain={ws.domain}")
            if ws.owner_team:
                parts.append(f"team={ws.owner_team}")
            if ws.provider:
                parts.append(f"provider={ws.provider}")
            console.print(f"[dim]Using workspace defaults: {', '.join(parts)}[/dim]\n")

        # ── Industry skills ──────────────────────────────────────────
        _load_industry_skills(ws_root, context, console)

    except Exception:  # noqa: BLE001
        pass  # Workspace config is optional — never block on it.


def _print_discovery_hint(console: Any) -> None:
    """Nudge the user to add sample data for better contracts."""
    if not console:
        return
    console.print(
        "[dim]Tip: drop sample CSV, Parquet, or JSON files in this directory "
        "(or use [bold]--discovery-path[/bold]) and copilot will use their schemas[/dim]"
    )


# ---------------------------------------------------------------------------
# CI/CD auto-scaffolding hook (post-copilot)
# ---------------------------------------------------------------------------


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


def _normalize_ci_provider(value: Optional[str]) -> Optional[str]:
    """Map legacy/provider aliases to the CLI-facing CI provider names."""
    if value is None:
        return None
    return _CI_PROVIDER_ALIASES.get(value, value)


def _ci_killswitch_enabled() -> bool:
    """Return True if the env kill switch disables CI auto-scaffolding."""
    return os.environ.get("FLUID_FORGE_AUTO_CI", "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }


def _prompt_ci_menu(
    console: Any,
    ask_dialog_question_fn: Callable[[Any, Any], Any],
    *,
    memory_default: Optional[str],
    complexity_default: str,
) -> tuple[Optional[str], str]:
    """Interactive CI provider + complexity menu.

    Returns ``(provider, complexity)``; ``provider`` is ``None`` when the
    user selects *None (skip)* or the prompt is cancelled.
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


def _resolve_ci_choice(
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
    if _ci_killswitch_enabled():
        return (None, "standard")

    # 2. Explicit "no CI"
    if get_cli_arg_fn(args, "no_ci", False):
        return (None, "standard")

    raw_complexity = (
        get_cli_arg_fn(args, "ci_complexity", None) or context.get("ci_complexity") or "standard"
    )
    complexity = raw_complexity if raw_complexity in _CI_COMPLEXITY_VALUES else "standard"

    ci_flag = _normalize_ci_provider(get_cli_arg_fn(args, "ci", None))

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
        # In interactive mode, the context-seeded value (from ci-state
        # or personal memory) becomes the menu's preselected default.
        # ci-state has already been merged into context by the caller,
        # so this single lookup honours the documented precedence.
        memory_default = context.get("ci_provider")
        if memory_default not in _CI_PROVIDER_VALUES:
            memory_default = None
        return _prompt_ci_menu(
            console,
            ask_dialog_question_fn,
            memory_default=memory_default,
            complexity_default=complexity,
        )

    # 8. Non-interactive with a recorded ci-state provider → use it.
    # This is what makes `fluid forge` on another teammate's machine
    # automatically refresh the committed CI files without --ci.
    recorded_provider = _normalize_ci_provider(context.get("ci_provider"))
    if recorded_provider in _CI_PROVIDER_VALUES:
        return (recorded_provider, complexity)

    # 9. Non-interactive with no flag and no ci-state → silent skip
    return (None, complexity)


def _scaffold_ci_pipeline(
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

    Returns ``(provider, complexity)`` when files were generated (or planned
    in dry-run), otherwise ``(None, None)``.
    """
    is_interactive = not bool(get_cli_arg_fn(args, "non_interactive", False))

    # Slice 8: a committed ci-state.json in the target dir records the
    # provider/complexity that last produced the committed CI files.
    # Thread its values into context BEFORE _resolve_ci_choice runs so
    # the recorded choice beats personal memory (but stays beneath
    # explicit --ci flags and the kill switch).
    try:
        from fluid_build.cli.artifact_ci_state import load_ci_state

        recorded = load_ci_state(target_dir)
    except Exception:  # noqa: BLE001 — ci-state read is best-effort
        recorded = None

    if recorded is not None:
        # ci-state beats personal memory (product-scoped > user-scoped).
        # Explicit --ci / --ci-complexity flags still win because
        # _resolve_ci_choice consults them before looking at context.
        context = dict(context)
        context["ci_provider"] = _normalize_ci_provider(recorded.provider)
        context["ci_complexity"] = recorded.complexity

    provider, complexity = _resolve_ci_choice(
        args,
        context,
        is_interactive=is_interactive,
        ask_dialog_question_fn=ask_dialog_question_fn,
        get_cli_arg_fn=get_cli_arg_fn,
        console=console,
    )

    if not provider:
        # Surface an explicit acknowledgement only when the user actively
        # opted out; silent skips (non-interactive without flag) stay quiet.
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

    # Drift-aware collision check (slice 8).  We use the recorded
    # ci-state.json to distinguish three cases:
    #
    #   pristine          → file exists and its body matches what the
    #                       last generation recorded.  Safe to silently
    #                       overwrite.
    #   drifted           → file exists but its body differs from the
    #                       recorded sha.  The user has hand-edited it.
    #                       Skip the whole scaffold to preserve the
    #                       edits (legacy behavior for this case).
    #   missing_from_state → file exists but ci-state has no record of
    #                       it.  This is the first-ever generation in
    #                       a repo that already has CI files from
    #                       another source.  Skip to be safe.
    #
    # When none of the above trip, the generation proceeds.
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

    # Emit the committed ci-state.json so teammates and `fluid forge`
    # runs on other machines can detect drift against this provider
    # choice.  Best-effort: failure never aborts the forge command.
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


def run_ai_copilot_mode(
    args: Any,
    logger: logging.Logger,
    *,
    copilot_class: type,
    get_cli_arg_fn: Callable[[Any, str, Any], Any],
    load_context_fn: Callable[..., Dict[str, Any]],
    get_target_directory_fn: Callable[[Any, str], Path],
    context_error_cls: type[Exception],
    build_interview_summary_fn: Callable[[Mapping[str, Any]], Dict[str, Any]],
    console_factory: Optional[Callable[[], Any]] = Console if RICH_AVAILABLE else None,
    llm_readiness_fn: Callable[[], Any] = check_llm_readiness,
    ask_dialog_question_fn: Callable[[Any, Any], Any] = ask_dialog_question,
    ask_secret_text_fn: Callable[..., Optional[str]] = ask_secret_text,
    route_mode_fn: Optional[Callable[[str], int]] = None,
    fallback_mode_choices: Sequence[Mapping[str, str]] = (),
) -> int:
    """Run Forge with AI copilot assistance."""
    console = console_factory() if console_factory else None

    try:
        copilot = copilot_class()
        is_non_interactive = bool(get_cli_arg_fn(args, "non_interactive", False))
        enable_recovery = bool(get_cli_arg_fn(args, "_enable_copilot_recovery", False))

        # Slice UX-L: performance stats accumulator — populated at
        # each stage and rendered at the end by
        # ``print_forge_performance_summary``.
        import time as _time

        _run_start = _time.monotonic()
        perf_stats: Dict[str, Any] = {
            "streaming": False,
            "discovery_cache_hit": False,
            "discovery_files": 0,
            "discovery_scan_ms": 0,
            "skills_loaded": False,
            "skills_precompiled": False,
            "skills_label": "",
            "interview_skipped": False,
            "generation_attempts": 0,
            "generation_time_s": 0.0,
            "agent_loop_rounds": 0,
            "agent_loop_tool_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

        context: Dict[str, Any] = {}

        # Inherit workspace defaults (domain, provider, owner) if available.
        _apply_workspace_defaults(context, console)

        # Show mode awareness when inside a workspace with implicit mode.
        implicit_mode = bool(get_cli_arg_fn(args, "_implicit_mode", False))
        if implicit_mode and not is_non_interactive and console:
            _print_mode_awareness(console)

        copilot_options = {
            "llm_provider": get_cli_arg_fn(args, "llm_provider"),
            "llm_model": get_cli_arg_fn(args, "llm_model"),
            "llm_endpoint": get_cli_arg_fn(args, "llm_endpoint"),
            "llm_routing_model": get_cli_arg_fn(args, "llm_routing_model"),
            "llm_routing_endpoint": get_cli_arg_fn(args, "llm_routing_endpoint"),
            "tiered": bool(get_cli_arg_fn(args, "tiered", False)),
            "require_llm": bool(get_cli_arg_fn(args, "require_llm", False)),
            "discover": get_cli_arg_fn(args, "discover", True),
            "discovery_path": get_cli_arg_fn(args, "discovery_path"),
            "memory": get_cli_arg_fn(args, "memory", True),
            "save_memory": get_cli_arg_fn(args, "save_memory", False),
            "non_interactive": is_non_interactive,
            "fragment_first": bool(get_cli_arg_fn(args, "fragments", False)),
            "no_fragments": bool(get_cli_arg_fn(args, "no_fragments", False)),
            "no_generate": bool(get_cli_arg_fn(args, "no_generate", False)),
        }

        context_arg = get_cli_arg_fn(args, "context")
        if context_arg:
            try:
                loaded_context = load_context_fn(
                    context_arg,
                    console,
                    context_error_cls=context_error_cls,
                )
                context.update(loaded_context)
                if console:
                    print_dialog_status(console, status="success", message="Loaded extra context.")
            except context_error_cls as exc:
                if console:
                    print_dialog_status(
                        console,
                        status="error",
                        message=f"Couldn't use the context file: {exc}",
                        detail="Continuing without it for now.",
                    )
                else:
                    logger.warning("Context validation failed: %s", exc)

        if get_cli_arg_fn(args, "provider"):
            context["provider"] = get_cli_arg_fn(args, "provider")
        if get_cli_arg_fn(args, "template"):
            context["template"] = get_cli_arg_fn(args, "template")
        if get_cli_arg_fn(args, "domain") and "domain" not in context:
            context["domain"] = get_cli_arg_fn(args, "domain")
        explicit_target_dir = get_cli_arg_fn(args, "target_dir")
        if explicit_target_dir:
            copilot_options["target_dir"] = str(Path(explicit_target_dir).expanduser())

        if not get_cli_arg_fn(args, "non_interactive", False):
            # Load personal memory (per-engineer preferences)
            try:
                from fluid_build.cli.forge_copilot_personal_memory import load_personal_memory

                personal_prefs = load_personal_memory()
                if personal_prefs:
                    # Apply as soft defaults (lower precedence than explicit args).
                    # ``ci_provider`` / ``ci_complexity`` are used later by the
                    # auto-CI hook only in interactive mode.
                    pref_keys = (
                        "preferred_provider",
                        "preferred_engine",
                        "preferred_domain",
                        "owner_team",
                        "preferred_ci_provider",
                        "preferred_ci_complexity",
                    )
                    for key in pref_keys:
                        if personal_prefs.get(key) and key.replace("preferred_", "") not in context:
                            mapped_key = key.replace("preferred_", "")
                            context.setdefault(mapped_key, personal_prefs[key])
                    if console:
                        print_dialog_status(
                            console,
                            status="info",
                            message="Loaded your personal preferences.",
                        )
            except ImportError:
                personal_prefs = None

        force_llm_setup = bool(get_cli_arg_fn(args, "_force_llm_setup", False))
        if not is_non_interactive and (enable_recovery or force_llm_setup):
            needs_setup = force_llm_setup
            readiness_error = None
            if not force_llm_setup:
                readiness = llm_readiness_fn()
                needs_setup = not readiness.ready and readiness.error is not None
                readiness_error = readiness.error if needs_setup else None
            if needs_setup:
                # Synthesise a minimal error to enter the recovery flow.
                if readiness_error is None:
                    readiness_error = CopilotGenerationError(
                        "copilot_llm_setup_needed",
                        "LLM credentials not configured.",
                        suggestions=["Choose a provider and paste a new API key."],
                    )
                recovery_result = _handle_copilot_recovery(
                    args=args,
                    console=console,
                    error=readiness_error,
                    llm_readiness_fn=llm_readiness_fn,
                    route_mode_fn=route_mode_fn,
                    fallback_mode_choices=fallback_mode_choices,
                    ask_dialog_question_fn=ask_dialog_question_fn,
                    ask_secret_text_fn=ask_secret_text_fn,
                )
                if isinstance(recovery_result, int):
                    return recovery_result
                copilot_options.update(recovery_result)

        if not is_non_interactive:
            runtime_inputs = copilot.prepare_runtime_inputs(copilot_options)
            copilot_options.update(runtime_inputs)
            if console:
                llm_cfg = runtime_inputs.get("llm_config")
                if llm_cfg:
                    display = PROVIDER_DISPLAY_NAMES.get(llm_cfg.provider, llm_cfg.provider)
                    plan_suffix = ""
                    if getattr(llm_cfg, "routing_model", None):
                        plan_suffix = f" · routing {llm_cfg.routing_model}"
                    if bool(get_cli_arg_fn(args, "tiered", False)):
                        plan_suffix += " · tiered"
                    console.print(
                        f"[dim]AI: [bold]{display}[/bold] / {llm_cfg.model}  "
                        f"{plan_suffix}  "
                        f"(reset with [bold]fluid ai setup --clear[/bold])[/dim]"
                    )
                    try:
                        run_plan = build_llm_run_plan(
                            llm_cfg,
                            tiered=bool(get_cli_arg_fn(args, "tiered", False)),
                        )
                        logical_stage = next(
                            (
                                stage
                                for stage in run_plan.get("stages", [])
                                if stage.get("stage") == "logical_modeler"
                            ),
                            {},
                        )
                        routing_model = run_plan.get("routing_model")
                        console.print(
                            "[dim]AI plan: interview/self-check use "
                            f"{routing_model}; logical modeling uses "
                            f"{logical_stage.get('model')}; contract + dbt stay deterministic.[/dim]"
                        )
                    except Exception:  # noqa: BLE001
                        pass
                discovery = runtime_inputs.get("discovery_report")
                if discovery:
                    _print_discovery_summary(console, discovery)
                else:
                    _print_discovery_hint(console)
                console.print()
                print_copilot_intro_panel(console)
                console.print(
                    "[dim]I'll help you create the perfect data product by understanding your needs...[/dim]\n"
                )
            capability_warnings = list(runtime_inputs.get("capability_warnings") or [])
            if console and capability_warnings:
                print_dialog_status(
                    console,
                    status="warning",
                    message="Copilot couldn't fully verify some local providers.",
                    detail=(
                        f"{capability_warnings[0]} "
                        "Continuing with best-effort defaults. You can review or override the provider later."
                    ),
                )
            # Show existing data products in workspace before interview
            discovery_report = runtime_inputs["discovery_report"]
            existing_contracts = getattr(discovery_report, "existing_contracts", None) or []
            if existing_contracts and console:
                _show_existing_products(console, existing_contracts)
                # Pass to interview so LLM can detect duplicates
                context["existing_products"] = [
                    {"id": c.get("id", ""), "name": c.get("name", "")} for c in existing_contracts
                ]

            # Resolve preliminary target_dir for early scaffold (samples/ + models/).
            # If --target-dir was provided, use it. Otherwise we'll use a
            # temporary name; the final target_dir is resolved after the
            # interview when we know the project_goal.
            explicit_target = get_cli_arg_fn(args, "target_dir")
            _prelim_target = Path(explicit_target).expanduser() if explicit_target else None

            interview_state = run_adaptive_copilot_interview(
                initial_context=context,
                console=console,
                llm_config=runtime_inputs["llm_config"],
                discovery_report=discovery_report,
                capability_matrix=runtime_inputs["capability_matrix"],
                project_memory=runtime_inputs["project_memory"],
                target_dir=_prelim_target,
                quiet=getattr(args, "quiet", False),
            )
            copilot_options["interview_state"] = interview_state
            context = interview_state.finalize()
            assumptions = list(context.get("assumptions_used") or [])
            if console and assumptions:
                print_assumptions_panel(console, assumptions)
        else:
            for key, value in {
                "project_goal": "Data Analytics Platform",
                "data_sources": "Database tables",
                "use_case": "analytics",
                "complexity": "intermediate",
            }.items():
                context.setdefault(key, value)
            context["interview_summary"] = build_interview_summary_fn(context)

        context = normalize_copilot_context(context)

        # --- Domain auto-detection: load expertise packs transparently ---
        explicit_domain = get_cli_arg_fn(args, "domain", None)

        # BYODA: when --domain points to an unknown agent, offer to scaffold.
        if explicit_domain:
            from fluid_build.cli.forge_agent_specs import (
                AgentSpecError,
                load_user_or_builtin_spec,
                scaffold_user_agent,
            )

            try:
                load_user_or_builtin_spec(explicit_domain)
            except AgentSpecError as spec_err:
                # Show the specific validation error so the user can fix it.
                if console and not is_non_interactive:
                    print_dialog_status(
                        console,
                        status="warning",
                        message=f"Agent spec error for '{explicit_domain}': {spec_err}",
                    )
                else:
                    logger.warning("Invalid agent spec for %s: %s", explicit_domain, spec_err)
                explicit_domain = None
            except FileNotFoundError:
                if console and not is_non_interactive:
                    print_dialog_status(
                        console,
                        status="warning",
                        message=f"No agent found for '{explicit_domain}'.",
                    )
                    from fluid_build.cli.forge_dialogs import ask_confirmation

                    if ask_confirmation(console, "Create a custom domain agent?", default=True):
                        try:
                            path = scaffold_user_agent(explicit_domain)
                            print_dialog_status(
                                console,
                                status="success",
                                message=f"Created {path.relative_to(Path.cwd())}",
                            )
                            console.print(
                                f"  Edit the file to customize questions, rules, and suggestions.\n"
                                f"  Then re-run: [bold]fluid forge --domain {explicit_domain}[/bold]\n"
                            )
                        except Exception as scaffold_err:  # noqa: BLE001
                            print_dialog_status(
                                console,
                                status="error",
                                message=f"Could not create agent: {scaffold_err}",
                            )
                        return 0
                    # User declined — continue without domain enrichment.
                    explicit_domain = None
                else:
                    logger.warning("Unknown domain agent: %s (skipping)", explicit_domain)
                    explicit_domain = None

        if explicit_domain or not context.get("domain_expertise"):
            from fluid_build.cli.forge_domain_enrichment import (
                detect_domain,
                enrich_context_with_domain,
            )

            domain = explicit_domain or detect_domain(context)
            logger.debug("Domain detection: explicit=%s, detected=%s", explicit_domain, domain)
            if domain:
                context = enrich_context_with_domain(context, domain)
                if console:
                    print_dialog_status(
                        console,
                        status="info",
                        message=f"Loaded {domain} domain expertise pack.",
                    )

        # --- Team memory: load shared conventions ---
        try:
            from fluid_build.cli.forge_team_memory import (
                TEAM_MEMORY_FILENAME,
                load_team_memory,
            )
            from fluid_build.util.workspace import find_workspace_root

            ws_root = find_workspace_root(Path.cwd()) or Path.cwd()
            team_memory_path = ws_root / ".fluid" / TEAM_MEMORY_FILENAME
            tm = load_team_memory(ws_root)
            if tm is not None:
                perf_stats["team_memory"] = tm.summary_line()
                if console:
                    print_dialog_status(
                        console,
                        status="info",
                        message=f"Loaded team memory ({tm.summary_line()}).",
                    )
            elif team_memory_path.exists() and console:
                # File exists but failed to parse — show actionable error.
                print_dialog_status(
                    console,
                    status="warning",
                    message=f"Could not parse {TEAM_MEMORY_FILENAME}. Check YAML syntax.",
                )
            elif console and not is_non_interactive:
                print_dialog_status(
                    console,
                    status="info",
                    message="No team memory found. Create .fluid/team-memory.yaml to share conventions.",
                )
        except Exception:  # noqa: BLE001
            pass

        # Slice UX-L: populate perf_stats from what we know so far.
        from fluid_build.cli.forge_copilot_llm_providers import streaming_is_enabled

        _llm_cfg = copilot_options.get("llm_config")
        if _llm_cfg:
            perf_stats["provider"] = getattr(_llm_cfg, "provider", "")
            perf_stats["model"] = getattr(_llm_cfg, "model", "")
            perf_stats["routing_model"] = getattr(_llm_cfg, "routing_model", None)
        perf_stats["streaming"] = streaming_is_enabled()
        # Skills info was already set by _load_industry_skills via context.
        if context.get("compiled_skills"):
            perf_stats["skills_loaded"] = True
            perf_stats["skills_precompiled"] = True
            perf_stats["skills_label"] = context["compiled_skills"].get("industry", "loaded")
        elif context.get("industry_skills"):
            perf_stats["skills_loaded"] = True
            perf_stats["skills_precompiled"] = False
            ind = context["industry_skills"].get("industry", {})
            perf_stats["skills_label"] = ind.get("label", "loaded")
        # Interview skip info was set in context by the interview.
        perf_stats["interview_skipped"] = bool(context.get("_interview_skipped"))

        project_name = context.get("project_goal", "my-data-product").lower().replace(" ", "-")
        target_dir = get_target_directory_fn(args, project_name)
        copilot_options["target_dir"] = str(target_dir)

        # Slice UX-H: default fluid forge is now minimal — only
        # contract.fluid.yaml + .fluid/forge-receipt.json land on disk.
        # The legacy ForgeEngine path (extracts/, loads/, transforms/,
        # config/, docs/, tests/, scripts/, requirements.txt,
        # .env.example, README.md, …) runs only when the user explicitly
        # opts in via --scaffold <template>.
        scaffold_template = get_cli_arg_fn(args, "scaffold", None)
        use_agent_loop = bool(get_cli_arg_fn(args, "agent_loop", False)) or bool(
            os.environ.get("FLUID_COPILOT_AGENT_LOOP")
        )

        if scaffold_template:
            success_result = copilot.create_project(
                target_dir,
                context,
                copilot_options,
                dry_run=bool(get_cli_arg_fn(args, "dry_run", False)),
            )
            if not success_result:
                return 1
        elif use_agent_loop:
            # Slice UX-K: multi-turn agent loop with tool use.
            success_result = _create_project_agent_loop(
                target_dir=target_dir,
                context=context,
                copilot_options=copilot_options,
                copilot=copilot,
                dry_run=bool(get_cli_arg_fn(args, "dry_run", False)),
                logger=logger,
                console=console,
                perf_stats=perf_stats,
            )
            if not success_result:
                return 1
        else:
            success_result = _create_project_minimal(
                copilot=copilot,
                target_dir=target_dir,
                context=context,
                copilot_options=copilot_options,
                dry_run=bool(get_cli_arg_fn(args, "dry_run", False)),
                logger=logger,
                console=console,
            )
            if not success_result:
                return 1

        # Surface provenance from the copilot result so the forge
        # receipt can include it (args is the shared namespace between
        # the mode runner and the receipt writer in forge.py).
        if hasattr(copilot, "_last_provenance"):
            args._copilot_provenance = copilot._last_provenance
            score = copilot._last_provenance.get("self_eval_score")
            if score is not None:
                perf_stats["self_eval_score"] = score

        # Post-generation: create data + dbt scaffolding (slice UX-H:
        # gated on --scaffold so the minimal path leaves an empty
        # product dir except for the contract + receipt).
        if scaffold_template:
            _scaffold_data_folder(target_dir, context, console)

        # Post-generation: auto-scaffold a CI/CD pipeline (optional).
        ci_provider, ci_complexity = _scaffold_ci_pipeline(
            args,
            target_dir,
            context,
            console,
            ask_dialog_question_fn=ask_dialog_question_fn,
            get_cli_arg_fn=get_cli_arg_fn,
            dry_run=bool(get_cli_arg_fn(args, "dry_run", False)),
        )
        if ci_provider:
            context["ci_provider"] = ci_provider
        if ci_complexity:
            context["ci_complexity"] = ci_complexity

        # Save personal memory (per-engineer preferences)
        try:
            from fluid_build.cli.forge_copilot_personal_memory import save_personal_memory

            save_personal_memory(context, console)
        except ImportError:
            pass

        # Slice UX-L: render the performance summary panel.
        perf_stats["generation_time_s"] = round(_time.monotonic() - _run_start, 1)
        try:
            from fluid_build.cli.forge_copilot_llm_providers import get_cumulative_token_usage

            usage = get_cumulative_token_usage()
            perf_stats.update(usage)
        except Exception:  # noqa: BLE001
            pass
        try:
            from fluid_build.cli.forge_ui import print_forge_performance_summary

            print_forge_performance_summary(console, perf_stats)
        except Exception:  # noqa: BLE001 — summary is best-effort
            pass

        return 0
    except KeyboardInterrupt:
        logger.info("AI Copilot cancelled by user")
        return 130
    except CopilotGenerationError as exc:
        logger.exception("AI Copilot mode failed")
        if console:
            console.print(f"[red]❌ AI Copilot failed: {exc.message}[/red]")
            for suggestion in exc.suggestions:
                console.print(f"[dim]• {suggestion}[/dim]")
        else:
            console_error(f"AI Copilot failed: {exc.message}")
            for suggestion in exc.suggestions:
                cprint(f"  • {suggestion}")
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI Copilot mode failed")
        is_key_error = "api_key" in str(exc).lower() or "missing_llm" in str(exc).lower()
        if console:
            console.print(f"[red]AI Copilot failed: {exc}[/red]")
            if is_key_error:
                console.print(
                    "[yellow]Tip: Run 'fluid ai setup' to configure your LLM provider,[/yellow]\n"
                    "[yellow]or use 'fluid forge --blank' / guided mode without AI.[/yellow]"
                )
        return 1


def run_domain_agent_mode(
    args: Any,
    logger: logging.Logger,
    *,
    ai_agents: Mapping[str, type],
    gather_context_fn: Callable[[Any, Any], Dict[str, Any]],
    load_context_fn: Callable[..., Dict[str, Any]],
    get_target_directory_fn: Callable[[Any, str], Path],
    context_error_cls: type[Exception],
    console_factory: Optional[Callable[[], Any]] = Console if RICH_AVAILABLE else None,
) -> int:
    """Run Forge with a specialized domain agent."""
    console = console_factory() if console_factory else None

    try:
        agent_name = args.agent

        if not agent_name:
            if console and not args.non_interactive and Table is not None:
                console.print("\n[bold blue]🎯 Available Domain Agents[/bold blue]")
                table = Table()
                table.add_column("Agent", style="cyan")
                table.add_column("Domain", style="green")
                table.add_column("Description", style="white")
                for name, agent_class in ai_agents.items():
                    agent_instance = agent_class()
                    table.add_row(name, agent_instance.domain, agent_instance.description)
                console.print(table)

                from fluid_build.cli.forge_copilot_interview import InterviewQuestion
                from fluid_build.cli.forge_dialogs import ask_dialog_question

                selection = ask_dialog_question(
                    console,
                    InterviewQuestion(
                        id="agent",
                        field="agent",
                        prompt="Which agent would you like to use?",
                        type="choice",
                        choices=[{"label": name, "value": name} for name in ai_agents.keys()],
                        required=False,
                        allow_skip=True,
                        default="copilot",
                    ),
                )
                agent_name = selection.value or "copilot"
            else:
                agent_name = "copilot"

        if agent_name not in ai_agents:
            if console:
                console.print(f"[red]❌ Unknown agent: {agent_name}[/red]")
                console.print(f"[dim]Available agents: {', '.join(ai_agents.keys())}[/dim]")
            return 1

        agent = ai_agents[agent_name]()

        if console and not args.non_interactive:
            console.print(f"\n[bold blue]🎯 Starting {agent.name.title()} Domain Agent[/bold blue]")
            console.print(f"[dim]{agent.description}[/dim]\n")

        context: Dict[str, Any] = {}
        if args.context:
            try:
                from fluid_build.cli.forge_validation import validate_context_dict

                loaded_context = load_context_fn(
                    args.context,
                    console,
                    context_error_cls=context_error_cls,
                )
                is_valid, error = validate_context_dict(loaded_context)
                if is_valid:
                    context.update(loaded_context)
                    if console:
                        print_dialog_status(
                            console, status="success", message="Loaded extra context."
                        )
                elif console:
                    print_dialog_status(
                        console,
                        status="warning",
                        message=f"Context loaded with a warning: {error}",
                    )
            except context_error_cls as exc:
                if console:
                    print_dialog_status(
                        console,
                        status="error",
                        message=f"Couldn't use the context file: {exc}",
                    )

        if not args.non_interactive:
            context.update(gather_context_fn(agent, console))
        else:
            context = {
                "project_goal": f"{agent.domain.title()} Data Product",
                "data_sources": "Various sources",
                "use_case": "analytics",
                "complexity": "intermediate",
            }

        suggestions = agent.analyze_requirements(context)
        if console and not args.non_interactive:
            console.print("\n[bold green]🤖 Agent Analysis Complete[/bold green]\n")
            console.print(
                f"[cyan]Recommended Template:[/cyan] {suggestions.get('recommended_template')}"
            )
            console.print(
                f"[cyan]Recommended Provider:[/cyan] {suggestions.get('recommended_provider')}"
            )
            if suggestions.get("security_requirements"):
                console.print("\n[yellow]🔒 Security Requirements:[/yellow]")
                for requirement in suggestions["security_requirements"][:3]:
                    console.print(f"  • {requirement}")
            console.print()

        project_name = (
            context.get("project_goal", f"{agent.domain}-data-product").lower().replace(" ", "-")
        )
        from fluid_build.cli.forge_validation import sanitize_project_name

        target_dir = get_target_directory_fn(args, sanitize_project_name(project_name))
        success_result = agent.create_project(target_dir, context)
        return 0 if success_result else 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("Domain agent mode failed")
        if console:
            console.print(f"[red]❌ Domain agent failed: {exc}[/red]")
        return 1


def run_template_mode(
    args: Any,
    logger: logging.Logger,
    *,
    get_target_directory_fn: Callable[[Any, str], Path],
    console_factory: Optional[Callable[[], Any]] = Console if RICH_AVAILABLE else None,
) -> int:
    """Run Forge with traditional template mode."""
    console = console_factory() if console_factory else None

    try:
        if console and not args.non_interactive:
            console.print("\n[bold blue]📋 Template Mode[/bold blue]")
            console.print("[dim]Creating project from template...[/dim]\n")

        from datetime import datetime

        from fluid_build.forge.core.engine import ForgeEngine, GenerationContext
        from fluid_build.forge.core.registry import template_registry

        template_name = args.template or "starter"
        target_dir = get_target_directory_fn(args, f"{template_name}-project")
        provider = args.provider or "local"
        template = template_registry.get(template_name)
        if not template:
            available = template_registry.list_available()
            logger.error(
                "Template '%s' not found. Available templates: %s",
                template_name,
                ", ".join(available),
            )
            return 1

        metadata = template.get_metadata()
        context = GenerationContext(
            project_config={
                "name": target_dir.name,
                "description": f"A {template_name} data product",
                "domain": "analytics",
                "owner": "data-team",
                "provider": provider,
            },
            target_dir=target_dir,
            template_metadata=metadata,
            provider_config={"provider": provider},
            user_selections={},
            forge_version="2.0.0",
            creation_time=datetime.now().isoformat(),
        )

        ForgeEngine()
        logger.info("📝 Generating %s project...", template_name)

        if args.dry_run if hasattr(args, "dry_run") else False:
            logger.info("DRY RUN: Would create project in %s", target_dir)
            logger.info("Template: %s", metadata.name)
            logger.info("Description: %s", metadata.description)
            return 0

        target_dir.mkdir(parents=True, exist_ok=True)
        contract = template.generate_contract(context)
        import yaml

        with open(target_dir / "contract.fluid.yaml", "w") as handle:
            yaml.dump(contract, handle, default_flow_style=False, sort_keys=False)

        for path_str, content in template.generate_structure(context).items():
            if path_str.endswith("/"):
                (target_dir / path_str.rstrip("/")).mkdir(parents=True, exist_ok=True)

        try:
            template._create_readme(target_dir, context)
        except (AttributeError, TypeError):
            pass

        if console:
            console.print(f"[green]✅ Template project created at {target_dir}[/green]")
        else:
            success(f"Template project created at {target_dir}")

        logger.info("\n📖 Next Steps:")
        logger.info("1. cd %s", target_dir)
        logger.info("2. Review contract.fluid.yaml")
        logger.info("3. fluid validate contract.fluid.yaml")
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.exception("Template mode failed")
        if console:
            console.print(f"[red]❌ Template mode failed: {exc}[/red]")
        else:
            console_error(f"Template mode failed: {exc}")
        return 1


def _create_project_agent_loop(
    *,
    target_dir: Path,
    context: Dict[str, Any],
    copilot_options: Dict[str, Any],
    copilot: Any,
    dry_run: bool,
    logger: logging.Logger,
    console: Any,
    perf_stats: Optional[Dict[str, Any]] = None,
) -> bool:
    """Slice UX-K: run the multi-turn agent loop with tool use.

    This is the ``--agent-loop`` path.  Instead of the single-shot
    prompt + repair-retry loop, the LLM calls tools iteratively to
    discover the workspace, pick a template, build and validate the
    contract.  The final result is the same shape as the minimal path
    — ``contract.fluid.yaml`` written via ``write_contract``.
    """
    from fluid_build.cli.forge_contract_factory import write_contract
    from fluid_build.cli.forge_copilot_agent_loop import run_copilot_agent_loop
    from fluid_build.cli.forge_copilot_llm_providers import (
        CopilotGenerationError,
        resolve_llm_config,
    )
    from fluid_build.cli.forge_copilot_runtime import (
        build_capability_matrix,
        discover_local_context,
    )
    from fluid_build.util.workspace import find_workspace_root

    try:
        llm_config = copilot_options.get("llm_config")
        if not llm_config:
            llm_config = resolve_llm_config(
                type("Args", (), copilot_options)(),
                environ=None,
            )

        if console:
            try:
                console.print(
                    "[cyan]Running in agent-loop mode[/cyan] " "[dim](multi-turn tool use)[/dim]\n"
                )
            except Exception:  # noqa: BLE001
                pass

        # SECURITY_REVIEW S-003/S-004: determine the workspace root at
        # the CLI-invoked entry point (not in the agent loop) so it
        # reflects the human operator's intent. find_workspace_root
        # walks up from cwd looking for a fluid project marker; we
        # fall back to cwd if nothing is found.
        ws_root = find_workspace_root(Path.cwd()) or Path.cwd()

        result = run_copilot_agent_loop(
            context=context,
            llm_config=llm_config,
            project_memory=copilot_options.get("project_memory"),
            capability_matrix=copilot_options.get("capability_matrix"),
            console=console,
            perf_stats=perf_stats,
            workspace_root=ws_root,
        )

        contract = result.get("contract")
        if not contract:
            logger.error("Agent loop returned no contract")
            if console:
                try:
                    console.print("[red]Agent loop did not produce a contract.[/red]")
                except Exception:  # noqa: BLE001
                    pass
            return False

        if dry_run:
            if console:
                try:
                    console.print(
                        f"[dim]DRY RUN: would write {target_dir}/contract.fluid.yaml[/dim]"
                    )
                except Exception:  # noqa: BLE001
                    pass
            return True

        target_dir.mkdir(parents=True, exist_ok=True)
        contract_path = target_dir / "contract.fluid.yaml"
        write_contract(contract, contract_path, command="fluid forge --agent-loop")

        if console:
            try:
                console.print(f"\n[green]Wrote[/green] [cyan]{contract_path}[/cyan]")
            except Exception:  # noqa: BLE001
                pass
        return True

    except CopilotGenerationError as exc:
        logger.exception("Agent loop failed")
        if console:
            try:
                console.print(f"[red]{exc.message}[/red]")
                for sug in getattr(exc, "suggestions", []) or []:
                    console.print(f"[dim]{sug}[/dim]")
            except Exception:  # noqa: BLE001
                pass
        return False
    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent loop crashed")
        if console:
            try:
                console.print(f"[red]Agent loop failed: {exc}[/red]")
            except Exception:  # noqa: BLE001
                pass
        return False


def _create_project_minimal(
    *,
    copilot: Any,
    target_dir: Path,
    context: Dict[str, Any],
    copilot_options: Dict[str, Any],
    dry_run: bool,
    logger: logging.Logger,
    console: Any,
) -> bool:
    """Slice UX-H minimal path — run the copilot LLM without ForgeEngine.

    The AI copilot flow still runs the full interview, hits the LLM,
    validates the result, and produces a :class:`CopilotGenerationResult`
    — exactly the same behavior as ``CopilotAgent.create_project``.
    The only difference is where the generated contract lands:

    * Legacy path: ``ForgeEngine`` materialises a full opinionated
      project tree (``extracts/``, ``loads/``, ``transforms/``,
      ``config/``, ``docs/``, ``tests/``, ``scripts/``,
      ``requirements.txt``, ``.env.example``, ``README.md``, …).

    * Minimal path (this function): the generated contract is written
      verbatim to ``<target_dir>/contract.fluid.yaml`` via
      :func:`fluid_build.cli.forge_contract_factory.write_contract`,
      which injects the slice-4 ``metadata.provenance`` envelope.  No
      other files land on disk from here — the outer
      ``_scaffold_ci_pipeline`` call still writes optional CI files,
      and the ``forge.py::run`` caller still writes
      ``.fluid/forge-receipt.json``.

    Returns ``True`` on success, ``False`` on failure.  Never raises —
    errors are logged and a red panel is shown to the user.
    """
    from pydantic import BaseModel

    from fluid_build.cli.forge_contract_factory import write_contract
    from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError

    def _write_ai_work_receipt(generation_result: Any) -> Optional[Path]:
        """Persist a concise, user-facing AI run receipt."""
        ai_run_plan = getattr(generation_result, "ai_run_plan", None)
        provenance = getattr(generation_result, "provenance", None) or {}
        if not ai_run_plan and not provenance:
            return None
        payload = {
            "kind": "ForgeAIWorkReceipt",
            "runPlan": ai_run_plan or provenance.get("ai_run_plan"),
            "agentEvents": list(provenance.get("agent_events") or []),
            "fallbackUsed": bool(provenance.get("fallback_used", False)),
            "fallbackEvents": list(provenance.get("fallback_events") or []),
            "repairUsed": bool(provenance.get("repair_used", False)),
            "repairEvents": list(provenance.get("repair_events") or []),
            "provenance": provenance,
        }
        receipt_path = target_dir / ".fluid" / "ai-work-receipt.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        return receipt_path

    def _serialize_logical_model(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                json.loads(value)
            except Exception:  # noqa: BLE001
                return None
            return value
        if isinstance(value, BaseModel):
            try:
                return value.model_dump_json(indent=2, by_alias=True)
            except Exception:  # noqa: BLE001
                return None
        if isinstance(value, dict):
            try:
                return json.dumps(value, indent=2, sort_keys=True)
            except TypeError:
                return None
        if isinstance(value, list):
            try:
                return json.dumps(value, indent=2)
            except TypeError:
                return None
        return None

    try:
        # Context is already normalized by the caller (run_ai_copilot_mode).
        options = dict(copilot_options or {})
        options.setdefault("target_dir", str(target_dir))

        # Index upstream products so the LLM can emit real dbt SQL with
        # correct source identifiers and join keys. Honors the anchor
        # workspace plus ``FLUID_UPSTREAM_CONTRACTS`` (colon-separated),
        # used for upstream contracts living in a different repository
        # from the current product. Cheap: filesystem walk of <= 4 levels.
        try:
            from fluid_build.util.upstream_discovery import (
                discover_upstream_products,
                project_upstream_for_prompt,
            )

            upstream_raw = discover_upstream_products(target_dir)
            if upstream_raw:
                upstream_projection = project_upstream_for_prompt(upstream_raw)
                if upstream_projection:
                    context["upstream_products"] = upstream_projection
                    logger.debug(
                        "upstream_products_indexed: count=%d",
                        len(upstream_projection),
                    )
        except Exception as exc:  # noqa: BLE001 — discovery is best-effort
            logger.debug("upstream_discovery_failed: %s", exc)

        if dry_run:
            if console:
                try:
                    console.print(
                        f"[dim]DRY RUN: would generate and write {target_dir}/contract.fluid.yaml[/dim]"
                    )
                except Exception:  # noqa: BLE001
                    pass
            logger.info("DRY RUN: would generate minimal AI contract in %s", target_dir)
            return True

        try:
            generation_result = copilot.generate_project_artifacts(context, options)
        except CopilotGenerationError as generation_error:
            recovered_result = copilot._attempt_generation_recovery(
                context=context,
                options=options,
                error=generation_error,
            )
            if recovered_result is None:
                raise
            context = normalize_copilot_context(
                options.get("interview_state").normalized_context
                if options.get("interview_state")
                else context
            )
            generation_result = recovered_result

        suggestions = generation_result.suggestions
        contract = generation_result.contract

        # Stash provenance on the copilot object so the caller can
        # include it in the forge receipt.
        if getattr(generation_result, "provenance", None):
            copilot._last_provenance = generation_result.provenance

        # Optional UI: reuse the copilot's own analysis panel so the
        # minimal path has feature parity with the engine path aside
        # from filesystem output.
        try:
            copilot._show_ai_analysis(context, suggestions, generation_result)
        except Exception as exc:  # noqa: BLE001 — UI must never fail the run
            logger.debug("copilot_show_ai_analysis_failed", extra={"error": str(exc)})

        # Write the LLM-generated contract using the slice-4 envelope
        # writer.  write_contract injects metadata.provenance with the
        # correct 'fluid forge' command string.
        target_dir.mkdir(parents=True, exist_ok=True)
        contract_path = target_dir / "contract.fluid.yaml"
        sidecar_path = target_dir / f"{contract_path.name}.model.json"

        # ── Fragment layout decision ─────────────────────────────
        from fluid_build.cli.forge_contract_fragments import (
            is_complex_enough_for_fragments,
            split_contract_to_fragments,
        )

        force_fragments = options.get("fragment_first", False)
        force_flat = options.get("no_fragments", False)
        existing_fragments = (target_dir / "fragments").is_dir()

        use_fragments = False
        if force_fragments:
            use_fragments = True
        elif force_flat:
            use_fragments = False
        elif existing_fragments:
            use_fragments = True
        else:
            use_fragments = is_complex_enough_for_fragments(contract)

        # ── Transformation engine artifact generation ────────────
        # If the contract has a builds[].engine and we have a registered
        # generator, produce engine artifacts (dbt project, SQL scripts,
        # etc.) and write them alongside the contract.  This runs BEFORE
        # ``write_contract`` because the engine may mutate the contract
        # (e.g. setting ``builds[].repository`` to point at the generated
        # project directory so ``fluid apply --build`` can find it).
        engine_files = _generate_engine_artifacts(
            contract,
            target_dir=target_dir,
            context=context,
            discovery_report=generation_result.discovery_report,
            logger=logger,
            console=console,
            no_generate=options.get("no_generate", False),
        )

        # Schedule generation — produce Airflow DAGs, Dagster pipelines,
        # Prefect flows alongside the contract.
        schedule_files = _generate_schedule_artifacts(
            contract,
            target_dir=target_dir,
            context=context,
            logger=logger,
            console=console,
            no_generate=options.get("no_generate", False),
        )

        if use_fragments:
            root_contract, fragment_files = split_contract_to_fragments(contract)
            write_contract(root_contract, contract_path, command="fluid forge")
            for rel_path, content in fragment_files.items():
                fpath = target_dir / rel_path
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(content, encoding="utf-8")
        else:
            fragment_files = {}
            write_contract(contract, contract_path, command="fluid forge")

        logical_model = getattr(generation_result, "logical_model", None)
        serialized_logical = _serialize_logical_model(logical_model)
        if serialized_logical is not None:
            sidecar_path.write_text(serialized_logical, encoding="utf-8")
            if str(context.get("review_data_model", "")).lower() == "true":
                try:
                    from fluid_build.cli.forge_data_model import review_logical_model

                    reviewed = review_logical_model(
                        sidecar_path,
                        logger,
                        contract_path=contract_path,
                    )
                    if reviewed is not None:
                        sidecar_path.write_text(
                            reviewed.model_dump_json(indent=2, by_alias=True),
                            encoding="utf-8",
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.debug("logical_review_failed: %s", exc)

        # Write additional files (dbt models, SQL, etc.) — these were
        # previously ignored in the minimal path.
        #
        # Merge order matters.  Engine skeletons land first, then
        # schedule files, then the LLM's additional_files — so when the
        # LLM ships a real dbt mart SQL at the same path the engine
        # emitted a TODO skeleton, the LLM content wins.  Infrastructure
        # files the LLM never touches (dbt_project.yml, profiles.yml,
        # sources.yml) still come from the engine.
        additional_files: Dict[str, str] = {}
        additional_files.update(engine_files)
        additional_files.update(schedule_files)
        llm_files = generation_result.additional_files or {}
        if llm_files:
            additional_files.update(llm_files)
        if additional_files:
            for rel_path, content in additional_files.items():
                fpath = target_dir / rel_path
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(content, encoding="utf-8")

        ai_receipt_path = _write_ai_work_receipt(generation_result)

        # Persist project memory the same way the legacy path does, so
        # subsequent forge runs in this product have the full history.
        try:
            copilot._maybe_save_project_memory(
                target_dir=target_dir,
                context=context,
                suggestions=suggestions,
                generation_result=generation_result,
                copilot_options=options,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001 — memory save is best-effort
            logger.debug("copilot_memory_save_failed", extra={"error": str(exc)})

        # ── Tell the user what happened ──────────────────────────
        if console:
            try:
                console.print(f"\n[green]✅ Wrote[/green] [cyan]{contract_path}[/cyan]")
                if fragment_files:
                    console.print(
                        f"[green]   + {len(fragment_files)} fragments under fragments/[/green]"
                    )
                    for rel_path in sorted(fragment_files):
                        console.print(f"[dim]     {rel_path}[/dim]")
                    console.print("\n[bold]📦 Layout: Fragment-first (modular)[/bold]")
                    console.print(
                        "[dim]   Your contract was split into composable fragments under fragments/.[/dim]"
                    )
                    console.print(
                        "[dim]   • fluid bundle        — reassemble into a single contract[/dim]"
                    )
                    console.print(
                        "[dim]   • --no-fragments      — next forge will produce a single file instead[/dim]"
                    )
                elif not force_flat and is_complex_enough_for_fragments(contract):
                    console.print("\n[bold]📦 Layout: Single file[/bold]")
                    console.print(
                        "[dim]   For larger contracts, fragments help with reuse and team collaboration.[/dim]"
                    )
                    console.print(
                        "[dim]   • fluid split         — break into composable fragments[/dim]"
                    )
                    console.print(
                        "[dim]   • --fragments          — next forge will auto-split[/dim]"
                    )
                if additional_files:
                    n = len(additional_files)
                    console.print(f"[green]   + {n} additional file{'s' if n != 1 else ''}[/green]")
                if ai_receipt_path:
                    console.print("[green]   + AI work receipt[/green]")
                if engine_files:
                    console.print(
                        "[dim]   Tip: use 'fluid generate transformation' to re-generate transformations.[/dim]"
                    )
                if schedule_files:
                    console.print(
                        "[dim]   Tip: use 'fluid generate schedule' to re-generate your schedule.[/dim]"
                    )
            except Exception:  # noqa: BLE001
                pass

        if fragment_files:
            context["_authoring_mode"] = "fragment-first"

        return True

    except CopilotGenerationError as exc:
        logger.exception("AI Copilot minimal flow failed")
        if console:
            try:
                console.print(f"[red]❌ {exc.message}[/red]")
                for suggestion in getattr(exc, "suggestions", []) or []:
                    console.print(f"[dim]• {suggestion}[/dim]")
            except Exception:  # noqa: BLE001
                pass
        else:
            console_error(exc.message)
        return False
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        logger.exception("AI Copilot minimal flow crashed")
        if console:
            try:
                console.print(f"[red]❌ Failed to create project: {exc}[/red]")
            except Exception:  # noqa: BLE001
                pass
        else:
            console_error(f"Failed to create project: {exc}")
        return False


def _generate_engine_artifacts(
    contract: Dict[str, Any],
    *,
    target_dir: Path,
    context: Dict[str, Any],
    discovery_report: Any,
    logger: logging.Logger,
    console: Any,
    no_generate: bool = False,
) -> Dict[str, str]:
    """Generate transformation engine artifacts from the contract.

    Returns a dict of {relative_path: content} for files to write under
    target_dir.  Returns empty dict if generation is skipped or no engine
    is available.
    """
    if no_generate:
        return {}

    try:
        from fluid_build.engines import get_engine, has_engine
        from fluid_build.util.contract import get_build_engine, get_builds

        builds = get_builds(contract)
        if not builds:
            return {}

        build = builds[0]
        engine_name = get_build_engine(build) or context.get("build_engine", "")
        if not engine_name:
            return {}

        # Map provider-specific engine names to base engine names
        engine_map = {"dbt-bigquery": "dbt", "dbt-duckdb": "dbt"}
        resolved_name = engine_map.get(engine_name, engine_name)

        if not has_engine(resolved_name):
            if console:
                try:
                    console.print(
                        f"\n[dim]Engine '{engine_name}' is set in your contract. "
                        f"Transformation artifact generation is not yet available for this engine.\n"
                        f"You can write your transformation code manually, or use 'fluid generate' later.[/dim]"
                    )
                except Exception:  # noqa: BLE001
                    pass
            return {}

        engine = get_engine(resolved_name)
        if engine is None:
            return {}

        # Validate
        issues = engine.validate(contract, build)
        errors = [i for i in issues if i.severity.value == "error"]
        if errors:
            if logger:
                for issue in errors:
                    logger.warning("engine_validation: %s", issue)
            return {}

        # Build schema_context from discovery report
        schema_context = None
        if discovery_report and hasattr(discovery_report, "sample_files"):
            schemas = {}
            for sample in discovery_report.sample_files:
                if sample.get("columns"):
                    # Use the filename without extension as key
                    path_str = sample.get("path", "")
                    name = Path(path_str).stem if path_str else "unknown"
                    schemas[name] = {"columns": sample["columns"]}
            if schemas:
                schema_context = {"schemas": schemas}

        # Build transformation intent from domain expertise if available
        transformation_intent = None
        domain_expertise = context.get("domain_expertise", {})
        modeling_standards = domain_expertise.get("data_modeling_standards")
        if modeling_standards:
            from fluid_build.engines.base import TransformationIntent

            transformation_intent = TransformationIntent(
                canonical_model=domain_expertise.get("domain"),
                user_data_model=modeling_standards,
                data_modeling_technique=context.get("data_modeling_technique"),
            )

        # Include user-supplied data models from discovery
        if (
            transformation_intent is None
            and discovery_report
            and hasattr(discovery_report, "user_data_models")
            and discovery_report.user_data_models
        ):
            from fluid_build.engines.base import TransformationIntent

            # Merge all user model schemas into one dict
            merged_cols = {}
            for model in discovery_report.user_data_models:
                merged_cols.update(model.get("columns", {}))
            transformation_intent = TransformationIntent(
                user_data_model=merged_cols,
                data_modeling_technique=context.get("data_modeling_technique"),
            )

        # Even when neither domain expertise nor a user data model is
        # available, we still want the engine to see the modeling
        # technique so it can pick the right skeleton shape in the
        # fallback path.  Build a minimal intent carrying just that.
        if transformation_intent is None and context.get("data_modeling_technique"):
            from fluid_build.engines.base import TransformationIntent

            transformation_intent = TransformationIntent(
                data_modeling_technique=context["data_modeling_technique"],
            )

        # Stamp the chosen technique onto the contract so downstream
        # CLIs (``fluid generate transformation``) can surface it
        # in their banner without re-running the interview. We use the
        # top-level ``labels`` map because FLUID 0.7.2 ``metadata``
        # declares ``additionalProperties: false`` — only ``labels``
        # accepts arbitrary string-valued keys.
        _technique = context.get("data_modeling_technique")
        if _technique:
            labels = contract.setdefault("labels", {})
            labels["dataModelingTechnique"] = _technique

        # Generate artifacts under the repository path (or default).
        # When the build doesn't specify a repository we pick a
        # conventional sub-directory and persist it back on the build so
        # ``fluid apply --build`` can locate the generated project.
        repository_explicit = build.get("repository")
        if repository_explicit:
            repository = repository_explicit
            if repository.startswith("./"):
                repository = repository[2:]
        else:
            repository = f"{resolved_name}_project"
            build["repository"] = f"./{repository}"

        files = engine.generate(
            contract,
            build,
            schema_context=schema_context,
            transformation_intent=transformation_intent,
            workspace_root=target_dir,
        )

        # Prefix all paths with the repository directory
        prefixed = {f"{repository}/{rel_path}": content for rel_path, content in files.items()}

        if console and prefixed:
            try:
                console.print(
                    f"\n[green]Generated {len(prefixed)} transformation files[/green] "
                    f"[dim]({resolved_name} engine → {repository}/)[/dim]"
                )
            except Exception:  # noqa: BLE001
                pass

        return prefixed

    except ImportError:
        # engines module not available — skip silently
        return {}
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.debug("engine_artifact_generation_failed: %s", exc)
        return {}


def _generate_schedule_artifacts(
    contract: Dict[str, Any],
    *,
    target_dir: Path,
    context: Dict[str, Any],
    logger: logging.Logger,
    console: Any,
    no_generate: bool = False,
) -> Dict[str, str]:
    """Generate schedule engine artifacts from the contract.

    Returns a dict of {relative_path: content} for files to write under
    target_dir.  Returns empty dict if generation is skipped or no scheduler
    is available.
    """
    if no_generate:
        return {}

    try:
        from fluid_build.schedulers import get_scheduler, has_scheduler

        # Resolve scheduler name from contract or interview context
        orchestration = contract.get("orchestration", {})
        scheduler_name = orchestration.get("engine") or context.get("schedule_engine", "")
        if not scheduler_name:
            return {}

        # Synthesize orchestration from builds when the contract lacks one.
        # The LLM generates builds (transformations) but not orchestration
        # (scheduling), so we derive tasks from the build steps.
        if not orchestration and contract.get("builds"):
            from fluid_build.schedulers.synthesis import synthesize_orchestration_from_builds

            synthesized = synthesize_orchestration_from_builds(
                contract,
                scheduler_name,
                provider=context.get("provider", ""),
            )
            if synthesized:
                contract = {**contract, "orchestration": synthesized}
                orchestration = synthesized
                if console:
                    try:
                        n = len(synthesized.get("tasks", []))
                        console.print(
                            f"\n[dim]Synthesized {scheduler_name} schedule from "
                            f"{n} build step{'s' if n != 1 else ''}[/dim]"
                        )
                    except Exception:  # noqa: BLE001
                        pass

        # Skip if BYOS path is set (user has their own schedule)
        if context.get("byos_path"):
            if console:
                try:
                    byos = context["byos_path"]
                    console.print(f"\n[green]Using existing schedule:[/green] [dim]{byos}[/dim]")
                except Exception:  # noqa: BLE001
                    pass
            return {}

        if not has_scheduler(scheduler_name):
            if console:
                try:
                    console.print(
                        f"\n[dim]Scheduler '{scheduler_name}' is set in your contract. "
                        f"Schedule artifact generation is not yet available for this scheduler.\n"
                        f"You can write your schedule code manually, or use "
                        f"'fluid generate schedule' later.[/dim]"
                    )
                except Exception:  # noqa: BLE001
                    pass
            return {}

        scheduler = get_scheduler(scheduler_name)
        if scheduler is None:
            return {}

        # Validate
        issues = scheduler.validate(contract)
        errors = [i for i in issues if i.severity.value == "error"]
        if errors:
            if logger:
                for issue in errors:
                    logger.warning("scheduler_validation: %s", issue)
            return {}

        # Resolve provider and config
        provider = contract.get("provider", context.get("provider", ""))
        provider_config: Dict[str, Any] = {}
        metadata = contract.get("metadata", {})
        if provider == "gcp":
            provider_config = {
                "project": metadata.get("gcp_project", "my-project"),
                "region": metadata.get("gcp_region", "us-central1"),
            }
        elif provider == "aws":
            provider_config = {
                "region": metadata.get("aws_region", "us-east-1"),
            }
        elif provider == "snowflake":
            provider_config = {
                "connection_id": metadata.get("snowflake_connection_id", "snowflake_default"),
            }

        # Generate
        files = scheduler.generate(
            contract,
            provider=provider,
            provider_config=provider_config,
        )

        # Prefix all paths with a dags/ directory
        output_dir = {"airflow": "dags", "dagster": "pipelines", "prefect": "flows"}.get(
            scheduler_name, "schedules"
        )
        prefixed = {f"{output_dir}/{rel_path}": content for rel_path, content in files.items()}

        if console and prefixed:
            try:
                console.print(
                    f"\n[green]Generated {len(prefixed)} schedule files[/green] "
                    f"[dim]({scheduler_name} scheduler → {output_dir}/)[/dim]"
                )
            except Exception:  # noqa: BLE001
                pass

        return prefixed

    except ImportError:
        # schedulers module not available — skip silently
        return {}
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.debug("schedule_artifact_generation_failed: %s", exc)
        return {}


def _scaffold_data_folder(target_dir: Path, context: dict, console: Any) -> None:
    """Create data/ and optional dbt/ scaffolding after copilot generation."""
    try:
        # Always create data/ folder with guidance
        data_dir = target_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / ".gitkeep").touch(exist_ok=True)
        readme = data_dir / "README.md"
        if not readme.exists():
            readme.write_text(
                "# Sample Data\n\n"
                "Place sample data files here (CSV, JSON, Parquet).\n"
                "Forge will use them to infer schemas and enrich your contract.\n\n"
                "Re-run `fluid forge` after adding files for richer generation.\n",
                encoding="utf-8",
            )

        # Create dbt scaffolding if data_modeling was requested
        if context.get("data_modeling"):
            dbt_dir = target_dir / "dbt"
            (dbt_dir / "models" / "staging").mkdir(parents=True, exist_ok=True)
            (dbt_dir / "models" / "marts").mkdir(parents=True, exist_ok=True)

            # dbt_project.yml
            project_name = target_dir.name.replace("-", "_")
            dbt_project = dbt_dir / "dbt_project.yml"
            if not dbt_project.exists():
                import yaml as _yaml

                dbt_config = {
                    "name": project_name,
                    "version": "1.0.0",
                    "config-version": 2,
                    "model-paths": ["models"],
                    "target-path": "target",
                    "clean-targets": ["target", "dbt_packages"],
                }
                dbt_project.write_text(
                    _yaml.dump(dbt_config, default_flow_style=False, sort_keys=False),
                    encoding="utf-8",
                )

            if console and RICH_AVAILABLE:
                console.print(
                    "\n[green]Created dbt project scaffolding:[/green]\n"
                    "  dbt/models/staging/  [dim](stg_ source models)[/dim]\n"
                    "  dbt/models/marts/    [dim](fct_ and dim_ tables)[/dim]\n"
                    "  dbt/dbt_project.yml\n\n"
                    "[dim]Add sample data to data/ and re-run forge for richer contracts.[/dim]"
                )
        elif console and RICH_AVAILABLE:
            console.print(
                "\n[dim]Tip: Add sample data to data/ and re-run forge for richer contracts.[/dim]"
            )
    except OSError as exc:
        if console and RICH_AVAILABLE:
            console.print(f"[yellow]Could not create scaffolding: {exc}[/yellow]")


def _show_existing_products(console: Any, existing_contracts: list) -> None:
    """Display a table of data products already in the workspace."""
    if not console or not RICH_AVAILABLE or not existing_contracts:
        return
    try:
        from rich.table import Table as RichTable

        table = RichTable(
            title="Existing Data Products in Workspace",
            border_style="dim",
            show_lines=False,
        )
        table.add_column("#", style="dim", width=3)
        table.add_column("ID", style="cyan")
        table.add_column("Name", style="white")
        table.add_column("Provider", style="green")

        for i, contract in enumerate(existing_contracts[:10], 1):
            providers = ", ".join(contract.get("providers") or ["—"])
            table.add_row(
                str(i),
                contract.get("id", "—"),
                contract.get("name", "—"),
                providers,
            )

        console.print()
        console.print(table)
        console.print(
            f"[dim]{len(existing_contracts)} data product(s) found. "
            "The AI will check for duplicates during the interview.[/dim]\n"
        )
    except ImportError:
        pass


def run_guided_mode(
    args: Any,
    logger: logging.Logger,
    *,
    get_target_directory_fn: Callable[[Any, str], Path],
    console_factory: Optional[Callable[[], Any]] = Console if RICH_AVAILABLE else None,
) -> int:
    """Create a data product via 4 quick interactive prompts (no LLM required)."""
    import os

    console = console_factory() if console_factory else None

    # Guard: guided mode requires interactive stdin
    if not sys.stdin.isatty():
        logger.error("Guided mode requires an interactive terminal")
        if console:
            console.print("[red]Guided mode requires an interactive terminal.[/red]")
        return 1

    try:
        from fluid_build.cli.forge_ui import ask_numbered_choice

        if console and RICH_AVAILABLE:
            from rich.panel import Panel
            from rich.prompt import Prompt

            console.print(
                Panel(
                    "Let's create a new data product in 3 quick steps.\n"
                    "[dim]Press Enter to accept the default for any question.[/dim]",
                    title="Forge — Guided Mode",
                    border_style="cyan",
                )
            )

            # Step 1: Name
            product_id = Prompt.ask(
                "[bold]Step 1/3:[/bold] What do you want to call this data product?",
                default="my-data-product",
            )

            # Step 2: Domain
            domain = ask_numbered_choice(
                console,
                "Step 2/3: What area does this data product belong to?",
                [
                    ("analytics", "Analytics -- dashboards, reports, metrics"),
                    ("data-engineering", "Data Engineering -- pipelines, ETL, transformations"),
                    ("ml", "Machine Learning -- features, models, predictions"),
                    ("governance", "Governance -- data quality, compliance, lineage"),
                ],
                default=1,
            )

            # Step 3: Provider
            provider = ask_numbered_choice(
                console,
                "Step 3/3: Where will this data product run?",
                [
                    ("local", "Local (DuckDB) -- great for getting started"),
                    ("gcp", "Google Cloud (BigQuery)"),
                    ("snowflake", "Snowflake"),
                    ("aws", "AWS (S3 + Glue)"),
                ],
                default=1,
            )

            owner = os.getenv("USER", "data-team")
            description = f"{product_id.replace('-', ' ').title()} data product"
        else:
            product_id = (
                input("Step 1/3 -- Product name [my-data-product]: ").strip() or "my-data-product"
            )

            cprint("\nStep 2/3 -- What area does this belong to?")
            cprint("  1. Analytics")
            cprint("  2. Data Engineering")
            cprint("  3. Machine Learning")
            cprint("  4. Governance")
            d = input("Enter number [1]: ").strip() or "1"
            domain = {"1": "analytics", "2": "data-engineering", "3": "ml", "4": "governance"}.get(
                d, "analytics"
            )

            cprint("\nStep 3/3 -- Where will it run?")
            cprint("  1. Local (DuckDB)")
            cprint("  2. Google Cloud")
            cprint("  3. Snowflake")
            cprint("  4. AWS")
            p = input("Enter number [1]: ").strip() or "1"
            provider = {"1": "local", "2": "gcp", "3": "snowflake", "4": "aws"}.get(p, "local")

            owner = os.getenv("USER", "data-team")
            description = f"{product_id.replace('-', ' ').title()} data product"

        from fluid_build.cli.forge_contract_factory import (
            build_minimal_contract,
            create_and_validate_contract,
        )
        from fluid_build.cli.forge_validation import sanitize_project_name

        safe_id = sanitize_project_name(product_id, strict=False)
        target_dir = get_target_directory_fn(args, safe_id)

        dry_run = getattr(args, "dry_run", False)
        if dry_run:
            if console:
                console.print(f"[dim]DRY RUN: Would create {safe_id} in {target_dir}[/dim]")
            return 0

        engine = "dbt" if provider in ("gcp", "local") else "sql"
        contract = build_minimal_contract(
            product_id=safe_id,
            name=product_id.replace("-", " ").title(),
            domain=domain,
            owner=owner,
            description=description,
            engine=engine,
            tags=["guided"],
        )

        contract_path = create_and_validate_contract(contract, target_dir, logger, console)
        if not contract_path:
            return 1

        # Minimal directory scaffolding
        (target_dir / "config").mkdir(exist_ok=True)
        (target_dir / "docs").mkdir(exist_ok=True)
        if engine == "dbt":
            (target_dir / "dbt" / "models").mkdir(parents=True, exist_ok=True)
        else:
            (target_dir / "sql").mkdir(exist_ok=True)

        from fluid_build.cli.forge_contract_factory import DOCS_URL as _DOCS_URL

        if console and RICH_AVAILABLE:
            from rich.panel import Panel

            console.print(
                Panel(
                    f"[green]Created data product:[/green] [bold]{safe_id}[/bold]\n\n"
                    f"  Contract: {contract_path}\n\n"
                    "Next steps:\n"
                    f"  1. cd {target_dir}\n"
                    "  2. Edit contract.fluid.yaml with your builds\n"
                    "  3. fluid validate contract.fluid.yaml\n"
                    "  4. fluid plan contract.fluid.yaml --out runtime/plan.json\n"
                    "  5. fluid apply runtime/plan.json\n\n"
                    f"[dim]Docs: {_DOCS_URL}[/dim]\n"
                    "[dim]Tip: run 'fluid ai setup' to unlock AI Copilot.[/dim]",
                    title="Forge Complete",
                    border_style="green",
                )
            )
        else:
            success(f"Created data product at {target_dir}")
            cprint(f"Docs: {_DOCS_URL}")

        return 0
    except KeyboardInterrupt:
        logger.info("Guided mode cancelled")
        return 130
    except Exception as exc:  # noqa: BLE001
        logger.exception("Guided mode failed")
        if console:
            console.print(f"[red]Guided mode failed: {exc}[/red]")
        return 1
