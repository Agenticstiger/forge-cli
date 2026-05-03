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

"""Slash commands for the forge interview loop (Phase 0.5 #19, #3 partial).

The interview runs free-text Q&A. Without slash commands, a user who
typed ``skip`` for AI setup at the start has no in-loop way to change
their mind — they'd have to abort the run, re-launch ``fluid forge``,
and start over.

This module adds a small, opinionated set of in-loop commands:

* ``:ai-setup`` — re-run AI setup mid-interview (Gap #19)
* ``:override`` — interrupt the agent and redirect (Gap #3 partial)
* ``:show-work`` — toggle live streaming on/off (Gap WC5)
* ``:doctor`` — surface ``fluid doctor`` without leaving the run
* ``:help`` — list the commands above
* ``:quit`` — abort gracefully

The commands are detected by :func:`maybe_handle_slash_command` —
return value indicates whether the command was handled (caller should
re-prompt the user) or the input is a real answer.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

LOG = logging.getLogger(__name__)


@dataclass
class SlashCommand:
    """One row in the slash-command catalog."""

    name: str
    description: str
    handler: Callable[..., None]


def _cmd_ai_setup(console: Any, **_kwargs: Any) -> None:
    """Re-run AI setup mid-interview (Gap #19)."""
    try:
        from fluid_build.cli.ai_setup import run_ai_setup_inline

        config = run_ai_setup_inline(console)
        if config:
            console.print(f"[green]✓ AI re-configured: {config.provider} / {config.model}[/green]")
        else:
            console.print("[yellow]AI setup didn't complete. Continuing in current mode.[/yellow]")
    except Exception as exc:  # noqa: BLE001
        LOG.debug("ai_setup_slash_failed: %s", exc)
        if console:
            console.print(f"[red]:ai-setup failed: {exc}[/red]")


def _cmd_show_work(console: Any, *, state: Any = None, **_kwargs: Any) -> None:
    """Toggle live streaming of agent reasoning + tool calls (Gap WC5)."""
    if state is None or not hasattr(state, "options"):
        if console:
            console.print(
                "[dim]:show-work toggles streaming once the agent loop is "
                "running — try it again after this question.[/dim]"
            )
        return
    current = bool(getattr(state, "show_work", False))
    new_value = not current
    state.show_work = new_value
    if console:
        if new_value:
            console.print("[green]✓ --show-work ON — reasoning will stream.[/green]")
        else:
            console.print("[dim]--show-work OFF.[/dim]")


def _cmd_doctor(console: Any, **_kwargs: Any) -> None:
    """Run ``fluid doctor`` inline (Gap #15 — promote to first-class)."""
    if console:
        console.print(
            "[bold]Running fluid doctor in-line… "
            "[dim](pretty-print of environment readiness)[/dim][/bold]"
        )
    try:
        from argparse import Namespace

        from fluid_build.cli.doctor import run as run_doctor

        run_doctor(Namespace(extended=False, features_only=False), LOG)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("doctor_slash_failed: %s", exc)
        if console:
            console.print(
                f"[yellow]Couldn't run doctor inline ({exc}); try "
                "[bold]fluid doctor[/bold] in another terminal.[/yellow]"
            )


def _cmd_override(console: Any, *, state: Any = None, **_kwargs: Any) -> None:
    """Interrupt the agent loop and redirect (Gap #3 partial).

    Surfaces a small redirect dialog: switch engine, restart with
    new context, or export current state. The runtime watches
    ``state.override_action`` and acts on it at the next agent-loop
    boundary.
    """
    try:
        from fluid_build.cli.forge_ui import ask_numbered_choice

        action = ask_numbered_choice(
            console,
            "Redirect this run:",
            [
                ("switch_engine", "Try a different engine (e.g. Snowflake instead of BigQuery)"),
                ("restart_context", "Restart with new context (re-enter the interview)"),
                ("export_state", "Export current state and quit"),
                ("cancel", "Never mind, keep going"),
            ],
            default=4,
        )
    except Exception:  # noqa: BLE001
        action = "cancel"
    if state is not None:
        state.override_action = action
    if console and action != "cancel":
        console.print(f"[cyan]Override queued: {action}.[/cyan]")


def _cmd_help(console: Any, **_kwargs: Any) -> None:
    """List the available slash commands."""
    if not console:
        return
    try:
        from rich.table import Table

        table = Table.grid(padding=(0, 2))
        for cmd in _CATALOG.values():
            table.add_row(f"[bold]:{cmd.name}[/bold]", cmd.description)
        console.print(table)
    except Exception:  # noqa: BLE001
        for cmd in _CATALOG.values():
            print(f"  :{cmd.name}  —  {cmd.description}")


def _cmd_quit(console: Any, *, state: Any = None, **_kwargs: Any) -> None:
    """Abort the interview gracefully."""
    if state is not None:
        state.override_action = "quit"
    if console:
        console.print("[yellow]Quitting interview. Run fluid forge again to retry.[/yellow]")
    raise KeyboardInterrupt(":quit")


_CATALOG: Dict[str, SlashCommand] = {
    "ai-setup": SlashCommand(
        name="ai-setup",
        description="Re-run AI setup (handy if you skipped earlier or want to switch providers).",
        handler=_cmd_ai_setup,
    ),
    "override": SlashCommand(
        name="override",
        description="Interrupt the agent loop — switch engine, restart, or export state.",
        handler=_cmd_override,
    ),
    "show-work": SlashCommand(
        name="show-work",
        description="Toggle live streaming of agent reasoning + tool calls.",
        handler=_cmd_show_work,
    ),
    "doctor": SlashCommand(
        name="doctor",
        description="Inline run of fluid doctor — environment readiness check.",
        handler=_cmd_doctor,
    ),
    "help": SlashCommand(
        name="help",
        description="Show this list.",
        handler=_cmd_help,
    ),
    "quit": SlashCommand(
        name="quit",
        description="Abort the interview gracefully.",
        handler=_cmd_quit,
    ),
}


def is_slash_command(text: str) -> bool:
    """Return True for any string that looks like a slash command."""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    return stripped.startswith(":") and len(stripped) > 1


def maybe_handle_slash_command(
    text: str,
    *,
    console: Any = None,
    state: Any = None,
) -> bool:
    """Detect + dispatch a slash command. Returns True iff handled.

    The handler runs synchronously; on return the caller should
    re-prompt the user (or abort if the command raised
    ``KeyboardInterrupt``).
    """
    if not is_slash_command(text):
        return False
    raw = text.strip().lstrip(":")
    parts: List[str]
    try:
        parts = shlex.split(raw)
    except ValueError:
        parts = raw.split()
    name = parts[0].lower() if parts else ""
    cmd = _CATALOG.get(name)
    if cmd is None:
        if console:
            console.print(f"[yellow]Unknown command: :{name}.  Type :help to list.[/yellow]")
        return True
    cmd.handler(console=console, state=state, args=parts[1:])
    return True


__all__ = [
    "SlashCommand",
    "is_slash_command",
    "maybe_handle_slash_command",
]
