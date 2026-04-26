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

"""Friendly fallback rendering for parsers that require a subcommand.

Any CLI command that has ``add_subparsers(..., required=True)`` shows a
bare-bones argparse error when the user runs the command without
picking a subcommand::

    usage: fluid forge data-model [-h] {from-ddl,from-intent,...} ...
    fluid forge data-model: error: the following arguments are required: data_model_action

That's not world-class UX.  Instead, callers can flip ``required=False``
and dispatch through :func:`render_subcommand_guide` to render a Rich
panel that:

* lists every subcommand with a one-line description;
* highlights the highest-leverage path with a "Recommended:" tag based
  on a caller-supplied :class:`SubcommandHint` heuristic (e.g. "I see
  ``intent.yaml`` in the cwd → prefer ``from-intent``");
* shows a "Quick start" example so the operator can paste the exact
  next command without scrolling docs.

The result is the same shape of guidance the AI copilot interview
gives, but without requiring an LLM round-trip — purely deterministic
context detection from the cwd and a small heuristics layer the caller
provides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from fluid_build.cli.console import cprint


@dataclass
class SubcommandEntry:
    """One row in the subcommand guide panel.

    Attributes
    ----------
    name:
        The subcommand verb as the user types it (``"from-intent"``).
    description:
        A short single-sentence summary, suitable for a CLI cell.
    example:
        Optional copy-pasteable example command.  When present, the
        guide renders it under the description as a dim grey line.
    """

    name: str
    description: str
    example: Optional[str] = None


@dataclass
class SubcommandHint:
    """Optional context-aware recommendation.

    Callers compute this from the cwd / args / config and pass it to
    :func:`render_subcommand_guide`.  When set, the matching
    :class:`SubcommandEntry` is rendered with a leading "Recommended:"
    tag and the ``rationale`` is shown beneath the panel.

    The detector is a callable so the caller can defer expensive
    filesystem walks until the guide is actually requested.
    """

    subcommand: str
    rationale: str


@dataclass
class SubcommandGuide:
    """Full payload for :func:`render_subcommand_guide`."""

    command_path: str
    """The qualified command the user typed, e.g. ``"fluid forge data-model"``."""

    headline: str
    """One-line summary of what the command does, shown above the menu."""

    entries: List[SubcommandEntry] = field(default_factory=list)
    """The subcommand rows, in display order."""

    hint_provider: Optional[Callable[[], Optional[SubcommandHint]]] = None
    """Optional zero-arg callable that returns a :class:`SubcommandHint`
    or ``None``.  Lazily invoked once the guide is rendered."""

    quick_start: Optional[str] = None
    """Optional copy-pasteable line shown at the bottom under
    ``Try this first:`` — typically a sensible default invocation."""


def render_subcommand_guide(guide: SubcommandGuide) -> int:
    """Render ``guide`` to the console and return an exit code.

    Returns ``0`` (success — no error condition; the user just hasn't
    chosen a subcommand yet) so a CI script that pipes the output
    through ``less`` or captures it for help-tooling doesn't see a
    spurious failure.  Bash-driven flows that DO want a non-zero exit
    on missing subcommand can pass ``--require-llm``-style flags via
    the parent command and the caller can decide.
    """

    width = 78
    cprint("")
    cprint(f"[bold cyan]{guide.command_path}[/bold cyan]")
    cprint(f"[dim]{guide.headline}[/dim]")
    cprint(f"[dim]{'─' * width}[/dim]")

    hint = guide.hint_provider() if guide.hint_provider else None
    recommended = (hint.subcommand if hint else "").strip()

    name_width = max((len(e.name) for e in guide.entries), default=0)
    for entry in guide.entries:
        is_recommended = entry.name == recommended
        marker = "★" if is_recommended else " "
        name_cell = entry.name.ljust(name_width)
        if is_recommended:
            cprint(
                f"  [bold yellow]{marker}[/bold yellow] "
                f"[bold]{name_cell}[/bold]  {entry.description}"
            )
        else:
            cprint(f"  {marker} [bold]{name_cell}[/bold]  {entry.description}")
        if entry.example:
            cprint(f"     [dim]{entry.example}[/dim]")

    cprint(f"[dim]{'─' * width}[/dim]")

    if hint:
        cprint(f"[yellow]Recommended:[/yellow] {hint.rationale}")
    if guide.quick_start:
        cprint(f"[bold]Try this first:[/bold] [green]{guide.quick_start}[/green]")
    cprint("")
    return 0


def hint_from_first_match(
    candidates: List[tuple[bool, str, str]],
) -> Optional[SubcommandHint]:
    """Return the first ``(matched, subcommand, rationale)`` whose
    ``matched`` flag is True.  Convenience for callers that want a
    declarative "first hit wins" detection ladder.

    Example::

        hint = hint_from_first_match([
            (Path("intent.yaml").is_file(), "from-intent",
             "found intent.yaml in cwd"),
            (any(Path(".").glob("*.sql")),  "from-ddl",
             "found .sql files in cwd"),
        ])
    """

    for matched, subcommand, rationale in candidates:
        if matched:
            return SubcommandHint(subcommand=subcommand, rationale=rationale)
    return None


__all__ = [
    "SubcommandEntry",
    "SubcommandGuide",
    "SubcommandHint",
    "hint_from_first_match",
    "render_subcommand_guide",
]
