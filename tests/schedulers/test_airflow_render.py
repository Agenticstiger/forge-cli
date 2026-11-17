# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""One renderer behind every command that writes an Airflow DAG for an ``orchestration`` section."""

from __future__ import annotations

import argparse
import copy
import logging
import re
from pathlib import Path

import pytest
import yaml

from fluid_build.schedulers.airflow import render_dag
from tests.schedulers.airflow_fixtures import CONTRACTS

LOG = logging.getLogger("test_airflow_render")


def _contract(provider: str, *tasks: dict) -> dict:
    return {
        "id": "p",
        "name": "p",
        "orchestration": {
            "tasks": [{"type": "provider_action", **t} for t in tasks],
        },
        "provider": provider,
    }


def _dag(provider: str, *tasks: dict, **config) -> str:
    return render_dag(
        _contract(provider, *tasks),
        provider=provider,
        provider_config=config,
        allow_unmapped=True,
    )


def _norm(source: str) -> str:
    return re.sub(r"Generated: .*", "Generated: <ts>", source)


# --- the operators a provider's module used to own ------------------------------------------


@pytest.mark.parametrize(
    "task,operator",
    [
        (
            {"action": "aws.redshift.execute_sql", "params": {"sql": "SELECT 1"}},
            "RedshiftDataOperator",
        ),
        (
            {"action": "aws.lambda.invoke", "params": {"function": "f"}},
            "LambdaInvokeFunctionOperator",
        ),
        (
            {"action": "aws.glue.ensure_database", "params": {"database": "d"}},
            "_ensure_glue_database",
        ),
        ({"action": "aws.glue.run_job", "params": {"job_name": "j"}}, "GlueJobOperator"),
    ],
)
def test_aws_operators_are_imported_only_when_used(task, operator):
    source = _dag("aws", {"taskId": "t", **task}, region="eu-west-1")
    assert operator in source
    others = {"RedshiftDataOperator", "LambdaInvokeFunctionOperator", "AthenaOperator"} - {operator}
    assert not any(f"import {name}" in source for name in others)


def test_glue_helpers_are_defined_exactly_when_a_glue_task_calls_them():
    glue = _dag("aws", {"taskId": "t", "action": "aws.glue.ensure_table", "params": {"table": "x"}})
    assert "def _ensure_glue_table(" in glue and "def _ensure_glue_database(" not in glue
    assert "def _ensure_glue" not in _dag(
        "aws", {"taskId": "t", "action": "aws.s3.ensure_bucket", "params": {"bucket": "b"}}
    )


def test_athena_without_an_output_location_uses_the_accounts_default_bucket():
    task = {"taskId": "t", "action": "aws.athena.query", "params": {"query": "SELECT 1"}}
    scoped = _dag("aws", task, account_id="123456789012", region="eu-west-1")
    assert "s3://aws-athena-query-results-123456789012-eu-west-1/" in scoped
    with pytest.raises(ValueError, match="output_location"):
        _dag("aws", task)


def test_gcp_pubsub_dataflow_and_table_operators():
    source = _dag(
        "gcp",
        {"taskId": "a", "action": "gcp.pubsub.create_topic", "params": {"topic": "t"}},
        {"taskId": "b", "action": "gcp.dataflow.run_template", "params": {"template": "gs://x"}},
        {
            "taskId": "c",
            "action": "gcp.bigquery.create_table",
            "params": {"table_id": "t", "dataset_id": "d"},
        },
        project="p",
        region="r",
    )
    assert "PubSubCreateTopicOperator(" in source
    assert "DataflowTemplatedJobStartOperator(" in source
    assert "BigQueryCreateTableOperator = None" in source
    assert "PubSubCreateSubscriptionOperator" not in source


def test_a_bigquery_destination_table_reaches_the_job_configuration():
    source = _dag(
        "gcp",
        {
            "taskId": "q",
            "action": "gcp.bigquery.query",
            "params": {"query": "SELECT 1", "destination_dataset": "d", "destination_table": "t"},
        },
        project="proj",
    )
    assert "'destinationTable': {'projectId': 'proj', 'datasetId': 'd', 'tableId': 't'}" in source


def test_snowflake_ddl_is_built_from_validated_identifiers():
    source = _dag(
        "snowflake",
        {
            "taskId": "t",
            "action": "sf.table.create_table",
            "params": {
                "table": "ORDERS",
                "schema": "SALES",
                "columns": [{"name": "ID", "type": "NUMBER(10,2)"}],
            },
        },
        database="DB",
    )
    assert "sql='CREATE TABLE IF NOT EXISTS DB.SALES.ORDERS (ID NUMBER(10,2))'" in source


@pytest.mark.parametrize(
    "action,params",
    [
        ("sf.database.create_database", {"database": 'X"; DROP DATABASE prod; --'}),
        ("sf.schema.create_schema", {"schema": "S; DROP TABLE t"}),
        ("sf.table.create_table", {"table": "T", "columns": [{"name": "a", "type": "INT); DROP"}]}),
    ],
)
def test_a_snowflake_identifier_that_could_break_out_of_the_ddl_is_refused(action, params):
    with pytest.raises(ValueError):
        _dag("snowflake", {"taskId": "t", "action": action, "params": params})


def test_snowflake_hook_params_follow_the_providers_config_only():
    task = {"taskId": "t", "action": "sf.query.run_query", "params": {"sql": "SELECT 1"}}
    assert "hook_params" not in _dag("snowflake", task, connection_id="sf_conn")
    configured = _dag("snowflake", task, connection_id="sf_conn", warehouse="WH")
    assert "conn_id='sf_conn'" in configured and "hook_params={'warehouse': 'WH'}" in configured


# --- the hardening that every provider now gets ------------------------------------------------


def test_ids_that_sanitise_alike_stay_two_tasks_and_keep_their_edges():
    source = _dag(
        "gcp",
        {"taskId": "a-b", "action": "gcp.unknown.x"},
        {"taskId": "a.b", "action": "gcp.unknown.x"},
        {"taskId": "c", "action": "gcp.unknown.x", "dependsOn": ["a-b", "a.b"]},
        project="p",
    )
    assert "[a_b, a_b_2] >> c" in source


def test_a_dependency_on_a_task_outside_the_dag_is_skipped_not_invented():
    contract = _contract(
        "aws",
        {
            "taskId": "t",
            "action": "aws.s3.ensure_bucket",
            "params": {"bucket": "b"},
            "dependsOn": ["x"],
        },
    )
    source = render_dag(contract, provider="aws", allow_unmapped=True)
    assert "# dependency on non-DAG task 'x' skipped" in source
    assert ">>" not in source.replace("# dependency", "")


@pytest.mark.parametrize("task_id", ["dag", "task", "default_args", "S3CreateBucketOperator"])
def test_a_task_never_takes_a_name_the_dag_module_binds(task_id):
    source = _dag(
        "aws", {"taskId": task_id, "action": "aws.s3.ensure_bucket", "params": {"bucket": "b"}}
    )
    # The module's own binding of the name is the only one.
    own = 1 if task_id in ("dag", "default_args") else 0
    assert len(re.findall(rf"^{task_id} = ", source, re.M)) == own
    assert source.count("dag = DAG(") == 1


# --- one DAG whichever command renders it ------------------------------------------------------


def _valid_for_export(provider: str) -> dict:
    """The golden contract without its dangling edges: ``export`` rejects those up front."""
    contract = copy.deepcopy(CONTRACTS[provider])
    tasks = contract["orchestration"]["tasks"]
    ids = {t["taskId"] for t in tasks}
    for task in tasks:
        task["dependsOn"] = [d for d in task.get("dependsOn", []) if d in ids]
    contract["orchestration"]["tasks"] = [t for t in tasks if t["taskId"] != "unmapped"]
    contract["fluidVersion"] = "0.7.7"
    contract["kind"] = "DataProduct"
    contract["metadata"] = {
        "gcp_project": "my-project",
        "gcp_region": "europe-west6",
        "aws_account_id": "123456789012",
        "aws_region": "eu-west-1",
    }
    return contract


def _only_dag(directory: Path) -> str:
    (path,) = sorted(directory.rglob("*_dag.py"))
    return _norm(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "provider,project,region",
    [("gcp", "my-project", "europe-west6"), ("aws", "123456789012", "eu-west-1")],
)
def test_generate_schedule_export_and_generate_airflow_write_the_same_dag(
    provider, project, region, tmp_path, monkeypatch
):
    from fluid_build.cli import export, generate_airflow, generate_schedule

    monkeypatch.chdir(tmp_path)
    contract_path = tmp_path / "contract.fluid.yaml"
    contract_path.write_text(yaml.safe_dump(_valid_for_export(provider)), encoding="utf-8")

    assert (
        generate_schedule.run(
            argparse.Namespace(
                contract=str(contract_path),
                output=str(tmp_path / "schedule"),
                scheduler="airflow",
                overwrite=False,
                env=None,
                dag_env=None,
                dag_contract_path=None,
                list_schedulers=False,
                verbose=False,
            ),
            LOG,
        )
        == 0
    )
    assert (
        export.run(
            argparse.Namespace(
                contract=str(contract_path),
                engine="airflow",
                output_dir=str(tmp_path / "export"),
                provider=None,
                env=None,
                verbose=False,
                project=project,
                region=region,
            ),
            LOG,
        )
        == 0
    )
    target = tmp_path / "alias" / "pipeline.py"
    assert (
        generate_airflow.run(
            argparse.Namespace(
                contract=str(contract_path), output=str(target), env=None, verbose=False
            ),
            LOG,
        )
        == 0
    )

    scheduled = _only_dag(tmp_path / "schedule")
    assert _only_dag(tmp_path / "export") == scheduled
    assert _norm(target.read_text(encoding="utf-8")) == scheduled


def test_generate_airflow_names_one_successor_and_refuses_a_file_for_many_dags(
    tmp_path, monkeypatch, caplog
):
    from fluid_build.cli import generate_airflow

    monkeypatch.chdir(tmp_path)
    contract_path = tmp_path / "contract.fluid.yaml"
    contract_path.write_text(yaml.safe_dump(_valid_for_export("gcp")), encoding="utf-8")
    args = argparse.Namespace(
        contract=str(contract_path), output=str(tmp_path / "dags"), env=None, verbose=False
    )
    with caplog.at_level(logging.WARNING):
        assert generate_airflow.run(args, LOG) == 0
    assert "fluid generate schedule --scheduler airflow" in caplog.text
    assert "fluid export" not in caplog.text
    assert len(list((tmp_path / "dags").glob("*_dag.py"))) == 1


def test_render_dag_refuses_a_contract_without_orchestration_or_tasks():
    with pytest.raises(ValueError, match="orchestration"):
        render_dag({"id": "x"}, provider="aws")
    with pytest.raises(ValueError, match="no tasks"):
        render_dag({"id": "x", "orchestration": {"tasks": []}}, provider="aws")


def test_generate_airflow_writes_into_a_populated_dags_folder(tmp_path, monkeypatch):
    from fluid_build.cli import generate_airflow

    monkeypatch.chdir(tmp_path)
    (tmp_path / "dags").mkdir()
    (tmp_path / "dags" / "other_dag.py").write_text("# keep\n")
    contract_path = tmp_path / "contract.fluid.yaml"
    contract_path.write_text(yaml.safe_dump(_valid_for_export("gcp")), encoding="utf-8")
    args = argparse.Namespace(
        contract=str(contract_path), output="dags", env=None, verbose=False, contract_path=None
    )
    assert generate_airflow.run(args, LOG) == 0
    assert (tmp_path / "dags" / "other_dag.py").read_text() == "# keep\n"
    assert len(list((tmp_path / "dags").glob("golden_*_dag.py"))) == 1


def test_generate_airflow_without_output_prints_only_the_dag(tmp_path, monkeypatch, capsys):
    from fluid_build.cli import generate_airflow

    monkeypatch.chdir(tmp_path)
    contract_path = tmp_path / "contract.fluid.yaml"
    contract_path.write_text(yaml.safe_dump(_valid_for_export("gcp")), encoding="utf-8")
    args = argparse.Namespace(
        contract=str(contract_path), output=None, env=None, verbose=False, contract_path=None
    )
    assert generate_airflow.run(args, LOG) == 0
    out = capsys.readouterr().out
    compile(out, "dag", "exec")
    assert not (tmp_path / "dags").exists()


# --- an action no emitter maps fails closed ----------------------------------------------------

_UNMAPPED = {
    "aws": "aws.kinesis.create_stream",
    "gcp": "gcp.spanner.create_instance",
    "snowflake": "sf.stage.refresh",
}


@pytest.mark.parametrize("provider", sorted(_UNMAPPED))
def test_an_unmapped_action_is_refused_and_named(provider: str):
    contract = _contract(provider, {"taskId": "lost", "action": _UNMAPPED[provider]})
    with pytest.raises(ValueError) as caught:
        render_dag(contract, provider=provider, provider_config={"project": "p"})
    assert "lost" in str(caught.value) and _UNMAPPED[provider] in str(caught.value)
    assert "FLUID_AIRFLOW_ALLOW_UNMAPPED" in str(caught.value)


def test_the_opt_in_renders_the_log_task_and_warns_with_every_task_listed(caplog):
    contract = _contract(
        "gcp",
        {"taskId": "a", "action": "gcp.unknown.one"},
        {"taskId": "b", "action": "gcp.unknown.two"},
    )
    with caplog.at_level("WARNING"):
        source = render_dag(
            contract, provider="gcp", provider_config={"project": "p"}, allow_unmapped=True
        )
    assert "logger.warning" in source
    warning = " ".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert "a (gcp.unknown.one)" in warning and "b (gcp.unknown.two)" in warning


def test_the_environment_switch_is_the_same_opt_in(monkeypatch):
    contract = _contract("aws", {"taskId": "t", "action": "aws.kinesis.create_stream"})
    with pytest.raises(ValueError):
        render_dag(contract, provider="aws")
    monkeypatch.setenv("FLUID_AIRFLOW_ALLOW_UNMAPPED", "1")
    assert "create_stream" in render_dag(contract, provider="aws")


def test_a_mapped_contract_needs_no_opt_in():
    contract = _contract(
        "aws", {"taskId": "t", "action": "aws.s3.ensure_bucket", "params": {"bucket": "b"}}
    )
    assert "S3CreateBucketOperator" in render_dag(contract, provider="aws")


def test_generate_airflow_takes_allow_unmapped_and_restores_the_environment(monkeypatch):
    import os

    from fluid_build.cli import generate_airflow

    monkeypatch.delenv("FLUID_AIRFLOW_ALLOW_UNMAPPED", raising=False)
    parser = argparse.ArgumentParser()
    generate_airflow.register(parser.add_subparsers())
    args = parser.parse_args(["generate-airflow", "c.yaml", "--allow-unmapped"])
    assert args.allow_unmapped is True

    seen = {}

    def forward(args, logger, output):
        seen["env"] = os.environ.get("FLUID_AIRFLOW_ALLOW_UNMAPPED")
        return 0

    monkeypatch.setattr(generate_airflow, "_forward", forward)
    assert generate_airflow.run(args, logging.getLogger("t")) == 0
    assert seen["env"] == "1"
    assert "FLUID_AIRFLOW_ALLOW_UNMAPPED" not in os.environ


# --- nothing is invented: refuse what the contract leaves unsaid -------------------------------


def _refused(provider: str, *tasks: dict, **config) -> str:
    with pytest.raises(ValueError) as caught:
        render_dag(_contract(provider, *tasks), provider=provider, provider_config=config)
    return str(caught.value)


def test_an_unknown_glue_operation_is_unmapped_not_a_glue_job():
    task = {"taskId": "rs", "action": "aws.glue.delete_table", "params": {"table": "t"}}
    assert "rs (aws.glue.delete_table)" in _refused("aws", task)
    source = _dag("aws", task)
    assert "GlueJobOperator(" not in source


@pytest.mark.parametrize("operation", ["run_job", "start_job_run"])
def test_a_glue_job_runs_only_for_run_job_and_start_job_run(operation):
    task = {"taskId": "j", "action": f"aws.glue.{operation}", "params": {"job_name": "etl"}}
    assert "GlueJobOperator(" in render_dag(_contract("aws", task), provider="aws")
    assert "params.job_name" in _refused(
        "aws", {"taskId": "j", "action": f"aws.glue.{operation}", "params": {}}
    )


def test_athena_without_a_query_is_refused_not_select_1():
    task = {"taskId": "q", "action": "aws.athena.query", "params": {"output_location": "s3://r/"}}
    assert "params.query" in _refused("aws", task)


def test_a_gcp_task_without_a_project_is_refused_not_my_project():
    task = {"taskId": "t", "action": "gcp.pubsub.create_topic", "params": {"topic": "t"}}
    assert "GCP project" in _refused("gcp", task)
    assert "my-project" not in render_dag(
        _contract("gcp", task), provider="gcp", provider_config={"project": "real-proj"}
    )


def test_snowflake_without_a_database_is_refused_not_unknown_db():
    task = {"taskId": "t", "action": "sf.schema.create_schema", "params": {"schema": "S"}}
    assert "database" in _refused("snowflake", task)
    ok = render_dag(
        _contract("snowflake", task),
        provider="snowflake",
        provider_config={"database": "ANALYTICS"},
    )
    assert "ANALYTICS.S" in ok and "UNKNOWN" not in ok


def test_snowflake_create_table_needs_its_columns_and_a_query_needs_its_sql():
    table = {"taskId": "t", "action": "sf.table.create_table", "params": {"table": "T"}}
    assert "params.columns" in _refused("snowflake", table, database="DB")
    query = {"taskId": "q", "action": "sf.sql.query", "params": {}}
    assert "params.sql" in _refused("snowflake", query, database="DB")


def test_a_contract_with_no_provider_action_task_is_refused_not_an_empty_dag():
    contract = {
        "id": "p",
        "provider": "aws",
        "orchestration": {"tasks": [{"taskId": "x", "type": "shell", "command": "true"}]},
    }
    with pytest.raises(ValueError, match="no provider_action task"):
        render_dag(contract, provider="aws")


def test_the_log_only_task_logs_at_warning_level_when_the_dag_runs():
    source = render_dag(
        _contract("aws", {"taskId": "t", "action": "aws.kinesis.create_stream"}),
        provider="aws",
        allow_unmapped=True,
    )
    assert "python_callable=lambda: logger.warning(" in source


# --- overrides and resolved settings on the command line ---------------------------------------


def _write_contract(tmp_path: Path, provider: str, task: dict) -> Path:
    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "sales.orders",
        "name": "Orders",
        "provider": provider,
        "orchestration": {
            "schedule": "0 2 * * *",
            "engine": "airflow",
            "tasks": [{"type": "provider_action", **task}],
        },
    }
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return path


def _generate_schedule(path: Path, out: Path, **overrides) -> int:
    from fluid_build.cli import generate_schedule

    args = argparse.Namespace(
        contract=str(path),
        output=str(out),
        scheduler="airflow",
        overwrite=True,
        env=None,
        dag_env=None,
        dag_contract_path=None,
        dag_id=None,
        dag_schedule=None,
        list_schedulers=False,
        verbose=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return generate_schedule.run(args, LOG)


def test_generate_schedule_reads_the_project_from_the_environment(tmp_path, monkeypatch):
    task = {"taskId": "t", "action": "gcp.pubsub.create_topic", "params": {"topic": "orders"}}
    path = _write_contract(tmp_path, "gcp", task)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "real-proj")
    assert _generate_schedule(path, tmp_path / "dags") == 0
    source = (tmp_path / "dags" / "sales_orders_dag.py").read_text(encoding="utf-8")
    assert "project_id='real-proj'" in source and "my-project" not in source


def test_generate_schedule_refuses_a_gcp_task_when_no_project_resolves(tmp_path, monkeypatch):
    for name in ("GOOGLE_CLOUD_PROJECT", "FLUID_PROJECT", "GCLOUD_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "fluid_build.providers.gcp.util.config._get_adc_project", lambda: None, raising=False
    )
    task = {"taskId": "t", "action": "gcp.pubsub.create_topic", "params": {"topic": "orders"}}
    path = _write_contract(tmp_path, "gcp", task)
    assert _generate_schedule(path, tmp_path / "dags") == 1
    assert not (tmp_path / "dags").exists() or not list((tmp_path / "dags").glob("*.py"))


def test_generate_schedule_takes_dag_id_and_schedule_overrides(tmp_path):
    task = {"taskId": "t", "action": "aws.s3.ensure_bucket", "params": {"bucket": "b"}}
    path = _write_contract(tmp_path, "aws", task)
    assert _generate_schedule(path, tmp_path / "dags", dag_id="my_dag", dag_schedule="@daily") == 0
    source = (tmp_path / "dags" / "my_dag_dag.py").read_text(encoding="utf-8")
    assert "dag_id='my_dag'" in source and "schedule='@daily'" in source


def test_generate_airflow_forwards_dag_id_and_schedule(tmp_path, monkeypatch, capsys):
    from fluid_build.cli import generate_airflow

    task = {"taskId": "t", "action": "aws.s3.ensure_bucket", "params": {"bucket": "b"}}
    path = _write_contract(tmp_path, "aws", task)
    parser = argparse.ArgumentParser()
    generate_airflow.register(parser.add_subparsers())
    args = parser.parse_args(
        [
            "generate-airflow",
            str(path),
            "--dag-id",
            "custom",
            "--schedule",
            "@hourly",
            "-o",
            str(tmp_path / "d.py"),
        ]
    )
    assert generate_airflow.run(args, LOG) == 0
    source = (tmp_path / "d.py").read_text(encoding="utf-8")
    assert "dag_id='custom'" in source and "schedule='@hourly'" in source


@pytest.mark.parametrize("flag", [["--dag-id", "mine"], ["--schedule", "@daily"]])
def test_a_scheduled_build_dag_refuses_the_overrides_it_cannot_honour(tmp_path, monkeypatch, flag):
    from fluid_build.cli import generate_schedule
    from tests.cli._schedule_dag_fixtures import DEMO_CONTRACT_PATH, write_project

    write_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    parser = argparse.ArgumentParser()
    generate_schedule.register_subcommand(parser.add_subparsers())
    args = parser.parse_args(["schedule", DEMO_CONTRACT_PATH, "-o", "out", *flag])
    assert generate_schedule.run(args, LOG) == 2
    assert not (tmp_path / "out").exists()
