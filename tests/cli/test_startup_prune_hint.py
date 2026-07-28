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

"""Tests for the startup prune-hint in ``fluid forge``.

The hint fires when the workspace's ``.fluid/agents/`` carries more than
50 directories older than 30 days. It's non-blocking, prints once per
process, and suppressed by ``FLUID_FORGE_NO_PRUNE_HINT=1``.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fluid_build.cli import _forge_resume


def _make_old_dir(workspace: Path, name: str, age_days: float) -> Path:
    d = workspace / ".fluid" / "agents" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "cost.json").write_text('{"total_usd": 0.01}')
    target_ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).timestamp()
    try:
        os.utime(d, (target_ts, target_ts))
        os.utime(d / "cost.json", (target_ts, target_ts))
    except OSError:
        pass
    return d


@pytest.fixture(autouse=True)
def _reset_state():
    _forge_resume.reset_prune_hint_state()
    yield
    _forge_resume.reset_prune_hint_state()


def test_more_than_50_old_dirs_prints_hint(tmp_path: Path, capsys):
    for i in range(55):
        _make_old_dir(tmp_path, f"20260101-000000-old{i:03d}", age_days=40)
    printed = _forge_resume.maybe_print_prune_hint(tmp_path)
    assert printed is True
    err = capsys.readouterr().err
    assert "fluid agents prune" in err
    assert "MB" in err
    assert "old runs" in err


def test_49_old_dirs_no_hint(tmp_path: Path, capsys):
    for i in range(49):
        _make_old_dir(tmp_path, f"20260101-000000-old{i:03d}", age_days=40)
    printed = _forge_resume.maybe_print_prune_hint(tmp_path)
    assert printed is False
    err = capsys.readouterr().err
    assert err == ""


def test_recent_dirs_dont_count_toward_threshold(tmp_path: Path, capsys):
    """Recent dirs (<30d) should not count toward the threshold."""
    # 60 recent + 5 old → still under threshold.
    for i in range(60):
        _make_old_dir(tmp_path, f"20260527-000000-new{i:03d}", age_days=1)
    for i in range(5):
        _make_old_dir(tmp_path, f"20260101-000000-old{i:03d}", age_days=40)
    printed = _forge_resume.maybe_print_prune_hint(tmp_path)
    assert printed is False


def test_env_var_suppresses_hint(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("FLUID_FORGE_NO_PRUNE_HINT", "1")
    for i in range(55):
        _make_old_dir(tmp_path, f"20260101-000000-old{i:03d}", age_days=40)
    printed = _forge_resume.maybe_print_prune_hint(tmp_path)
    assert printed is False


def test_no_workspace_no_hint(tmp_path: Path):
    # Empty tmp_path — no .fluid/agents/ dir.
    printed = _forge_resume.maybe_print_prune_hint(tmp_path)
    assert printed is False


def test_hint_prints_at_most_once(tmp_path: Path, capsys):
    """Per-process guard: second call doesn't print again."""
    for i in range(55):
        _make_old_dir(tmp_path, f"20260101-000000-old{i:03d}", age_days=40)
    p1 = _forge_resume.maybe_print_prune_hint(tmp_path)
    p2 = _forge_resume.maybe_print_prune_hint(tmp_path)
    assert p1 is True
    assert p2 is False


def test_hint_threshold_override(tmp_path: Path, capsys):
    """Caller-supplied threshold overrides the default."""
    for i in range(10):
        _make_old_dir(tmp_path, f"20260101-000000-old{i:03d}", age_days=40)
    # threshold=5 → should print.
    printed = _forge_resume.maybe_print_prune_hint(tmp_path, threshold=5)
    assert printed is True
