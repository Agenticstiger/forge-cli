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

"""``fluid stats`` aggregates cost.json across forge runs."""

from __future__ import annotations

import json
import logging
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from fluid_build.cli._preview_panel import (
    CostSnapshot,
    PreviewPanel,
    new_run_id,
)
from fluid_build.cli.stats import _summarise
from fluid_build.cli.stats import run as stats_run


def _seed_run(workspace: Path, *, provider="openai", model="gpt-4o", tokens=1500, usd=0.03):
    """Create a single .fluid/agents/<run-id>/cost.json under workspace."""
    panel = PreviewPanel(run_id=new_run_id(), target_dir=workspace)
    panel.cost = CostSnapshot(
        provider=provider,
        model=model,
        input_tokens=tokens // 2,
        output_tokens=tokens // 2,
        total_tokens=tokens,
        total_usd=usd,
        wall_clock_seconds=2.5,
    )
    panel.persist_artifacts()
    return panel


def test_stats_aggregates_two_runs(tmp_path, capsys):
    _seed_run(tmp_path, provider="openai", model="gpt-4o", tokens=1000, usd=0.02)
    _seed_run(tmp_path, provider="anthropic", model="claude-haiku", tokens=500, usd=0.01)

    args = Namespace(root=str(tmp_path), since="365d", by=None, emit_json=True)
    rc = stats_run(args, logging.getLogger("test"))
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["runs_count"] == 2
    assert parsed["total"]["total_tokens"] == 1500
    assert parsed["total"]["total_usd"] == pytest.approx(0.03)


def test_stats_groups_by_provider(tmp_path, capsys):
    _seed_run(tmp_path, provider="openai", tokens=1000, usd=0.02)
    _seed_run(tmp_path, provider="openai", tokens=500, usd=0.01)
    _seed_run(tmp_path, provider="anthropic", tokens=200, usd=0.005)

    args = Namespace(root=str(tmp_path), since="365d", by="provider", emit_json=True)
    stats_run(args, logging.getLogger("test"))
    parsed = json.loads(capsys.readouterr().out)
    groups = parsed["groups"]
    assert groups["openai"]["runs"] == 2
    assert groups["openai"]["total_tokens"] == 1500
    assert groups["anthropic"]["runs"] == 1


def test_stats_filters_by_since(tmp_path, capsys):
    """A run with a really old timestamp is excluded by ``--since``."""
    _seed_run(tmp_path, tokens=100, usd=0.001)  # current run; kept by --since 7d
    # Manually rename a run dir to a very old timestamp.
    old_panel = PreviewPanel(run_id="20200101-000000-deadbe", target_dir=tmp_path)
    old_panel.cost = CostSnapshot(provider="openai", model="x", total_tokens=999999, total_usd=10.0)
    old_panel.persist_artifacts()

    args = Namespace(root=str(tmp_path), since="7d", by=None, emit_json=True)
    stats_run(args, logging.getLogger("test"))
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["runs_count"] == 1
    assert parsed["total"]["total_tokens"] == 100  # the old run was filtered out


def test_summarise_handles_no_runs():
    summary = _summarise([], group_by=None)
    assert summary["runs_count"] == 0
    assert summary["total"]["total_tokens"] == 0


def test_stats_picks_up_product_type_from_contract(tmp_path, capsys):
    """Stats joins cost.json with the sibling contract's productType."""
    product_dir = tmp_path / "products" / "p1"
    product_dir.mkdir(parents=True)
    (product_dir / "contract.fluid.yaml").write_text(
        yaml.safe_dump(
            {
                "fluidVersion": "0.7.3",
                "kind": "DataProduct",
                "id": "x.y.p1",
                "name": "p1",
                "domain": "x",
                "metadata": {"layer": "Bronze", "productType": "SDP", "owner": {"team": "d"}},
                "exposes": [],
            }
        )
    )
    panel = PreviewPanel(run_id=new_run_id(), target_dir=product_dir)
    panel.cost = CostSnapshot(provider="openai", model="m", total_tokens=500, total_usd=0.01)
    panel.persist_artifacts()

    args = Namespace(root=str(tmp_path), since="365d", by="type", emit_json=True)
    stats_run(args, logging.getLogger("test"))
    parsed = json.loads(capsys.readouterr().out)
    assert "SDP" in parsed["groups"]
    assert parsed["groups"]["SDP"]["runs"] == 1


# ---------------------------------------------------------------------------
# fluid stats --judge — aggregates judge.json receipts across runs.
# ---------------------------------------------------------------------------


def _seed_judge_run(
    workspace: Path,
    *,
    run_id: str | None = None,
    total: int = 24,
    axes: dict | None = None,
    model: str = "claude-opus-4-7",
) -> Path:
    """Drop a synthetic judge.json into .fluid/agents/<run-id>/."""
    rid = run_id or new_run_id()
    run_dir = workspace / ".fluid" / "agents" / rid
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "total": total,
        "model": model,
        "run_id": rid,
        "axes": axes
        or {
            "correctness": {"score": 4, "reasoning": "ok", "suggestions": []},
            "completeness": {"score": 4, "reasoning": "ok", "suggestions": []},
            "security": {"score": 3, "reasoning": "ok", "suggestions": []},
            "governance": {"score": 4, "reasoning": "ok", "suggestions": []},
            "performance": {"score": 4, "reasoning": "ok", "suggestions": []},
            "documentation": {"score": 5, "reasoning": "ok", "suggestions": []},
        },
    }
    (run_dir / "judge.json").write_text(json.dumps(payload), encoding="utf-8")
    return run_dir


def test_stats_judge_aggregates(tmp_path, capsys):
    _seed_judge_run(tmp_path, total=24)
    _seed_judge_run(
        tmp_path,
        total=18,
        axes={
            "correctness": 3,
            "completeness": 3,
            "security": 2,
            "governance": 4,
            "performance": 3,
            "documentation": 3,
        },
    )

    args = Namespace(root=str(tmp_path), since="365d", by=None, emit_json=True, judge=True)
    rc = stats_run(args, logging.getLogger("test"))
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["runs_count"] == 2
    assert parsed["average_total"] == pytest.approx(21.0)
    # Flat axes from int payload + nested axes from dict payload both
    # roll up under the same axis name.
    assert "correctness" in parsed["axes"]
    assert parsed["axes"]["correctness"] == pytest.approx(3.5)


def test_stats_judge_empty_workspace(tmp_path, capsys):
    args = Namespace(root=str(tmp_path), since="365d", by=None, emit_json=True, judge=True)
    rc = stats_run(args, logging.getLogger("test"))
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["runs_count"] == 0
    assert parsed["average_total"] == 0
    assert parsed["axes"] == {}


def test_stats_judge_unreadable_file_is_skipped(tmp_path, capsys):
    good = _seed_judge_run(tmp_path, total=20)
    # Drop a corrupted judge.json under a second run dir.
    bad_dir = tmp_path / ".fluid" / "agents" / "20260101-000000-deadbe"
    bad_dir.mkdir(parents=True)
    (bad_dir / "judge.json").write_text("{not-valid-json")

    args = Namespace(root=str(tmp_path), since="365d", by=None, emit_json=True, judge=True)
    stats_run(args, logging.getLogger("test"))
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["runs_count"] == 1  # only the good one
    assert parsed["average_total"] == pytest.approx(20.0)
    assert good.exists()  # sanity


# ---------------------------------------------------------------------------
# H22 — Deterministic runs (no LLM call) are visible to fluid stats
# ---------------------------------------------------------------------------
#
# Pre-fix: deterministic runs (e.g. ``fluid forge data-model from-source
# --deterministic``) never wrote a cost.json, so ``fluid stats`` reported
# "0 runs" even though the coordinator manifest clearly recorded a
# completed forge. Now ``RunCostTracker.persist_to_run_dir`` ALWAYS
# writes a receipt — ``mode="deterministic"`` + zero counts — and
# ``_collect_runs`` surfaces them with ``mode`` so ``--by mode`` can
# split them out.


def _seed_deterministic_run(workspace: Path, *, run_id: str | None = None) -> Path:
    """Persist a deterministic (zero-token, zero-USD) cost.json under
    .fluid/agents/<run-id>/ via the real RunCostTracker.persist_to_run_dir
    path so the test exercises the production write."""
    from fluid_build.copilot.cost import get_run_tracker, reset_run_tracker

    reset_run_tracker()
    rid = run_id or new_run_id()
    run_dir = workspace / ".fluid" / "agents" / rid
    get_run_tracker().persist_to_run_dir(run_dir, wall_clock_seconds=1.5)
    reset_run_tracker()
    return run_dir


def test_stats_counts_deterministic_runs(tmp_path, capsys):
    """A from-source --deterministic run must appear in ``fluid stats``."""
    _seed_run(tmp_path, provider="openai", model="gpt-4o", tokens=2000, usd=0.04)
    _seed_deterministic_run(tmp_path)

    args = Namespace(root=str(tmp_path), since="365d", by=None, emit_json=True)
    stats_run(args, logging.getLogger("test"))
    parsed = json.loads(capsys.readouterr().out)
    # Both the LLM run AND the deterministic run are counted.
    assert parsed["runs_count"] == 2
    # USD aggregates only the runs that paid for LLM cost — deterministic
    # contributes nothing (None → skipped in the sum).
    assert parsed["total"]["total_usd"] == pytest.approx(0.04)
    # Token totals reflect only the LLM run.
    assert parsed["total"]["total_tokens"] == 2000


def test_stats_groups_by_mode_separates_deterministic(tmp_path, capsys):
    """``--by mode`` splits deterministic / llm cohorts apart."""
    _seed_run(tmp_path, provider="openai", model="gpt-4o", tokens=500, usd=0.01)
    _seed_run(tmp_path, provider="gemini", model="gemini-2.5-flash", tokens=300, usd=0.005)
    _seed_deterministic_run(tmp_path)
    _seed_deterministic_run(tmp_path)
    _seed_deterministic_run(tmp_path)

    args = Namespace(root=str(tmp_path), since="365d", by="mode", emit_json=True)
    stats_run(args, logging.getLogger("test"))
    parsed = json.loads(capsys.readouterr().out)
    groups = parsed["groups"]
    # Three deterministic runs grouped, two LLM runs grouped.
    assert groups["deterministic"]["runs"] == 3
    assert groups["llm"]["runs"] == 2
    # Deterministic group contributes zero tokens / USD.
    assert groups["deterministic"]["total_tokens"] == 0
    assert groups["deterministic"]["total_usd"] == 0.0
    # LLM group carries the aggregate.
    assert groups["llm"]["total_tokens"] == 800


# ---------------------------------------------------------------------------
# H20 — ``--judge --by X`` is rejected with a clear error instead of
# silently dropping ``--by``. UX-finding 08 caught this: ``--judge``
# short-circuited at line 91 *before* consuming ``--by``, so the user
# got the ungrouped table either way.
# ---------------------------------------------------------------------------


def test_stats_judge_by_provider_is_rejected(tmp_path, capsys):
    """``--judge --by provider`` returns non-zero with a helpful error."""
    _seed_judge_run(tmp_path)

    args = Namespace(root=str(tmp_path), since="365d", by="provider", emit_json=True, judge=True)
    rc = stats_run(args, logging.getLogger("test"))
    assert rc == 2  # non-zero exit
    captured = capsys.readouterr()
    # The rejection message points the user at the --json escape hatch.
    assert "--judge --by provider" in captured.err
    assert "not yet supported" in captured.err
    assert "--json" in captured.err


def test_stats_judge_by_type_is_rejected(tmp_path, capsys):
    """Same rejection for the ``type`` dim."""
    _seed_judge_run(tmp_path)

    args = Namespace(root=str(tmp_path), since="365d", by="type", emit_json=True, judge=True)
    rc = stats_run(args, logging.getLogger("test"))
    assert rc == 2
    assert "--judge --by type" in capsys.readouterr().err


def test_stats_judge_without_by_still_works(tmp_path, capsys):
    """The plain ``--judge`` path stays green — H20 only rejects the combo."""
    _seed_judge_run(tmp_path, total=24)

    args = Namespace(root=str(tmp_path), since="365d", by=None, emit_json=True, judge=True)
    rc = stats_run(args, logging.getLogger("test"))
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["runs_count"] == 1


# ---------------------------------------------------------------------------
# H21 — ``--since garbage`` raises a clean error instead of silently
# no-filtering (mirrors the ``--older-than`` behaviour in memory_cmd).
# ---------------------------------------------------------------------------


def test_stats_since_invalid_relative_returns_error(tmp_path, capsys):
    """``--since 999z`` (unknown unit) is rejected with example forms."""
    _seed_run(tmp_path, tokens=100, usd=0.001)

    args = Namespace(root=str(tmp_path), since="999z", by=None, emit_json=True)
    rc = stats_run(args, logging.getLogger("test"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "--since" in err
    assert "999z" in err
    # Carries example forms so the user knows what's valid.
    assert "30d" in err or "24h" in err or "ISO" in err


def test_stats_since_invalid_days_returns_error(tmp_path, capsys):
    """``--since garbageD`` — a clearly malformed days spec."""
    args = Namespace(root=str(tmp_path), since="garbaged", by=None, emit_json=True)
    rc = stats_run(args, logging.getLogger("test"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "garbaged" in err
    assert "--since" in err


def test_stats_since_empty_is_no_cutoff(tmp_path, capsys):
    """An empty string still means 'no cutoff' (legitimate)."""
    _seed_run(tmp_path, tokens=100, usd=0.001)
    args = Namespace(root=str(tmp_path), since="", by=None, emit_json=True)
    rc = stats_run(args, logging.getLogger("test"))
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["runs_count"] == 1


def test_parse_since_raises_on_bad_input():
    """Unit-level: ``_parse_since`` raises ValueError on garbage."""
    from fluid_build.cli.stats import _parse_since

    with pytest.raises(ValueError) as exc:
        _parse_since("999z")
    assert "--since" in str(exc.value)
    assert "999z" in str(exc.value)


def test_parse_since_handles_empty_and_none():
    """Empty / None legitimately means 'no cutoff' — must not raise."""
    from fluid_build.cli.stats import _parse_since

    assert _parse_since(None) is None
    assert _parse_since("") is None
    assert _parse_since("   ") is None


def test_parse_since_accepts_iso_date():
    """ISO date round-trips."""
    from fluid_build.cli.stats import _parse_since

    dt = _parse_since("2026-04-01")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 4 and dt.day == 1


def test_collect_runs_back_compat_old_cost_json_without_mode(tmp_path):
    """Older cost.json receipts (preview-panel-authored, no ``mode``
    field) must still surface with an inferred ``mode`` so the
    ``--by mode`` grouping is well-defined."""
    from fluid_build.cli.stats import _collect_runs

    # Hand-craft an old-style cost.json: no mode field, zero tokens
    # (mimics a deterministic-shape preview-panel receipt).
    run_dir = tmp_path / ".fluid" / "agents" / new_run_id()
    run_dir.mkdir(parents=True)
    (run_dir / "cost.json").write_text(
        json.dumps(
            {
                "provider": "",
                "model": "",
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "total_usd": None,
                "wall_clock_seconds": 0.0,
            }
        )
    )
    runs = list(_collect_runs(tmp_path, cutoff=None))
    assert len(runs) == 1
    # Inferred — zero tokens + no calls → deterministic.
    assert runs[0]["mode"] == "deterministic"
