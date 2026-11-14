# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Shared Forge user-facing copy and Rich panel helpers."""

from __future__ import annotations

__all__ = [
    "ask_numbered_choice",
    "print_forge_performance_summary",
    "print_interview_phase",
    "print_welcome_panel",
]


import os
import sys
from typing import Any, Mapping, Sequence

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


def print_welcome_panel(console: Any) -> None:
    """Render the interactive mode chooser welcome panel."""
    if not console or not RICH_AVAILABLE:
        return
    welcome_text = (
        "🔨 **Forge** — Add a data product to this workspace\n\n"
        "Your coding agent, or one LLM call, writes the contract with the FLUID skills.\n"
        "[dim]Use [bold]--blank[/bold] to skip AI and start from an empty contract.[/dim]\n\n"
        "[dim]Power-user options: [bold]fluid forge --help[/bold] (LLM and "
        "coding-agent flags).[/dim]"
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
