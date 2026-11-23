# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A table no forge build writes (``iceberg.maintenance.runtime``) still gets its maintenance DAG.

There is no writer build, so the DAG's ``BUILD_ID`` is empty; its one task runs the same
``fluid apply --mode maintain --expose-id <expose> --yes`` and the run starts on the runtime the
block names.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

import pytest

from fluid_build.schedulers.airflow import fluid_apply
from tests.cli._schedule_dag_fixtures import load_dag
from tests.spark.conftest import CONTRACT as STREAM_CONTRACT

pytestmark = pytest.mark.unit

PRODUCT = "commerce.orders-stream"
FILE = "maintain_order_lines_dag.py"
ROLE = "arn:aws:iam::123456789012:role/orders-maint"


def _contract(**maintenance: Any) -> Dict[str, Any]:
    contract = copy.deepcopy(STREAM_CONTRACT)
    contract["builds"] = []
    block: Dict[str, Any] = {
        "schedule": "0 3 * * *",
        "runtime": {
            "runtime": "emr-serverless",
            "applicationId": "00f1abcd2345",
            "executionRoleArn": ROLE,
        },
        "compaction": {},
        "removeOrphanFiles": {"olderThan": "P7D"},
    }
    block.update(maintenance)
    contract["exposes"][0]["iceberg"] = {"maintenance": block}
    return contract


def _files(contract: Dict[str, Any], env: Any = None) -> Dict[str, str]:
    return fluid_apply.render_fluid_apply_dags(contract, env=env, contract_path="c.fluid.yaml")


def test_a_table_no_build_writes_is_one_maintenance_dag() -> None:
    dags = fluid_apply.scheduled_dags(_contract())
    (dag,) = dags
    assert (dag.key, dag.build_id, dag.maintains) == (
        "maintain_order_lines",
        "",
        "order_lines",
    )
    assert fluid_apply.scheduled_builds(_contract()) == []


def test_the_dag_file_has_no_build_and_names_the_table(monkeypatch) -> None:
    files = _files(_contract())
    assert sorted(files) == [FILE]
    source = files[FILE]
    assert "BUILD_ID = ''" in source
    assert "EXPOSE_ID = 'order_lines'" in source
    loaded = load_dag(source, monkeypatch)
    assert loaded.dag["dag_id"] == f"{PRODUCT}__maintain_order_lines"
    assert loaded.dag["schedule"] == "0 3 * * *"
    assert loaded.dag["max_active_runs"] == 1 and loaded.dag["catchup"] is False
    assert loaded.dag["default_args"]["retries"] == 0
    (task,) = loaded.tasks
    assert task["task_id"] == "fluid_maintain"
    assert task["env"]["FLUID_DAG_BUILD_ID"] == ""
    assert task["env"]["FLUID_DAG_EXPOSE_ID"] == "order_lines"
    assert "outlets" not in task


def test_the_doc_says_no_forge_build_writes_the_table() -> None:
    source = _files(_contract())[FILE]
    doc = " ".join(source.split('"""', 2)[1].split())
    assert doc.startswith("Scheduled fluid table maintenance")
    assert "No forge build writes the table" in doc
    assert "iceberg.maintenance.runtime" in doc


def test_the_script_runs_the_maintain_mode_for_the_expose() -> None:
    source = _files(_contract())[FILE]
    assert "--mode maintain --expose-id" in source
    assert fluid_apply.MAINTENANCE_APPLY_ARGS == ("--mode", "maintain")
    assert "--build-id" not in " ".join(
        ln for ln in fluid_apply.MAINTENANCE_SCRIPT_LINES if ln.startswith('set -- "$@" --mode')
    )


def test_the_rendered_file_compiles() -> None:
    for env in (None, "aws"):
        for name, source in _files(_contract(), env).items():
            compile(source, name, "exec")


def test_the_env_is_part_of_the_dag_id() -> None:
    assert f"dag_id='{PRODUCT}__aws__maintain_order_lines'" in _files(_contract(), "aws")[FILE]


def test_the_cron_and_timezone_come_from_the_block() -> None:
    source = _files(_contract(schedule="30 4 * * 0", timezone="Europe/Zurich"))[FILE]
    assert "SCHEDULE = '30 4 * * 0'" in source
    assert "TIMEZONE = 'Europe/Zurich'" in source
    assert "RETRIES = 0" in source


def test_a_table_a_build_writes_keeps_its_build_id() -> None:
    contract = copy.deepcopy(STREAM_CONTRACT)
    contract["exposes"][0]["iceberg"] = {"maintenance": {"schedule": "0 3 * * *", "compaction": {}}}
    source = _files(contract)[FILE]
    assert "BUILD_ID = 'ingest-orders'" in source
    assert "No forge build writes the table" not in source


def test_a_block_that_does_not_read_is_refused() -> None:
    contract = _contract()
    del contract["exposes"][0]["iceberg"]["maintenance"]["runtime"]["executionRoleArn"]
    with pytest.raises(fluid_apply.ScheduleRenderError):
        _files(contract)


def test_a_runtime_table_needs_its_schedule() -> None:
    contract = _contract()
    del contract["exposes"][0]["iceberg"]["maintenance"]["schedule"]
    with pytest.raises(fluid_apply.ScheduleRenderError, match="schedule is required"):
        _files(contract)
