# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the resume / fork / from-stage flag matrix on ``fluid forge``.

These exercise ``_resolve_resume_args`` (the helper that validates and
resolves the flags) plus the underlying ``maybe_prompt_resume``. We
construct a tmp workspace with a paused run, then check each flag combo.

Skip the prompt path here — that's covered by
``test_forge_resume_prompt.py``.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fluid_build.cli import _forge_resume
from fluid_build.cli.forge import _resolve_resume_args, _ResumeFlagError


def _build_run(workspace: Path, *, run_id: str, paused: bool = True) -> Path:
    run_dir = workspace / ".fluid" / "agents" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "cost.json").write_text(json.dumps({"total_usd": 0.04, "total_tokens": 600}))
    (run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "status": "paused" if paused else "done",
                "stages_completed": 3,
                "stages_total": 7,
                "last_stage": "builder",
            }
        )
    )
    if paused:
        (run_dir / ".paused").write_text(
            json.dumps({"current_stage": 4, "stages_total": 7, "last_stage": "builder"})
        )
    return run_dir


@pytest.fixture
def workspace_with_paused(tmp_path: Path) -> Path:
    """tmp_path with one paused run."""
    now = datetime.now(timezone.utc)
    rid = now.strftime("%Y%m%d-%H%M%S") + "-paused01"
    _build_run(tmp_path, run_id=rid, paused=True)
    return tmp_path


@pytest.fixture
def workspace_empty(tmp_path: Path) -> Path:
    return tmp_path


# ---------------------------------------------------------------------------
# --resume <id> explicit
# ---------------------------------------------------------------------------


def test_resume_explicit_id_resolves(workspace_with_paused: Path, monkeypatch):
    # Build args namespace via argparse to match the real shape.
    runs = list(workspace_with_paused.glob(".fluid/agents/*/"))
    target_id = runs[0].name
    args = argparse.Namespace(
        resume=target_id,
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=False,
    )
    # Force non-TTY so the prompt path is skipped.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    _resolve_resume_args(args, workspace_root=workspace_with_paused, console=None)
    assert args._resume_run_id == target_id


def test_resume_bare_picks_most_recent(workspace_with_paused: Path, monkeypatch):
    runs = list(workspace_with_paused.glob(".fluid/agents/*/"))
    expected = runs[0].name
    # ``--resume`` with no arg → argparse stores the sentinel.
    args = argparse.Namespace(
        resume="__RESUME_BARE__",
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=False,
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    _resolve_resume_args(args, workspace_root=workspace_with_paused, console=None)
    assert args._resume_run_id == expected


# ---------------------------------------------------------------------------
# --from-stage typo handling
# ---------------------------------------------------------------------------


def test_from_stage_typo_raises_with_did_you_mean():
    args = argparse.Namespace(
        resume="__RESUME_BARE__",
        no_resume=False,
        from_stage="builderr",
        fork=None,
        or_fail=False,
    )
    with pytest.raises(_ResumeFlagError) as exc:
        _resolve_resume_args(args, workspace_root=Path("/tmp/__nonexistent__"), console=None)
    msg = str(exc.value)
    assert "Unknown stage" in msg
    assert "builder" in msg  # the did-you-mean suggestion
    # Valid stages listed.
    assert "logical" in msg or "Valid" in msg


def test_from_stage_valid_stage_passes(workspace_with_paused: Path):
    args = argparse.Namespace(
        resume="__RESUME_BARE__",
        no_resume=False,
        from_stage="builder",
        fork=None,
        or_fail=False,
    )
    # Should not raise.
    _resolve_resume_args(args, workspace_root=workspace_with_paused, console=None)


# ---------------------------------------------------------------------------
# --fork
# ---------------------------------------------------------------------------


def test_fork_without_from_stage_errors():
    args = argparse.Namespace(
        resume=None,
        no_resume=False,
        from_stage=None,
        fork="abc123",
        or_fail=False,
    )
    with pytest.raises(_ResumeFlagError) as exc:
        _resolve_resume_args(args, workspace_root=Path("/tmp/__nonexistent__"), console=None)
    assert "--fork requires --from-stage" in str(exc.value)


def test_fork_with_from_stage_passes(workspace_with_paused: Path):
    args = argparse.Namespace(
        resume=None,
        no_resume=False,
        from_stage="builder",
        fork="abc123",
        or_fail=False,
    )
    # Validation passes; --fork-with-from-stage is a legal combo.
    _resolve_resume_args(args, workspace_root=workspace_with_paused, console=None)


# ---------------------------------------------------------------------------
# --or-fail
# ---------------------------------------------------------------------------


def test_or_fail_no_resumable_errors(workspace_empty: Path):
    args = argparse.Namespace(
        resume=None,
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=True,
    )
    with pytest.raises(_ResumeFlagError) as exc:
        _resolve_resume_args(args, workspace_root=workspace_empty, console=None)
    assert "No resumable run found" in str(exc.value)


def test_or_fail_with_resumable_succeeds(workspace_with_paused: Path, monkeypatch):
    args = argparse.Namespace(
        resume="__RESUME_BARE__",
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=True,
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    _resolve_resume_args(args, workspace_root=workspace_with_paused, console=None)
    assert args._resume_run_id is not None


# ---------------------------------------------------------------------------
# --no-resume
# ---------------------------------------------------------------------------


def test_no_resume_overrides_auto_detect(workspace_with_paused: Path, monkeypatch):
    """--no-resume always returns None, even with FLUID_FORGE_AUTO_RESUME=1."""
    monkeypatch.setenv("FLUID_FORGE_AUTO_RESUME", "1")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    args = argparse.Namespace(
        resume=None,
        no_resume=True,
        from_stage=None,
        fork=None,
        or_fail=False,
    )
    _resolve_resume_args(args, workspace_root=workspace_with_paused, console=None)
    assert args._resume_run_id is None


def test_no_resume_with_or_fail_errors(workspace_with_paused: Path):
    args = argparse.Namespace(
        resume=None,
        no_resume=True,
        from_stage=None,
        fork=None,
        or_fail=True,
    )
    with pytest.raises(_ResumeFlagError):
        _resolve_resume_args(args, workspace_root=workspace_with_paused, console=None)


# ---------------------------------------------------------------------------
# Default (no flags) behavior
# ---------------------------------------------------------------------------


def test_default_no_flags_no_paused_returns_none(workspace_empty: Path, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    args = argparse.Namespace(
        resume=None,
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=False,
    )
    _resolve_resume_args(args, workspace_root=workspace_empty, console=None)
    assert args._resume_run_id is None


def test_default_no_flags_non_tty_no_auto_resume_env_returns_none(
    workspace_with_paused: Path, monkeypatch
):
    monkeypatch.delenv("FLUID_FORGE_AUTO_RESUME", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    args = argparse.Namespace(
        resume=None,
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=False,
    )
    _resolve_resume_args(args, workspace_root=workspace_with_paused, console=None)
    assert args._resume_run_id is None


def test_default_no_flags_non_tty_with_auto_resume_env_resumes(
    workspace_with_paused: Path, monkeypatch
):
    monkeypatch.setenv("FLUID_FORGE_AUTO_RESUME", "1")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    args = argparse.Namespace(
        resume=None,
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=False,
    )
    _resolve_resume_args(args, workspace_root=workspace_with_paused, console=None)
    assert args._resume_run_id is not None


# ---------------------------------------------------------------------------
# validate_from_stage helper
# ---------------------------------------------------------------------------


def test_validate_from_stage_known_passes():
    ok, msg = _forge_resume.validate_from_stage("builder")
    assert ok
    assert msg == ""


def test_validate_from_stage_unknown_returns_msg():
    ok, msg = _forge_resume.validate_from_stage("builderr")
    assert not ok
    assert "Unknown stage" in msg
    assert "builder" in msg


def test_validate_from_stage_empty_fails():
    ok, msg = _forge_resume.validate_from_stage("")
    assert not ok


# ---------------------------------------------------------------------------
# H16 — fake --resume <id> must fail loudly, not silently drop the flag
# ---------------------------------------------------------------------------


def test_resume_fake_id_raises_with_exit_code_2(workspace_empty: Path, monkeypatch):
    """A non-existent --resume id should error out (exit 2) and never
    silently fall back to fresh mode — see ``02-pause-resume.md`` H16."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    args = argparse.Namespace(
        resume="does-not-exist-12345",
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=False,
    )
    with pytest.raises(_ResumeFlagError) as exc:
        _resolve_resume_args(args, workspace_root=workspace_empty, console=None)
    assert exc.value.exit_code == 2
    msg = str(exc.value)
    # The error must point the operator at the diagnostic command.
    assert "not found" in msg
    assert "fluid agents list" in msg


def test_from_stage_with_fake_resume_id_raises(workspace_empty: Path, monkeypatch):
    """``--from-stage X --resume <fake-id>`` must fail at parse time,
    not crash downstream with an EOF reading the interview prompt."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    args = argparse.Namespace(
        resume="never-existed-abc",
        no_resume=False,
        from_stage="builder",
        fork=None,
        or_fail=False,
    )
    with pytest.raises(_ResumeFlagError) as exc:
        _resolve_resume_args(args, workspace_root=workspace_empty, console=None)
    assert exc.value.exit_code == 2
    assert "not found" in str(exc.value)


def test_from_stage_without_resume_raises_exit_code_2(workspace_empty: Path):
    """``--from-stage`` on its own (no --resume / --fork) must error
    out — it's nonsensical to time-travel inside a fresh run."""
    args = argparse.Namespace(
        resume=None,
        no_resume=False,
        from_stage="builder",
        fork=None,
        or_fail=False,
    )
    with pytest.raises(_ResumeFlagError) as exc:
        _resolve_resume_args(args, workspace_root=workspace_empty, console=None)
    assert exc.value.exit_code == 2
    assert "requires --resume" in str(exc.value) or "requires" in str(exc.value)


# ---------------------------------------------------------------------------
# --or-fail policy violation → exit 1 (not 2)
# ---------------------------------------------------------------------------


def test_or_fail_no_resumable_exits_one_not_two(workspace_empty: Path):
    """``--or-fail`` is a policy gate, not a usage error — exit 1 per
    spec; not exit 2 (which would imply argparse usage failure)."""
    args = argparse.Namespace(
        resume=None,
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=True,
    )
    with pytest.raises(_ResumeFlagError) as exc:
        _resolve_resume_args(args, workspace_root=workspace_empty, console=None)
    assert exc.value.exit_code == 1


def test_or_fail_with_fake_resume_id_exits_two_not_one(workspace_empty: Path, monkeypatch):
    """``--or-fail --resume <fake-id>`` — the fake id is a usage error,
    not a policy violation. Exit 2 (usage) wins over exit 1 (policy)
    because the operator's input itself is malformed: there's nothing
    for ``--or-fail`` to validate against. Distinguishing the two
    keeps scripts that gate on "no resumable" from misfiring on a
    typo'd argument."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    args = argparse.Namespace(
        resume="does-not-exist-12345",
        no_resume=False,
        from_stage=None,
        fork=None,
        or_fail=True,
    )
    with pytest.raises(_ResumeFlagError) as exc:
        _resolve_resume_args(args, workspace_root=workspace_empty, console=None)
    assert exc.value.exit_code == 2


# ---------------------------------------------------------------------------
# DEFAULT_STAGE_NAMES is in sync with checkpoint.STAGE_NAMES
# ---------------------------------------------------------------------------


def test_default_stage_names_matches_checkpoint_stage_names():
    """Fallback list MUST match the canonical tuple byte-for-byte.
    Drift here is what produced the stale list referenced in
    ``02-pause-resume.md`` LOW finding #12."""
    from fluid_build.copilot.checkpoint import STAGE_NAMES as CANONICAL

    assert _forge_resume.DEFAULT_STAGE_NAMES == tuple(CANONICAL)
    # And the get_stage_names() helper resolves to the same list.
    assert _forge_resume.get_stage_names() == tuple(CANONICAL)
