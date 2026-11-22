# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A table's Spark maintenance (``exposes[].iceberg.maintenance``) gets a DAG of its own.

Its one task runs ``fluid apply --mode maintain --expose-id <expose> --yes``
(:data:`fluid_apply.MAINTENANCE_APPLY_ARGS`) on the table's ``schedule``: no retries (the next
run is the retry), no outlets, one run at a time.
"""

from __future__ import annotations

import argparse
import copy
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.build_runners.spark._reconcile import DEFAULT_RUN_WAIT_S
from fluid_build.cli import generate_schedule
from fluid_build.schedulers.airflow import fluid_apply
from tests.cli._schedule_dag_fixtures import load_dag
from tests.spark.conftest import CONTRACT as STREAM_CONTRACT

MAINTENANCE = {
    "schedule": "0 3 * * *",
    "compaction": {},
    "expireSnapshots": {"olderThan": "P5D"},
}
PRODUCT = "commerce.orders-stream"
FILE = "maintain_order_lines_dag.py"


def _contract(**maintenance: Any) -> Dict[str, Any]:
    contract = copy.deepcopy(STREAM_CONTRACT)
    block = {**MAINTENANCE, **maintenance}
    contract["exposes"][0]["iceberg"] = {"maintenance": block}
    return contract


def _files(contract: Dict[str, Any], env: Any = None) -> Dict[str, str]:
    return fluid_apply.render_fluid_apply_dags(contract, env=env, contract_path="c.fluid.yaml")


def _kept_alive(contract: Dict[str, Any]) -> Dict[str, Any]:
    contract["builds"][0]["execution"] = {"retries": {"maxAttempts": 5}}
    return contract


def test_a_stream_with_maintenance_gets_one_dag_for_the_table(monkeypatch) -> None:
    files = _files(_contract())
    assert sorted(files) == [FILE]
    loaded = load_dag(files[FILE], monkeypatch)
    assert loaded.dag["dag_id"] == f"{PRODUCT}__maintain_order_lines"
    assert loaded.dag["schedule"] == "0 3 * * *"
    assert loaded.dag["max_active_runs"] == 1 and loaded.dag["catchup"] is False
    assert loaded.dag["default_args"]["retries"] == 0
    assert loaded.dag["default_args"]["execution_timeout"] == timedelta(
        seconds=fluid_apply.MAINTENANCE_TASK_TIMEOUT_S
    )
    (task,) = loaded.tasks
    assert task["task_id"] == "fluid_maintain"
    assert task["env"]["FLUID_DAG_BUILD_ID"] == "ingest-orders"
    assert task["env"]["FLUID_DAG_EXPOSE_ID"] == "order_lines"
    assert "outlets" not in task
    assert "EXPOSE_ID = 'order_lines'" in files[FILE]
    assert "BUILD_ID = 'ingest-orders'" in files[FILE]


def test_the_env_is_part_of_the_dag_id() -> None:
    assert f"dag_id='{PRODUCT}__aws__maintain_order_lines'" in _files(_contract(), "aws")[FILE]


def test_the_script_ends_with_the_maintain_arguments() -> None:
    lines = fluid_apply.MAINTENANCE_SCRIPT_LINES
    assert 'set -- "$@" --mode maintain --expose-id "$FLUID_DAG_EXPOSE_ID" --yes' in lines
    assert fluid_apply.MAINTENANCE_APPLY_ARGS == ("--mode", "maintain")
    assert lines[-3:] == fluid_apply.BASH_SCRIPT_LINES[-3:]
    assert "--mode maintain --expose-id" in _files(_contract())[FILE]
    assert "--build-id" not in " ".join(ln for ln in lines if ln.startswith('set -- "$@" --mode'))


def test_the_timezone_and_cron_come_from_the_table() -> None:
    source = _files(_contract(schedule="30 4 * * 0", timezone="Europe/Zurich"))[FILE]
    assert "SCHEDULE = '30 4 * * 0'" in source
    assert "TIMEZONE = 'Europe/Zurich'" in source
    assert "RETRIES = 0" in source


def test_the_task_outlives_the_run_it_waits_for() -> None:
    assert fluid_apply.MAINTENANCE_TASK_TIMEOUT_S > DEFAULT_RUN_WAIT_S


def test_a_kept_alive_stream_gets_both_dags() -> None:
    files = _files(_kept_alive(_contract()))
    assert sorted(files) == ["ingest_orders_dag.py", FILE]
    assert 'task_id="fluid_keepalive"' in files["ingest_orders_dag.py"]
    assert 'task_id="fluid_maintain"' in files[FILE]


def test_a_batch_build_gets_its_dag_and_the_maintenance_dag() -> None:
    contract = _contract()
    build = contract["builds"][0]
    build["execution"] = {"trigger": {"type": "schedule", "schedule": "0 2 * * *"}}
    build["properties"] = {
        "sql": "SELECT 1 AS event_date",
        "parameters": {"spark": {"runtime": "emr-serverless", "mode": "batch"}},
    }
    assert sorted(_files(contract)) == ["ingest_orders_dag.py", FILE]


def test_scheduled_builds_leave_maintenance_out() -> None:
    contract = _kept_alive(_contract())
    assert [b.build_id for b in fluid_apply.scheduled_builds(contract)] == ["ingest-orders"]
    dags = fluid_apply.scheduled_dags(contract)
    assert [(d.key, d.maintains) for d in dags] == [
        ("ingest-orders", None),
        ("maintain_order_lines", "order_lines"),
    ]
    assert fluid_apply.scheduled_builds(_contract()) == []


def test_a_glue_table_gets_no_dag() -> None:
    contract = _contract(backend="glue", roleArn="arn:aws:iam::123456789012:role/glue-optimizer")
    del contract["exposes"][0]["iceberg"]["maintenance"]["schedule"]
    assert _files(contract) == {}
    assert not fluid_apply.has_scheduled_builds(contract, maintenance=True)


def test_an_invalid_maintenance_block_is_refused() -> None:
    with pytest.raises(fluid_apply.ScheduleRenderError):
        _files(_contract(compaction={"strategy": "nope"}))


def test_every_rendered_file_compiles() -> None:
    for contract in (_contract(), _kept_alive(_contract())):
        for name, source in _files(contract, "aws").items():
            compile(source, name, "exec")


def test_two_dags_on_one_file_name_are_refused() -> None:
    """A build called ``maintain_order_lines`` and the table's maintenance share a file name."""
    contract = _kept_alive(_contract())
    contract["builds"][0]["id"] = "maintain-order-lines"
    contract["exposes"][0]["exposeId"] = "order_lines"
    contract["builds"][0]["outputs"] = ["order_lines"]
    # The contract check names it first; the renderer's own file check is the backstop.
    with pytest.raises(
        fluid_apply.ScheduleRenderError,
        match="has the name this table's maintenance takes|same DAG file",
    ):
        _files(contract)


def test_a_dag_it_rendered_reads_back_as_the_products(tmp_path: Path) -> None:
    (tmp_path / FILE).write_text(_files(_contract(), "aws")[FILE], encoding="utf-8")
    assert fluid_apply.rendered_dag_of(tmp_path / FILE) == (PRODUCT, "aws")


def test_fluid_generate_schedule_writes_the_maintenance_dag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "c.fluid.yaml").write_text(
        yaml.safe_dump(_contract(), sort_keys=False), encoding="utf-8"
    )
    parser = argparse.ArgumentParser()
    generate_schedule.register_subcommand(parser.add_subparsers())
    monkeypatch.chdir(tmp_path)
    args = parser.parse_args(["schedule", "c.fluid.yaml", "-o", "out"])
    assert generate_schedule.run(args, logging.getLogger("test.maintenance")) == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [FILE]


def test_a_maintenance_dag_alerts_through_the_contracts_routes() -> None:
    """The contract's alert routes are wired into its maintenance DAG as into its other DAGs:
    a failed maintenance run alerts like a failed build."""
    contract = _contract()
    contract["alerts"] = {
        "routes": [
            {"name": "ops", "kind": "slack", "connection": "slack_ops"},
            {"name": "oncall", "kind": "pagerduty", "connection": "pagerduty_ops"},
        ]
    }
    source = _files(contract)[FILE]
    compile(source, FILE, "exec")
    assert "**ALERT_TASK_ARGS," in source and "**ALERT_DAG_ARGS," in source
    assert "SlackWebhookNotifier" in source and "PagerdutyNotifier" in source
    assert "on_failure_callback" in source
    # Its documentation names the routes, after the maintenance run's own text.
    doc = source.split('"""', 2)[1]
    assert doc.startswith("Scheduled fluid table maintenance")
    assert "Alerts, from the contract's alerts.routes: ops (slack" in " ".join(doc.split())
