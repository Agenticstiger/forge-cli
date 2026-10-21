"""Every Airflow DAG forge-cli generates parses on Airflow 2.7+ and on Airflow 3.

Airflow 3 removed ``DAG(schedule_interval=...)`` and ``airflow.utils.dates.days_ago``,
moved the core operators and sensors to apache-airflow-providers-standard, and
exports ``DAG``, ``dag`` and ``task`` from ``airflow.sdk``. A DAG that uses the
removed names fails to import on Airflow 3 (``TypeError`` / ``ImportError``), so
``fluid export --engine airflow``, ``fluid generate schedule``, ``fluid init``,
``fluid scaffold-composer`` and ``fluid schedule-sync`` all have to emit the
shape both versions accept: ``schedule=``, a fixed ``start_date``, and each
moved name imported from its Airflow 3 home with an ``except ImportError``
fallback to the Airflow 2 path (``providers/common/codegen_utils.py``).

This renders every generator and checks the source, so it needs no Airflow
install. The author also loaded each rendered DAG into a real Airflow 3.2.2 and
2.10.5 ``DagBag`` (see the PR).
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Imported only in an ``except ImportError`` fallback, behind its Airflow 3 path.
AIRFLOW2_ONLY = {
    "airflow.operators.bash",
    "airflow.operators.python",
    "airflow.sensors.external_task",
    "airflow.decorators",
}
AIRFLOW3_HOMES = ("airflow.sdk", "airflow.providers.standard.")

_TASKS = [
    {
        "taskId": "load",
        "type": "provider_action",
        "action": "aws.athena.query",
        "params": {"query": "SELECT 1"},
    },
    {
        "taskId": "publish",
        "type": "provider_action",
        "action": "aws.athena.query",
        "params": {"query": "SELECT 2"},
        "dependsOn": ["load"],
    },
]


def _contract(action_prefix: str = "aws.athena") -> dict:
    tasks = [{**t, "action": f"{action_prefix}.query"} for t in _TASKS]
    return {
        "id": "sales.orders",
        "name": "Orders",
        "orchestration": {"schedule": "0 2 * * *", "timezone": "UTC", "tasks": tasks},
    }


def _provider_actions() -> list:
    from fluid_build.forge.core.provider_actions import ActionType

    def action(action_id, kind, provider, params, depends_on=None):
        return SimpleNamespace(
            action_id=action_id,
            action_type=ActionType(kind),
            provider=provider,
            params=params,
            depends_on=depends_on or [],
            description=None,
        )

    return [
        action(
            "provision-dataset",
            "provisionDataset",
            "gcp",
            {"exposeId": "orders", "binding": {"location": {"project": "p", "dataset": "d"}}},
        ),
        action(
            "schedule-build",
            "scheduleTask",
            "gcp",
            {"engine": "dbt", "script": "orders_model", "buildId": "b1"},
            ["provision-dataset"],
        ),
    ]


def _runtime_dag(actions: list) -> str:
    from fluid_build.runtimes.airflow_provider_actions import AirflowDAGGenerator

    parser = MagicMock()
    parser.parse.return_value = actions
    with patch("fluid_build.forge.core.provider_actions.ProviderActionParser", return_value=parser):
        return AirflowDAGGenerator(logger=logging.getLogger(__name__)).generate_dag(
            {"id": "sales.orders", "name": "Orders", "domain": "sales"}
        )


def _scheduler(provider: str) -> str:
    from fluid_build.schedulers.airflow import AirflowScheduler

    prefix = {"aws": "aws.athena", "gcp": "gcp.bigquery", "snowflake": "sf.snowflake"}[provider]
    (code,) = AirflowScheduler().generate(_contract(prefix), provider=provider).values()
    return code


def _aws(taskflow: bool = False, mixed: bool = False) -> str:
    from fluid_build.providers.aws.codegen import airflow

    contract = _contract()
    if mixed:  # an operator task upstream of a @task
        contract["orchestration"]["tasks"][0].update(
            action="aws.s3.ensure_bucket", params={"bucket": "b"}
        )
    render = airflow.generate_airflow_dag_taskflow if taskflow else airflow.generate_airflow_dag
    return render(contract, "123456789012", "eu-west-1")


def _gcp(dataflow: bool = False) -> str:
    from fluid_build.providers.gcp.codegen.airflow import generate_airflow_dag

    contract = _contract("gcp.bigquery")
    contract["orchestration"]["tasks"].append(
        {
            "taskId": "create_orders",
            "type": "provider_action",
            "action": "gcp.bigquery.create_table",
            "params": {
                "dataset_id": "sales",
                "table_id": "orders",
                "schema": [{"name": "id", "type": "INT64"}],
            },
        }
    )
    if dataflow:
        contract["orchestration"]["tasks"].append(
            {
                "taskId": "flow",
                "type": "provider_action",
                "action": "gcp.dataflow.run_template",
                "params": {"template": "gs://t", "job_name": "j"},
            }
        )
    return generate_airflow_dag(contract, "my-project", "europe-west1")


def _snowflake() -> str:
    from fluid_build.providers.snowflake.codegen.airflow import generate_airflow_dag

    return generate_airflow_dag(_contract("sf.snowflake"), account="acct", database="DB")


def _snowflake_orchestration() -> str:
    """Every operator it imports, in its own task shape (tasks keyed by ``name``)."""
    from fluid_build.providers.snowflake.orchestration.airflow_generator import generate_airflow_dag

    orchestration = {
        "schedule": "0 2 * * *",
        "tasks": [
            {
                "name": "load",
                "type": "provider_action",
                "action": "sf.sql.execute",
                "params": {"sql": "SELECT 1"},
            },
            {"name": "notify", "type": "bash", "command": "echo done"},
            {
                "name": "upstream",
                "type": "sensor",
                "external_dag_id": "raw",
                "external_task_id": "land",
            },
        ],
    }
    return generate_airflow_dag({"id": "sales.orders", "name": "Orders"}, orchestration)


def _init_dag(tmp_path: Path) -> str:
    from fluid_build.cli.init import create_basic_dag  # re-exported; the helper module imports init

    create_basic_dag(
        tmp_path, {"name": "orders", "orchestration": {"schedule": "@daily"}}, MagicMock()
    )
    (dag,) = tmp_path.rglob("*.py")
    return dag.read_text()


def _scaffold_composer() -> str:
    from fluid_build.cli.scaffold_composer import DAG_TMPL

    return DAG_TMPL.format(
        dag_id="'orders'",
        cron="'0 2 * * *'",
        validate_cmd="'true'",
        plan_cmd="'true'",
        apply_cmd="'true'",
    )


def _schedule_sync() -> str:
    from fluid_build.cli._acquisition_stage_ext import _render_airflow_dag

    return _render_airflow_dag("sales.orders", "b1", {}, "0 2 * * *")


RENDERS = {
    "runtime with actions": lambda tmp: _runtime_dag(_provider_actions()),
    "runtime without actions": lambda tmp: _runtime_dag([]),
    "scheduler aws": lambda tmp: _scheduler("aws"),
    "scheduler gcp": lambda tmp: _scheduler("gcp"),
    "scheduler snowflake": lambda tmp: _scheduler("snowflake"),
    "aws codegen": lambda tmp: _aws(),
    "aws codegen taskflow": lambda tmp: _aws(taskflow=True),
    "aws codegen taskflow with an operator": lambda tmp: _aws(taskflow=True, mixed=True),
    "gcp codegen": lambda tmp: _gcp(),
    "gcp codegen with dataflow": lambda tmp: _gcp(dataflow=True),
    "snowflake codegen": lambda tmp: _snowflake(),
    "snowflake orchestration": lambda tmp: _snowflake_orchestration(),
    "fluid init": _init_dag,
    "scaffold-composer": lambda tmp: _scaffold_composer(),
    "schedule-sync": lambda tmp: _schedule_sync(),
}


def _airflow2_only_imports_outside_a_fallback(tree: ast.AST) -> list[str]:
    """Each Airflow-2-only import, unless it sits in an ``except ImportError:`` whose
    ``try`` imports the Airflow 3 home first."""
    fallbacks: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        tries_airflow3 = any(
            isinstance(n, ast.ImportFrom) and (n.module or "").startswith(AIRFLOW3_HOMES)
            for stmt in node.body
            for n in ast.walk(stmt)
        )
        for handler in node.handlers:
            catches_import_error = (
                isinstance(handler.type, ast.Name) and handler.type.id == "ImportError"
            )
            if tries_airflow3 and catches_import_error:
                fallbacks.update(id(n) for stmt in handler.body for n in ast.walk(stmt))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or id(node) in fallbacks:
            continue
        module = node.module or ""
        names = {a.name for a in node.names}
        if module in AIRFLOW2_ONLY or (module == "airflow" and names & {"DAG", "dag", "task"}):
            found.append(f"line {node.lineno}: from {module} import {', '.join(sorted(names))}")
    return found


@pytest.mark.parametrize("name", sorted(RENDERS))
def test_the_generated_dag_parses_on_airflow_2_and_3(name: str, tmp_path: Path):
    source = RENDERS[name](tmp_path)
    tree = ast.parse(source)  # it is Python at all
    assert (
        "schedule_interval" not in source
    ), "Airflow 3 removed DAG(schedule_interval=); use schedule="
    assert "days_ago" not in source, "Airflow 3 removed airflow.utils.dates.days_ago"
    assert not _airflow2_only_imports_outside_a_fallback(
        tree
    ), _airflow2_only_imports_outside_a_fallback(tree)
    assert any(
        isinstance(n, ast.keyword) and n.arg == "schedule" for n in ast.walk(tree)
    ), "no schedule="


def test_the_import_check_can_fail():
    bare = "from airflow import DAG\nfrom airflow.operators.bash import BashOperator\n"
    assert len(_airflow2_only_imports_outside_a_fallback(ast.parse(bare))) == 2
    # A fallback with no Airflow 3 import in its ``try`` is no fallback.
    hollow = "try:\n    import json\nexcept ImportError:\n    from airflow.operators.bash import BashOperator\n"
    assert len(_airflow2_only_imports_outside_a_fallback(ast.parse(hollow))) == 1
    from fluid_build.providers.common.codegen_utils import (
        airflow_dag_import,
        airflow_standard_import,
    )

    shimmed = airflow_dag_import() + airflow_standard_import("BashOperator")
    assert _airflow2_only_imports_outside_a_fallback(ast.parse(shimmed)) == []


def test_the_dataflow_import_comes_only_with_a_dataflow_task(tmp_path: Path):
    """Its module needs the apache-beam provider; an unused failing import stops the DAG."""
    assert "DataflowTemplatedJobStartOperator" not in RENDERS["gcp codegen"](tmp_path)
    assert "DataflowTemplatedJobStartOperator" in RENDERS["gcp codegen with dataflow"](tmp_path)


def test_no_generator_emits_an_operator_its_provider_removed(tmp_path: Path):
    """snowflake provider 6.0 removed SnowflakeOperator; google provider 14 removed
    BigQueryCreateEmptyTableOperator, which survives only behind the newer operator."""
    for name, render in RENDERS.items():
        workdir = tmp_path / name.replace(" ", "_")
        workdir.mkdir()
        source = render(workdir)
        assert "SnowflakeOperator" not in source, name
        if "BigQueryCreateEmptyTableOperator" in source:
            assert "BigQueryCreateTableOperator = None" in source, name


def test_taskflow_wires_task_results_not_task_functions(tmp_path: Path):
    source = RENDERS["aws codegen taskflow"](tmp_path)
    assert "load_result >> publish_result" in source
    assert "    load >> publish" not in source
    # An operator task is wired as itself; only a @task is wired by its result.
    assert "    load >> publish_result" in RENDERS["aws codegen taskflow with an operator"](
        tmp_path
    )
