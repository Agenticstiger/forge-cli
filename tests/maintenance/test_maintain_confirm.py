# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid apply --mode maintain`` at a terminal asks before a run that may delete rows, snapshots
and files, as ``--mode reconcile`` asks before it starts or cancels one; a no starts nothing."""

from __future__ import annotations

import logging
from types import SimpleNamespace

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
