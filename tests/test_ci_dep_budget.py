# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``scripts/ci/dep_budget.py`` (pure parts; no ``uv`` is run)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "dep_budget.py"
_spec = importlib.util.spec_from_file_location("dep_budget", _SCRIPT)
assert _spec and _spec.loader
budget = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(budget)


def test_parse_packages_normalises_and_ignores_markers_and_comments() -> None:
    compiled = "# a comment\nPyYAML==6.0\ntyping_extensions==4.0 ; python_version < '3.11'\n\n"
    assert budget.parse_packages(compiled) == ["pyyaml", "typing-extensions"]


def test_check_passes_at_or_under_ceiling() -> None:
    assert budget.check("t", ["a", "b"], 2, ["a", "b"]) is None


def test_check_names_added_and_removed_when_over_ceiling() -> None:
    msg = budget.check("t", ["a", "c", "d"], 2, ["a", "b"])
    assert msg and "3 packages (ceiling 2)" in msg
    assert "added vs baseline:   c, d" in msg
    assert "removed vs baseline: b" in msg


def test_every_target_has_a_committed_baseline_within_its_ceiling() -> None:
    for name, (_, _, ceiling) in budget.TARGETS.items():
        listed = (budget.BASELINES / f"{name}.txt").read_text().split()
        assert 0 < len(listed) <= ceiling, name


def test_parse_packages_counts_url_requirements() -> None:
    compiled = "click @ https://example.invalid/click.whl\nfoo @ git+https://x/y\nbar==1\n"
    assert budget.parse_packages(compiled) == ["bar", "click", "foo"]


def test_parse_packages_rejects_a_line_it_does_not_understand() -> None:
    with pytest.raises(ValueError, match="unrecognised line"):
        budget.parse_packages("bar==1\n-e file:///somewhere\n")


def test_a_uv_failure_shows_uvs_own_reason(monkeypatch) -> None:
    def fail(*_a, **_k):
        raise subprocess.CalledProcessError(1, ["uv"], stderr="error: No solution found")

    monkeypatch.setattr(budget.subprocess, "run", fail)
    with pytest.raises(SystemExit, match="No solution found"):
        budget.compile_target("linux", "3.12")


def test_update_reports_the_diff_against_the_old_baseline(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "t.txt").write_text("a\nb\n")
    monkeypatch.setattr(budget, "BASELINES", tmp_path)
    monkeypatch.setattr(budget, "TARGETS", {"t": ("linux", "3.12", 2)})
    monkeypatch.setattr(budget, "compile_target", lambda *_: ["a", "c", "d"])
    assert budget.main(["--update"]) == 1
    assert "added vs baseline:   c, d" in capsys.readouterr().err
    assert (tmp_path / "t.txt").read_text().split() == ["a", "c", "d"]


def test_a_count_under_the_ceiling_asks_for_a_ratchet(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "t.txt").write_text("a\n")
    monkeypatch.setattr(budget, "BASELINES", tmp_path)
    monkeypatch.setattr(budget, "TARGETS", {"t": ("linux", "3.12", 5)})
    monkeypatch.setattr(budget, "compile_target", lambda *_: ["a"])
    assert budget.main([]) == 0
    assert "lower it to 1" in capsys.readouterr().out
