# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the Ctrl-C → pause signal handler.

The handler:
* Writes a ``.paused`` JSON marker under ``.fluid/agents/<run-id>/``.
* Prints a resume hint to stderr.
* Exits with code 130 (POSIX SIGINT convention).
* Is idempotent — a second SIGINT inside the same process is a no-op
  (still exits, doesn't crash).

We drive it through ``os.kill(os.getpid(), signal.SIGINT)`` against an
``exit_fn`` override so the test process doesn't actually exit.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from fluid_build.cli import _signal_handler


@pytest.fixture(autouse=True)
def _reset_state():
    """Re-arm the re-entrance guard between tests."""
    _signal_handler.reset_handler_state()
    yield
    # Always restore the SIGINT default after each test so we don't
    # leak a handler into the rest of the suite.
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def _state_factory(**overrides):
    state = {
        "current_stage": 3,
        "stages_total": 7,
        "stage_name": "builder",
        "age_seconds": 720.0,
        "cost_so_far": 0.04,
    }
    state.update(overrides)
    return lambda: state


def test_write_paused_marker(tmp_path: Path):
    run_dir = tmp_path / "run01"
    marker = _signal_handler.write_paused_marker(
        run_dir,
        current_stage=4,
        stages_total=7,
        stage_name="builder",
        cost_so_far=0.06,
    )
    assert marker.is_file()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["current_stage"] == 4
    assert payload["stages_total"] == 7
    assert payload["last_stage"] == "builder"
    assert payload["cost_so_far_usd"] == 0.06
    assert "paused_at" in payload


def test_write_paused_marker_idempotent(tmp_path: Path):
    """Re-running write_paused_marker overwrites cleanly."""
    run_dir = tmp_path / "run02"
    _signal_handler.write_paused_marker(
        run_dir, current_stage=1, stages_total=7, stage_name="logical"
    )
    _signal_handler.write_paused_marker(
        run_dir, current_stage=3, stages_total=7, stage_name="builder"
    )
    payload = json.loads((run_dir / ".paused").read_text())
    assert payload["current_stage"] == 3
    assert payload["last_stage"] == "builder"


def test_install_pause_handler_writes_marker_on_sigint(tmp_path: Path, capsys):
    run_dir = tmp_path / "run03"
    run_dir.mkdir(parents=True, exist_ok=True)
    exit_calls = []
    _signal_handler.install_pause_handler(
        run_id="20260527-143000-zzz999",
        run_dir=run_dir,
        get_state=_state_factory(),
        saver=None,
        exit_fn=exit_calls.append,
    )

    os.kill(os.getpid(), signal.SIGINT)

    # Marker landed.
    assert (run_dir / ".paused").is_file()
    payload = json.loads((run_dir / ".paused").read_text())
    assert payload["current_stage"] == 3
    assert payload["last_stage"] == "builder"

    # Hint printed to stderr.
    err = capsys.readouterr().err
    assert "Paused" in err
    assert "stage 3/7" in err
    assert "builder" in err
    assert "fluid forge" in err
    assert "fluid agents prune --run-id 20260527-143000-zzz999" in err

    # Exit code 130.
    assert exit_calls == [_signal_handler.SIGINT_EXIT_CODE]


def test_install_pause_handler_idempotent_on_second_sigint(tmp_path: Path, capsys):
    """A second Ctrl-C should not crash the handler."""
    run_dir = tmp_path / "run04"
    run_dir.mkdir(parents=True, exist_ok=True)
    exit_calls = []
    _signal_handler.install_pause_handler(
        run_id="20260527-143000-iii000",
        run_dir=run_dir,
        get_state=_state_factory(),
        saver=None,
        exit_fn=exit_calls.append,
    )

    os.kill(os.getpid(), signal.SIGINT)
    # Second SIGINT — must not crash.
    os.kill(os.getpid(), signal.SIGINT)

    # Both calls must have routed to exit_fn(130).
    assert exit_calls == [
        _signal_handler.SIGINT_EXIT_CODE,
        _signal_handler.SIGINT_EXIT_CODE,
    ]
    # Marker still exists.
    assert (run_dir / ".paused").is_file()


def test_install_pause_handler_calls_saver_mark_paused(tmp_path: Path, capsys):
    """When the saver exposes ``mark_paused``, the handler calls it."""
    run_dir = tmp_path / "run05"
    run_dir.mkdir(parents=True, exist_ok=True)

    class _FakeSaver:
        def __init__(self):
            self.paused_calls = []

        def mark_paused(self, run_id: str) -> None:
            self.paused_calls.append(run_id)

    saver = _FakeSaver()
    exit_calls = []
    _signal_handler.install_pause_handler(
        run_id="rid-xx",
        run_dir=run_dir,
        get_state=_state_factory(),
        saver=saver,
        exit_fn=exit_calls.append,
    )
    os.kill(os.getpid(), signal.SIGINT)
    assert saver.paused_calls == ["rid-xx"]


def test_install_pause_handler_saver_without_mark_paused_no_crash(tmp_path: Path):
    """A saver that doesn't have ``mark_paused`` is silently tolerated."""
    run_dir = tmp_path / "run06"
    run_dir.mkdir(parents=True, exist_ok=True)

    class _MinimalSaver:
        """No mark_paused method — handler must not crash."""

    exit_calls = []
    _signal_handler.install_pause_handler(
        run_id="rid-min",
        run_dir=run_dir,
        get_state=_state_factory(),
        saver=_MinimalSaver(),
        exit_fn=exit_calls.append,
    )
    os.kill(os.getpid(), signal.SIGINT)
    assert exit_calls == [_signal_handler.SIGINT_EXIT_CODE]
    assert (run_dir / ".paused").is_file()


def test_handler_state_snapshot_failure_does_not_crash(tmp_path: Path):
    """If get_state() raises, the handler still writes the marker (with zeros)
    and prints the hint — robustness is non-negotiable on Ctrl-C."""
    run_dir = tmp_path / "run07"
    run_dir.mkdir(parents=True, exist_ok=True)

    def broken_state():
        raise RuntimeError("boom")

    exit_calls = []
    _signal_handler.install_pause_handler(
        run_id="rid-broken",
        run_dir=run_dir,
        get_state=broken_state,
        saver=None,
        exit_fn=exit_calls.append,
    )
    os.kill(os.getpid(), signal.SIGINT)
    assert exit_calls == [_signal_handler.SIGINT_EXIT_CODE]
    # Marker still written, just with zeros.
    assert (run_dir / ".paused").is_file()
    payload = json.loads((run_dir / ".paused").read_text())
    assert payload["current_stage"] == 0
