# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid import airflow`` records the ids exactly as Airflow spells them when it can.

The schema of the ``fluidVersion`` a proposal declares says what a ``dagId`` and a ``taskId`` may
hold. The 0.7.7 preview takes Airflow's own rule (ASCII letters of either case, digits, ``_``,
``.`` and ``-``), so an id is recorded as it is and the report has nothing to say about ids. The
released schemas (0.7.6 and before) take lower case only, so there an id with capitals is recorded
under the importer's lower case identifier of it, as before (``test_airflow_ids.py``).
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest

from fluid_build.cli.import_workflow import _id_rules, _pipeline
from fluid_build.cli.import_workflow._id_rules import id_rules
from fluid_build.cli.import_workflow.airflow import AirflowImporter
from fluid_build.schema_manager import FluidSchemaManager

HEAD = """
from airflow import DAG
from airflow.operators.empty import EmptyOperator
from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
"""
CONVENTIONS = 'script_roots:\n  "s3://acme-code/jobs/": jobs\nowners:\n  retail: {team: retail}\n'
DAGS = {
    "nightly.py": """
    with DAG("Sales_Daily_V2", schedule="0 3 * * *", default_args={"owner": "retail"}) as dag:
        start = EmptyOperator(task_id="Start_Cluster")
        load = SparkSubmitOperator(task_id="Load.Orders-V2", application="s3://acme-code/jobs/clean.py")
        start >> load
    """,
}


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


def _import(root: Path, monkeypatch: Any, version: str, **options: Any):
    monkeypatch.chdir(root)
    monkeypatch.setattr(_pipeline, "contract_version", lambda: version)
    return AirflowImporter().import_to_contracts(
        "dags", options={"conventions": "conventions.yaml", **options}
    )


def _airflow(contracts: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {c["id"]: c["builds"][0]["execution"]["orchestration"]["airflow"] for c in contracts}


def _tasks(airflow: Dict[str, Any]) -> Dict[str, Any]:
    return {t["taskId"]: t.get("dependencies") for t in airflow.get("tasks", [])}


def _id_lines(report: Any) -> List[str]:
    return [x for x in report.required_defaults if " is recorded as " in x]


def test_the_preview_schema_records_the_ids_exactly_as_airflow_spells_them(tmp_path, monkeypatch):
    _project(tmp_path, DAGS, ["clean"])
    contracts, report = _import(tmp_path, monkeypatch, "0.7.7")
    assert contracts[0]["fluidVersion"] == "0.7.7"
    airflow = _airflow(contracts)["retail.clean"]
    assert airflow["dagId"] == "Sales_Daily_V2"
    assert airflow["dagConfig"] == {"schedule": "0 3 * * *"}
    assert _tasks(airflow) == {"Load.Orders-V2": ["Start_Cluster"]}
    assert _id_lines(report) == []
    assert not [x for x in report.unsupported if "id" in x]
    result = FluidSchemaManager().validate_contract(
        contracts[0], schema_version="0.7.7", offline_only=True
    )
    assert result.is_valid, result.errors


def test_ids_that_differ_only_in_case_stay_two_ids_under_the_preview_schema(tmp_path, monkeypatch):
    _project(
        tmp_path,
        {
            "a.py": """
            with DAG("Orders", schedule=None, default_args={"owner": "retail"}) as dag:
                SparkSubmitOperator(task_id="Run", application="s3://acme-code/jobs/first.py")
                SparkSubmitOperator(task_id="run", application="s3://acme-code/jobs/first.py")
            """,
            "b.py": """
            with DAG("orders", schedule=None, default_args={"owner": "retail"}) as dag:
                SparkSubmitOperator(task_id="Run", application="s3://acme-code/jobs/second.py")
            """,
        },
        ["first", "second"],
    )
    contracts, report = _import(tmp_path, monkeypatch, "0.7.7")
    airflow = _airflow(contracts)
    assert airflow["retail.first"]["dagId"] == "Orders"
    assert set(_tasks(airflow["retail.first"])) == {"Run", "run"}
    assert airflow["retail.second"]["dagId"] == "orders"
    assert _id_lines(report) == []


def test_a_product_made_of_a_dag_or_a_task_is_still_named_by_a_lower_case_identifier(
    tmp_path, monkeypatch
):
    _project(tmp_path, DAGS, ["clean"])
    contracts, _ = _import(tmp_path, monkeypatch, "0.7.7", split_by="dag")
    assert [c["id"] for c in contracts] == ["sales_daily_v2"]
    assert _airflow(contracts)["sales_daily_v2"]["dagId"] == "Sales_Daily_V2"


def test_the_released_schemas_still_get_the_lower_case_mapping(tmp_path, monkeypatch):
    _project(tmp_path, DAGS, ["clean"])
    contracts, report = _import(tmp_path, monkeypatch, "0.7.6")
    airflow = _airflow(contracts)["retail.clean"]
    assert airflow["dagId"] == "sales_daily_v2"
    assert _tasks(airflow) == {"load.orders-v2": ["start_cluster"]}
    lines = _id_lines(report)
    assert any("is recorded as dagId 'sales_daily_v2'" in x for x in lines)
    assert all("lower case letters, digits" in x for x in lines)
    result = FluidSchemaManager().validate_contract(
        contracts[0], schema_version="0.7.6", offline_only=True
    )
    assert result.is_valid, result.errors


def test_the_default_target_is_the_newest_stable_schema_and_decides_which_rule_applies():
    stable = _pipeline.contract_version()
    rules = id_rules(stable)
    assert rules.dag("nightly_orders") and rules.task("nightly_orders")
    # when the schema proposals declare takes Airflow's ids, the importer records them exactly
    assert rules.lower_case_only == (not rules.dag("Nightly_Orders"))


class TestIdRules:
    def test_the_preview_takes_airflows_rule(self):
        rules = id_rules("0.7.7")
        assert not rules.lower_case_only
        for taken in ("Sales_Daily_V2", "a", "x" * 250, "a.b-C_d"):
            assert rules.dag(taken) and rules.task(taken)
        for refused in ("", "Sales Daily", "a/b", "x" * 251, "Salès", "a\n"):
            assert not rules.dag(refused) and not rules.task(refused)

    @pytest.mark.parametrize("version", ["0.7.6", "0.7.5", "0.7.1"])
    def test_a_released_schema_takes_lower_case_only(self, version):
        rules = id_rules(version)
        assert rules.lower_case_only
        assert rules.dag("sales_daily") and rules.task("a.b-c")
        assert not rules.dag("Sales") and not rules.task("Sales")
        assert "lower case" in rules.spelling

    @pytest.mark.parametrize("version", ["", "9.9.9", "../fluid-schema-0.7.7", "0.7.7/../x", "x"])
    def test_a_schema_that_is_not_bundled_gets_the_lower_case_rule(self, version):
        rules = id_rules(version)
        assert rules.lower_case_only
        assert not rules.dag("Sales")

    def test_a_schema_that_holds_no_id_rule_gets_the_lower_case_rule(self, monkeypatch, tmp_path):
        (tmp_path / "fluid-schema-0.0.1.json").write_text('{"$defs": {}}', encoding="utf-8")
        monkeypatch.setattr(_id_rules, "_SCHEMAS", tmp_path)
        id_rules.cache_clear()
        try:
            assert id_rules("0.0.1").lower_case_only
        finally:
            id_rules.cache_clear()
