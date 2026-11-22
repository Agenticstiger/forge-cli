# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What a conventions file may point at, and what an environment value may be, end to end.

A team keeps its conventions in a folder of their own beside the repositories they describe
(``config/conventions.yaml``), not inside one of them. A path in the file may reach into the
directories named on the command line (the jobs directory, ``--dags``) as well as into the
file's own folder, and nowhere else. An ``environments`` value may be empty: a table prefix is
``""`` in production and ``dev_`` in development, and that is not the same as a variable the file
never names.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Dict

import pytest
import yaml

from fluid_build._errors import FluidUserError
from fluid_build.cli.import_workflow import _pipeline
from fluid_build.cli.import_workflow.airflow import AirflowImporter
from fluid_build.cli.import_workflow.pyspark import PySparkImporter

DAG = """
from datetime import datetime
from airflow import DAG
from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator

with DAG(
    dag_id="orders_daily",
    schedule="0 2 * * *",
    start_date=datetime(2026, 1, 1),
    default_args={"owner": "retail-data"},
) as dag:
    SparkSubmitOperator(task_id="orders", application="s3://acme-code/jobs/orders.py")
"""
JOB = """
from acme_config.settings import RAW, CURATED
from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()
spark.table(RAW).write.saveAsTable(CURATED)
"""
SETTINGS = 'RAW = "retail.orders_raw"\nCURATED = "retail.orders"\n'


def lay_out(root: Path, conventions_at: str, script_root: str, settings: str) -> None:
    """Two repositories under ``repos/``: the jobs, and the DAGs with the constants the jobs read."""
    files: Dict[str, str] = {
        "repos/dags/orders_dag.py": DAG,
        "repos/dags/acme_config/settings.py": SETTINGS,
        "repos/spark-scripts/jobs/orders.py": JOB,
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    conventions = {
        "script_roots": {"s3://acme-code/jobs/": script_root},
        "config_modules": [{"module": "acme_config.settings", "path": settings}],
        "defaults": {"domain": "retail", "platform": "aws"},
        "owners": {"retail-data": {"team": "retail-data"}},
    }
    target = root / conventions_at
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(conventions), encoding="utf-8")


def run_pyspark(conventions_at: str):
    return PySparkImporter().import_to_contracts(
        "repos/spark-scripts", options={"dags": "repos/dags", "conventions": conventions_at}
    )


def test_conventions_beside_the_repositories_give_what_conventions_inside_them_give(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    lay_out(
        tmp_path,
        "repos/conventions.yaml",
        "spark-scripts/jobs",
        "dags/acme_config/settings.py",
    )
    inside, _report = run_pyspark("repos/conventions.yaml")
    assert [c["id"] for c in inside] == ["retail.orders"]  # the constants came from the DAGs repo

    lay_out(
        tmp_path,
        "config/conventions.yaml",
        "../repos/spark-scripts/jobs",
        "../repos/dags/acme_config/settings.py",
    )
    beside, _report = run_pyspark("config/conventions.yaml")
    assert beside == inside
    (build,) = beside[0]["builds"]
    assert build["execution"]["orchestration"]["airflow"]["dagId"] == "orders_daily"  # the DAG
    assert beside[0]["metadata"]["owner"] == {"team": "retail-data"}  # was joined to the job


@pytest.mark.parametrize(
    "script_root, settings",
    [
        ("../repos/elsewhere", "../repos/dags/acme_config/settings.py"),
        ("../repos/spark-scripts-old/jobs", "../repos/dags/acme_config/settings.py"),
        ("../repos/spark-scripts/jobs", "../repos/dags-old/acme_config/settings.py"),
        ("../repos/spark-scripts/jobs", "../../outside/settings.py"),
        ("/etc", "../repos/dags/acme_config/settings.py"),
        ("../repos/spark-scripts/jobs", "/etc/hosts"),
    ],
    ids=[
        "a sibling that is no named directory",
        "a name that starts like a named directory",
        "a config module in such a sibling",
        "a config module two levels up",
        "an absolute directory elsewhere",
        "an absolute config module elsewhere",
    ],
)
def test_a_path_outside_every_named_directory_is_refused_before_anything_is_read(
    tmp_path, monkeypatch, script_root, settings
):
    monkeypatch.chdir(tmp_path)
    lay_out(tmp_path, "config/conventions.yaml", script_root, settings)
    for sibling in ("repos/elsewhere", "repos/spark-scripts-old/jobs", "repos/dags-old"):
        (tmp_path / sibling).mkdir(parents=True, exist_ok=True)
    with pytest.raises(FluidUserError) as refused:
        run_pyspark("config/conventions.yaml")
    assert "leaves the directory of the conventions file" in refused.value.why
    assert refused.value.extras == {"conventions": "conventions.yaml"}


def test_a_link_out_of_a_named_directory_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    lay_out(
        tmp_path,
        "config/conventions.yaml",
        "../repos/spark-scripts/linked",
        "../repos/dags/acme_config/settings.py",
    )
    (tmp_path / "outside").mkdir()
    (tmp_path / "repos" / "spark-scripts" / "linked").symlink_to(tmp_path / "outside")
    with pytest.raises(FluidUserError) as refused:
        run_pyspark("config/conventions.yaml")
    assert "leaves the directory of the conventions file" in refused.value.why


def test_airflow_names_the_dags_directory_only_so_a_repository_beside_it_is_not_reached(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    lay_out(
        tmp_path,
        "config/conventions.yaml",
        "../repos/spark-scripts/jobs",
        "../repos/dags/acme_config/settings.py",
    )
    with pytest.raises(FluidUserError) as refused:
        AirflowImporter().import_to_contracts(
            "repos/dags", options={"conventions": "config/conventions.yaml"}
        )
    assert "script_roots" in refused.value.why  # the one path the DAGs directory does not hold


def test_the_directories_named_are_the_one_scanned_and_for_pyspark_the_dags_one():
    named = _pipeline._named_directories
    assert named("pyspark", "jobs", {"dags": "dags"}) == ["jobs", "dags"]
    assert named("pyspark", "jobs", {}) == ["jobs"]
    assert named("pyspark", "jobs", {"dags": None}) == ["jobs"]
    assert named("airflow", "dags", {"dags": "ignored"}) == ["dags"]  # that scan never reads it


# ── an empty environment value ────────────────────────────────────────

PREFIXED = """
import os
from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()
PREFIX = os.environ["TABLE_PREFIX"]
spark.table("raw.events").write.saveAsTable(f"curated.{PREFIX}orders")
"""


def run_environment(tmp_path: Path, monkeypatch, conventions: str):
    (tmp_path / "jobs").mkdir(exist_ok=True)
    (tmp_path / "jobs" / "orders.py").write_text(textwrap.dedent(PREFIXED), encoding="utf-8")
    (tmp_path / "conventions.yaml").write_text(textwrap.dedent(conventions), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return PySparkImporter().import_to_contracts(
        "jobs", options={"conventions": "conventions.yaml"}
    )


@pytest.mark.parametrize(
    "selected, table",
    [("prod", "curated.orders"), ("dev", "curated.dev_orders")],
)
def test_a_prefix_that_is_empty_in_production_gives_another_table_than_in_development(
    tmp_path, monkeypatch, selected, table
):
    contracts, report = run_environment(
        tmp_path,
        monkeypatch,
        f"""
        environment: {selected}
        environments:
          prod: {{TABLE_PREFIX: ""}}
          dev: {{TABLE_PREFIX: dev_}}
        """,
    )
    assert [c["id"] for c in contracts] == [table], report.unsupported
    assert not any("TABLE_PREFIX" in line for line in report.unsupported)


def test_a_variable_the_file_does_not_name_stays_unresolved_where_an_empty_one_is_a_value(
    tmp_path, monkeypatch
):
    contracts, report = run_environment(
        tmp_path,
        monkeypatch,
        """
        environment: prod
        environments:
          prod: {OTHER_PREFIX: ""}
        """,
    )
    assert contracts == []  # the write is not a table the code names
    assert any("TABLE_PREFIX" in line for line in report.unsupported), report.unsupported


def test_a_null_is_neither_an_empty_value_nor_a_missing_one(tmp_path, monkeypatch):
    conventions = """
        environment: prod
        environments:
          prod: {TABLE_PREFIX: }
        """
    with pytest.raises(FluidUserError) as refused:
        run_environment(tmp_path, monkeypatch, conventions)
    assert "environments.prod.TABLE_PREFIX" in refused.value.why
    assert 'write ""' in refused.value.why
