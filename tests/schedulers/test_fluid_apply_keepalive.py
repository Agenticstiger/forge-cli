# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An always-on Spark stream that declares ``execution.retries`` gets a keepalive DAG.

Its one task runs the reconcile-only ``fluid apply`` (:data:`fluid_apply.KEEPALIVE_APPLY_ARGS`)
for that stream every few minutes, so a stream a Spot interruption stopped starts again. One
run at a time, no catch-up, no Airflow retries: the retry budget is the build's own.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.schedulers.airflow import fluid_apply
from tests.cli._schedule_dag_fixtures import load_dag, run_task
from tests.schedulers.fluid_apply_fixtures import SPARK_CONTRACT


def _contract(**execution: Any) -> Dict[str, Any]:
    contract = copy.deepcopy(SPARK_CONTRACT)
    contract["builds"][0]["execution"] = {"retries": {"maxAttempts": 5}, **execution}
    return contract


def _files(contract: Dict[str, Any], env: Any = "aws") -> Dict[str, str]:
    return fluid_apply.render_fluid_apply_dags(contract, env=env, contract_path="native.fluid.yaml")


def _keepalive_config(schedule: Any) -> Dict[str, Any]:
    return {
        "orchestration": {
            "engine": "airflow",
            "airflow": {"dagId": "events_keepalive", "dagConfig": {"schedule": schedule}},
        }
    }


@pytest.mark.parametrize("airflow3", [True, False])
def test_a_stream_with_retries_gets_a_keepalive_dag(monkeypatch, airflow3: bool):
    files = _files(_contract())
    assert sorted(files) == ["daily_curated_dag.py", "stream_raw_dag.py"]
    loaded = load_dag(files["stream_raw_dag.py"], monkeypatch, airflow3=airflow3)
    assert loaded.dag["dag_id"] == "platform.events__aws__stream-raw"
    assert loaded.dag["schedule"] == fluid_apply.KEEPALIVE_SCHEDULE
    assert loaded.dag["max_active_runs"] == 1 and loaded.dag["catchup"] is False
    assert loaded.dag["default_args"]["retries"] == 0
    assert loaded.dag["default_args"]["execution_timeout"] == timedelta(
        seconds=fluid_apply.KEEPALIVE_TASK_TIMEOUT_S
    )
    (task,) = loaded.tasks
    assert task["task_id"] == "fluid_keepalive"
    assert task["env"]["FLUID_DAG_BUILD_ID"] == "stream-raw"
    assert "outlets" not in task
    assert task["bash_command"] == "\n".join(fluid_apply.KEEPALIVE_SCRIPT_LINES)


def test_each_run_is_the_reconcile_only_apply_of_that_stream(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "native.fluid.yaml").write_text("id: x\n", encoding="utf-8")
    (task,) = load_dag(_files(_contract())["stream_raw_dag.py"], monkeypatch).tasks
    run = run_task(task, tmp_path, {"FLUID_PROJECT_DIR": str(project)})
    assert run.returncode == 0, run.stderr
    assert run.argv is not None
    assert run.argv[2:] == [
        "--env",
        "aws",
        *fluid_apply.KEEPALIVE_APPLY_ARGS,
        "--build-id",
        "stream-raw",
        "--yes",
    ]


def test_the_reconcile_arguments_live_in_one_constant():
    tail = [line for line in fluid_apply.KEEPALIVE_SCRIPT_LINES if "--build-id" in line]
    assert tail == [
        'set -- "$@" '
        + " ".join(fluid_apply.KEEPALIVE_APPLY_ARGS)
        + ' --build-id "$FLUID_DAG_BUILD_ID" --yes'
    ]
    # The keepalive script is the apply script but for that line, and holds no contract text.
    differ = set(fluid_apply.KEEPALIVE_SCRIPT_LINES) ^ set(fluid_apply.BASH_SCRIPT_LINES)
    assert len(differ) == 2
    for word in fluid_apply.KEEPALIVE_APPLY_ARGS:
        assert word.replace("-", "").isalnum(), word


def test_without_retries_a_stream_still_gets_no_dag():
    assert sorted(_files(copy.deepcopy(SPARK_CONTRACT))) == ["daily_curated_dag.py"]


def test_dag_config_schedule_sets_the_cadence(monkeypatch):
    source = _files(_contract(**_keepalive_config("*/2 * * * *")))["stream_raw_dag.py"]
    assert load_dag(source, monkeypatch).dag["schedule"] == "*/2 * * * *"


@pytest.mark.parametrize(
    "schedule, message",
    [
        ("@once", "a keepalive DAG reconciles the stream on a recurring cron"),
        ("*/90 * * *", "is not a five-field cron expression"),
    ],
)
def test_a_cadence_airflow_cannot_run_is_refused(schedule: str, message: str):
    with pytest.raises(fluid_apply.ScheduleRenderError) as exc:
        _files(_contract(**_keepalive_config(schedule)))
    assert "execution.orchestration.airflow.dagConfig.schedule" in str(exc.value)
    assert message in str(exc.value)


@pytest.mark.parametrize("engine", ["none", "dagster"])
def test_a_stream_on_another_engine_gets_no_keepalive(engine: str):
    contract = _contract(orchestration={"engine": engine})
    assert sorted(_files(contract)) == ["daily_curated_dag.py"]


def test_a_stream_that_runs_to_completion_is_a_batch_not_a_keepalive():
    contract = _contract()
    contract["builds"][0]["properties"]["stream"]["trigger"] = {"availableNow": True}
    contract["builds"][0]["execution"]["trigger"] = {"type": "schedule", "schedule": "@hourly"}
    source = _files(contract)["stream_raw_dag.py"]
    assert 'task_id="fluid_apply"' in source and "fluid_keepalive" not in source


def test_a_streams_only_contract_with_retries_needs_a_dag():
    contract = _contract()
    contract["builds"] = contract["builds"][:1]
    assert fluid_apply.has_scheduled_builds(contract)
    assert not fluid_apply.runs_only_outside_schedulers(contract)
    assert sorted(_files(contract)) == ["stream_raw_dag.py"]


def test_a_batch_still_does_not_wait_for_the_stream_it_depends_on():
    contract = _contract()
    contract["builds"][1]["dependencies"] = [{"buildId": "stream-raw"}]
    (daily,) = [d for d in fluid_apply.scheduled_dags(contract) if not d.keepalive]
    assert [t.build_id for t in daily.tasks] == ["daily-curated"]
    assert daily.unwaited[0][0] == "stream-raw"


def test_the_keepalive_reads_back_as_the_products_dag(tmp_path: Path):
    (tmp_path / "stream_raw_dag.py").write_text(_files(_contract())["stream_raw_dag.py"])
    assert fluid_apply.rendered_dag_of(tmp_path / "stream_raw_dag.py") == ("platform.events", "aws")
