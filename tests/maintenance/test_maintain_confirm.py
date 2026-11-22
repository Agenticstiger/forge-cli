# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid apply --mode maintain`` at a terminal asks before a run that may delete rows, snapshots
and files, as ``--mode reconcile`` asks before it starts or cancels one; a no starts nothing."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from fluid_build.build_runners.spark import _reconcile
from fluid_build.cli import _spark_apply

from .conftest import maintained, spark_block


def _maintain(monkeypatch, capsys, confirm):
    started = []

    def run_once(run, **_kwargs):
        started.append(run.maintains)
        return SimpleNamespace(
            action="succeeded", error=False, line=lambda: "ok", as_dict=lambda: {}
        )

    monkeypatch.setattr(_reconcile, "run_once", run_once)
    monkeypatch.setattr(_spark_apply, "_require_boto3", lambda: None)
    questions = []

    def ask(question):
        questions.append(question)
        return confirm

    rc = _spark_apply.maintain_only(
        maintained(spark_block()),
        logging.getLogger("t"),
        expose_id=None,
        dry_run=False,
        confirm=ask,
    )
    return rc, started, questions, " ".join(capsys.readouterr().out.split())  # wrapped


def test_a_no_starts_nothing(monkeypatch, capsys) -> None:
    rc, started, questions, out = _maintain(monkeypatch, capsys, confirm=False)
    assert rc == 0 and started == []
    assert len(questions) == 1 and "may delete rows, snapshots and files" in questions[0]
    assert "order_lines maintenance (build ingest-orders, emr-serverless): not started" in out


def test_a_yes_starts_the_run(monkeypatch, capsys) -> None:
    rc, started, _questions, _out = _maintain(monkeypatch, capsys, confirm=True)
    assert rc == 0 and started == ["order_lines"]


def test_a_dry_run_lists_what_the_run_would_do(monkeypatch, capsys) -> None:
    """What would delete rows or files is named before anything runs."""
    monkeypatch.setattr(_reconcile, "run_once", lambda *a, **k: pytest.fail("a dry run started"))
    block = spark_block(
        compaction={"targetFileSizeMb": 256},
        rewriteManifests={},
        expireSnapshots={"olderThan": "P5D", "retainLast": 3},
        removeOrphanFiles={"olderThan": "P3D"},
    )
    rc = _spark_apply.maintain_only(
        maintained(block), logging.getLogger("t"), expose_id="order_lines", dry_run=True
    )
    out = " ".join(capsys.readouterr().out.split())
    assert rc == 0
    for line in (
        "would start one run",
        "compact fluid.commerce.order_lines (binpack; partial-progress.enabled=true, "
        + "partial-progress.max-commits=10, target-file-size-bytes=268435456)",
        "rewrite the manifests of fluid.commerce.order_lines",
        "expire the snapshots of fluid.commerce.order_lines older than 5 days, keeping the last 3",
        "delete the files under s3://commerce-lake/iceberg/commerce/order_lines/ that no "
        + "snapshot of fluid.commerce.order_lines references, older than 3 days",
    ):
        assert line in out, line
