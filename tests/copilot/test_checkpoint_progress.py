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

"""StageProgressFormatter — plain-text contract pins.

Tests assert against the no-rich rendering path so plain text is the
deterministic contract. The "saved $X" trust-builder must appear ONLY
on cached lines. The footer's cost split must render in the exact
"cached $X | this session $Y" layout."""

from __future__ import annotations

import pytest

from fluid_build.copilot.checkpoint_progress import StageProgressFormatter

# ---------------------------------------------------------------------
# Plain-text determinism
# ---------------------------------------------------------------------


def test_resume_header_plain_text_is_deterministic():
    f = StageProgressFormatter(use_rich=False)
    out = f.render_resume_header(run_id="abc123", age_str="4m")
    assert out == "Resuming run abc123 (paused 4m ago)"


def test_stage_line_cached_carries_saved_usd():
    f = StageProgressFormatter(use_rich=False)
    out = f.render_stage_line("logical", "cached", index=1, total=8, saved_usd=0.0123)
    assert "cached" in out
    assert "saved $0.0123" in out
    assert "1/8" in out
    assert "logical" in out
    # The cached icon is the green check.
    assert "✓" in out


def test_stage_line_pending_does_not_carry_saved_usd():
    f = StageProgressFormatter(use_rich=False)
    # Even if the caller passes a value (defensive: the formatter is the
    # single source of truth for the suffix).
    out = f.render_stage_line("builder", "pending", index=3, total=8, saved_usd=0.0500)
    assert "saved" not in out
    assert "pending" in out
    assert "·" in out


def test_stage_line_running_does_not_carry_saved_usd():
    f = StageProgressFormatter(use_rich=False)
    out = f.render_stage_line("contract_forge", "running", index=2, total=8, saved_usd=0.05)
    assert "saved" not in out
    assert "running" in out
    assert "→" in out


def test_stage_line_failed_does_not_carry_saved_usd():
    f = StageProgressFormatter(use_rich=False)
    out = f.render_stage_line("readme", "failed", index=4, total=8, saved_usd=0.10, elapsed_s=12.3)
    assert "saved" not in out
    assert "failed" in out
    assert "✗" in out
    # Elapsed surfaces on failed lines so the operator sees how long
    # the failed stage ran.
    assert "12.3s" in out


def test_stage_line_running_with_elapsed():
    f = StageProgressFormatter(use_rich=False)
    out = f.render_stage_line("validator", "running", index=6, total=8, elapsed_s=4.2)
    assert "4.2s" in out
    assert "running" in out


# ---------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------


def test_summary_footer_carries_both_cost_components():
    f = StageProgressFormatter(use_rich=False)
    out = f.render_summary_footer(completed=7, total=8, cached_cost=0.0431, session_cost=0.0089)
    assert "7/8 stages complete" in out
    assert "cached $0.0431" in out
    assert "this session $0.0089" in out


def test_summary_footer_renders_zero_split_cleanly():
    f = StageProgressFormatter(use_rich=False)
    out = f.render_summary_footer(completed=8, total=8, cached_cost=0.0, session_cost=0.0)
    assert "8/8 stages complete" in out
    assert "cached $0.0000" in out
    assert "this session $0.0000" in out


# ---------------------------------------------------------------------
# Rich-vs-plain symmetry
# ---------------------------------------------------------------------


def test_rich_render_does_not_break_when_rich_missing(monkeypatch):
    """Even with ``use_rich=True``, when rich isn't importable the
    formatter must fall back to plain text. The constructor checks
    importability — we simulate that by patching the check."""
    import fluid_build.copilot.checkpoint_progress as mod

    monkeypatch.setattr(mod, "_rich_available", lambda: False)
    f = StageProgressFormatter(use_rich=True)
    out = f.render_stage_line("logical", "cached", index=1, total=8, saved_usd=0.01)
    # No rich markup should leak through.
    assert "[dim green]" not in out
    assert "[bright_blue]" not in out
    assert "saved $0.0100" in out


def test_rich_render_emits_markup_when_rich_present(monkeypatch):
    """Mirror of the previous test — when rich IS importable, plain
    text gets wrapped in style tags."""
    import fluid_build.copilot.checkpoint_progress as mod

    monkeypatch.setattr(mod, "_rich_available", lambda: True)
    f = StageProgressFormatter(use_rich=True)
    out = f.render_stage_line("logical", "cached", index=1, total=8, saved_usd=0.01)
    # Style markup is present.
    assert "[dim green]" in out
    assert "saved $0.0100" in out


# ---------------------------------------------------------------------
# Sanity — all four status values are exercised
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,icon",
    [
        ("cached", "✓"),
        ("running", "→"),
        ("pending", "·"),
        ("failed", "✗"),
    ],
)
def test_each_status_uses_distinct_icon(status, icon):
    f = StageProgressFormatter(use_rich=False)
    out = f.render_stage_line("logical", status, index=1, total=8, saved_usd=0.01, elapsed_s=1.0)
    assert icon in out
