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

"""Shared stage-progress formatter.

Single rendering surface for the staged forge pipeline's stage
list. Used by:

* ``fluid forge`` live output (during resume, to show "skipping
  cached stage", "running stage", "pending").
* ``fluid agents show`` (post-hoc summary).
* Future TUI / web UI (one formatter, many consumers).

Borrows the colour palette + icon set from
``cli/_preview_panel.py::render`` and falls back to plain text when
``rich`` isn't available. Plain-text mode is the deterministic
contract — tests assert against the no-rich render and don't have
to mock the terminal.
"""

from __future__ import annotations

from typing import List, Literal, Optional

Status = Literal["cached", "running", "pending", "failed"]


# Icons + colour markup. Rich-style markup is stripped automatically
# in the plain-text path below.
_ICON_CACHED = "✓"  # ✓
_ICON_RUNNING = "→"  # →
_ICON_PENDING = "·"  # ·
_ICON_FAILED = "✗"  # ✗

_RICH_STYLE = {
    "cached": ("dim green", _ICON_CACHED),
    "running": ("bright_blue", _ICON_RUNNING),
    "pending": ("dim", _ICON_PENDING),
    "failed": ("red", _ICON_FAILED),
}


def _rich_available() -> bool:
    try:
        import rich  # noqa: F401

        return True
    except ImportError:
        return False


class StageProgressFormatter:
    """Render stage progress consistently across surfaces.

    Methods return strings rather than printing — the caller (CLI,
    TUI, log file) picks the sink. Plain-text mode is the
    deterministic default for tests; rich-mode is selected
    automatically when rich is importable and ``use_rich=True``
    (the default).
    """

    def __init__(self, *, use_rich: bool = True) -> None:
        # ``use_rich`` is the constructor knob; the actual selection
        # is the AND of the knob and rich's importability so a caller
        # who passes ``use_rich=True`` on a system without rich gets
        # the plain-text fallback automatically.
        self._use_rich = bool(use_rich) and _rich_available()

    # ----- public API -------------------------------------------------

    def render_resume_header(self, run_id: str, age_str: str) -> str:
        """Render the panel header shown when a resume is detected."""
        line = f"Resuming run {run_id} (paused {age_str} ago)"
        if self._use_rich:
            return f"[bold]{line}[/bold]"
        return line

    def render_stage_line(
        self,
        stage: str,
        status: Status,
        *,
        index: int,
        total: int,
        saved_usd: Optional[float] = None,
        elapsed_s: Optional[float] = None,
    ) -> str:
        """Render one stage line.

        ``saved_usd`` is only stamped on cached lines (the trust-
        builder — operators see exactly how much money the resume
        saved them); pending/running/failed lines suppress it even
        if the caller passes a value, so the formatter is the
        single source of truth.
        ``elapsed_s`` appends a duration suffix on running / failed
        / cached lines when present.
        """
        style, icon = _RICH_STYLE.get(status, ("dim", _ICON_PENDING))
        prefix = f"[{index}/{total}]"
        # Suffix construction. Trailing whitespace is stripped at
        # the end so the line is byte-deterministic.
        suffix_parts: List[str] = []
        if status == "cached" and saved_usd is not None:
            suffix_parts.append(f"saved ${saved_usd:.4f}")
        if elapsed_s is not None and status in {"cached", "running", "failed"}:
            suffix_parts.append(f"{elapsed_s:.1f}s")
        suffix = ("  " + "  ".join(suffix_parts)) if suffix_parts else ""

        body = f"{prefix} {icon} {stage:<16} ({status}){suffix}".rstrip()
        if self._use_rich:
            return f"[{style}]{body}[/{style}]"
        return body

    def render_summary_footer(
        self,
        *,
        completed: int,
        total: int,
        cached_cost: float,
        session_cost: float,
    ) -> str:
        """Render the post-run summary footer.

        Layout::

            ─────────────────────────────────────────────
              7/8 stages complete  |  cached $0.0431  |  this session $0.0089

        ``cached_cost`` is the sum of all skipped-stage cost_usd
        records (money the operator didn't have to pay again).
        ``session_cost`` is what THIS invocation spent (delta of
        the run-cost tracker after the resume).
        """
        ratio = f"{completed}/{total} stages complete"
        cached = f"cached ${cached_cost:.4f}"
        session = f"this session ${session_cost:.4f}"
        body = f"  {ratio}  |  {cached}  |  {session}"
        if self._use_rich:
            return f"[bold]{body}[/bold]"
        return body


__all__ = ["StageProgressFormatter", "Status"]
