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
