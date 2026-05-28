# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Preview panel surfaces predicted quality + enrichment badge.

The panel's QualitySnapshot is populated by the runtime from the
generation result's provenance. Surfacing it BEFORE the write closes
the feedback loop — the user can iterate on a low-scoring contract
rather than discovering the score post-hoc in
``.fluid/agents/<run-id>/judge.json``.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

from fluid_build.cli._preview_panel import (
    PendingFile,
    PreviewPanel,
    QualitySnapshot,
    _render_plain,
    render,
)


def _panel(tmp_path: Path, **kwargs) -> PreviewPanel:
    p = PreviewPanel(run_id="20260527-000000-test01", target_dir=tmp_path)
    p.add_file("contract.fluid.yaml", "kind: DataProduct\n")
    for k, v in kwargs.items():
        setattr(p, k, v)
    return p


def test_quality_snapshot_has_data_false_by_default():
    assert QualitySnapshot().has_data is False


def test_quality_snapshot_has_data_true_with_axes():
    assert QualitySnapshot(axes={"correctness": 4}).has_data is True


def test_quality_snapshot_has_data_true_with_total():
    assert QualitySnapshot(total=20).has_data is True


def test_plain_render_omits_quality_when_no_data(tmp_path, capsys):
    panel = _panel(tmp_path)
    _render_plain(panel)
    out = capsys.readouterr().out
    assert "Predicted quality" not in out


def test_plain_render_shows_quality_with_axes_and_total(tmp_path, capsys):
    panel = _panel(
        tmp_path,
        quality=QualitySnapshot(
            total=24,
            axes={
                "correctness": 4,
                "completeness": 4,
                "security": 3,
                "governance": 4,
                "performance": 4,
                "documentation": 5,
            },
        ),
    )
    _render_plain(panel)
    out = capsys.readouterr().out
    assert "Predicted quality: 24/30" in out
    assert "correctness   4/5" in out
    assert "security      3/5" in out


def test_plain_render_shows_enrichment_badge(tmp_path, capsys):
    panel = _panel(
        tmp_path,
        quality=QualitySnapshot(total=24, enrichment_applied=True),
    )
    _render_plain(panel)
    out = capsys.readouterr().out
    assert "enrichment applied" in out


def test_plain_render_shows_critique_badge(tmp_path, capsys):
    panel = _panel(
        tmp_path,
        quality=QualitySnapshot(total=24, critique_applied=True),
    )
    _render_plain(panel)
    out = capsys.readouterr().out
    assert "critique applied" in out


def test_plain_render_both_badges_together(tmp_path, capsys):
    panel = _panel(
        tmp_path,
        quality=QualitySnapshot(total=24, enrichment_applied=True, critique_applied=True),
    )
    _render_plain(panel)
    out = capsys.readouterr().out
    assert "enrichment applied" in out
    assert "critique applied" in out


@pytest.mark.skipif(
    "not __import__('importlib.util').util.find_spec('rich')",
    reason="rich is optional",
)
def test_rich_render_shows_quality_block(tmp_path):
    """Smoke test: the rich-path renderer doesn't crash when quality has data."""
    from rich.console import Console

    sink = StringIO()
    console = Console(file=sink, force_terminal=False, width=120)
    panel = _panel(
        tmp_path,
        quality=QualitySnapshot(
            total=24,
            axes={
                "correctness": 4,
                "completeness": 4,
                "security": 3,
                "governance": 4,
                "performance": 4,
                "documentation": 5,
            },
        ),
    )
    render(panel, console=console)
    out = sink.getvalue()
    assert "Predicted quality" in out
    assert "24/30" in out


def test_rich_render_omits_quality_when_empty(tmp_path):
    """No quality data → no Predicted quality line in rich output."""
    from rich.console import Console

    sink = StringIO()
    console = Console(file=sink, force_terminal=False, width=120)
    panel = _panel(tmp_path)
    render(panel, console=console)
    out = sink.getvalue()
    assert "Predicted quality" not in out
