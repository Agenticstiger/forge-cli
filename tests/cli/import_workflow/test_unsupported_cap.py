# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The "unsupported" list of an import report is capped; the rest is counted."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from fluid_build.cli.import_workflow import _pipeline
from fluid_build.cli.import_workflow.airflow import AirflowImporter
from fluid_build.cli.import_workflow.registry import ImportReport

pytestmark = pytest.mark.unit

DAG = textwrap.dedent("""
    from datetime import datetime
    from airflow import DAG
    from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator

    with DAG(dag_id="dag_{n}", schedule=None, start_date=datetime(2026, 1, 1)) as dag:
        SparkSubmitOperator(task_id="task_{n}", application="s3://elsewhere/jobs/job_{n}.py")
    """)


def test_an_estate_of_unjoined_tasks_lists_a_bounded_number_of_lines(tmp_path: Path):
    dags = tmp_path / "dags"
    dags.mkdir()
    total = _pipeline.MAX_UNSUPPORTED_LINES + 40
    for n in range(total):
        (dags / f"dag_{n}.py").write_text(DAG.format(n=n), encoding="utf-8")

    _contracts, report = AirflowImporter().import_to_contracts(str(dags), options={})

    assert len(report.unsupported) == _pipeline.MAX_UNSUPPORTED_LINES + 1
    assert report.unsupported[-1] == "40 more line(s) like these are not listed"
    assert "not joined" in report.unsupported[0]


def test_a_report_within_the_cap_is_left_alone():
    report = ImportReport()
    report.unsupported.extend(f"line {n}" for n in range(_pipeline.MAX_UNSUPPORTED_LINES))
    _pipeline._cap_unsupported(report)
    assert len(report.unsupported) == _pipeline.MAX_UNSUPPORTED_LINES
    assert not any("more line" in line for line in report.unsupported)
