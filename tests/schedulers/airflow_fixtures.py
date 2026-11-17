# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Orchestration contracts that every Airflow DAG renderer is held to.

One contract per provider covers every operator the renderer can emit, with
ids chosen to hit the hardening paths (an id that collides with a name the DAG
binds, two ids that sanitise alike, a dependency on a task outside the DAG).
``test_airflow_golden.py`` renders them and compares with the snapshots under
``golden/``; ``golden/DIFFERENCES.md`` explains every change against the
per-provider generators this renderer replaced.
"""

from __future__ import annotations

from typing import Any, Dict, List


def _task(task_id: str, action: str, params: Dict[str, Any] | None = None, *deps: str):
    task: Dict[str, Any] = {"taskId": task_id, "type": "provider_action", "action": action}
    if params is not None:
        task["params"] = params
    if deps:
        task["dependsOn"] = list(deps)
    return task


def _contract(provider: str, tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "id": f"golden.{provider}_pipeline",
        "name": f"Golden {provider} pipeline",
        "provider": provider,
        "orchestration": {
            "schedule": "0 2 * * *",
            "timezone": "Europe/Zurich",
            "engine": "airflow",
            "tasks": tasks,
        },
    }


AWS_TASKS = [
    _task("make_bucket", "aws.s3.ensure_bucket", {"bucket": "lake-raw", "region": "eu-west-1"}),
    _task("make_db", "aws.glue.ensure_database", {"database": "lake"}, "make_bucket"),
    _task(
        "make_table",
        "aws.glue.ensure_table",
        {
            "database": "lake",
            "table": "orders",
            "location": "s3://lake-raw/orders/",
            "columns": [{"Name": "id", "Type": "bigint"}],
        },
        "make_db",
    ),
    _task("crawl", "aws.glue.run_job", {"job_name": "orders_transform"}, "make_table"),
    _task(
        "report",
        "aws.athena.execute_query",
        {"query": "SELECT 1", "database": "lake"},
        "crawl",
    ),
    _task(
        "warehouse",
        "aws.redshift.execute_sql",
        {"sql": "SELECT 2", "cluster": "dwh", "database": "dev"},
        "report",
    ),
    _task(
        "notify", "aws.lambda.invoke", {"function": "notify", "payload": {"k": "v"}}, "warehouse"
    ),
    # An id the DAG's own names would shadow, and two ids that sanitise alike.
    _task("dag", "aws.lambda.invoke", {"function": "f"}),
    _task("a-b", "aws.lambda.invoke", {"function": "f"}),
    _task("a.b", "aws.lambda.invoke", {"function": "f"}, "a-b", "not-in-this-dag"),
    _task("unmapped", "aws.kinesis.create_stream", {"name": "s"}),
]

GCP_TASKS = [
    _task("ds", "gcp.bigquery.create_dataset", {"dataset_id": "sales", "location": "EU"}),
    _task(
        "tbl",
        "gcp.bigquery.create_table",
        {"dataset_id": "sales", "table_id": "orders", "schema": [{"name": "id", "type": "INT64"}]},
        "ds",
    ),
    _task(
        "load",
        "gcp.bigquery.query",
        {
            "query": "SELECT 1",
            "destination_dataset": "sales",
            "destination_table": "orders",
        },
        "tbl",
    ),
    _task("bucket", "gcp.gcs.create_bucket", {"bucket": "sales-raw", "storage_class": "NEARLINE"}),
    _task("topic", "gcp.pubsub.create_topic", {"topic": "orders"}),
    _task(
        "sub",
        "gcp.pubsub.create_subscription",
        {"topic": "orders", "subscription": "orders-sub"},
        "topic",
    ),
    _task(
        "flow",
        "gcp.dataflow.run_template",
        {"template": "gs://t/tpl", "job_name": "j", "parameters": {"a": "b"}},
        "sub",
        "load",
    ),
    _task("task", "gcp.bigquery.query", {"query": "SELECT 2"}),
    _task("unmapped", "gcp.spanner.create_instance", {"name": "i"}, "gone"),
]

SNOWFLAKE_TASKS = [
    _task("make_db", "sf.database.create_database", {"database": "ANALYTICS"}),
    _task("make_schema", "sf.schema.create_schema", {"schema": "SALES"}, "make_db"),
    _task(
        "make_table",
        "sf.table.create_table",
        {
            "table": "ORDERS",
            "schema": "SALES",
            "columns": [{"name": "ID", "type": "INTEGER"}, {"name": "AMT", "type": "NUMBER(10,2)"}],
        },
        "make_schema",
    ),
    _task("load", "sf.query.run_query", {"sql": "INSERT INTO ORDERS SELECT 1"}, "make_table"),
    _task("check", "snowflake.sql.execute_sql", {"sql": "SELECT COUNT(*) FROM ORDERS"}, "load"),
    _task("unmapped", "sf.stage.refresh", {"stage": "S"}),
]

CONTRACTS: Dict[str, Dict[str, Any]] = {
    "aws": _contract("aws", AWS_TASKS),
    "gcp": _contract("gcp", GCP_TASKS),
    "snowflake": _contract("snowflake", SNOWFLAKE_TASKS),
}

#: What each CLI/provider path passes the renderer for these contracts.
PROVIDER_CONFIG: Dict[str, Dict[str, Any]] = {
    "aws": {"account_id": "123456789012", "region": "eu-west-1"},
    "gcp": {"project": "my-project", "region": "europe-west6"},
    "snowflake": {
        "connection_id": "snowflake_default",
        "account": "acct",
        "database": "ANALYTICS",
        "warehouse": "COMPUTE_WH",
    },
}
