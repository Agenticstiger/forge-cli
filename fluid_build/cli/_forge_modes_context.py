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

"""``fluid forge`` context-prep helpers — physical extraction.

Lifted from ``cli/forge_modes.py`` (host file was 1576 LOC). 292
LOC of pre-LLM context preparation:

* :func:`_choose_recovery_mode` / :func:`_handle_copilot_recovery` —
  surface a discovery + recovery menu after a copilot failure.
* :func:`_print_discovery_summary` / :func:`_print_mode_awareness` /
  :func:`_print_discovery_hint` — Rich-flavoured console UI.
* :func:`_load_industry_skills` — lazy load industry-pack skills
  into the LLM context.
* :func:`_apply_workspace_defaults` — pull workspace-config defaults
  into the interview context.

``forge_modes.py`` re-imports each at module top so existing call
sites keep resolving.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from fluid_build.cli.console import cprint, success
from fluid_build.cli.console import error as console_error
from fluid_build.cli.forge_copilot_interview import InterviewQuestion
from fluid_build.cli.forge_copilot_llm_providers import CopilotGenerationError

# Indirection: tests patch
# ``fluid_build.cli.forge_modes.<helper>`` so we resolve through the
# host module at call time. Local fallback imports are kept for
# bare-name compatibility during the bootstrapping window.
from fluid_build.cli.forge_dialogs import (
    ask_confirmation as _fallback_ask_confirmation,
)
from fluid_build.cli.forge_dialogs import (
    ask_dialog_question as _fallback_ask_dialog_question,
)
from fluid_build.cli.forge_dialogs import (
    print_dialog_status as _fallback_print_dialog_status,
)
from fluid_build.cli.forge_ui import (
    print_copilot_recovery_panel as _fallback_print_copilot_recovery_panel,
)
from fluid_build.cli.forge_ui import (
    print_free_tier_guide as _fallback_print_free_tier_guide,
)
from fluid_build.cli.forge_ui import (
    print_welcome_panel as _fallback_print_welcome_panel,
)


def _fm_attr(name: str, default):
    """Resolve ``name`` via the host ``cli.forge_modes`` module so
    test patches on ``forge_modes.<name>`` flow through to bare-name
    references inside this module. Returns ``default`` when the
    host hasn't (yet) bound a value."""
    from fluid_build.cli import forge_modes as _fm

    return getattr(_fm, name, default)


def ask_confirmation(*args, **kwargs):
    return _fm_attr("ask_confirmation", _fallback_ask_confirmation)(*args, **kwargs)


def ask_dialog_question(*args, **kwargs):
    return _fm_attr("ask_dialog_question", _fallback_ask_dialog_question)(*args, **kwargs)


def print_dialog_status(*args, **kwargs):
    return _fm_attr("print_dialog_status", _fallback_print_dialog_status)(*args, **kwargs)


def print_copilot_recovery_panel(*args, **kwargs):
    return _fm_attr("print_copilot_recovery_panel", _fallback_print_copilot_recovery_panel)(
        *args, **kwargs
    )


def print_free_tier_guide(*args, **kwargs):
    return _fm_attr("print_free_tier_guide", _fallback_print_free_tier_guide)(*args, **kwargs)


def print_welcome_panel(*args, **kwargs):
    return _fm_attr("print_welcome_panel", _fallback_print_welcome_panel)(*args, **kwargs)


def _create_session_llm_config(*args, **kwargs):
    """Forward to the host's ``_create_session_llm_config`` so the
    recovery flow can build a session-only ``LlmConfig`` after
    inline AI setup. Resolved at call time so test patches on the
    host module flow through."""
    from fluid_build.cli import forge_modes as _fm

    return _fm._create_session_llm_config(*args, **kwargs)


from fluid_build.cli.workspace_config import load_workspace_config


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


# CI/CD auto-scaffolder — physically extracted to
# ``cli/_forge_ci_scaffolder.py``. The constants + functions are
# re-exported here under the same names so existing imports keep
# resolving.
from fluid_build.cli._forge_ci_scaffolder import (  # noqa: E402
    _CI_COMPLEXITY_CHOICES,
    _CI_COMPLEXITY_VALUES,
    _CI_PROVIDER_ALIASES,
    _CI_PROVIDER_CHOICES,
    _CI_PROVIDER_VALUES,
)
from fluid_build.cli._forge_ci_scaffolder import (
    ci_killswitch_enabled as _ci_killswitch_enabled,
)
from fluid_build.cli._forge_ci_scaffolder import (
    normalize_ci_provider as _normalize_ci_provider,
)
from fluid_build.cli._forge_ci_scaffolder import (
    prompt_ci_menu as _prompt_ci_menu,
)
from fluid_build.cli._forge_ci_scaffolder import (
    resolve_ci_choice as _resolve_ci_choice,
)
from fluid_build.cli._forge_ci_scaffolder import (
    scaffold_ci_pipeline as _legacy_scaffold_ci_pipeline,
)
