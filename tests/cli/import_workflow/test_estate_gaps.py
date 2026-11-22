# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What the importer reads of an estate whose code builds its commands and paths in loops.

Each test lays out a small project (DAGs, jobs, a conventions file), runs ``fluid import pyspark``
over it as a library call and reads the contracts and the report. They pin the shapes a real
estate was measured to lose.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from fluid_build.cli.import_workflow.airflow import AirflowImporter
from fluid_build.cli.import_workflow.pyspark import PySparkImporter

DAG_HEAD = """
from airflow import DAG
from airflow.providers.amazon.aws.operators.emr import EmrCreateJobFlowOperator
from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
"""
JOB = """
from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()
spark.table("raw.orders").write.saveAsTable("silver.orders")
"""


def lay_out(root: Path, files: Dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


def run_pyspark(root: Path, monkeypatch, files: Dict[str, str], **options: Any):
    lay_out(root, files)
    monkeypatch.chdir(root)
    merged = {"dags": "dags", "conventions": "conventions.yaml", **options}
    return PySparkImporter().import_to_contracts("jobs", options=merged)


def by_id(contracts: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {c["id"]: c for c in contracts}


def dag_of(contract: Dict[str, Any]) -> Optional[str]:
    (build,) = contract["builds"][:1]
    airflow = (build.get("execution") or {}).get("orchestration", {}).get("airflow") or {}
    return airflow.get("dagId")


CONVENTIONS = """
script_roots: {"s3://code/jobs/": jobs}
defaults: {domain: retail, platform: aws}
owners: {orders-team: {team: orders}}
"""


# ── G3: a command line built by += and a loop ─────────────────────────


def test_a_spark_submit_command_built_by_additions_and_a_loop_joins_its_task_to_the_job(
    tmp_path, monkeypatch
):
    contracts, report = run_pyspark(
        tmp_path,
        monkeypatch,
        {
            "jobs/merge.py": JOB,
            "conventions.yaml": CONVENTIONS,
            "dags/copy.py": DAG_HEAD
            + """
CFG = {"APP_ENV": "prod", "REGION": "eu"}
SPARK_SUBMIT = ["spark-submit", "--deploy-mode", "cluster"]
for _var, _val in CFG.items():
    SPARK_SUBMIT += ["--conf", f"spark.yarn.appMasterEnv.{_var}={_val}"]
    SPARK_SUBMIT += ["--conf", f"spark.executorEnv.{_var}={_val}"]
SPARK_SUBMIT += ["s3://code/jobs/merge.py", "--date", "2026-01-01"]
FLOW = {
    "Name": "copy",
    "Steps": [
        {"Name": "merge", "ActionOnFailure": "TERMINATE_CLUSTER",
         "HadoopJarStep": {"Jar": "command-runner.jar", "Args": SPARK_SUBMIT}}
    ],
}
with DAG(dag_id="copy", schedule="30 1 * * *", default_args={"owner": "orders-team"}) as dag:
    EmrCreateJobFlowOperator(task_id="create_job_flow", job_flow_overrides=FLOW)
""",
        },
    )
    (contract,) = contracts
    assert contract["id"] == "silver.orders"
    assert dag_of(contract) == "copy"  # the task was read, and joined to jobs/merge.py
    assert contract["metadata"]["owner"] == {"team": "orders"}
    assert not any("copy" in line and "not joined" in line for line in report.notes)


def test_the_same_command_written_out_gives_the_same_proposal(tmp_path, monkeypatch):
    built = run_pyspark(
        tmp_path / "built",
        monkeypatch,
        {
            "jobs/merge.py": JOB,
            "conventions.yaml": CONVENTIONS,
            "dags/copy.py": DAG_HEAD
            + """
CMD = ["spark-submit"]
CMD += ["--conf", "a=1"]
CMD += ["s3://code/jobs/merge.py"]
FLOW = {"Steps": [{"Name": "m", "HadoopJarStep": {"Jar": "command-runner.jar", "Args": CMD}}]}
with DAG(dag_id="copy", schedule="@daily", default_args={"owner": "orders-team"}) as dag:
    EmrCreateJobFlowOperator(task_id="create_job_flow", job_flow_overrides=FLOW)
""",
        },
    )[0]
    written = run_pyspark(
        tmp_path / "written",
        monkeypatch,
        {
            "jobs/merge.py": JOB,
            "conventions.yaml": CONVENTIONS,
            "dags/copy.py": DAG_HEAD
            + """
CMD = ["spark-submit", "--conf", "a=1", "s3://code/jobs/merge.py"]
FLOW = {"Steps": [{"Name": "m", "HadoopJarStep": {"Jar": "command-runner.jar", "Args": CMD}}]}
with DAG(dag_id="copy", schedule="@daily", default_args={"owner": "orders-team"}) as dag:
    EmrCreateJobFlowOperator(task_id="create_job_flow", job_flow_overrides=FLOW)
""",
        },
    )[0]
    assert built == written


# ── G6: a landing path with a run date in it ──────────────────────────

LANDING_CONVENTIONS = """
paths:
  - prefix: "s3://lake/landing/retail/"
    database: landing
    platform: aws
    format: json
defaults: {domain: retail, platform: aws}
"""


def test_a_landing_read_with_a_run_date_is_a_consumed_source(tmp_path, monkeypatch):
    contracts, report = run_pyspark(
        tmp_path,
        monkeypatch,
        {
            "conventions.yaml": LANDING_CONVENTIONS,
            "dags/none.py": "x = 1\n",
            "jobs/orders.py": """
                from pyspark.sql import SparkSession

                LANDING = "s3://lake/landing/retail"

                def run(spark, run_date):
                    orders = spark.read.json(f"{LANDING}/orders/dt={run_date}/")
                    orders.write.saveAsTable("silver.orders")
            """,
        },
    )
    (contract,) = contracts
    assert [(c["productId"], c["exposeId"]) for c in contract["consumes"]] == [
        ("landing.orders", "orders")
    ]
    assert not any("function parameter" in line for line in report.unsupported)
    assert any("is taken as that table" in line for line in report.notes)


# ── Airflow Variables ─────────────────────────────────────────────────

VARIABLE_DAG = (
    DAG_HEAD
    + """
from airflow.models import Variable

ENV = Variable.get("environment", "dev")
JOBS = {"dev": "s3://code-dev/jobs", "prod": "s3://code/jobs"}

with DAG(dag_id="legacy", schedule="@daily", default_args={"owner": "orders-team"}) as dag:
    SparkSubmitOperator(
        task_id="run",
        application=f"{JOBS[ENV]}/merge.py",
        application_args=[
            "--env", "{{ var.value.environment }}", "--date", "{{ ds }}",
            "--x", "{{ var.value.unknown }}",
        ],
    )
"""
)


def task_of(contract: Dict[str, Any]) -> Dict[str, Any]:
    (task,) = contract["builds"][0]["execution"]["orchestration"]["airflow"]["tasks"]
    return task


def test_a_dag_that_reads_its_environment_from_an_airflow_variable_is_joined_to_its_job(
    tmp_path, monkeypatch
):
    contracts, report = run_pyspark(
        tmp_path,
        monkeypatch,
        {
            "jobs/merge.py": JOB,
            "dags/legacy.py": VARIABLE_DAG,
            "conventions.yaml": CONVENTIONS
            + "airflow_variables:\n  prod: {environment: prod}\n  dev: {environment: dev}\n"
            + "environment: prod\n",
        },
    )
    (contract,) = contracts
    assert dag_of(contract) == "legacy"
    # the templates of the Variables the conventions name are filled in; any other stays as written
    assert task_of(contract)["params"]["args"] == [
        "--env",
        "prod",
        "--date",
        "{{ ds }}",
        "--x",
        "{{ var.value.unknown }}",
    ]


def test_without_the_variables_the_dag_is_not_joined_and_its_templates_stay(tmp_path, monkeypatch):
    contracts, report = run_pyspark(
        tmp_path,
        monkeypatch,
        {
            "jobs/merge.py": JOB,
            "dags/legacy.py": VARIABLE_DAG,
            "conventions.yaml": CONVENTIONS,
        },
    )
    (contract,) = contracts
    assert dag_of(contract) is None
    assert any("the Airflow Variable environment" in line for line in report.notes), report.notes


def test_the_environment_chosen_decides_which_variables_are_read(tmp_path, monkeypatch):
    conventions = CONVENTIONS + (
        "airflow_variables:\n  prod: {environment: prod}\n  dev: {environment: dev}\n"
        "environment: dev\n"
    )
    contracts, report = run_pyspark(
        tmp_path,
        monkeypatch,
        {"jobs/merge.py": JOB, "dags/legacy.py": VARIABLE_DAG, "conventions.yaml": conventions},
    )
    (contract,) = contracts
    assert task_of(contract)["params"]["args"][:2] == ["--env", "dev"]
    # dev reads its job from s3://code-dev/jobs, which no script root maps: joined by file name
    assert task_of(contract)["params"]["script"] == "s3://code-dev/jobs/merge.py"
    assert any("by file name only" in line for line in report.unsupported), report.unsupported


def test_a_bash_command_with_a_variable_template_in_it_is_read_with_the_variable_filled_in(
    tmp_path, monkeypatch
):
    contracts, _report = run_pyspark(
        tmp_path,
        monkeypatch,
        {
            "jobs/merge.py": JOB,
            "dags/bash.py": """
from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG(dag_id="bash", schedule="@daily", default_args={"owner": "orders-team"}) as dag:
    BashOperator(
        task_id="run",
        bash_command="spark-submit {{ var.value.code_root }}/merge.py --env {{ var.value.environment }}",
    )
""",
            "conventions.yaml": CONVENTIONS
            + "airflow_variables:\n  prod: {code_root: 's3://code/jobs', environment: prod}\n",
        },
    )
    (contract,) = contracts
    assert dag_of(contract) == "bash"
    task = task_of(contract)
    assert task["params"]["script"] == "s3://code/jobs/merge.py"
    assert task["params"]["args"] == ["--env", "prod"]
