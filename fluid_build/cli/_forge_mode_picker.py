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

"""``fluid forge`` mode picker — show the user every authoring path
and let them choose, instead of dropping straight into AI mode.

The picker only fires when:

* No mode flag is on the command line (``--blank`` / ``--refine`` /
  ``--from-product`` / ``--from-product-list`` /
  ``-y/--yes`` / ``--non-interactive``).
* stdin is a TTY (interactive run).
* The user hasn't passed ``--data-product-type`` (which signals they
  already know what they want).
* The return-user threshold isn't tripped — repeat users go straight
  through (the welcome scan suppresses for them already).

What the picker does:

1. Reads the welcome scan findings (workspace state, existing
   contract, return-user flag).
2. Pre-highlights the most likely mode:

   * Existing ``contract.fluid.yaml`` in cwd → ``refine``.
   * Existing products in workspace + user is going to author an
     ADP/CDP → ``from_product``.
   * Otherwise → ``ai`` (the default, AI copilot).

3. Renders a menu of every authoring path with a short description.
4. Sets the appropriate flag on ``args`` so the existing dispatch in
   ``forge.py::run`` picks the choice up unchanged. This keeps the
   blast radius of the picker tiny — no new dispatch path, just
   pre-population of an existing one.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mode definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForgeMode:
    """One row in the picker — what the user sees + what we set on args."""

    key: str
    title: str
    description: str
    sets_args: tuple  # ((attr, value), ...) — applied when the user picks this row

    @property
    def label(self) -> str:
        return self.title


_MODES: tuple = (
    ForgeMode(
        key="ai",
        title="🧠 AI Copilot",
        description=(
            "Describe your product in plain English; the LLM proposes the "
            "contract, builds, schema, and tests. Best for new SDP/ADP/CDP."
        ),
        sets_args=(),
    ),
    ForgeMode(
        key="from_product",
        title="🔗 Compose from existing products",
        description=(
            "Author an ADP or CDP that joins / aggregates upstream products "
            "in this workspace. Surfaces a catalog of existing products to "
            "pick from. Composition rules enforced (--from-product)."
        ),
        sets_args=(("_pick_from_product", True),),
    ),
    ForgeMode(
        key="refine",
        title="✏️  Refine an existing contract",
        description=(
            "Iterate on a contract you already have. Reads the prior run's "
            ".fluid/agents/<run-id>/ artifacts and asks the LLM what to "
            "change (--refine)."
        ),
        sets_args=(("refine", "contract.fluid.yaml"),),
    ),
    ForgeMode(
        key="template",
        title="📋 Template-based",
        description=(
            "Start from a known pattern (analytics, etl, ml-pipeline, ...). "
            "AI fills in the gaps but the structure is fixed."
        ),
        sets_args=(),  # routes to AI mode with template hint
    ),
    ForgeMode(
        key="blank",
        title="🧱 Blank scaffold",
        description=(
            "An empty contract with the right metadata. No AI call. For "
            "power users who want to author by hand."
        ),
        sets_args=(("blank", True),),
    ),
)


def _key_to_mode(key: str) -> Optional[ForgeMode]:
    for m in _MODES:
        if m.key == key:
            return m
    return None


# ---------------------------------------------------------------------------
# Detection — which mode to pre-highlight?
# ---------------------------------------------------------------------------


def _detect_default_mode(*, target_dir: Path, findings: Any) -> str:
    """Pick the most-likely mode from environmental signals."""
    if findings is not None and getattr(findings, "has_contract_in_cwd", False):
        return "refine"
    # An existing workspace with multiple products → composition is likely.
    if findings is not None and getattr(findings, "existing_products", 0) >= 1:
        return "from_product"
    return "ai"


# ---------------------------------------------------------------------------
# Trigger conditions
# ---------------------------------------------------------------------------


def should_show_picker(args: Any) -> bool:
    """Return True only when the picker should run.

    * Skip when any mode flag was already passed.
    * Skip in non-interactive / no-TTY runs.
    * Skip when ``FLUID_FORGE_NO_PICKER=1`` (testing / CI).
    """
    import os

    if os.environ.get("FLUID_FORGE_NO_PICKER"):
        return False
    if getattr(args, "non_interactive", False):
        return False
    if getattr(args, "blank", False):
        return False
    if getattr(args, "refine", None):
        return False
    if list(getattr(args, "from_product", []) or []):
        return False
    if getattr(args, "from_product_list", None):
        return False
    if getattr(args, "no_llm", False) or getattr(args, "deterministic", False):
        return False
    try:
        if not sys.stdin.isatty():
            return False
    except Exception:  # noqa: BLE001
        return False
    return True


# ---------------------------------------------------------------------------
# Renderer + prompt
# ---------------------------------------------------------------------------


def _render_picker(
    *,
    default_key: str,
    findings: Any,
    console: Any,
) -> None:
    """Render the picker panel — Rich preferred, plain fallback."""
    try:
        from rich.panel import Panel
        from rich.table import Table

        table = Table.grid(padding=(0, 2))
        table.add_column(justify="right", style="dim cyan")
        table.add_column()
        for idx, mode in enumerate(_MODES, 1):
            marker = "[bold green]→[/bold green]" if mode.key == default_key else " "
            table.add_row(
                f"{marker} [bold]{idx}[/bold])",
                f"[bold]{mode.title}[/bold]\n[dim]{mode.description}[/dim]",
            )
        subtitle = ""
        if findings is not None and getattr(findings, "scan_duration_ms", 0):
            subtitle = (
                f"detected in {findings.scan_duration_ms}ms · "
                f"{getattr(findings, 'existing_products', 0)} existing products · "
                f"{'in workspace' if getattr(findings, 'in_workspace', False) else 'no workspace'}"
            )
        console.print(
            Panel(
                table,
                title="[bold]How would you like to forge?[/bold]",
                subtitle=f"[dim]{subtitle}[/dim]" if subtitle else "",
                border_style="cyan",
            )
        )
    except Exception:  # noqa: BLE001
        print("\n=== fluid forge — pick a mode ===")
        for idx, mode in enumerate(_MODES, 1):
            marker = "→" if mode.key == default_key else " "
            print(f" {marker} {idx}) {mode.title}")
            print(f"     {mode.description}")


def _prompt_choice(default_key: str, *, input_fn: Any = None) -> str:
    """Read 1..N from the user; returns the chosen key.

    Pressing Enter accepts the default. Invalid input re-prompts up to
    3 times before falling back to the default.
    """
    fn = input_fn or input
    default_idx = next((i for i, m in enumerate(_MODES, 1) if m.key == default_key), 1)
    for _ in range(3):
        try:
            raw = fn(f"Choose [1-{len(_MODES)}, default {default_idx}]: ").strip()
        except (KeyboardInterrupt, EOFError):
            return default_key
        except OSError:
            return default_key
        if not raw:
            return default_key
        try:
            n = int(raw)
        except ValueError:
            print(f"Pick a number between 1 and {len(_MODES)}.")
            continue
        if 1 <= n <= len(_MODES):
            return _MODES[n - 1].key
        print(f"Pick a number between 1 and {len(_MODES)}.")
    return default_key


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def pick_mode(
    args: Any,
    *,
    console: Any = None,
    input_fn: Any = None,
    target_dir: Optional[Path] = None,
) -> str:
    """Run the picker and apply the user's choice to ``args``.

    Returns the chosen mode key. Side-effect: ``args`` is mutated to
    carry the selected mode flag (e.g. ``args.blank=True`` for blank,
    ``args.refine="contract.fluid.yaml"`` for refine).

    Caller is responsible for not invoking this when
    :func:`should_show_picker` returns False.
    """
    target = (target_dir or Path.cwd()).resolve()

    # Best-effort welcome scan to drive the default mode + subtitle
    # context. Scan is bounded; if it fails the picker still runs.
    findings = None
    try:
        from fluid_build.cli._welcome_scan import run_welcome_scan

        findings = run_welcome_scan(start=target)
    except Exception:  # noqa: BLE001
        LOG.debug("forge_mode_picker_welcome_scan_failed", exc_info=True)

    # The picker ALWAYS shows for interactive users — skipping it
    # silently was the original UX bug. Return-user state only changes
    # the *default* selection (so a user who's done 50 forges can hit
    # Enter to land on AI without re-reading the menu); the menu still
    # renders so every alternative path stays visible.
    default = _detect_default_mode(target_dir=target, findings=findings)

    if console is None:
        try:
            from rich.console import Console

            console = Console()
        except Exception:  # noqa: BLE001
            console = None
    if console is not None:
        _render_picker(default_key=default, findings=findings, console=console)
    else:
        print("\n=== fluid forge — pick a mode ===")
        for idx, mode in enumerate(_MODES, 1):
            marker = "→" if mode.key == default else " "
            print(f" {marker} {idx}) {mode.title}")

    chosen_key = _prompt_choice(default, input_fn=input_fn)
    chosen = _key_to_mode(chosen_key)
    if chosen is None:
        return "ai"

    for attr, value in chosen.sets_args:
        setattr(args, attr, value)
    return chosen.key


__all__ = ["ForgeMode", "pick_mode", "should_show_picker"]
