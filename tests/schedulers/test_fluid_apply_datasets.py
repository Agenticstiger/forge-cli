# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``trigger.type: dataset | schedule_and_dataset`` render as data-aware DAGs, and every task
declares the assets of the exposes its build writes as ``outlets``.

Airflow 3 names them ``Asset`` (``airflow.sdk``), Airflow 2 ``Dataset`` (``airflow.datasets``);
the generated file imports the Airflow 3 name first, as it does ``DAG`` and ``BashOperator``.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

import pytest

from fluid_build.schedulers.airflow import fluid_apply
from tests.cli._schedule_dag_fixtures import (
    StubAny,
    StubAsset,
    StubAssetOrTime,
    StubCron,
    load_dag,
)
from tests.schedulers.fluid_apply_fixtures import SPARK_CONTRACT, events_pipeline

DAILY = "s3://lake/iceberg/curated/events_daily"
RAW = "s3://lake/iceberg/raw/events"


def _with_consumer(trigger: Dict[str, Any], **extra: Any) -> Dict[str, Any]:
    contract = copy.deepcopy(SPARK_CONTRACT)
    contract["builds"].append(
        {
            "id": "consumer",
            "engine": "dbt",
            "outputs": [],
            "execution": {"trigger": trigger},
            **extra,
        }
    )
    return contract


def _files(contract: Dict[str, Any], env: Any = None) -> Dict[str, str]:
    return fluid_apply.render_fluid_apply_dags(
        contract, env=env, contract_path="contract.fluid.yaml"
    )


def _refused(contract: Dict[str, Any]) -> str:
    with pytest.raises(fluid_apply.ScheduleRenderError) as exc:
        _files(contract)
    return str(exc.value)


def _expose_ref(expose_id: str, product: str = "platform.events") -> Dict[str, str]:
    return {"productId": product, "exposeId": expose_id}


# --- the schedule ----------------------------------------------------------------------------


@pytest.mark.parametrize("airflow3", [True, False])
def test_a_dataset_trigger_runs_when_every_dataset_updates(monkeypatch, airflow3: bool):
    contract = _with_consumer(
        {"type": "dataset", "datasets": [_expose_ref("events_daily"), "bigquery://p/d/t"]}
    )
    source = _files(contract)["consumer_dag.py"]
    loaded = load_dag(source, monkeypatch, airflow3=airflow3)
    kind = "Asset" if airflow3 else "Dataset"
    assert loaded.dag["schedule"] == [StubAsset(DAILY, kind), StubAsset("bigquery://p/d/t", kind)]
    assert loaded.namespace["DATASETS"] == [DAILY, "bigquery://p/d/t"]
    assert "SCHEDULE" not in loaded.namespace
    assert "This DAG runs when every asset in DATASETS has been updated since its last run." in (
        " ".join(source.split())
    )


@pytest.mark.parametrize("airflow3", [True, False])
def test_datasets_operator_any_runs_on_any_update(monkeypatch, airflow3: bool):
    contract = _with_consumer(
        {"type": "dataset", "datasetsOperator": "any", "datasets": ["s3://a/x", "s3://b/y"]}
    )
    loaded = load_dag(_files(contract)["consumer_dag.py"], monkeypatch, airflow3=airflow3)
    kind = "Asset" if airflow3 else "Dataset"
    assert loaded.dag["schedule"] == StubAny(
        (StubAsset("s3://a/x", kind), StubAsset("s3://b/y", kind)),
        "AssetAny" if airflow3 else "DatasetAny",
    )


@pytest.mark.parametrize("airflow3", [True, False])
def test_schedule_and_dataset_runs_on_the_cron_or_the_datasets(monkeypatch, airflow3: bool):
    contract = _with_consumer(
        {
            "type": "schedule_and_dataset",
            "schedule": "0 * * * *",
            "timezone": "Europe/Berlin",
            "datasets": [_expose_ref("events_daily")],
        }
    )
    loaded = load_dag(_files(contract)["consumer_dag.py"], monkeypatch, airflow3=airflow3)
    kind = "Asset" if airflow3 else "Dataset"
    assert loaded.dag["schedule"] == StubAssetOrTime(
        StubCron("0 * * * *", "Europe/Berlin"),
        [StubAsset(DAILY, kind)],
        "AssetOrTimeSchedule" if airflow3 else "DatasetOrTimeSchedule",
    )
    assert loaded.namespace["SCHEDULE"] == "0 * * * *"


def test_the_events_pipeline_runs_hourly_and_after_the_daily_batch(monkeypatch):
    """The stream runs alone; the daily DAG writes events_daily; the hourly DAG runs on its cron
    and whenever that table updates: the whole graph comes from the contract."""
    files = _files(events_pipeline())
    assert sorted(files) == ["daily_curated_dag.py", "hourly_marts_dag.py"]
    daily = load_dag(files["daily_curated_dag.py"], monkeypatch)
    assert [(t["task_id"], t.get("trigger_rule")) for t in daily.tasks] == [
        ("daily-curated", None),
        ("daily-users", "all_success"),
    ]
    assert daily.edges == [("daily-curated", "daily-users")]
    assert daily.tasks[0]["outlets"] == [StubAsset(DAILY, "Asset")]
    hourly = load_dag(files["hourly_marts_dag.py"], monkeypatch)
    assert hourly.dag["schedule"].assets == [StubAsset(DAILY, "Asset")]
    assert hourly.dag["schedule"].timetable == StubCron("0 * * * *", "UTC")


def test_a_dataset_trigger_counts_as_a_scheduled_build():
    contract = {"id": "p", "builds": [{"id": "b", "execution": {"trigger": {"type": "dataset"}}}]}
    assert fluid_apply.has_scheduled_builds(contract)
    assert fluid_apply.uses_fluid_apply_dags(contract, engine=None)


# --- what a dataset names ---------------------------------------------------------------------


def test_another_products_expose_is_named_by_its_uri():
    message = _refused(
        _with_consumer({"type": "dataset", "datasets": [_expose_ref("x", "sales.orders")]})
    )
    assert "names sales.orders/x, an expose of another product" in message
    assert "write its asset URI as a string" in message


def test_an_expose_the_contract_does_not_declare_is_refused():
    message = _refused(_with_consumer({"type": "dataset", "datasets": [_expose_ref("nope")]}))
    assert "names expose 'nope', which this contract does not declare" in message
    assert "'events_raw', 'events_daily'" in message


def test_an_expose_written_only_by_a_stream_would_never_fire():
    message = _refused(_with_consumer({"type": "dataset", "datasets": [_expose_ref("events_raw")]}))
    assert "is written by 'stream-raw', which no DAG runs" in message
    assert "the trigger would never fire" in message


def test_an_expose_that_names_no_asset_is_refused_with_why():
    contract = _with_consumer({"type": "dataset", "datasets": [_expose_ref("events_daily")]})
    contract["exposes"][1]["binding"]["location"]["bucket"] = "{{ env.LAKE_BUCKET }}"
    message = _refused(contract)
    assert "expose 'events_daily' names no Airflow asset" in message
    assert "filled in on the worker at run time" in message


@pytest.mark.parametrize(
    "raw, why",
    [
        ("airflow://x", "Airflow reserves the airflow scheme"),
        ("s3://reader@bucket/k", "carries user info"),
        ("s3://bucket/k?v=1", "takes no query or fragment"),
        ("bigquery://p/only-one", "has 2 path parts after its host"),
        ('s3://b/k"""', "holds a character a URI cannot"),
        ("{{ env.URI }}", "filled in on the worker at run time"),
    ],
)
def test_a_dataset_uri_airflow_would_refuse_is_refused_here(raw: str, why: str):
    assert why in _refused(_with_consumer({"type": "dataset", "datasets": [raw]}))


@pytest.mark.parametrize(
    "trigger, message",
    [
        ({"type": "dataset"}, "needs trigger.datasets"),
        ({"type": "dataset", "datasets": []}, "needs trigger.datasets"),
        ({"type": "dataset", "datasets": ["s3://a/b"], "schedule": "@daily"}, "takes no schedule"),
        ({"type": "schedule_and_dataset", "datasets": ["s3://a/b"]}, "needs trigger.schedule"),
        (
            {"type": "schedule", "schedule": "@daily", "datasets": ["s3://a/b"]},
            "trigger.datasets needs",
        ),
        (
            {"type": "dataset", "datasets": ["s3://a/b"], "datasetsOperator": "some"},
            "not all or any",
        ),
        ({"type": "dataset", "datasets": [7]}, "must be an asset URI or {productId, exposeId}"),
        ({"type": "dataset", "datasets": [{"exposeId": "x"}]}, "needs both productId and exposeId"),
    ],
)
def test_a_trigger_that_cannot_be_rendered_says_what_to_change(trigger, message):
    assert message in _refused(_with_consumer(trigger))


# --- outlets ----------------------------------------------------------------------------------


def test_each_task_declares_the_assets_its_build_writes(monkeypatch):
    loaded = load_dag(
        _files(events_pipeline())["daily_curated_dag.py"], monkeypatch, airflow3=False
    )
    assert [t["outlets"] for t in loaded.tasks] == [
        [StubAsset(DAILY, "Dataset")],
        [StubAsset("s3://lake/iceberg/curated/users_daily", "Dataset")],
    ]


def test_a_build_without_outputs_writes_every_expose_no_build_claims():
    contract = {
        "id": "p",
        "builds": [{"id": "only", "execution": {"trigger": {"cron": "@daily"}}}],
        "exposes": [
            {
                "exposeId": "x",
                "binding": {
                    "platform": "gcp",
                    "location": {"project": "pr", "dataset": "d", "table": "x"},
                },
            },
            {
                "exposeId": "y",
                "binding": {"platform": "local", "location": {"path": "./out/y.parquet"}},
            },
        ],
    }
    (dag,) = fluid_apply.scheduled_dags(contract)
    assert dag.tasks[0].outlets == ("bigquery://pr/d/x",)


def test_an_expose_that_names_no_asset_is_no_outlet():
    contract = copy.deepcopy(SPARK_CONTRACT)
    contract["exposes"][1]["binding"]["location"]["bucket"] = "{{ env.LAKE }}"
    source = _files(contract)["daily_curated_dag.py"]
    assert "outlets=" not in source and "import Asset" not in source


# --- loops ------------------------------------------------------------------------------------


def test_a_dag_triggered_by_its_own_output_is_refused():
    contract = copy.deepcopy(SPARK_CONTRACT)
    contract["builds"][1]["execution"]["trigger"] = {
        "type": "schedule_and_dataset",
        "schedule": "@daily",
        "datasets": [DAILY + "/"],
    }
    message = _refused(contract)
    assert (
        f"the DAG of build 'daily-curated' writes {DAILY}, which is also one of its own" in message
    )


def test_dags_that_trigger_each_other_are_refused():
    contract = copy.deepcopy(SPARK_CONTRACT)
    contract["builds"][1]["execution"]["trigger"] = {
        "type": "dataset",
        "datasets": ["s3://lake/other"],
    }
    contract["builds"].append(
        {
            "id": "other",
            "outputs": ["other_table"],
            "execution": {
                "trigger": {"type": "dataset", "datasets": [_expose_ref("events_daily")]}
            },
        }
    )
    contract["exposes"].append(
        {
            "exposeId": "other_table",
            "binding": {"platform": "aws", "location": {"bucket": "lake", "path": "other"}},
        }
    )
    message = _refused(contract)
    assert "start each other in a loop through the assets they write" in message


def _uris(dags: List[fluid_apply.ScheduledDag]) -> List[tuple]:
    return [(d.build_id, d.schedule.datasets, tuple(t.outlets for t in d.tasks)) for d in dags]


def test_a_dataset_given_twice_is_one_asset():
    contract = _with_consumer(
        {"type": "dataset", "datasets": [_expose_ref("events_daily"), DAILY + "/"]}
    )
    consumer = [d for d in fluid_apply.scheduled_dags(contract) if d.build_id == "consumer"]
    assert _uris(consumer) == [("consumer", (DAILY,), ((),))]


def test_a_schedule_and_dataset_trigger_needs_a_recurring_cron():
    message = _refused(
        _with_consumer(
            {"type": "schedule_and_dataset", "schedule": "@once", "datasets": ["s3://a/b"]}
        )
    )
    assert "runs on a recurring cron, and @once is not one" in message


@pytest.mark.parametrize(
    "writer, why",
    [
        (
            {"execution": {"orchestration": {"engine": "dagster"}, "trigger": {"cron": "@daily"}}},
            "names another engine",
        ),
        ({"execution": {"trigger": {"type": "manual"}}}, "its trigger.type is manual"),
        ({}, "it has no trigger, and no build it depends on has one"),
    ],
)
def test_a_dataset_trigger_on_what_no_task_writes_is_refused(writer, why):
    contract = copy.deepcopy(SPARK_CONTRACT)
    contract["builds"] = [
        {"id": "writer", "outputs": ["events_daily"], **writer},
        {
            "id": "consumer",
            "outputs": [],
            "execution": {
                "trigger": {"type": "dataset", "datasets": [_expose_ref("events_daily")]}
            },
        },
    ]
    message = _refused(contract)
    assert "is written by 'writer', which no DAG runs" in message and why in message


def test_a_writer_that_runs_in_another_builds_dag_fires_the_trigger():
    contract = copy.deepcopy(SPARK_CONTRACT)
    contract["builds"] = [
        {"id": "root", "outputs": [], "execution": {"trigger": {"cron": "@daily"}}},
        {"id": "writer", "outputs": ["events_daily"], "dependencies": [{"buildId": "root"}]},
        {
            "id": "consumer",
            "outputs": [],
            "execution": {
                "trigger": {"type": "dataset", "datasets": [_expose_ref("events_daily")]}
            },
        },
    ]
    assert sorted(_files(contract)) == ["consumer_dag.py", "root_dag.py"]


def test_a_self_trigger_says_how_outputs_change_who_writes_what():
    contract = copy.deepcopy(SPARK_CONTRACT)
    contract["builds"][1]["execution"]["trigger"] = {
        "type": "schedule_and_dataset",
        "schedule": "@daily",
        "datasets": [DAILY],
    }
    assert "a build with no outputs counts as writing every expose no build lists" in _refused(
        contract
    )


def test_a_writer_whose_id_is_no_string_is_no_task_and_no_crash():
    contract = copy.deepcopy(SPARK_CONTRACT)
    contract["builds"] = [
        {"id": ["w"], "outputs": ["events_daily"]},
        {
            "id": "consumer",
            "outputs": [],
            "execution": {
                "trigger": {"type": "dataset", "datasets": [_expose_ref("events_daily")]}
            },
        },
    ]
    assert "which no DAG runs" in _refused(contract)
