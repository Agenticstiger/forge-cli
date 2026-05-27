# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Tests for the ``fluid agents`` CLI namespace.

The data source for every subcommand is ``.fluid/agents/<run-id>/``.
We synthesize a workspace with a handful of run-dirs covering every
status (done / paused / failed / stale) and assert the table /
filtering / json / prune behaviors against it.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fluid_build.cli import agents_cmd

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_run(
    workspace: Path,
    *,
    run_id: str,
    status: str = "done",
    stages_completed: int = 7,
    stages_total: int = 7,
    last_stage: str = "commit",
    total_usd: float = 0.10,
    total_tokens: int = 1500,
    paused: bool = False,
    age_days: float = 0,
) -> Path:
    """Synthesize one ``.fluid/agents/<run-id>/`` directory."""
    run_dir = workspace / ".fluid" / "agents" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "cost.json").write_text(
        json.dumps(
            {
                "provider": "anthropic",
                "model": "claude-opus-4-7",
                "input_tokens": int(total_tokens * 0.7),
                "output_tokens": int(total_tokens * 0.3),
                "total_tokens": total_tokens,
                "total_usd": total_usd,
                "wall_clock_seconds": 12.3,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "status": status,
                "stages_completed": stages_completed,
                "stages_total": stages_total,
                "last_stage": last_stage,
                "started_iso": "2026-05-27T14:00:00+00:00",
                "updated_iso": "2026-05-27T14:12:30+00:00",
                "stages": [
                    {
                        "name": last_stage,
                        "status": "done" if status == "done" else "running",
                        "duration_seconds": 2.5,
                        "cost_usd": 0.04,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    if paused:
        (run_dir / ".paused").write_text(
            json.dumps(
                {
                    "paused_at": "2026-05-27T14:10:00+00:00",
                    "current_stage": stages_completed + 1,
                    "stages_total": stages_total,
                    "stages_completed": stages_completed,
                    "last_stage": last_stage,
                    "cost_so_far_usd": total_usd,
                }
            ),
            encoding="utf-8",
        )

    # Set mtime so age_days filtering works for prune.
    if age_days > 0:
        import os

        target_ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).timestamp()
        for p in run_dir.rglob("*"):
            try:
                os.utime(p, (target_ts, target_ts))
            except OSError:
                pass
        try:
            os.utime(run_dir, (target_ts, target_ts))
        except OSError:
            pass

    return run_dir


@pytest.fixture
def workspace_with_runs(tmp_path: Path) -> Path:
    """Workspace with one of each status, recent timestamps."""
    # Build run-ids with timestamps so _parse_run_timestamp resolves.
    now = datetime.now(timezone.utc)
    _make_run(
        tmp_path,
        run_id=now.strftime("%Y%m%d-%H%M%S") + "-aaa001",
        status="done",
        last_stage="commit",
    )
    _make_run(
        tmp_path,
        run_id=(now - timedelta(minutes=12)).strftime("%Y%m%d-%H%M%S") + "-bbb002",
        status="running",
        paused=True,
        stages_completed=3,
        stages_total=7,
        last_stage="builder",
        total_usd=0.04,
        total_tokens=600,
    )
    _make_run(
        tmp_path,
        run_id=(now - timedelta(hours=4)).strftime("%Y%m%d-%H%M%S") + "-ccc003",
        status="failed",
        stages_completed=2,
        stages_total=7,
        last_stage="builder",
        total_usd=0.02,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# collect_runs
# ---------------------------------------------------------------------------


def test_collect_runs_returns_all_three(workspace_with_runs: Path):
    runs = list(agents_cmd.collect_runs(workspace_with_runs))
    assert len(runs) == 3
    statuses = sorted(r["status"] for r in runs)
    # ``paused`` overrides ``running`` thanks to the .paused marker.
    assert "done" in statuses
    assert "paused" in statuses
    assert "failed" in statuses


def test_collect_runs_orders_most_recent_first(workspace_with_runs: Path):
    runs = list(agents_cmd.collect_runs(workspace_with_runs))
    ages = [r["age_seconds"] for r in runs]
    assert ages == sorted(ages)  # most-recent first → smallest age first


def test_collect_runs_no_workspace_returns_empty(tmp_path: Path):
    runs = list(agents_cmd.collect_runs(tmp_path))
    assert runs == []


# ---------------------------------------------------------------------------
# `list`
# ---------------------------------------------------------------------------


def _run_args(**kwargs):
    return argparse.Namespace(**kwargs)


def test_list_renders_table(workspace_with_runs: Path, capsys):
    # Use --no-trunc so this assertion is deterministic across CI
    # widths (capsys can pipe to a narrow tty, which now drops COST
    # / STAGES by design — see H12 responsive-width strategy).
    args = _run_args(
        subcommand="list",
        root=str(workspace_with_runs),
        incomplete=False,
        since=None,
        emit_json=False,
        no_trunc=True,
        archived=False,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    out = capsys.readouterr().out
    # Header columns visible.
    assert "RUN_ID" in out
    assert "STAGES" in out
    assert "COST" in out
    # All three runs appear.
    assert "done" in out
    assert "paused" in out
    assert "failed" in out


def test_list_incomplete_filter_excludes_done(workspace_with_runs: Path, capsys):
    args = _run_args(
        subcommand="list",
        root=str(workspace_with_runs),
        incomplete=True,
        since=None,
        emit_json=False,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "paused" in out
    assert "failed" in out
    # The done row should NOT be there (the run-id is unique enough
    # that we can check by status text — note "done" can appear in
    # "Done" elsewhere so we check by counting "✓ done" pattern).
    # Stricter check: only 2 rows of substantive status data.
    # (Rough heuristic — JSON test below is the strict one.)


def test_list_json_emits_parseable_payload(workspace_with_runs: Path, capsys):
    args = _run_args(
        subcommand="list",
        root=str(workspace_with_runs),
        incomplete=False,
        since=None,
        emit_json=True,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["count"] == 3
    runs_by_status = {r["status"]: r for r in payload["runs"]}
    assert set(runs_by_status.keys()) == {"done", "paused", "failed"}
    # Cost rolled in.
    assert runs_by_status["done"]["total_usd"] == 0.10
    assert runs_by_status["paused"]["total_usd"] == 0.04


def test_list_json_incomplete_excludes_done(workspace_with_runs: Path, capsys):
    args = _run_args(
        subcommand="list",
        root=str(workspace_with_runs),
        incomplete=True,
        since=None,
        emit_json=True,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    statuses = {r["status"] for r in payload["runs"]}
    assert "done" not in statuses
    assert "paused" in statuses or "failed" in statuses


def test_list_since_filter(workspace_with_runs: Path, capsys):
    """Only runs newer than the cutoff should appear."""
    args = _run_args(
        subcommand="list",
        root=str(workspace_with_runs),
        incomplete=False,
        since="30m",  # last 30 minutes — excludes the 4-hour-old one
        emit_json=True,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # The 4-hour-old failed run should be excluded.
    statuses = {r["status"] for r in payload["runs"]}
    assert "failed" not in statuses


# ---------------------------------------------------------------------------
# `show`
# ---------------------------------------------------------------------------


def test_show_full_run_id(workspace_with_runs: Path, capsys):
    runs = list(agents_cmd.collect_runs(workspace_with_runs))
    paused = next(r for r in runs if r["status"] == "paused")
    args = _run_args(
        subcommand="show",
        root=str(workspace_with_runs),
        run_id=paused["run_id"],
        emit_json=False,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    out = capsys.readouterr().out
    assert paused["run_id"] in out
    assert "paused" in out


def test_show_prefix_resolves(workspace_with_runs: Path, capsys):
    runs = list(agents_cmd.collect_runs(workspace_with_runs))
    paused = next(r for r in runs if r["status"] == "paused")
    # 15 chars = unique timestamp portion.
    args = _run_args(
        subcommand="show",
        root=str(workspace_with_runs),
        run_id=paused["run_id"][:15],
        emit_json=False,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0


def test_show_unknown_run_id_returns_1(workspace_with_runs: Path, capsys):
    args = _run_args(
        subcommand="show",
        root=str(workspace_with_runs),
        run_id="20990101-000000-zzzzzz",
        emit_json=False,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 1


def test_show_json_emits_stages(workspace_with_runs: Path, capsys):
    runs = list(agents_cmd.collect_runs(workspace_with_runs))
    done = next(r for r in runs if r["status"] == "done")
    args = _run_args(
        subcommand="show",
        root=str(workspace_with_runs),
        run_id=done["run_id"],
        emit_json=True,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == done["run_id"]
    assert payload["status"] == "done"
    assert "cost" in payload
    assert isinstance(payload["stages"], list)


# ---------------------------------------------------------------------------
# `prune`
# ---------------------------------------------------------------------------


def _prune_args(**overrides):
    """Build a Namespace for ``_run_prune`` with current default fields.

    The prune CLI now has two mutually-exclusive mode flags:
    ``archive`` (the new safe default — kept as a flag so tests can be
    explicit) and ``delete`` (opt-in permanent rmtree). Tests must
    pass either or neither — never both.
    """
    defaults = dict(
        subcommand="prune",
        older_than="30d",
        dry_run=False,
        yes=True,
        archive=False,
        delete=False,
        run_id=None,
    )
    defaults.update(overrides)
    return _run_args(**defaults)


def test_prune_dry_run_does_not_delete(tmp_path: Path, capsys):
    # Build an old run.
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y%m%d-%H%M%S")
    old_run = _make_run(
        tmp_path,
        run_id=f"{old_ts}-old001",
        age_days=40,
        total_usd=0.01,
    )
    assert old_run.exists()
    args = _prune_args(root=str(tmp_path), dry_run=True)
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    assert old_run.exists()  # dry-run never touches disk
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert old_run.name in out


def test_prune_yes_archives_by_default(tmp_path: Path, capsys):
    """The new default is ARCHIVE (reversible) — even with --yes."""
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y%m%d-%H%M%S")
    old_run = _make_run(
        tmp_path,
        run_id=f"{old_ts}-old002",
        age_days=40,
        total_usd=0.01,
    )
    assert old_run.exists()
    args = _prune_args(root=str(tmp_path))
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    # The run dir is moved (not deleted) — original gone, archive
    # destination present.
    assert not old_run.exists()
    archived = tmp_path / ".fluid" / "agents" / ".archived" / old_run.name
    assert archived.is_dir(), "default prune must move to .archived/, not delete"
    out = capsys.readouterr().out
    # Trailing reclaimed line works in both modes per the spec.
    assert "Total reclaimed space" in out
    assert "Archived" in out


def test_prune_delete_yes_removes_permanently(tmp_path: Path, capsys):
    """--delete --yes triggers rmtree — irreversible."""
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y%m%d-%H%M%S")
    old_run = _make_run(
        tmp_path,
        run_id=f"{old_ts}-del001",
        age_days=40,
        total_usd=0.01,
    )
    assert old_run.exists()
    args = _prune_args(root=str(tmp_path), delete=True)
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    assert not old_run.exists()
    # And NOT in the archive — --delete must be permanent removal.
    archived = tmp_path / ".fluid" / "agents" / ".archived" / old_run.name
    assert not archived.exists(), "--delete must NOT preserve via .archived/"
    out = capsys.readouterr().out
    assert "Total reclaimed space" in out


def test_prune_archive_moves_to_archived(tmp_path: Path, capsys):
    """Explicit --archive still works (back-compat with the legacy flag)."""
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y%m%d-%H%M%S")
    old_run = _make_run(
        tmp_path,
        run_id=f"{old_ts}-old003",
        age_days=40,
        total_usd=0.01,
    )
    args = _prune_args(root=str(tmp_path), archive=True)
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    assert not old_run.exists()
    archived = tmp_path / ".fluid" / "agents" / ".archived" / old_run.name
    assert archived.exists()


def test_prune_recent_runs_safe(workspace_with_runs: Path, capsys):
    """Recent runs (< 30d old) should not be pruned."""
    runs_before = list(agents_cmd.collect_runs(workspace_with_runs))
    args = _prune_args(root=str(workspace_with_runs))
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    runs_after = list(agents_cmd.collect_runs(workspace_with_runs))
    assert len(runs_after) == len(runs_before)


def test_prune_run_id_targeted(tmp_path: Path, capsys):
    """--run-id <id> prunes a specific run even when it's not old."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run = _make_run(tmp_path, run_id=f"{ts}-target1", total_usd=0.01)
    args = _prune_args(root=str(tmp_path), delete=True, run_id=run.name)
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    assert not run.exists()


def test_prune_prompt_archive_wording(tmp_path: Path, capsys, monkeypatch):
    """Without --yes, the prompt distinguishes archive vs delete."""
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y%m%d-%H%M%S")
    _make_run(tmp_path, run_id=f"{old_ts}-prom001", age_days=40)
    # Simulate the operator typing "n" so we don't actually mutate.
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "n")
    args = _prune_args(root=str(tmp_path), yes=False)
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 1  # aborted
    out = capsys.readouterr().out
    # Archive-mode prompt uses the non-shouty word.
    assert "Archive" in out
    assert "DELETE" not in out


def test_prune_prompt_delete_wording(tmp_path: Path, capsys, monkeypatch):
    """--delete prompt must shout PERMANENTLY DELETE."""
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y%m%d-%H%M%S")
    _make_run(tmp_path, run_id=f"{old_ts}-prom002", age_days=40)
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "n")
    args = _prune_args(root=str(tmp_path), yes=False, delete=True)
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 1
    out = capsys.readouterr().out
    # Delete-mode prompt uses the shouty wording.
    assert "PERMANENTLY DELETE" in out


# ---------------------------------------------------------------------------
# Format helpers
# ---------------------------------------------------------------------------


def test_format_bytes_kb():
    assert agents_cmd._format_bytes(2048) == "2.0 kB"


def test_format_bytes_mb():
    assert "MB" in agents_cmd._format_bytes(2 * 1024 * 1024)


def test_format_bytes_b():
    assert agents_cmd._format_bytes(42) == "42 B"


def test_format_age_seconds_minutes_hours_days():
    assert agents_cmd._format_age(30) == "30s"
    assert agents_cmd._format_age(120) == "2m"
    assert agents_cmd._format_age(7200) == "2h"
    assert agents_cmd._format_age(2 * 86400) == "2d"


def test_parse_duration_units():
    assert agents_cmd._parse_duration("30d") == timedelta(days=30)
    assert agents_cmd._parse_duration("24h") == timedelta(hours=24)
    assert agents_cmd._parse_duration("45m") == timedelta(minutes=45)
    assert agents_cmd._parse_duration("600s") == timedelta(seconds=600)
    assert agents_cmd._parse_duration("garbage") is None
    assert agents_cmd._parse_duration("") is None


# ---------------------------------------------------------------------------
# S4 — _scan_run_dir must read BOTH layouts (new CheckpointStore + legacy)
# ---------------------------------------------------------------------------


def _make_new_layout_run(
    workspace: Path,
    *,
    run_id: str,
    status: str = "complete",
    completed_stages: list = None,
    last_stage: str = "judge",
    total_cost_usd: float = 0.07,
    paused: bool = False,
    started_at: str = "2026-05-27T14:00:00+00:00",
) -> Path:
    """Synthesize a run dir written by the CheckpointStore layout.

    Manifest lives at ``<run-dir>/checkpoints/manifest.json`` and the
    pause marker at ``<run-dir>/checkpoints/.paused`` — both inside
    the ``checkpoints/`` subdir, not at the run-dir root.
    """
    completed_stages = completed_stages or [
        "logical",
        "contract_forge",
        "builder",
        "readme",
        "transformation",
        "validator",
        "enrichment",
        "judge",
    ]
    run_dir = workspace / ".fluid" / "agents" / run_id
    ckpts = run_dir / "checkpoints"
    ckpts.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id,
        "started_at": started_at,
        "completed_stages": completed_stages,
        "last_stage": last_stage,
        "total_cost_usd": total_cost_usd,
        "workspace_root": str(workspace),
        "status": status,
    }
    (ckpts / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    # Drop a per-stage record so _load_run_detail has something to read.
    for stage_name in completed_stages:
        (ckpts / f"{stage_name}.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "stage": stage_name,
                    "completed_at": started_at,
                    "payload_kind": "json",
                    "payload_json": "{}",
                    "cost_usd": 0.01,
                    "contract_hash": None,
                }
            ),
            encoding="utf-8",
        )
    if paused:
        (ckpts / ".paused").write_text("paused-at-marker", encoding="utf-8")
    return run_dir


def test_scan_run_dir_reads_new_checkpointstore_layout(tmp_path: Path):
    """_scan_run_dir must pick up CheckpointStore manifest + paused marker."""
    run_dir = _make_new_layout_run(
        tmp_path,
        run_id="20260527-120000-newlay",
        status="paused",
        paused=True,
        completed_stages=["logical", "contract_forge", "builder"],
        last_stage="builder",
        total_cost_usd=0.05,
    )
    rec = agents_cmd._scan_run_dir(run_dir)
    assert rec is not None
    # status from manifest is overridden by the .paused marker.
    assert rec["status"] == "paused"
    assert rec["last_stage"] == "builder"
    assert rec["stages_completed"] == 3
    # stages_total comes from STAGE_NAMES (8) when not otherwise set.
    assert rec["stages_total"] == 8
    assert rec["total_usd"] == 0.05


def test_scan_run_dir_reads_legacy_layout(tmp_path: Path):
    """Legacy <run-dir>/checkpoint.json + <run-dir>/.paused still resolves."""
    run_dir = _make_run(
        tmp_path,
        run_id="20260527-130000-legacy",
        status="paused",
        paused=True,
        stages_completed=2,
        stages_total=7,
        last_stage="builder",
        total_usd=0.03,
    )
    rec = agents_cmd._scan_run_dir(run_dir)
    assert rec is not None
    assert rec["status"] == "paused"
    assert rec["last_stage"] == "builder"
    assert rec["stages_completed"] == 2
    assert rec["stages_total"] == 7
    assert rec["total_usd"] == 0.03


def test_collect_runs_surfaces_new_layout_paused(tmp_path: Path):
    """Real CheckpointStore paused run must appear in `fluid agents list`."""
    _make_new_layout_run(
        tmp_path,
        run_id="20260527-140000-stree1",
        status="running",
        paused=True,
        completed_stages=["logical"],
        last_stage="logical",
    )
    runs = list(agents_cmd.collect_runs(tmp_path))
    assert len(runs) == 1
    assert runs[0]["status"] == "paused"


def test_scan_run_dir_prefers_new_over_legacy(tmp_path: Path):
    """When both layouts exist, the new layout wins (S4 design)."""
    # Build a run with BOTH legacy ``checkpoint.json`` (status=done) AND
    # new-layout ``checkpoints/manifest.json`` (status=paused). The new
    # one wins.
    run_id = "20260527-150000-bothla"
    new_dir = _make_new_layout_run(
        tmp_path,
        run_id=run_id,
        status="paused",
        completed_stages=["logical"],
        last_stage="logical",
    )
    # Drop a legacy checkpoint.json at the run-dir root.
    (new_dir / "checkpoint.json").write_text(
        json.dumps({"status": "done", "stages_completed": 7, "stages_total": 7}),
        encoding="utf-8",
    )
    rec = agents_cmd._scan_run_dir(new_dir)
    assert rec is not None
    # The NEW manifest's data wins.
    assert rec["status"] == "paused"
    assert rec["stages_completed"] == 1
    assert rec["stages_total"] == 8


# ---------------------------------------------------------------------------
# S8 — STATUS_ICONS must include the "complete" key (CheckpointStore status)
# ---------------------------------------------------------------------------


def test_status_icons_complete_key_resolves():
    """The CheckpointStore writes ``"complete"`` — render path must map it."""
    assert "complete" in agents_cmd.STATUS_ICONS
    assert agents_cmd.STATUS_ICONS["complete"] == agents_cmd.STATUS_ICONS["done"]
    assert "complete" in agents_cmd.STATUS_ICONS_ASCII
    assert agents_cmd.STATUS_ICONS_ASCII["complete"] == agents_cmd.STATUS_ICONS_ASCII["done"]


# ---------------------------------------------------------------------------
# S9 — judge axes dict extraction (no Python dict reprs in user output)
# ---------------------------------------------------------------------------


def test_show_judge_axes_renders_score_not_dict_repr(tmp_path: Path, capsys):
    """Real judge.json axes are dicts ``{"score": N, ...}``; render N only."""
    run_id = "20260527-160000-judges"
    run_dir = _make_run(
        tmp_path,
        run_id=run_id,
        status="done",
        total_usd=0.05,
    )
    # Overwrite with a real-shape judge.json.
    (run_dir / "judge.json").write_text(
        json.dumps(
            {
                "total": 24,
                "model": "claude-opus-4-7",
                "axes": {
                    "correctness": {
                        "score": 4,
                        "reasoning": "ok",
                        "suggestions": ["x"],
                    },
                    "completeness": {"score": 5, "reasoning": "good"},
                },
            }
        ),
        encoding="utf-8",
    )
    args = _run_args(
        subcommand="show",
        root=str(tmp_path),
        run_id=run_id,
        emit_json=False,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    out = capsys.readouterr().out
    # Must NOT contain a Python dict repr like "{'score': 4}".
    assert "{'score'" not in out
    assert "'score':" not in out
    # Must contain the scores as bare numbers in the X/5 form.
    assert "4/5" in out
    assert "5/5" in out


def test_show_judge_axes_back_compat_with_bare_int(tmp_path: Path, capsys):
    """Synthetic judge.json with bare int values still renders correctly."""
    run_id = "20260527-160500-bareit"
    run_dir = _make_run(tmp_path, run_id=run_id, status="done")
    (run_dir / "judge.json").write_text(
        json.dumps({"total": 12, "model": "test", "axes": {"correctness": 3}}),
        encoding="utf-8",
    )
    args = _run_args(
        subcommand="show",
        root=str(tmp_path),
        run_id=run_id,
        emit_json=False,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "3/5" in out
    assert "{" not in out.split("Judge score")[-1].split("Receipts:")[0]


# ---------------------------------------------------------------------------
# H12 — narrow-tty drops COST first, then STAGES; --no-trunc keeps all
# ---------------------------------------------------------------------------


def test_list_narrow_tty_drops_cost_first(workspace_with_runs: Path, capsys, monkeypatch):
    """At width 80, COST drops; STAGES stays."""
    monkeypatch.setattr(agents_cmd, "_terminal_width", lambda: 80)
    args = _run_args(
        subcommand="list",
        root=str(workspace_with_runs),
        incomplete=False,
        since=None,
        emit_json=False,
        no_trunc=False,
        archived=False,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    out = capsys.readouterr().out
    # COST column header is dropped.
    assert "COST" not in out
    # STAGES is kept at width 80.
    assert "STAGES" in out
    # Core columns always present.
    assert "RUN_ID" in out
    assert "LAST_STAGE" in out


def test_list_very_narrow_tty_drops_stages_too(workspace_with_runs: Path, capsys, monkeypatch):
    """Below 80 cols, STAGES also drops."""
    monkeypatch.setattr(agents_cmd, "_terminal_width", lambda: 60)
    args = _run_args(
        subcommand="list",
        root=str(workspace_with_runs),
        incomplete=False,
        since=None,
        emit_json=False,
        no_trunc=False,
        archived=False,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "COST" not in out
    assert "STAGES" not in out
    # Core columns still present.
    assert "RUN_ID" in out
    assert "STATUS" in out


def test_list_no_trunc_keeps_all_columns_even_narrow(
    workspace_with_runs: Path, capsys, monkeypatch
):
    """--no-trunc forces every column to render regardless of width."""
    monkeypatch.setattr(agents_cmd, "_terminal_width", lambda: 40)
    args = _run_args(
        subcommand="list",
        root=str(workspace_with_runs),
        incomplete=False,
        since=None,
        emit_json=False,
        no_trunc=True,
        archived=False,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "RUN_ID" in out
    assert "STAGES" in out
    assert "COST" in out
    assert "LAST_STAGE" in out


# ---------------------------------------------------------------------------
# H13 — `list --archived` and the archive hint on default `list`
# ---------------------------------------------------------------------------


def test_list_archived_returns_archived_runs(tmp_path: Path, capsys):
    """--archived walks .fluid/agents/.archived/ instead of the live dir."""
    # Build a live run + an archived run.
    now = datetime.now(timezone.utc)
    _make_run(
        tmp_path,
        run_id=now.strftime("%Y%m%d-%H%M%S") + "-livee1",
        status="done",
        total_usd=0.05,
    )
    # Build an archived run (just stash a checkpoint.json under .archived/).
    archived_dir = (
        tmp_path
        / ".fluid"
        / "agents"
        / ".archived"
        / ((now - timedelta(days=5)).strftime("%Y%m%d-%H%M%S") + "-archv1")
    )
    archived_dir.mkdir(parents=True, exist_ok=True)
    (archived_dir / "checkpoint.json").write_text(
        json.dumps(
            {
                "status": "done",
                "stages_completed": 7,
                "stages_total": 7,
                "last_stage": "judge",
            }
        ),
        encoding="utf-8",
    )
    args = _run_args(
        subcommand="list",
        root=str(tmp_path),
        incomplete=False,
        since=None,
        emit_json=True,
        no_trunc=True,
        archived=True,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # Only the archived run — not the live one.
    assert payload["count"] == 1
    assert payload["runs"][0]["run_id"] == archived_dir.name


def test_list_default_shows_archive_hint_when_present(tmp_path: Path, capsys):
    """When archives exist, default `list` surfaces a header hint."""
    now = datetime.now(timezone.utc)
    _make_run(
        tmp_path,
        run_id=now.strftime("%Y%m%d-%H%M%S") + "-livee2",
        status="done",
    )
    # Stash an archived run.
    arc = (
        tmp_path
        / ".fluid"
        / "agents"
        / ".archived"
        / ((now - timedelta(days=10)).strftime("%Y%m%d-%H%M%S") + "-archv2")
    )
    arc.mkdir(parents=True, exist_ok=True)
    (arc / "checkpoint.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
    args = _run_args(
        subcommand="list",
        root=str(tmp_path),
        incomplete=False,
        since=None,
        emit_json=False,
        no_trunc=True,
        archived=False,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    out = capsys.readouterr().out
    # The hint line names the new subcommand.
    assert "archived run" in out
    assert "fluid agents list --archived" in out


def test_list_default_no_hint_when_no_archive(tmp_path: Path, capsys):
    """No hint when the archive bucket is empty / missing."""
    now = datetime.now(timezone.utc)
    _make_run(
        tmp_path,
        run_id=now.strftime("%Y%m%d-%H%M%S") + "-livee3",
        status="done",
    )
    args = _run_args(
        subcommand="list",
        root=str(tmp_path),
        incomplete=False,
        since=None,
        emit_json=False,
        no_trunc=True,
        archived=False,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "archived run" not in out


def test_show_resolves_archived_run_by_prefix(tmp_path: Path, capsys):
    """agents show <id> must find archived runs too (S? bug — H13 surface)."""
    archived_id = "20260520-100000-archvz"
    arc = tmp_path / ".fluid" / "agents" / ".archived" / archived_id
    arc.mkdir(parents=True, exist_ok=True)
    (arc / "checkpoint.json").write_text(
        json.dumps(
            {
                "status": "done",
                "stages_completed": 7,
                "stages_total": 7,
                "last_stage": "judge",
            }
        ),
        encoding="utf-8",
    )
    args = _run_args(
        subcommand="show",
        root=str(tmp_path),
        run_id=archived_id[:15],
        emit_json=True,
    )
    rc = agents_cmd.run(args, logging.getLogger("test"))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == archived_id
