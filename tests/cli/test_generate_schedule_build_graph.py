# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid generate schedule`` renders a contract's build graph: builds linked by
``builds[].dependencies`` run in one DAG, and a dataset trigger runs a DAG when the assets
another DAG's tasks write are updated.

The graph a reference AWS product needs, from its contract alone: a raw Kafka stream that
``fluid apply`` keeps running (no DAG), a daily Spark SQL batch and the build that runs after it
(one DAG), and an hourly dbt build that also runs whenever the daily table updates.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from fluid_build.cli import generate_schedule
from fluid_build.cli.scaffold_composer import run as scaffold_composer
from fluid_build.schedulers.airflow import fluid_apply
from tests.cli._schedule_dag_fixtures import StubAsset, load_dag, run_task
from tests.schedulers.fluid_apply_fixtures import SPARK_CONTRACT, events_pipeline

LOG = logging.getLogger("test.generate_schedule_build_graph")
CONTRACT = "contracts/events/contract.fluid.yaml"


def _project(root: Path, contract: dict) -> Path:
    path = root / CONTRACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return path


def _generate(root: Path, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    parser = argparse.ArgumentParser()
    generate_schedule.register_subcommand(parser.add_subparsers())
    monkeypatch.chdir(root)
    return generate_schedule.run(parser.parse_args(["schedule", CONTRACT, "-o", "out", *argv]), LOG)


def test_the_events_graph_renders_from_the_contract_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, events_pipeline())
    assert _generate(tmp_path, monkeypatch, "--env", "aws") == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "daily_curated_dag.py",
        "hourly_marts_dag.py",
    ]
    daily = load_dag((tmp_path / "out" / "daily_curated_dag.py").read_text(), monkeypatch)
    assert daily.dag["dag_id"] == "platform.events__aws__daily-curated"
    assert daily.edges == [("daily-curated", "daily-users")]
    hourly = load_dag((tmp_path / "out" / "hourly_marts_dag.py").read_text(), monkeypatch)
    assert hourly.dag["schedule"].assets == [
        StubAsset("s3://lake/iceberg/curated/events_daily", "Asset")
    ]
    assert daily.tasks[0]["outlets"] == hourly.dag["schedule"].assets


def test_each_task_runs_fluid_apply_for_its_own_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _project(tmp_path, events_pipeline())
    assert _generate(tmp_path, monkeypatch) == 0
    daily = load_dag((tmp_path / "out" / "daily_curated_dag.py").read_text(), monkeypatch)
    for task, build in zip(daily.tasks, ("daily-curated", "daily-users"), strict=True):
        run = run_task(task, tmp_path, {"FLUID_PROJECT_DIR": str(tmp_path)})
        assert run.returncode == 0, run.stderr
        assert run.argv is not None
        assert os.path.realpath(run.argv[1]) == os.path.realpath(contract)
        assert run.argv[2:] == ["--mode", "amend-and-build", "--build-id", build, "--yes"]


def test_a_graph_fluid_cannot_render_fails_with_what_to_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    contract = events_pipeline()
    contract["builds"][2]["execution"] = {"trigger": {"type": "schedule", "schedule": "@hourly"}}
    _project(tmp_path, contract)
    with caplog.at_level(logging.ERROR):
        assert _generate(tmp_path, monkeypatch) == 2
    text = " ".join(caplog.text.split())
    assert "generate_schedule_invalid_schedule" in text
    assert "builds 'daily-curated' and 'daily-users' are linked by builds[].dependencies" in text
    assert not (tmp_path / "out").exists()


def test_a_contract_of_streams_alone_needs_no_dag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    contract = dict(SPARK_CONTRACT, builds=[SPARK_CONTRACT["builds"][0]])
    _project(tmp_path, contract)
    assert _generate(tmp_path, monkeypatch) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "No build needs a DAG" in out
    assert "as a stream that fluid apply keeps running" in out
    assert "No scheduler engine specified" not in out
    assert not (tmp_path / "out").exists()


def test_a_kept_alive_stream_gets_its_keepalive_dag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = dict(SPARK_CONTRACT["builds"][0], execution={"retries": {"maxAttempts": 5}})
    _project(tmp_path, dict(SPARK_CONTRACT, builds=[stream]))
    assert _generate(tmp_path, monkeypatch) == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["stream_raw_dag.py"]


@pytest.mark.parametrize("scheduler", ["prefect", "dagster"])
def test_a_scheduler_with_no_keepalive_has_nothing_to_schedule_for_a_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, scheduler: str
) -> None:
    """Only the Airflow DAGs keep a stream running: another scheduler writes nothing for it,
    and is told where the keepalive DAG comes from."""
    stream = dict(SPARK_CONTRACT["builds"][0], execution={"retries": {"maxAttempts": 5}})
    _project(tmp_path, dict(SPARK_CONTRACT, builds=[stream]))
    assert _generate(tmp_path, monkeypatch, "--scheduler", scheduler) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "No build needs a DAG" in out
    assert "gets a keepalive DAG on Airflow: fluid generate schedule --scheduler airflow" in out
    assert not (tmp_path / "out").exists()


def test_dag_id_and_schedule_overrides_stay_refused_for_a_build_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _project(tmp_path, events_pipeline())
    with caplog.at_level(logging.ERROR):
        assert _generate(tmp_path, monkeypatch, "--dag-id", "x") == 2
    assert "generate_schedule_override_not_supported" in caplog.text


def test_the_composer_scaffold_agrees_that_streams_need_no_dag(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    contract = tmp_path / "contract.fluid.yaml"
    stream = dict(SPARK_CONTRACT["builds"][0], execution={"trigger": {"cron": "@daily"}})
    contract.write_text(yaml.safe_dump(dict(SPARK_CONTRACT, builds=[stream])), "utf-8")
    args = SimpleNamespace(contract=str(contract), env=None, out_dir=str(tmp_path / "dags"))
    assert scaffold_composer(args, LOG) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "as a stream that fluid apply keeps running" in out and "No DAG written." in out
    assert not (tmp_path / "dags").exists()


def test_the_composer_scaffold_leaves_a_kept_alive_stream_to_generate_schedule(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Its validate, plan and apply DAG cannot keep a stream running: it writes none, and says
    which command writes the stream's keepalive DAG."""
    contract = tmp_path / "contract.fluid.yaml"
    stream = dict(SPARK_CONTRACT["builds"][0], execution={"retries": {"maxAttempts": 5}})
    data = dict(SPARK_CONTRACT, builds=[stream])
    assert fluid_apply.has_scheduled_builds(data)  # generate schedule writes its keepalive DAG
    contract.write_text(yaml.safe_dump(data), "utf-8")
    args = SimpleNamespace(contract=str(contract), env=None, out_dir=str(tmp_path / "dags"))
    assert scaffold_composer(args, LOG) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "No DAG written." in out
    assert "gets a keepalive DAG from fluid generate schedule" in out
    assert not (tmp_path / "dags").exists()


def test_regenerating_removes_a_dag_whose_build_now_runs_in_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """With --overwrite, a fluid apply DAG an earlier run wrote for this product and env that
    the contract no longer makes is removed: left there, its build would run twice."""
    contract = events_pipeline()
    contract["builds"][2].pop("dependencies")
    contract["builds"][2]["execution"] = {"trigger": {"type": "schedule", "schedule": "@daily"}}
    _project(tmp_path, contract)
    assert _generate(tmp_path, monkeypatch) == 0
    out = tmp_path / "out"
    assert (out / "daily_users_dag.py").exists()
    rendered = fluid_apply.render_fluid_apply_dags(contract, env="aws", contract_path=CONTRACT)
    other_env = rendered["daily_users_dag.py"]
    (out / "daily_users_other_env_dag.py").write_text(other_env, encoding="utf-8")
    (out / "not_ours_dag.py").write_text(
        '"""A DAG someone wrote."""\nPRODUCT_ID = "platform.events"\n'
    )
    capsys.readouterr()

    _project(tmp_path, events_pipeline())  # daily-users depends on daily-curated again
    assert _generate(tmp_path, monkeypatch, "--overwrite") == 0
    assert sorted(p.name for p in out.iterdir()) == [
        "daily_curated_dag.py",
        "daily_users_other_env_dag.py",
        "hourly_marts_dag.py",
        "not_ours_dag.py",
    ]
    assert "daily_users_dag.py: an earlier run wrote it for this product and env" in (
        " ".join(capsys.readouterr().out.split())
    )
