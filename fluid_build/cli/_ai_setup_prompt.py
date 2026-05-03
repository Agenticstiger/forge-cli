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

"""Unified "AI not configured" panel — one source of truth for both
``fluid init`` and ``fluid forge`` (Phase 0.5, Gap #6).

Both commands historically rendered their own copy of the same prompt
("AI is not configured — set up an LLM provider"). This module collapses
them: every caller goes through :func:`render` and gets the same wording,
the same call-to-action, and the same doctor hint.

The module is render-only — actual setup logic lives in
:mod:`fluid_build.cli.ai_setup`. Keeping the rendering separate prevents
a circular import (ai_setup itself uses this module to compose its
top panel with consistent copy).
"""

from __future__ import annotations

import logging
from typing import Any

LOG = logging.getLogger(__name__)


def render(
    *,
    reason: str = "missing",
    console: Any = None,
    show_doctor_hint: bool = True,
) -> None:
    """Render the unified AI-not-configured panel.

    *reason*: one of ``missing`` (no key found), ``invalid`` (key found
    but failed validation), ``skipped`` (user previously skipped this
    session), ``rescue`` (post-3-attempt failure rescue).

    *show_doctor_hint*: highlight ``fluid doctor`` as a first-class
    next step (Gap #15 — was previously dim text, now a real call-out).
    """
    body = _body_for_reason(reason)
    title = _title_for_reason(reason)
    border = "yellow" if reason in {"missing", "skipped"} else "red"
    if console is None:
        try:
            from rich.console import Console

            console = Console()
        except Exception:  # noqa: BLE001
            print(f"\n=== {title} ===")
            print(body)
            if show_doctor_hint:
                print("Tip: run `fluid doctor` to diagnose your environment first.")
            return

    try:
        from rich.panel import Panel as _Panel

        if show_doctor_hint:
            body = (
                body + "\n\n" + "[bold]First-time?[/bold] Run [bold cyan]fluid doctor[/bold cyan] "
                "to check Python / credentials / installed CLIs before you start."
            )
        console.print(_Panel(body, title=title, border_style=border))
    except Exception:  # noqa: BLE001 — rich is optional
        print(f"\n=== {title} ===")
        print(body)


def _title_for_reason(reason: str) -> str:
    return {
        "missing": "AI Setup",
        "invalid": "AI key validation failed",
        "skipped": "AI not configured (skipped)",
        "rescue": "AI key validation failed — what next?",
    }.get(reason, "AI Setup")


def _body_for_reason(reason: str) -> str:
    if reason == "invalid":
        return (
            "The API key didn't validate. Common causes:\n"
            "  • Key expired or revoked\n"
            "  • Wrong provider for this key shape\n"
            "  • Network filter blocking the provider's API\n"
        )
    if reason == "skipped":
        return (
            "AI was skipped earlier this session. You can:\n"
            "  • Run [bold cyan]fluid ai setup[/bold cyan] to configure now\n"
            "  • Type [bold]:ai-setup[/bold] inside the interview to resume\n"
            "  • Use [bold]fluid forge --blank[/bold] for a no-AI scaffold\n"
        )
    if reason == "rescue":
        return (
            "Three attempts didn't validate. Pick a different path below "
            "instead of dead-ending — every option keeps your forge run alive."
        )
    # default — "missing"
    return (
        "Forge uses AI to generate your data product.\n\n"
        "Pick a provider on the next screen — or run "
        "[bold cyan]fluid forge --blank[/bold cyan] for a no-AI scaffold.\n\n"
        "Free option: [bold]Google Gemini[/bold] — no credit card, "
        "30-second signup."
    )


__all__ = ["render"]
