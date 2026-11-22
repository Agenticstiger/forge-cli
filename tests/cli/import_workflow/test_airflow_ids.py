# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Airflow DAG and task ids the contract's id pattern does not take, recorded under one it does.

Airflow takes capitals in a ``dag_id`` and a ``task_id`` (``Nightly_Orders_Dag``); the contract's
``dagId`` and ``taskId`` take lower case letters, digits, ``_``, ``.`` and ``-``. Such an id is
recorded as the importer's identifier of it, unique in the import, and the report says which
recorded id stands for which Airflow id: the running DAG keeps its own.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest

from fluid_build.cli.import_workflow import _proposal
from fluid_build.cli.import_workflow.airflow import AirflowImporter
from fluid_build.schema_manager import FluidSchemaManager

HEAD = """
from airflow import DAG
from airflow.operators.empty import EmptyOperator
from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
"""
CONVENTIONS = 'script_roots:\n  "s3://acme-code/jobs/": jobs\nowners:\n  retail: {team: retail}\n'


def _job(table: str) -> str:
    return (
        "from pyspark.sql import SparkSession\n"
        "spark = SparkSession.builder.getOrCreate()\n"
        f'spark.table("retail.{table}_raw").write.saveAsTable("retail.{table}")\n'
    )


def _project(root: Path, dags: Mapping[str, str], jobs: List[str]) -> None:
    for rel, text in dags.items():
        path = root / "dags" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(HEAD) + textwrap.dedent(text), encoding="utf-8")
    (root / "jobs").mkdir(exist_ok=True)
    for name in jobs:
        (root / "jobs" / f"{name}.py").write_text(_job(name), encoding="utf-8")
    (root / "conventions.yaml").write_text(CONVENTIONS, encoding="utf-8")


def _import(root: Path, monkeypatch: Any):
    monkeypatch.chdir(root)
    return AirflowImporter().import_to_contracts(
        "dags", options={"conventions": "conventions.yaml"}
    )


def _airflow(contracts: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {c["id"]: c["builds"][0]["execution"]["orchestration"]["airflow"] for c in contracts}


def _tasks(airflow: Dict[str, Any]) -> Dict[str, Any]:
    return {t["taskId"]: t.get("dependencies") for t in airflow.get("tasks", [])}


def _mapped(report: Any) -> List[str]:
    return [x for x in report.required_defaults if " is recorded as " in x]


SUFFIX = (
    ": a FLUID dagId or taskId has only lower case letters, digits, '_', '.' and '-'. The "
    "running DAG keeps its own id"
)


def test_a_dag_id_with_capitals_keeps_its_airflow_block_under_a_fluid_id(tmp_path, monkeypatch):
    _project(
        tmp_path,
        {
            "nightly.py": """
            with DAG("Nightly_Orders_Dag", schedule="0 3 * * *", default_args={"owner": "retail"}) as dag:
                SparkSubmitOperator(task_id="clean", application="s3://acme-code/jobs/clean.py")
            """
        },
        ["clean"],
    )
    contracts, report = _import(tmp_path, monkeypatch)
    airflow = _airflow(contracts)["retail.clean"]
    assert airflow["dagId"] == "nightly_orders_dag"
    assert airflow["dagConfig"] == {"schedule": "0 3 * * *"}
    assert _tasks(airflow) == {"clean": None}
    assert _mapped(report) == [
        "DAG 'Nightly_Orders_Dag' (dags/nightly.py:6) is recorded as dagId 'nightly_orders_dag'"
        + SUFFIX
    ]
    assert not [x for x in report.unsupported if "DAG id" in x]
    result = FluidSchemaManager().validate_contract(
        contracts[0], schema_version=contracts[0]["fluidVersion"], offline_only=True
    )
    assert result.is_valid, result.errors


def test_ids_that_fit_are_recorded_as_they_are_and_nothing_is_said(tmp_path, monkeypatch):
    _project(
        tmp_path,
        {
            "nightly.py": """
            with DAG("nightly.orders-v2", schedule=None, default_args={"owner": "retail"}) as dag:
                SparkSubmitOperator(task_id="clean_orders", application="s3://acme-code/jobs/clean.py")
            """
        },
        ["clean"],
    )
    contracts, report = _import(tmp_path, monkeypatch)
    airflow = _airflow(contracts)["retail.clean"]
    assert airflow["dagId"] == "nightly.orders-v2" and _tasks(airflow) == {"clean_orders": None}
    assert _mapped(report) == []


def test_two_ids_that_map_to_one_are_told_apart_in_file_order_and_said(tmp_path, monkeypatch):
    _project(
        tmp_path,
        {
            "a.py": """
            with DAG("Orders", schedule=None, default_args={"owner": "retail"}) as dag:
                SparkSubmitOperator(task_id="first", application="s3://acme-code/jobs/first.py")
            """,
            "b.py": """
            with DAG("ORDERS", schedule=None, default_args={"owner": "retail"}) as dag:
                SparkSubmitOperator(task_id="second", application="s3://acme-code/jobs/second.py")
            """,
            "c.py": """
            with DAG("orders", schedule=None, default_args={"owner": "retail"}) as dag:
                SparkSubmitOperator(task_id="third", application="s3://acme-code/jobs/third.py")
            """,
        },
        ["first", "second", "third"],
    )
    contracts, report = _import(tmp_path, monkeypatch)
    ids = {pid: airflow["dagId"] for pid, airflow in _airflow(contracts).items()}
    # the id that fits is kept; the other two are numbered in the order of their files
    assert ids == {
        "retail.first": "orders_2",
        "retail.second": "orders_3",
        "retail.third": "orders",
    }
    assert _mapped(report) == [
        "DAG 'ORDERS' (dags/b.py:6) is recorded as dagId 'orders_3' ('orders' is the recorded id "
        + "of DAG 'orders' (dags/c.py:6))"
        + SUFFIX,
        "DAG 'Orders' (dags/a.py:6) is recorded as dagId 'orders_2' ('orders' is the recorded id "
        + "of DAG 'orders' (dags/c.py:6))"
        + SUFFIX,
    ]


def test_task_ids_are_mapped_and_every_dependency_names_the_recorded_id(tmp_path, monkeypatch):
    _project(
        tmp_path,
        {
            "etl.py": """
            with DAG("etl", schedule=None, default_args={"owner": "retail"}) as dag:
                start = EmptyOperator(task_id="Start_Cluster")
                load_upper = SparkSubmitOperator(task_id="Load", application="s3://acme-code/jobs/upper.py")
                load = SparkSubmitOperator(task_id="load", application="s3://acme-code/jobs/lower.py")
                report = SparkSubmitOperator(task_id="Daily Report", application="s3://acme-code/jobs/daily.py")
                start >> load_upper >> report
                start >> load >> report
            """
        },
        ["upper", "lower", "daily"],
    )
    contracts, report = _import(tmp_path, monkeypatch)
    airflow = _airflow(contracts)
    assert _tasks(airflow["retail.upper"]) == {"load_2": ["start_cluster"]}
    assert _tasks(airflow["retail.lower"]) == {"load": ["start_cluster"]}
    assert _tasks(airflow["retail.daily"]) == {"daily-report": ["load_2", "load"]}
    lines = _mapped(report)
    assert (
        "task 'Load' of DAG 'etl' (dags/etl.py:8) is recorded as taskId 'load_2' ('load' is the "
        "recorded id of task 'load' of the same DAG)" + SUFFIX
    ) in lines
    assert any(x.startswith("task 'Start_Cluster' of DAG 'etl'") for x in lines)  # a dependency
    assert any(x.startswith("task 'Daily Report' of DAG 'etl'") for x in lines)
    assert not any(x.startswith("task 'load'") for x in lines)


@pytest.mark.parametrize("split", ["dag", "task"])
def test_a_product_made_of_a_dag_or_a_task_is_named_after_its_recorded_id(
    tmp_path, monkeypatch, split
):
    _project(
        tmp_path,
        {
            "a.py": """
            with DAG("Orders", schedule=None, default_args={"owner": "retail"}) as dag:
                SparkSubmitOperator(task_id="Run", application="s3://acme-code/jobs/first.py")
            """,
            "b.py": """
            with DAG("orders", schedule=None, default_args={"owner": "retail"}) as dag:
                SparkSubmitOperator(task_id="run", application="s3://acme-code/jobs/second.py")
            """,
        },
        ["first", "second"],
    )
    monkeypatch.chdir(tmp_path)
    contracts, _report = AirflowImporter().import_to_contracts(
        "dags", options={"conventions": "conventions.yaml", "split_by": split}
    )
    named = {c["id"]: _airflow([c])[c["id"]]["dagId"] for c in contracts}
    if split == "dag":
        assert named == {"orders_2": "orders_2", "orders": "orders"}
    else:
        assert named == {"orders_2.run": "orders_2", "orders.run": "orders"}


def test_a_re_import_records_the_same_ids_and_says_the_same(tmp_path, monkeypatch):
    _project(
        tmp_path,
        {
            "a.py": """
            with DAG("Orders", schedule=None, default_args={"owner": "retail"}) as dag:
                SparkSubmitOperator(task_id="First_Step", application="s3://acme-code/jobs/first.py")
            """,
            "b.py": """
            with DAG("ORDERS", schedule=None, default_args={"owner": "retail"}) as dag:
                SparkSubmitOperator(task_id="Second_Step", application="s3://acme-code/jobs/second.py")
            """,
        },
        ["first", "second"],
    )
    first, first_report = _import(tmp_path, monkeypatch)
    again, again_report = _import(tmp_path, monkeypatch)
    assert first == again
    assert _mapped(first_report) == _mapped(again_report)


def test_a_dag_id_that_is_not_a_constant_is_still_not_recorded(tmp_path, monkeypatch):
    _project(
        tmp_path,
        {
            "dyn.py": """
            import os
            with DAG(os.environ["DAG_NAME"], schedule=None, default_args={"owner": "retail"}) as dag:
                SparkSubmitOperator(task_id="clean", application="s3://acme-code/jobs/clean.py")
            """
        },
        ["clean"],
    )
    contracts, report = _import(tmp_path, monkeypatch)
    assert "orchestration" not in contracts[0]["builds"][0]["execution"]
    assert any("the DAG id" in x and "is not a constant" in x for x in report.unsupported)


def test_ids_recorded_under_another_id_are_listed_up_to_a_bound(tmp_path, monkeypatch):
    count = _proposal.MAX_ID_LINES + 5
    dags = {
        f"d{i:02d}.py": f"""
            with DAG("Nightly_{i:02d}", schedule=None, default_args={{"owner": "retail"}}) as dag:
                SparkSubmitOperator(task_id="t", application="s3://acme-code/jobs/j{i:02d}.py")
            """
        for i in range(count)
    }
    _project(tmp_path, dags, [f"j{i:02d}" for i in range(count)])
    _contracts, report = _import(tmp_path, monkeypatch)
    lines = _mapped(report)
    assert len(lines) == _proposal.MAX_ID_LINES
    assert (
        "5 more Airflow id(s) are recorded under a FLUID id (not listed one by one); the running "
        "DAGs keep their own ids"
    ) in report.required_defaults


SAMPLE = {
    "OneDomain/Clicks_Streaming_Dag/clicks_streaming_dag.py": """
        from airflow.operators.python import PythonOperator
        from acme_lib import emr_tasks as tasks
        from teamx.dags.OneDomain.Clicks_Streaming_Dag.settings import STREAM_KWARGS

        dag = DAG("Clicks_Streaming_Dag", schedule=None, default_args={"owner": "retail"})
        start = PythonOperator(task_id="start_cluster", python_callable=tasks.start_cluster, dag=dag)
        poll = PythonOperator(task_id="cluster_ready", python_callable=tasks.wait_until_ready, dag=dag)
        clicks_streaming = PythonOperator(task_id="clicks_streaming", python_callable=tasks.run_step, op_kwargs=STREAM_KWARGS, dag=dag)
        stop = PythonOperator(task_id="stop_cluster", python_callable=tasks.stop_cluster, dag=dag)
        start >> poll >> clicks_streaming >> stop
    """,
    "OneDomain/Clicks_Streaming_Dag/settings.py": """
        STREAM_KWARGS = {"additional_params": {"script_path": "s3://acme-code/jobs/clicks.py", "config_path": "s3://acme-conf/clicks.yaml"}}
    """,
}


def test_the_shape_of_a_python_task_estate_keeps_its_dags_tasks_and_dependencies(
    tmp_path, monkeypatch
):
    """Capitalised DAG ids, one folder per DAG with its settings next to it, and the cluster
    tasks around the one that submits: the build keeps the DAG under a FLUID id, the submitting
    task with its params, and its dependency on the task before it."""
    _project(tmp_path, SAMPLE, ["clicks"])
    (tmp_path / "conventions.yaml").write_text(
        CONVENTIONS
        + "submit_helpers:\n"
        + "  run_step:\n"
        + "    script: additional_params.script_path\n"
        + "    config: additional_params.config_path\n"
        + "    kind: emr_add_steps\n",
        encoding="utf-8",
    )
    contracts, report = _import(tmp_path, monkeypatch)
    airflow = _airflow(contracts)["retail.clicks"]
    assert airflow["dagId"] == "clicks_streaming_dag"
    (task,) = airflow["tasks"]
    assert task["taskId"] == "clicks_streaming" and task["dependencies"] == ["cluster_ready"]
    assert task["params"]["python_callable"] == "acme_lib.emr_tasks.run_step"
    assert task["params"]["config"] == "s3://acme-conf/clicks.yaml"
    assert _mapped(report) == [
        "DAG 'Clicks_Streaming_Dag' "
        + "(dags/OneDomain/Clicks_Streaming_Dag/clicks_streaming_dag.py:10) "
        + "is recorded as dagId 'clicks_streaming_dag'"
        + SUFFIX
    ]


def test_a_task_named_after_the_function_it_runs_keeps_both_under_its_recorded_id(
    tmp_path, monkeypatch
):
    """``extract = PythonOperator(task_id="Extract", python_callable=extract)``: the function is
    read from the def above (the name is the task only once the assignment is made), the task is
    recorded under its own FLUID id, numbered when another task holds that id, and the task after
    it depends on that recorded id through the variable."""
    _project(
        tmp_path,
        {
            "nightly.py": """
            import subprocess
            from airflow.operators.python import PythonOperator

            def extract(**kwargs):
                subprocess.run(["spark-submit", "s3://acme-code/jobs/orders.py"])

            with DAG("Nightly_ETL", schedule=None, default_args={"owner": "retail"}) as dag:
                start = EmptyOperator(task_id="Start")
                extract = PythonOperator(task_id="Extract", python_callable=extract)
                daily = SparkSubmitOperator(task_id="extract", application="s3://acme-code/jobs/daily.py")
                start >> extract >> daily
            """
        },
        ["orders", "daily"],
    )
    contracts, report = _import(tmp_path, monkeypatch)
    airflow = _airflow(contracts)
    assert {a["dagId"] for a in airflow.values()} == {"nightly_etl"}
    (task,) = airflow["retail.orders"]["tasks"]
    assert task["taskId"] == "extract_2" and task["dependencies"] == ["start"]
    assert task["params"]["python_callable"] == "extract"
    assert _tasks(airflow["retail.daily"]) == {"extract": ["extract_2"]}
    lines = _mapped(report)
    assert (
        "task 'Extract' of DAG 'Nightly_ETL' (dags/nightly.py:14) is recorded as taskId "
        "'extract_2' ('extract' is the recorded id of task 'extract' of the same DAG)" + SUFFIX
    ) in lines
    assert not any(x.startswith("task 'extract'") for x in lines)


@pytest.mark.parametrize(
    "airflow_id, recorded",
    [
        ("Nightly_Orders", "nightly_orders"),
        ("Bad DAG Id", "bad-dag-id"),
        ("  padded  ", "padded"),
        ("Café_Orders", "cafe_orders"),
        ("-leading.and.trailing-", "leading.and.trailing"),
    ],
)
def test_the_recorded_id_is_the_importers_identifier_of_the_airflow_id(airflow_id, recorded):
    assert _proposal.ident(airflow_id, "dag") == recorded
    assert _proposal._AIRFLOW_ID.fullmatch(recorded)
