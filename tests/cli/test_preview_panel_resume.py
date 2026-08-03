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

"""Tests for the resume-aware preview panel header.

When the panel's ``extra["resume"]`` block is set by the runtime, the
preview header surfaces "(resumed from stage X)" and the cost line
splits into "cached + this session = total".
"""

from __future__ import annotations

from pathlib import Path

from fluid_build.cli._preview_panel import (
    CostSnapshot,
    PreviewPanel,
    _build_panel_title,
    _format_cost_line,
    _format_resume_cost_line,
    _render_plain,
)


def _panel(tmp_path: Path, **kwargs) -> PreviewPanel:
    p = PreviewPanel(run_id="20260527-143022-test01", target_dir=tmp_path)
    p.add_file("contract.fluid.yaml", "kind: DataProduct\n")
    for k, v in kwargs.items():
        setattr(p, k, v)
    return p


def test_title_without_resume(tmp_path: Path):
    p = _panel(tmp_path)
    title = _build_panel_title(p)
    assert "20260527-143022-test01" in title
    assert "resumed" not in title


def test_title_with_resume_includes_stage(tmp_path: Path):
    p = _panel(tmp_path)
    p.extra["resume"] = {"from_stage": "builder"}
    title = _build_panel_title(p)
    assert "resumed from stage builder" in title


def test_title_with_resume_no_stage(tmp_path: Path):
    p = _panel(tmp_path)
    p.extra["resume"] = {"cached_usd": 0.04}
    title = _build_panel_title(p)
    assert "(resumed)" in title


def test_cost_line_without_resume(tmp_path: Path):
    p = _panel(tmp_path, cost=CostSnapshot(total_usd=0.06, total_tokens=600))
    base = _format_cost_line(p.cost)
    out = _format_resume_cost_line(p, base)
    assert out == base  # unchanged


def test_cost_line_with_resume_breaks_down(tmp_path: Path):
    p = _panel(tmp_path, cost=CostSnapshot(total_usd=0.06, total_tokens=600))
    p.extra["resume"] = {"cached_usd": 0.04, "from_stage": "builder"}
    out = _format_resume_cost_line(p, "ignored-base-line")
    # Format: ``$0.0400 cached + $0.0600 this session = $0.1000 total``
    assert "cached" in out
    assert "this session" in out
    assert "$0.0400" in out
    assert "$0.0600" in out
    assert "$0.1000" in out


def test_plain_render_shows_resume_header(tmp_path: Path, capsys):
    p = _panel(tmp_path)
    p.extra["resume"] = {"from_stage": "builder", "cached_usd": 0.04}
    _render_plain(p)
    out = capsys.readouterr().out
    assert "resumed from stage builder" in out
    assert "cached" in out


def test_plain_render_no_resume_no_resume_header(tmp_path: Path, capsys):
    p = _panel(tmp_path, cost=CostSnapshot(total_usd=0.06))
    _render_plain(p)
    out = capsys.readouterr().out
    assert "resumed" not in out
