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

"""Next-steps panel printer — shared post-success hint for init/forge/demo.

UX audit F5 ("receipts are invisible") and F6 ("error messages don't
teach") have a mirror twin in the happy path: after a successful run,
the user should never have to read docs to find their second command.

Every artifact-producing command (``fluid init``, ``fluid forge``,
``fluid demo``, and future ``fluid bundle --write-lock``) calls
:func:`print_next_steps` at the end of a successful run.  The helper
prints a single Rich panel with a short bulleted list of
copy-pasteable commands tailored to what the user just did.

Contextual content
------------------

The printer accepts a ``context`` enum that picks the step list:

* ``"init"``      — just created a workspace / first product; point at
  ``fluid status`` / ``fluid validate`` / ``fluid forge --ci``.
* ``"forge"``     — just created or refreshed a product; point at
  ``fluid status``, ``fluid validate``, ``fluid plan --env dev``.
* ``"forge-fragments"`` — same as ``forge`` but adds a
  ``fluid bundle --check`` step because the fragment-first layout
  has a lockfile to verify.
* ``"demo"``      — just ran the demo; point at inspecting the
  contract + receipt, then the bridge to ``fluid init my-project``.
* ``"bundle"``    — just ran ``fluid bundle``; point at the bundled
  file location and the ``--check`` mode.

Suppression
-----------

All command parsers gain a shared ``--quiet`` / ``-q`` flag that sets
``args.quiet=True``.  The helper respects that flag — when quiet mode
is active, nothing is printed.  This keeps the panel discoverable
without forcing it on users who script the CLI.
"""

from __future__ import annotations

__all__ = [
    "NEXT_STEPS",
    "NextStep",
    "print_next_steps",
]

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class NextStep:
    """One entry in a next-steps panel — a command plus a one-line hint."""

    command: str
    hint: str


#: Canonical step lists for every context the printer supports.
#:
#: New contexts go here and are picked up automatically by
#: :func:`print_next_steps`.  Commands are ordered by "what you probably
#: want to do next" so a user can copy-paste from the top and make
#: forward progress without thinking.
NEXT_STEPS: Dict[str, Sequence[NextStep]] = {
    "init": (
        NextStep("fluid status", "see what you have"),
        NextStep("fluid validate", "check the contract"),
        NextStep("fluid plan --env dev", "preview a run"),
        NextStep("fluid forge --ci github_actions", "add CI"),
    ),
    "forge": (
        NextStep("fluid status", "see what you have"),
        NextStep("fluid validate", "check the contract"),
        NextStep("fluid plan --env dev", "preview a run"),
    ),
    "forge-fragments": (
        NextStep("fluid status", "see what you have"),
        NextStep("fluid bundle --check", "verify fragment lockfile sync"),
        NextStep("fluid validate", "check the bundled contract"),
        NextStep("fluid plan --env dev", "preview a run"),
    ),
    "demo": (
        NextStep("cat contract.fluid.yaml", "read the contract you just built"),
        NextStep("cat .fluid/init-receipt.json", "see exactly what the demo produced"),
        NextStep("fluid validate", "check the contract is still valid"),
        NextStep("fluid init my-project", "start your own project"),
    ),
    "bundle": (
        NextStep("cat contract.bundled.yaml", "inspect the bundled output"),
        NextStep("fluid bundle --check", "verify nothing has drifted"),
        NextStep("fluid validate", "validate the bundled contract"),
    ),
}


def print_next_steps(
    context: str,
    *,
    console: Any = None,
    args: Any = None,
    quiet: Optional[bool] = None,
) -> None:
    """Print the next-steps panel for *context*.

    Parameters
    ----------
    context:
        Key into :data:`NEXT_STEPS` (``"init"``, ``"forge"``,
        ``"forge-fragments"``, ``"demo"``, ``"bundle"``).  Unknown
        contexts are treated as a no-op — the caller stays working
        even if the printer doesn't have a step list for their flow.
    console:
        Optional Rich Console.  When ``None``, falls back to plain
        ``cprint``.
    args:
        Optional argparse namespace.  When ``args.quiet`` is truthy,
        the panel is suppressed.  This is the normal way callers
        propagate the ``--quiet`` flag without threading an extra
        parameter.
    quiet:
        Explicit suppression override for callers that don't hold an
        ``args`` object (e.g. tests).  Takes precedence over *args*.

    The function never raises — a failure in the Rich renderer falls
    through to plain-text output, and a final except catch-all
    swallows anything else.
    """
    if quiet is None:
        quiet = bool(args is not None and getattr(args, "quiet", False))
    if quiet:
        return

    steps = NEXT_STEPS.get(context)
    if not steps:
        return

    try:
        _render(steps, console=console)
    except Exception:  # noqa: BLE001 — never raise from a hint printer
        try:
            _render_plain(steps)
        except Exception:  # pragma: no cover — defensive
            pass


def _render(steps: Sequence[NextStep], *, console: Any) -> None:
    if console is not None:
        try:
            from rich.panel import Panel
            from rich.table import Table

            table = Table(show_header=False, box=None, padding=(0, 2))
            table.add_column(style="bold bright_cyan", width=40)
            table.add_column(style="dim")

            for step in steps:
                table.add_row(step.command, step.hint)

            console.print()
            console.print(
                Panel(
                    table,
                    title="[bold bright_green]✅ Next steps[/bold bright_green]",
                    border_style="bright_green",
                    padding=(1, 2),
                )
            )
            console.print()
            return
        except Exception:  # noqa: BLE001 — fall through
            pass

    _render_plain(steps)


def _render_plain(steps: Sequence[NextStep]) -> None:
    from fluid_build.cli.console import cprint

    cprint("\nNext steps:")
    for step in steps:
        cprint(f"  {step.command}    # {step.hint}")
    cprint("")
