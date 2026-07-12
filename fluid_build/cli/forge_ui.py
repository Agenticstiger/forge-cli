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

"""Shared Forge user-facing copy and Rich panel helpers."""

from __future__ import annotations

__all__ = [
    "ask_numbered_choice",
    "build_copilot_analysis_text",
    "build_standard_next_steps",
    "print_assumptions_panel",
    "print_copilot_intro_panel",
    "print_copilot_recovery_panel",
    "print_forge_performance_summary",
    "print_free_tier_guide",
    "print_interview_phase",
    "print_welcome_panel",
    "show_copilot_analysis",
    "show_domain_analysis",
    "show_lines_panel",
    "show_next_steps_panel",
]


import os
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

try:
    from rich.panel import Panel

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised through non-Rich fallbacks elsewhere
    Panel = None  # type: ignore[assignment]
    RICH_AVAILABLE = False

FORGE_VALIDATE_COMMAND = "fluid validate contract.fluid.yaml"
FORGE_PLAN_COMMAND = "fluid plan contract.fluid.yaml --out runtime/plan.json"
FORGE_APPLY_COMMAND = "fluid apply runtime/plan.json"
FORGE_MARKET_SEARCH_COMMAND = 'fluid market --search "<keyword>"'
FORGE_DIALOG_HINT = (
    "Answer a few questions about your project using a number, short phrase, or your own wording"
)
FORGE_FLEXIBLE_INPUT_SUMMARY = "numbers, short phrases, and natural-language answers"
FORGE_WORKFLOW_STEPS = [
    "Run fluid forge",
    FORGE_DIALOG_HINT,
    "Copilot discovers local metadata and generates a full contract",
    "Forge validates and repairs the contract if needed",
    "Forge scaffolds only after validation passes",
    "Forge shows how memory influenced the run",
    "Save project-scoped memory only if you explicitly opt in",
]


def _build_panel(renderable: str, *, title: str, border_style: str) -> Any:
    if not RICH_AVAILABLE or Panel is None:
        return renderable
    return Panel(renderable, title=title, border_style=border_style)


def _arrow_keys_enabled(console: Any) -> bool:
    """Return True when an interactive arrow-key menu can be driven safely.

    Belt-and-suspenders, mirroring ``_forge_resume._is_interactive`` — an
    arrow-key menu needs a real bidirectional TTY, Rich for the redraw, and
    the optional ``readchar`` primitive for cross-platform keypress reading.
    Any gap (piped stdin/stdout, CI, pytest capture, missing dep, explicit
    opt-out) falls back to the numbered prompt.

    The ``readchar`` import is deliberately performed *here*, gated behind the
    ``isatty`` checks, so non-interactive paths (``fluid --help``, CI, pipes)
    never pay its import cost — keeping the CLI startup budget untouched.
    """
    if not (console and RICH_AVAILABLE):
        return False
    # Explicit opt-out for users / scripts that prefer typed numbers.
    if os.environ.get("FLUID_FORGE_NO_ARROW_KEYS") or os.environ.get("FLUID_NO_ARROW_KEYS"):
        return False
    # pytest captures stdin (``input()`` raises OSError); CI runners are
    # non-interactive even when isatty lies.
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("CI"):
        return False
    try:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return False
    except Exception:  # noqa: BLE001 - defensive: any stream weirdness → numbered
        return False
    try:
        import readchar  # noqa: F401  (lazy: only in genuine interactive sessions)
    except Exception:  # noqa: BLE001 - dep absent / import error → numbered fallback
        return False
    return True


def _arrow_key_select(
    console: Any,
    prompt: str,
    options: Sequence[tuple],
    default_idx: int,
) -> str:
    """Drive a live arrow-key single-select menu and return the chosen value.

    Navigation: ↑/↓ (and vim ``k``/``j``) move the cursor and wrap; typing a
    digit ``1``–``9`` moves the cursor to that row; Enter accepts the
    highlighted row. Digit-then-Enter preserves the old numbered
    muscle-memory exactly (and, crucially, keeps the trailing Enter from
    leaking to the next prompt). Ctrl-C propagates as ``KeyboardInterrupt``
    for the caller's existing abort handling.

    The caller wraps this in try/except so any terminal error degrades to the
    numbered prompt — this function assumes it is only reached on a real TTY.
    """
    from readchar import key, readkey
    from rich.live import Live
    from rich.markup import escape
    from rich.text import Text

    n = len(options)
    selected = min(max(default_idx, 0), n - 1)

    def _render(final: bool = False) -> Text:
        text = Text()
        text.append(prompt, style="bold")
        text.append("\n")
        for i, (_, label) in enumerate(options):
            if i == selected:
                text.append("❯ ", style="bold cyan")
                text.append(str(label), style="bold cyan")
            else:
                text.append("  ")
                text.append(str(label), style="dim")
            text.append("\n")
        if not final:
            text.append("↑/↓ or number to move · Enter to select", style="dim italic")
        return text

    # ``transient=True`` erases the interactive block on exit; we then print a
    # single collapsed confirmation line (questionary-style) so scrollback
    # stays clean and the record of what was chosen survives.
    with Live(_render(), console=console, auto_refresh=False, transient=True) as live:
        while True:
            keypress = readkey()
            if keypress in (key.UP, "k"):
                selected = (selected - 1) % n
            elif keypress in (key.DOWN, "j"):
                selected = (selected + 1) % n
            elif keypress in (key.ENTER, "\r", "\n"):
                break
            elif keypress == key.CTRL_C:
                raise KeyboardInterrupt
            elif keypress.isdigit():
                jump = int(keypress)
                if 1 <= jump <= n:
                    # Move the cursor only; Enter confirms. This keeps the old
                    # "type a number, then press Enter" muscle-memory intact
                    # and stops the trailing Enter leaking to the next prompt.
                    selected = jump - 1
                else:
                    continue
            else:
                continue
            live.update(_render(), refresh=True)

    value, label = options[selected]
    # Escape interpolated values: a label/prompt containing "[...]" would
    # otherwise be parsed as Rich markup (mis-render, or MarkupError that the
    # caller's except would turn into a spurious re-prompt). Mirrors the
    # navigation render above, which uses Text.append (no markup parsing).
    console.print(
        f"[cyan]❯[/cyan] [bold]{escape(prompt)}[/bold] [green]{escape(str(label))}[/green]"
    )
    return value


def ask_numbered_choice(
    console: Any,
    prompt: str,
    options: Sequence[tuple],
    *,
    default: int = 1,
) -> str:
    """Show an interactive menu and return the selected value.

    *options* is a sequence of ``(value, label)`` tuples.

    On a real TTY (with Rich + ``readchar``), renders an arrow-key selectable
    menu: ↑/↓ to move, Enter to accept, or type a number to jump. Everywhere
    else — piped/redirected I/O, CI, pytest, no Rich, ``readchar`` missing, or
    ``FLUID_FORGE_NO_ARROW_KEYS=1`` — it degrades to a numbered prompt where the
    user types a 1-based number (Enter accepts the default). The return
    contract is identical across both paths.

    Example::

        choice = ask_numbered_choice(
            console,
            "Where will this data product run?",
            [("local", "Local (DuckDB) -- great for getting started"),
             ("gcp", "Google Cloud (BigQuery)"),
             ("snowflake", "Snowflake"),
             ("aws", "AWS (S3 + Glue)")],
        )
    """
    if not options:
        return ""

    # Preferred path: live arrow-key navigation on a real terminal. Any
    # terminal/readchar failure (small window, raw-mode error, odd TERM)
    # degrades to the numbered prompt below. Ctrl-C propagates untouched.
    if _arrow_keys_enabled(console):
        try:
            return _arrow_key_select(console, prompt, options, default - 1)
        except KeyboardInterrupt:
            raise
        except Exception:  # noqa: BLE001 - never let a menu render kill the flow
            pass

    lines = []
    for i, (_, label) in enumerate(options, 1):
        marker = " [bold cyan](default)[/bold cyan]" if i == default and RICH_AVAILABLE else ""
        prefix = f"  [bold cyan]{i}[/bold cyan]" if RICH_AVAILABLE else f"  {i}"
        lines.append(f"{prefix}. {label}{marker}")
    menu_text = "\n".join(lines)

    if console and RICH_AVAILABLE:
        console.print(f"\n[bold]{prompt}[/bold]")
        console.print(menu_text)

        try:
            from rich.prompt import Prompt

            raw = Prompt.ask(
                "Enter number",
                default=str(default),
                show_default=True,
            )
        except (ImportError, EOFError):
            raw = str(default)
    else:
        # Rich isn't available — drop to plain stdout so the menu is still
        # usable. Bare ``print`` is intentional here; routing through
        # ``cprint`` would just add a redundant import hop for the
        # no-Rich path. See CODE_REVIEW C-011.
        print(f"\n{prompt}")  # noqa: T201 — no-Rich fallback
        for i, (_, label) in enumerate(options, 1):
            marker = " (default)" if i == default else ""
            print(f"  {i}. {label}{marker}")  # noqa: T201 — no-Rich fallback
        raw = input(f"Enter number [{default}]: ").strip() or str(default)

    try:
        idx = int(raw) - 1
        if 0 <= idx < len(options):
            return options[idx][0]
    except (ValueError, IndexError):
        pass

    # If input doesn't parse as a number, try matching by value or label
    raw_lower = raw.strip().lower()
    for value, label in options:
        if raw_lower == value.lower() or raw_lower == label.lower():
            return value

    # Fall back to default
    return options[default - 1][0]


def show_lines_panel(
    console: Any,
    lines: Sequence[str],
    *,
    title: str,
    border_style: str,
) -> None:
    """Print a simple multi-line panel when Rich is available."""
    if not console or not RICH_AVAILABLE or not lines:
        return
    console.print(_build_panel("\n".join(lines), title=title, border_style=border_style))


def print_welcome_panel(console: Any) -> None:
    """Render the interactive mode chooser welcome panel."""
    if not console or not RICH_AVAILABLE:
        return
    welcome_text = (
        "🔨 **Forge** — Add a data product to this workspace\n\n"
        "AI Copilot will interview you and generate a validated contract.\n"
        "[dim]Use [bold]--blank[/bold] to skip AI and start from an empty contract.[/dim]\n\n"
        "[dim]Power-user options: [bold]fluid forge --help[/bold] — LLM config, "
        "memory, discovery flags.[/dim]"
    )
    console.print(_build_panel(welcome_text, title="FLUID Forge", border_style="blue"))


def print_interview_phase(
    console: Any,
    *,
    phase: int,
    total: int,
    label: str,
) -> None:
    """Render a compact phase breadcrumb during the adaptive copilot interview.

    The interview is adaptive (variable number of rounds) so instead of a
    strict step counter this just marks which phase we're in, giving the user
    a sense of progress without overpromising a specific number of questions.
    """
    if not console:
        return
    breadcrumb = f"─── Phase {phase}/{total}: {label} ───"
    if RICH_AVAILABLE:
        console.print(f"\n[dim cyan]{breadcrumb}[/dim cyan]\n")
    else:
        console.print(f"\n{breadcrumb}\n")


def print_copilot_intro_panel(console: Any) -> None:
    """Render the concise intro used when copilot is ready to start."""
    if not console or not RICH_AVAILABLE:
        return
    intro_text = """
🤖 **AI Copilot** is ready

Here's what happens next:
  1. I'll ask about your goal and data sources
  2. Suggest a template and output format
  3. Generate a validated contract.fluid.yaml
  4. Scaffold the project files

[dim]Answer with a number, short phrase, or natural language.
Type 'skip' or 'not sure' — I'll pick sensible defaults.[/dim]
    """.strip()
    console.print(_build_panel(intro_text, title="Starting AI Copilot", border_style="blue"))


def print_copilot_recovery_panel(
    console: Any,
    *,
    message: str,
    suggestions: Sequence[str],
) -> None:
    """Render the onboarding recovery panel when copilot prerequisites are missing."""
    if not console or not RICH_AVAILABLE:
        return
    lines = [
        "🤖 **AI Copilot** can't start yet",
        "",
        message,
    ]
    if suggestions:
        lines.extend(["", "Helpful next steps:"])
        lines.extend(f"• {item}" for item in suggestions[:4])
    console.print(
        _build_panel("\n".join(lines), title="Copilot Setup Needed", border_style="yellow")
    )


def print_free_tier_guide(console: Any) -> None:
    """Show links to free LLM API key providers."""
    if not console or not RICH_AVAILABLE:
        return
    lines = [
        "[bold]No API key? Here's how to get one free:[/bold]",
        "",
        "  [cyan]Google AI Studio[/cyan]  Free Gemini key (15 req/min)",
        "  https://aistudio.google.com/apikey",
        "",
        "  [cyan]OpenRouter[/cyan]        Free models available",
        "  https://openrouter.ai/keys",
        "",
        "  [cyan]Ollama[/cyan]            Run models locally (no key needed)",
        "  https://ollama.com/download",
    ]
    show_lines_panel(console, lines, title="Free AI Options", border_style="blue")


def print_assumptions_panel(console: Any, assumptions: Sequence[str]) -> None:
    """Render the bounded assumptions summary shown after interviews."""
    if not console or not RICH_AVAILABLE or not assumptions:
        return
    assumption_lines = "\n".join(f"• {item}" for item in assumptions[:4])
    console.print(_build_panel(assumption_lines, title="📝 Assumptions Used", border_style="cyan"))


def build_copilot_analysis_text(
    *,
    context: Mapping[str, Any],
    suggestions: Mapping[str, Any],
    use_case_label: str,
    memory_lines: Sequence[str],
) -> str:
    """Build the copilot analysis panel body."""
    lines = [
        f"🎯 **Project Goal:** {context.get('project_goal', 'Not specified')}",
        f"📊 **Data Sources:** {context.get('data_sources', 'Not specified')}",
        f"🏗️ **Use Case:** {use_case_label}",
        f"⚙️ **Complexity:** {context.get('complexity', 'intermediate')}",
        "",
        "🤖 **AI Recommendations:**",
        f"• **Template:** {suggestions['recommended_template']}",
        f"• **Provider:** {suggestions['recommended_provider']}",
        "• **Patterns:** "
        + (", ".join(suggestions.get("recommended_patterns", [])) or "Standard patterns"),
        "",
        "💡 **Architecture Suggestions:**",
    ]

    for suggestion in suggestions.get("architecture_suggestions", []):
        lines.append(f"• {suggestion}")

    best_practices = list(suggestions.get("best_practices", []) or [])
    if best_practices:
        lines.extend(["", "✨ **Best Practices:**"])
        lines.extend(f"• {practice}" for practice in best_practices)

    assumptions = list(context.get("assumptions_used") or [])
    if assumptions:
        lines.extend(["", "📝 **Assumptions Used:**"])
        lines.extend(f"• {item}" for item in assumptions[:4])

    if memory_lines:
        lines.extend(["", "🧠 **Project Memory Guidance:**"])
        lines.extend(f"• {line}" for line in memory_lines)

    return "\n".join(lines)


def show_copilot_analysis(
    console: Any,
    *,
    context: Mapping[str, Any],
    suggestions: Mapping[str, Any],
    use_case_label: str,
    memory_lines: Sequence[str],
) -> None:
    """Render the copilot AI analysis panel."""
    if not console or not RICH_AVAILABLE:
        return
    text = build_copilot_analysis_text(
        context=context,
        suggestions=suggestions,
        use_case_label=use_case_label,
        memory_lines=memory_lines,
    )
    console.print(_build_panel(text.strip(), title="🧠 AI Analysis", border_style="blue"))


def build_domain_analysis_text(
    *,
    goal: str,
    data_sources: str,
    product_type: str,
    suggestions: Mapping[str, Any],
    domain: str,
) -> str:
    """Build the shared domain-agent analysis body."""
    patterns = ", ".join(suggestions.get("recommended_patterns", []) or ["Standard scaffolding"])
    return (
        f"🎯 **Project Goal:** {goal}\n"
        f"📊 **Data Sources:** {data_sources}\n"
        f"🏷️ **Domain Focus:** {product_type}\n\n"
        "🤖 **Recommendations:**\n"
        f"• Template: {suggestions.get('recommended_template')}\n"
        f"• Provider: {suggestions.get('recommended_provider')}\n"
        f"• Patterns: {patterns}\n\n"
        f"[dim]Optimized for {domain} workflows and guardrails.[/dim]"
    )


def show_domain_analysis(
    console: Any,
    *,
    goal: str,
    data_sources: str,
    product_type: str,
    suggestions: Mapping[str, Any],
    domain: str,
) -> None:
    """Render the domain-agent analysis panel."""
    if not console or not RICH_AVAILABLE:
        return
    console.print(
        _build_panel(
            build_domain_analysis_text(
                goal=goal,
                data_sources=data_sources,
                product_type=product_type,
                suggestions=suggestions,
                domain=domain,
            ).strip(),
            title="🧠 AI Analysis",
            border_style="blue",
        )
    )


def build_standard_next_steps(
    *,
    target_dir: Optional[Path] = None,
    provider: str = "local",
) -> str:
    """Build the shared official-command next-steps text."""
    lines: list[str] = []
    if target_dir:
        contract_path = target_dir / "contract.fluid.yaml"
        lines.extend(
            [
                f"[bold]Project folder:[/bold]  {target_dir}",
                f"[bold]Contract file:[/bold]   {contract_path}",
                "",
                "[bold]Next steps:[/bold]",
                f"  cd {target_dir}",
                f"  {FORGE_VALIDATE_COMMAND}",
            ]
        )
    else:
        lines.extend(
            [
                "[bold]Next steps:[/bold]",
                f"  {FORGE_VALIDATE_COMMAND}",
            ]
        )
    return "\n".join(lines)


def show_next_steps_panel(
    console: Any,
    *,
    target_dir: Optional[Path] = None,
    provider: str = "local",
) -> None:
    """Render the shared next-steps panel."""
    if not console or not RICH_AVAILABLE:
        return
    text = build_standard_next_steps(
        target_dir=target_dir,
        provider=provider,
    )
    console.print(_build_panel(text.strip(), title="Forge Complete", border_style="green"))


# ---------------------------------------------------------------------------
# Slice UX-L: post-generation performance summary
# ---------------------------------------------------------------------------

# Provider display names for the summary panel.
_PROVIDER_LABELS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic (Claude)",
    "gemini": "Google Gemini",
    "ollama": "Ollama (local)",
}


def print_forge_performance_summary(
    console: Any,
    stats: Mapping[str, Any],
) -> None:
    """Render a compact performance summary after contract generation.

    *stats* is a plain dict populated during ``run_ai_copilot_mode``
    with keys matching the ``perf_stats`` accumulator from the plan.
    Missing keys are tolerated — every line degrades gracefully.
    """
    if not console or not RICH_AVAILABLE:
        return

    lines: list[str] = []

    # Provider + model
    provider = stats.get("provider", "")
    model = stats.get("model", "")
    label = _PROVIDER_LABELS.get(provider, provider)
    if label and model:
        lines.append(f"  [bold]Provider[/bold]    {label} / {model}")

    # Mode (agent-loop vs single-shot)
    if stats.get("agent_loop_rounds"):
        rounds = stats["agent_loop_rounds"]
        tool_calls = stats.get("agent_loop_tool_calls", 0)
        lines.append(
            f"  [bold]Mode[/bold]        agent-loop ({rounds} rounds, {tool_calls} tool calls)"
        )
    else:
        # Streaming
        streaming = stats.get("streaming")
        if streaming is not None:
            lines.append(f"  [bold]Streaming[/bold]   {'on' if streaming else 'off'}")

    # Discovery
    discovery_files = stats.get("discovery_files", 0)
    cache_hit = stats.get("discovery_cache_hit", False)
    if discovery_files:
        cache_label = "cache hit" if cache_hit else "fresh scan"
        scan_ms = stats.get("discovery_scan_ms", 0)
        time_part = f", {scan_ms}ms" if scan_ms and not cache_hit else ""
        lines.append(
            f"  [bold]Discovery[/bold]   {cache_label} ({discovery_files} files{time_part})"
        )

    # Skills
    skills_loaded = stats.get("skills_loaded", False)
    if skills_loaded:
        skills_label = stats.get("skills_label", "loaded")
        precompiled = stats.get("skills_precompiled", False)
        compile_hint = "precompiled" if precompiled else "on-the-fly"
        lines.append(f"  [bold]Skills[/bold]      {skills_label} ({compile_hint})")
    else:
        lines.append("  [bold]Skills[/bold]      not installed")

    # Team memory
    team_memory = stats.get("team_memory")
    if team_memory:
        lines.append(f"  [bold]Team[/bold]        {team_memory}")

    # Interview
    interview_skipped = stats.get("interview_skipped", False)
    if interview_skipped:
        lines.append("  [bold]Interview[/bold]  skipped (sufficient context)")

    # Routing
    routing_model = stats.get("routing_model")
    if routing_model and routing_model != model:
        lines.append(f"  [bold]Routing[/bold]    interview -> {routing_model}")

    # Generation
    gen_time = stats.get("generation_time_s", 0)
    gen_attempts = stats.get("generation_attempts", 0)
    if gen_time > 0:
        if stats.get("agent_loop_rounds"):
            lines.append(f"  [bold]Time[/bold]        {gen_time:.1f}s")
        elif gen_attempts > 1:
            lines.append(f"  [bold]Generation[/bold]  {gen_attempts} attempts, {gen_time:.1f}s")
        else:
            lines.append(f"  [bold]Generation[/bold]  {gen_time:.1f}s")

    # Quality score (self-evaluation)
    self_eval = stats.get("self_eval_score")
    if self_eval is not None:
        color = "green" if self_eval >= 7 else "yellow" if self_eval >= 5 else "red"
        lines.append(f"  [bold]Quality[/bold]     [{color}]{self_eval}/10[/{color}]")

    # Token usage
    total_tokens = stats.get("total_tokens", 0)
    if total_tokens > 0:
        input_t = stats.get("input_tokens", 0)
        output_t = stats.get("output_tokens", 0)
        lines.append(
            f"  [bold]Tokens[/bold]      {input_t:,} in / {output_t:,} out ({total_tokens:,} total)"
        )

    # Tips (contextual suggestions)
    tips = []
    if not stats.get("skills_loaded"):
        tips.append("Run [bold]fluid skills install <industry>[/bold] for domain-aware contracts")
    if not stats.get("streaming") and not stats.get("agent_loop_rounds"):
        tips.append("Set [bold]FLUID_LLM_STREAMING=1[/bold] to see tokens as they arrive")

    if tips:
        lines.append("")
        for tip in tips[:2]:
            lines.append(f"  [dim]Tip: {tip}[/dim]")

    if not lines:
        return

    console.print(
        _build_panel(
            "\n".join(lines),
            title="Performance",
            border_style="dim",
        )
    )
