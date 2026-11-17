# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A scheduled Spark build is one DAG whose task runs ``fluid spark run``, not ``fluid apply``."""

from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict

import pytest

import fluid_build
from fluid_build.schedulers.airflow import fluid_apply
from tests.cli._schedule_dag_fixtures import load_dag, run_task

WORKER = {"FLUID_PROJECT_DIR": "", "PGPASSWORD": "pw"}


def _spark_build(**edits: Any) -> Dict[str, Any]:
    build: Dict[str, Any] = {
        "id": "daily_orders",
        "engine": "spark",
        "properties": {
            "sql": "SELECT 1",
            "parameters": {"spark": {"runtime": "emr-serverless"}},
        },
        "execution": {"trigger": {"type": "schedule", "schedule": "0 2 * * *"}},
    }
    build.update(edits)
    return build


def _contract(*builds: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "sales.orders",
        "name": "Orders",
        "builds": list(builds or [_spark_build()]),
    }


def _render(contract: Dict[str, Any], env: Any = None) -> Dict[str, str]:
    return fluid_apply.render_fluid_apply_dags(
        contract, env=env, contract_path="contracts/orders/contract.fluid.yaml"
    )


def _stream(trigger: Dict[str, Any]) -> Dict[str, Any]:
    return _spark_build(
        properties={
            "stream": {"source": "orders", "trigger": trigger},
            "parameters": {"spark": {"runtime": "glue"}},
        }
    )


def test_a_scheduled_spark_build_runs_fluid_spark_run(tmp_path: Path, monkeypatch):
    (source,) = _render(_contract()).values()
    loaded = load_dag(source, monkeypatch)
    (task,) = loaded.tasks
    assert task["task_id"] == "fluid_spark_run"
    assert loaded.dag["schedule"] == "0 2 * * *"

    project = tmp_path / "project"
    contract = project / "contracts/orders/contract.fluid.yaml"
    contract.parent.mkdir(parents=True)
    contract.write_text("id: x\n", encoding="utf-8")
    run = run_task(task, tmp_path, {**WORKER, "FLUID_PROJECT_DIR": str(project)})
    assert run.returncode == 0, run.stderr
    assert run.argv == ["spark", "run", str(contract), "--build-id", "daily_orders", "--json"]
    assert "apply" not in run.argv


def test_the_overlay_env_is_passed_to_spark_run(tmp_path: Path, monkeypatch):
    (source,) = _render(_contract(), env="aws").values()
    (task,) = load_dag(source, monkeypatch).tasks
    project = tmp_path / "project"
    contract = project / "contracts/orders/contract.fluid.yaml"
    contract.parent.mkdir(parents=True)
    contract.write_text("id: x\n", encoding="utf-8")
    run = run_task(task, tmp_path, {**WORKER, "FLUID_PROJECT_DIR": str(project)})
    assert run.argv[:3] == ["spark", "run", str(contract)]
    assert run.argv[3:5] == ["--env", "aws"]


def test_a_streaming_spark_build_gets_no_dag():
    contract = _contract(_stream({"processingTime": "30 seconds"}))
    assert _render(contract) == {}
    assert fluid_apply.scheduled_builds(contract) == []
    assert fluid_apply.has_scheduled_builds(contract) is False
    assert fluid_apply.uses_fluid_apply_dags(contract, engine="airflow") is False


def test_a_stream_with_available_now_is_a_scheduled_run():
    (source,) = _render(_contract(_stream({"availableNow": True}))).values()
    assert "fluid_spark_run" in source


def test_a_mode_batch_override_is_scheduled_even_with_a_stream_spec():
    build = _stream({"processingTime": "1 minute"})
    build["properties"]["parameters"]["spark"]["mode"] = "batch"
    assert len(_render(_contract(build))) == 1


def test_a_non_spark_build_still_runs_fluid_apply(monkeypatch):
    dbt = {
        "id": "dbt_build",
        "engine": "dbt",
        "execution": {"trigger": {"type": "schedule", "schedule": "0 3 * * *"}},
    }
    files = _render(_contract(_spark_build(), dbt))
    assert len(files) == 2
    tasks = {t["task_id"] for source in files.values() for t in load_dag(source, monkeypatch).tasks}
    assert tasks == {"fluid_spark_run", "fluid_apply"}


def test_retries_come_from_the_builds_execution_retries(monkeypatch):
    build = _spark_build()
    build["execution"]["retries"] = {"maxAttempts": 3}
    (source,) = _render(_contract(build)).values()
    assert load_dag(source, monkeypatch).dag["default_args"]["retries"] == 2


def test_the_run_wait_and_the_task_timeout_come_from_the_runtime_timeout(tmp_path, monkeypatch):
    build = _spark_build()
    build["execution"]["runtime"] = {"timeout": "PT2H"}
    (source,) = _render(_contract(build)).values()
    loaded = load_dag(source, monkeypatch)
    # The task outlives the run's own wait, so `fluid spark run` cancels the run itself.
    assert loaded.dag["default_args"]["execution_timeout"] == timedelta(hours=2, minutes=10)
    (task,) = loaded.tasks
    assert task["env"]["FLUID_DAG_RUN_TIMEOUT"] == "7200"
    project = tmp_path / "p"
    contract = project / "contracts/orders/contract.fluid.yaml"
    contract.parent.mkdir(parents=True)
    contract.write_text("id: x\n", encoding="utf-8")
    run = run_task(task, tmp_path, {**WORKER, "FLUID_PROJECT_DIR": str(project)})
    assert run.argv[-2:] == ["--timeout", "7200"]


def test_without_a_runtime_timeout_the_default_wait_and_a_longer_task_timeout(monkeypatch):
    (source,) = _render(_contract()).values()
    loaded = load_dag(source, monkeypatch)
    assert loaded.dag["default_args"]["execution_timeout"] == timedelta(hours=6, minutes=10)
    (task,) = loaded.tasks
    assert task["env"]["FLUID_DAG_RUN_TIMEOUT"] == ""


@pytest.mark.parametrize("timeout", ["soon", "2h", "P1M", "PT0S", 7200, "P99999999999D"])
def test_an_unusable_runtime_timeout_is_refused(timeout):
    build = _spark_build()
    build["execution"]["runtime"] = {"timeout": timeout}
    with pytest.raises(fluid_apply.ScheduleRenderError, match="timeout"):
        _render(_contract(build))


def test_the_dag_names_the_mwaa_requirement_pinned_to_the_release(monkeypatch):
    monkeypatch.setattr(fluid_build, "__version__", "0.28.0")
    (source,) = _render(_contract()).values()
    assert "data-product-forge[aws]==0.28.0" in source
    assert "requirements.txt" in source


@pytest.mark.parametrize("version", ["0.28.1.dev3", "0.28.0+local", ""])
def test_a_development_build_is_not_pinned_to_a_version_pypi_lacks(monkeypatch, version):
    monkeypatch.setattr(fluid_build, "__version__", version)
    (source,) = _render(_contract()).values()
    assert "data-product-forge[aws]\n" in source and "data-product-forge[aws]==" not in source


def test_the_apply_dag_does_not_mention_spark_or_the_requirement():
    dbt = {
        "id": "dbt_build",
        "engine": "dbt",
        "execution": {"trigger": {"type": "schedule", "schedule": "0 3 * * *"}},
    }
    (source,) = _render(_contract(dbt)).values()
    assert "spark" not in source.lower() and "requirements.txt" not in source


def test_values_reach_the_shell_only_as_environment_variables(monkeypatch):
    build = _spark_build(id="a_b")
    (source,) = _render(_contract(build)).values()
    (task,) = load_dag(source, monkeypatch).tasks
    assert "a_b" not in task["bash_command"] and "contracts/orders" not in task["bash_command"]
    assert task["env"]["FLUID_DAG_BUILD_ID"] == "a_b"


def test_a_spark_build_id_that_is_not_a_plain_identifier_is_refused():
    with pytest.raises(fluid_apply.ScheduleRenderError):
        _render(_contract(_spark_build(id="a; rm -rf /")))


def test_the_contract_is_not_changed_by_rendering():
    contract = _contract()
    before = copy.deepcopy(contract)
    _render(contract)
    assert contract == before


def test_the_apply_dags_task_id_is_rendered_as_it_always_was():
    dbt = {
        "id": "dbt_build",
        "engine": "dbt",
        "execution": {"trigger": {"type": "schedule", "schedule": "0 3 * * *"}},
    }
    (source,) = _render(_contract(dbt)).values()
    assert '        task_id="fluid_apply",\n' in source


def test_a_spark_build_the_managed_lane_would_refuse_keeps_its_apply_dag(monkeypatch):
    contract = _contract()
    contract["fluidVersion"] = "0.7.6"  # `fluid spark run` reads only 0.7.7 and later
    (source,) = _render(contract).values()
    (task,) = load_dag(source, monkeypatch).tasks
    assert task["task_id"] == "fluid_apply"


@pytest.mark.parametrize("build_id", ["my.build", "_x"])
def test_a_spark_build_id_fluid_spark_run_would_refuse_is_refused_at_generation(build_id):
    with pytest.raises(fluid_apply.ScheduleRenderError, match="fluid spark run"):
        _render(_contract(_spark_build(id=build_id)))


@pytest.mark.parametrize("product", ["_orders", "a..b"])
def test_a_product_id_fluid_spark_run_would_refuse_is_refused_at_generation(product):
    contract = _contract()
    contract["id"] = product
    with pytest.raises(fluid_apply.ScheduleRenderError, match="product id"):
        _render(contract)
