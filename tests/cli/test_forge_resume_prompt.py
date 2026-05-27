# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the auto-resume prompt UX in ``fluid forge``.

The prompt is the central UX surface for the resumability work — these
tests pin the exact behaviors the spec calls out:

* TTY + paused run + Enter → continue (returns the run-id)
* TTY + paused run + 'f' → fresh (returns None)
* TTY + paused run + '?' → show details, then re-prompt
* Non-TTY + no env → None (fresh, predictable for CI)
* Non-TTY + FLUID_FORGE_AUTO_RESUME=1 → most-recent
"""

from __future__ import annotations

import argparse
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fluid_build.cli import _forge_resume


def _build_paused_run(workspace: Path, *, age_minutes: int = 12) -> str:
    ts = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).strftime("%Y%m%d-%H%M%S")
    run_id = f"{ts}-paused01"
    run_dir = workspace / ".fluid" / "agents" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "cost.json").write_text(
        json.dumps(
            {
                "total_usd": 0.04,
                "total_tokens": 600,
                "provider": "anthropic",
                "model": "claude",
            }
        )
    )
    (run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "status": "paused",
                "stages_completed": 3,
                "stages_total": 7,
                "last_stage": "builder",
            }
        )
    )
    (run_dir / ".paused").write_text(
        json.dumps({"current_stage": 4, "stages_total": 7, "last_stage": "builder"})
    )
    return run_id


@pytest.fixture
def workspace_with_paused(tmp_path: Path) -> Path:
    _build_paused_run(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Prompt string shape — pin the approved wording.
# ---------------------------------------------------------------------------


def test_prompt_string_contains_approved_wording(workspace_with_paused: Path):
    """The prompt must contain the spec-approved wording."""
    runs = list(_forge_resume._find_resumable(workspace_with_paused))
    assert len(runs) == 1
    prompt = _forge_resume._build_prompt_string(runs[0])
    # Spec-approved core wording:
    assert "Found paused run" in prompt
    assert "stage" in prompt
    assert "[C/f/?]" in prompt
    # And the brand-icon pause symbol.
    assert "⏸" in prompt


def test_prompt_includes_stage_and_cost(workspace_with_paused: Path):
    runs = list(_forge_resume._find_resumable(workspace_with_paused))
    prompt = _forge_resume._build_prompt_string(runs[0])
    assert "3/7" in prompt
    assert "$0.04" in prompt
    assert "builder" in prompt


# ---------------------------------------------------------------------------
# TTY behavior — Enter / f / ?
# ---------------------------------------------------------------------------


def test_tty_enter_returns_run_id(workspace_with_paused: Path, monkeypatch):
    """Pressing Enter (empty answer) → continue, returns the run-id."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    inputs = iter([""])  # Enter

    def fake_input(prompt: str) -> str:
        return next(inputs)

    args = argparse.Namespace(
        resume=None,
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=False,
        _resume_explicit=False,
    )
    run_id = _forge_resume.maybe_prompt_resume(
        args, workspace_root=workspace_with_paused, input_fn=fake_input
    )
    assert run_id is not None
    assert run_id.endswith("-paused01")


def test_tty_c_returns_run_id(workspace_with_paused: Path):
    inputs = iter(["c"])

    def fake_input(prompt: str) -> str:
        return next(inputs)

    args = argparse.Namespace(
        resume=None,
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=False,
        _resume_explicit=False,
    )
    run_id = _forge_resume.maybe_prompt_resume(
        args, workspace_root=workspace_with_paused, input_fn=fake_input
    )
    assert run_id is not None


def test_tty_f_returns_none(workspace_with_paused: Path):
    inputs = iter(["f"])

    def fake_input(prompt: str) -> str:
        return next(inputs)

    args = argparse.Namespace(
        resume=None,
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=False,
        _resume_explicit=False,
    )
    run_id = _forge_resume.maybe_prompt_resume(
        args, workspace_root=workspace_with_paused, input_fn=fake_input
    )
    assert run_id is None


def test_tty_question_mark_shows_details_then_reprompts(workspace_with_paused: Path, capsys):
    """Pressing '?' prints details to stderr, then re-prompts."""
    inputs = iter(["?", ""])

    def fake_input(prompt: str) -> str:
        return next(inputs)

    args = argparse.Namespace(
        resume=None,
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=False,
        _resume_explicit=False,
    )
    run_id = _forge_resume.maybe_prompt_resume(
        args, workspace_root=workspace_with_paused, input_fn=fake_input
    )
    # Second prompt = Enter → continue.
    assert run_id is not None
    err = capsys.readouterr().err
    # Details rendered.
    assert "Candidates" in err
    assert "fluid agents" in err  # the inspection hint


def test_tty_invalid_answer_loops(workspace_with_paused: Path, capsys):
    """An unrecognised answer loops back to the prompt."""
    inputs = iter(["banana", "f"])

    def fake_input(prompt: str) -> str:
        return next(inputs)

    args = argparse.Namespace(
        resume=None,
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=False,
        _resume_explicit=False,
    )
    run_id = _forge_resume.maybe_prompt_resume(
        args, workspace_root=workspace_with_paused, input_fn=fake_input
    )
    assert run_id is None  # eventually picked 'f'


def test_tty_ctrl_c_treated_as_fresh(workspace_with_paused: Path, capsys):
    """KeyboardInterrupt at the prompt → fresh (None), with a status line."""

    def raising_input(prompt: str) -> str:
        raise KeyboardInterrupt()

    args = argparse.Namespace(
        resume=None,
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=False,
        _resume_explicit=False,
    )
    run_id = _forge_resume.maybe_prompt_resume(
        args, workspace_root=workspace_with_paused, input_fn=raising_input
    )
    assert run_id is None


# ---------------------------------------------------------------------------
# Non-TTY behavior — env-var matrix
# ---------------------------------------------------------------------------


def test_non_tty_no_env_returns_none(workspace_with_paused: Path, monkeypatch):
    monkeypatch.delenv("FLUID_FORGE_AUTO_RESUME", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    args = argparse.Namespace(
        resume=None,
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=False,
        _resume_explicit=False,
    )
    run_id = _forge_resume.maybe_prompt_resume(args, workspace_root=workspace_with_paused)
    assert run_id is None


def test_non_tty_with_env_returns_most_recent(workspace_with_paused: Path, monkeypatch):
    monkeypatch.setenv("FLUID_FORGE_AUTO_RESUME", "1")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    args = argparse.Namespace(
        resume=None,
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=False,
        _resume_explicit=False,
    )
    run_id = _forge_resume.maybe_prompt_resume(args, workspace_root=workspace_with_paused)
    assert run_id is not None


# ---------------------------------------------------------------------------
# Age formatter
# ---------------------------------------------------------------------------


def test_format_age_human_seconds():
    assert _forge_resume._format_age_human(30) == "30 sec"
    assert _forge_resume._format_age_human(1) == "1 sec"


def test_format_age_human_minutes():
    assert _forge_resume._format_age_human(720) == "12 min"


def test_format_age_human_hours():
    assert _forge_resume._format_age_human(7200) == "2 hours"
    assert _forge_resume._format_age_human(3600) == "1 hour"


def test_format_age_human_days():
    assert _forge_resume._format_age_human(2 * 86400) == "2 days"
    assert _forge_resume._format_age_human(86400) == "1 day"
