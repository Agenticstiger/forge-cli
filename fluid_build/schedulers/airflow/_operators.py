# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Provider operator emitters for orchestration DAGs.

One emitter per provider turns a ``provider_action`` task (``action`` shaped
``<prefix>.<service>.<operation>``) into the source of one Airflow operator.
Each emitter returns ``(source, uses)``: ``uses`` names the operator classes
and helper functions that source needs, so the DAG imports exactly those and
no provider module it does not call. An import that cannot resolve stops the
whole DAG from loading, which is why the Dataflow operator (it needs
apache-airflow-providers-apache-beam) is imported only by a DAG that uses it.

Every contract-derived value is emitted through
:func:`~fluid_build.providers.common.codegen_utils.py_str_literal` (strings),
``json_literal`` (mappings rebuilt with ``json.loads``) or ``repr`` of a
plain dict, and every variable name through the caller's collision-free
identifier map, so contract content cannot inject code that Airflow would run
when it parses the DAG. Snowflake DDL identifiers and column types go through
``providers/_sql_safety.py`` as well: the Python layer's escaping does not
protect the SQL the operator runs at task time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Set, Tuple

from fluid_build.providers._sql_safety import validate_ident, validate_sql_type_name
from fluid_build.providers.common.codegen_utils import (
    _stringify_keys,
    json_literal,
    py_str_literal,
)

Emitted = Tuple[str, Set[str]]

#: Import source for every name an emitter can ask for. A multi-line value is
#: emitted as is. Keys double as names the DAG module binds, so a task variable
#: may not take any of them (see :data:`RESERVED_NAMES`).
IMPORTS: Dict[str, str] = {
    # --- Google ---
    "BigQueryCreateEmptyDatasetOperator": (
        "from airflow.providers.google.cloud.operators.bigquery import "
        "BigQueryCreateEmptyDatasetOperator"
    ),
    "BigQueryInsertJobOperator": (
        "from airflow.providers.google.cloud.operators.bigquery import BigQueryInsertJobOperator"
    ),
    # google provider 13+ has BigQueryCreateTableOperator, 17.0 removed the
    # BigQueryCreateEmptyTableOperator it replaced, so import whichever exists.
    "BigQueryCreateTableOperator": (
        "try:  # apache-airflow-providers-google 13+ (17.0 removed BigQueryCreateEmptyTableOperator)\n"
        "    from airflow.providers.google.cloud.operators.bigquery import "
        "BigQueryCreateTableOperator\n"
        "except ImportError:  # older google providers (Airflow 2)\n"
        "    BigQueryCreateTableOperator = None\n"
        "    from airflow.providers.google.cloud.operators.bigquery import "
        "BigQueryCreateEmptyTableOperator"
    ),
    "GCSCreateBucketOperator": (
        "from airflow.providers.google.cloud.operators.gcs import GCSCreateBucketOperator"
    ),
    "PubSubCreateTopicOperator": (
        "from airflow.providers.google.cloud.operators.pubsub import PubSubCreateTopicOperator"
    ),
    "PubSubCreateSubscriptionOperator": (
        "from airflow.providers.google.cloud.operators.pubsub import "
        "PubSubCreateSubscriptionOperator"
    ),
    "DataflowTemplatedJobStartOperator": (
        "from airflow.providers.google.cloud.operators.dataflow import "
        "DataflowTemplatedJobStartOperator"
    ),
    # --- AWS ---
    "S3CreateBucketOperator": (
        "from airflow.providers.amazon.aws.operators.s3 import S3CreateBucketOperator"
    ),
    "GlueJobOperator": "from airflow.providers.amazon.aws.operators.glue import GlueJobOperator",
    "AthenaOperator": "from airflow.providers.amazon.aws.operators.athena import AthenaOperator",
    "RedshiftDataOperator": (
        "from airflow.providers.amazon.aws.operators.redshift_data import RedshiftDataOperator"
    ),
    "LambdaInvokeFunctionOperator": (
        "from airflow.providers.amazon.aws.operators.lambda_function import "
        "LambdaInvokeFunctionOperator"
    ),
    # --- Snowflake (through the common SQL operator: snowflake provider 6.0
    # removed SnowflakeOperator) ---
    "SQLExecuteQueryOperator": (
        "from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator"
    ),
}

#: Helpers an emitted file defines itself (see :data:`HELPERS`).
HELPER_NAMES = ("_ensure_glue_database", "_ensure_glue_table")

#: Names the generated module binds besides the task variables: its own
#: top-level names, the ``DAG`` / ``PythonOperator`` imports, every operator
#: class, and the helper functions. A task variable must not shadow one of
#: them: ``taskId: dag`` would rebind the DAG every later task is attached to.
RESERVED_NAMES = frozenset(
    {
        "dag",
        "task",
        "default_args",
        "logger",
        "json",
        "logging",
        "datetime",
        "timedelta",
        "ZoneInfo",
        "DAG",
        "PythonOperator",
        "BigQueryCreateEmptyTableOperator",
        *IMPORTS,
        *HELPER_NAMES,
    }
)

#: Glue Data Catalog helpers for ``aws.glue.ensure_database`` / ``ensure_table``.
HELPERS: Dict[str, str] = {
    "_ensure_glue_database": '''def _ensure_glue_database(database: str, region: str):
    """Ensure Glue database exists."""
    import boto3
    client = boto3.client('glue', region_name=region)

    try:
        client.create_database(DatabaseInput={'Name': database})
        logger.info(f"Created Glue database: {database}")
    except client.exceptions.AlreadyExistsException:
        logger.info(f"Glue database already exists: {database}")
''',
    "_ensure_glue_table": '''def _ensure_glue_table(database: str, table: str, params: dict, region: str):
    """Ensure Glue table exists."""
    import boto3
    client = boto3.client('glue', region_name=region)

    columns = params.get('columns', [])
    location = params.get('location', '')
    input_format = params.get('input_format', 'parquet')

    FORMAT_MAP = {
        'parquet': {
            'InputFormat': 'org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat',
            'OutputFormat': 'org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat',
            'SerDe': 'org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe',
        },
        'orc': {
            'InputFormat': 'org.apache.hadoop.hive.ql.io.orc.OrcInputFormat',
            'OutputFormat': 'org.apache.hadoop.hive.ql.io.orc.OrcOutputFormat',
            'SerDe': 'org.apache.hadoop.hive.ql.io.orc.OrcSerde',
        },
        'json': {
            'InputFormat': 'org.apache.hadoop.mapred.TextInputFormat',
            'OutputFormat': 'org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat',
            'SerDe': 'org.openx.data.jsonserde.JsonSerDe',
        },
        'csv': {
            'InputFormat': 'org.apache.hadoop.mapred.TextInputFormat',
            'OutputFormat': 'org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat',
            'SerDe': 'org.apache.hadoop.hive.serde2.OpenCSVSerde',
        },
    }
    fmt = FORMAT_MAP.get(input_format, FORMAT_MAP['parquet'])

    table_input = {
        'Name': table,
        'StorageDescriptor': {
            'Columns': columns,
            'Location': location,
            'InputFormat': fmt['InputFormat'],
            'OutputFormat': fmt['OutputFormat'],
            'SerdeInfo': {'SerializationLibrary': fmt['SerDe']},
        },
        'TableType': 'EXTERNAL_TABLE',
    }
    description = params.get('description')
    if description:
        table_input['Description'] = description

    try:
        client.get_table(DatabaseName=database, Name=table)
        client.update_table(DatabaseName=database, TableInput=table_input)
        logger.info(f"Updated Glue table: {database}.{table}")
    except client.exceptions.EntityNotFoundException:
        client.create_table(DatabaseName=database, TableInput=table_input)
        logger.info(f"Created Glue table: {database}.{table}")
''',
}


@dataclass
class TaskContext:
    """What an emitter needs to know about one task."""

    task: Dict[str, Any]
    var: str
    config: Dict[str, Any] = field(default_factory=dict)

    @property
    def task_id(self) -> str:
        return self.task.get("taskId") or "unnamed_task"

    @property
    def action(self) -> str:
        return self.task.get("action") or ""

    @property
    def params(self) -> Dict[str, Any]:
        params = self.task.get("params")
        return params if isinstance(params, dict) else {}

    @property
    def service_and_operation(self) -> Tuple[str, str]:
        parts = self.action.split(".")
        return (parts[1], parts[2]) if len(parts) >= 3 else ("", "")


def _need(ctx: TaskContext, what: str, *values: Any) -> Any:
    """The first of ``values`` that is set, or a refusal: a made-up name or id is never written."""
    for value in values:
        if value not in (None, "", [], {}):
            return value
    raise ValueError(
        f"task {ctx.task_id!r} ({ctx.action}): {what} is not set. Set it in the task's params "
        "or the provider settings; the renderer does not invent one."
    )


def _data(value: Any) -> str:
    """A contract mapping or list as a Python literal that parses and means the same.

    ``repr`` of a YAML date is ``datetime.date(2024, 1, 1)``, which the generated
    module cannot evaluate (it binds ``datetime`` to the class), and ``repr`` of a
    non-string key keeps its type. A JSON round trip leaves str, number, bool,
    None, list and dict: ``default=str`` turns a date into its text and
    ``_stringify_keys`` mirrors ``json.dumps`` for ``on:``/``1:`` keys.
    """
    return repr(json.loads(json.dumps(_stringify_keys(value), default=str)))


def _op(ctx: TaskContext, operator: str, kwargs: List[Tuple[str, str]], *uses: str) -> Emitted:
    """``<var> = <operator>(task_id=..., <kwargs>, dag=dag)`` and what it needs."""
    lines = [f"{ctx.var} = {operator}(", f"    task_id={py_str_literal(ctx.task_id)},"]
    lines += [f"    {key}={value}," for key, value in kwargs]
    lines += ["    dag=dag,", ")"]
    return "\n".join(lines), {operator, *uses}


#: Marker in an emitter's ``uses`` set: the task renders as a log line, not as
#: the action it names. ``render_dag`` refuses these unless the caller opts in.
UNMAPPED = "__unmapped__"


def generic_task(ctx: TaskContext) -> Emitted:
    """A ``PythonOperator`` that logs the action: nothing provider-specific to run.

    The task does nothing the action names, so it is marked ``UNMAPPED`` and the
    renderer fails closed on it unless told to allow it.

    The action and params are concatenated into the callable as ``repr``'d
    literals, never interpolated into a quoted string: ``repr({'k': 'v'})``
    contains quotes that would end an enclosing literal (a ``SyntaxError`` the
    scheduler reports as a DAG that never appears). ``default=str`` lets a
    param that YAML parses to a date serialise instead of raising.
    """
    params_json = repr(json.dumps(_stringify_keys(ctx.params), sort_keys=True, default=str))
    return (
        f"{ctx.var} = PythonOperator(\n"
        f"    task_id={py_str_literal(ctx.task_id)},\n"
        f"    python_callable=lambda: logger.warning(\n"
        f"        'Action: ' + {py_str_literal(ctx.action)} + ', Params: ' + {params_json}\n"
        f"    ),\n"
        f"    dag=dag,\n"
        f")",
        {UNMAPPED},
    )


# --------------------------------------------------------------------------
# GCP
# --------------------------------------------------------------------------


def gcp_task(ctx: TaskContext) -> Emitted:
    service, operation = ctx.service_and_operation
    params = ctx.params
    project = _need(ctx, "the GCP project", ctx.config.get("project"))
    region = ctx.config.get("region") or "us-central1"
    project_lit, region_lit = py_str_literal(project), py_str_literal(region)

    if service == "bigquery":
        if operation == "create_dataset":
            return _op(
                ctx,
                "BigQueryCreateEmptyDatasetOperator",
                [
                    (
                        "dataset_id",
                        py_str_literal(_need(ctx, "params.dataset_id", params.get("dataset_id"))),
                    ),
                    ("project_id", project_lit),
                    ("location", py_str_literal(params.get("location", region))),
                ],
            )
        if operation == "create_table":
            dataset_lit = py_str_literal(_need(ctx, "params.dataset_id", params.get("dataset_id")))
            table_lit = py_str_literal(_need(ctx, "params.table_id", params.get("table_id")))
            fields = params.get("schema", [])
            task_id_lit = py_str_literal(ctx.task_id)
            return (
                f"if BigQueryCreateTableOperator is not None:\n"
                f"    {ctx.var} = BigQueryCreateTableOperator(\n"
                f"        task_id={task_id_lit},\n"
                f"        dataset_id={dataset_lit},\n"
                f"        table_id={table_lit},\n"
                f"        project_id={project_lit},\n"
                f'        table_resource={{"schema": {{"fields": {_data(fields)}}}}},\n'
                f"        dag=dag,\n"
                f"    )\n"
                f"else:\n"
                f"    {ctx.var} = BigQueryCreateEmptyTableOperator(\n"
                f"        task_id={task_id_lit},\n"
                f"        dataset_id={dataset_lit},\n"
                f"        table_id={table_lit},\n"
                f"        project_id={project_lit},\n"
                f"        schema_fields={_data(fields)},\n"
                f"        dag=dag,\n"
                f"    )",
                {"BigQueryCreateTableOperator"},
            )
        if operation in ("query", "run_query"):
            query: Dict[str, Any] = {
                "query": _need(ctx, "params.query", params.get("query")),
                "useLegacySql": False,
            }
            dest_dataset = params.get("destination_dataset")
            dest_table = params.get("destination_table")
            if dest_dataset and dest_table:
                query["destinationTable"] = {
                    "projectId": project,
                    "datasetId": dest_dataset,
                    "tableId": dest_table,
                }
            return _op(
                ctx,
                "BigQueryInsertJobOperator",
                [
                    ("configuration", _data({"query": query})),
                    ("project_id", project_lit),
                    ("location", region_lit),
                ],
            )
    elif service in ("gcs", "storage") and operation in ("create_bucket", "ensure_bucket"):
        return _op(
            ctx,
            "GCSCreateBucketOperator",
            [
                ("bucket_name", py_str_literal(_need(ctx, "params.bucket", params.get("bucket")))),
                ("project_id", project_lit),
                ("storage_class", py_str_literal(params.get("storage_class", "STANDARD"))),
                ("location", py_str_literal(params.get("location", region))),
            ],
        )
    elif service == "pubsub":
        if operation == "create_topic":
            return _op(
                ctx,
                "PubSubCreateTopicOperator",
                [
                    ("topic", py_str_literal(_need(ctx, "params.topic", params.get("topic")))),
                    ("project_id", project_lit),
                ],
            )
        if operation == "create_subscription":
            return _op(
                ctx,
                "PubSubCreateSubscriptionOperator",
                [
                    ("topic", py_str_literal(_need(ctx, "params.topic", params.get("topic")))),
                    (
                        "subscription",
                        py_str_literal(
                            _need(ctx, "params.subscription", params.get("subscription"))
                        ),
                    ),
                    ("project_id", project_lit),
                ],
            )
    elif service == "dataflow" and operation == "run_template":
        return _op(
            ctx,
            "DataflowTemplatedJobStartOperator",
            [
                ("job_name", py_str_literal(params.get("job_name", ctx.task_id))),
                ("template", py_str_literal(params.get("template", ""))),
                ("parameters", _data(params.get("parameters", {}))),
                ("project_id", project_lit),
                ("location", region_lit),
            ],
        )
    return generic_task(ctx)


# --------------------------------------------------------------------------
# AWS
# --------------------------------------------------------------------------


def aws_task(ctx: TaskContext) -> Emitted:
    service, operation = ctx.service_and_operation
    params = ctx.params
    region = ctx.config.get("region", "us-east-1")
    account_id = ctx.config.get("account_id") or ""
    region_lit = py_str_literal(region)

    if service == "glue":
        if operation == "ensure_database":
            call = (
                f"_ensure_glue_database(\n"
                f"        database={py_str_literal(params.get('database'))},\n"
                f"        region={region_lit}\n"
                f"    )"
            )
            return _glue_helper_task(ctx, call, "_ensure_glue_database")
        if operation == "ensure_table":
            call = (
                f"_ensure_glue_table(\n"
                f"        database={py_str_literal(params.get('database'))},\n"
                f"        table={py_str_literal(params.get('table'))},\n"
                f"        params=json.loads({json_literal(params)}),\n"
                f"        region={region_lit}\n"
                f"    )"
            )
            return _glue_helper_task(ctx, call, "_ensure_glue_table")
        if operation in ("run_job", "start_job_run"):
            return _op(
                ctx,
                "GlueJobOperator",
                [
                    (
                        "job_name",
                        py_str_literal(_need(ctx, "params.job_name", params.get("job_name"))),
                    ),
                    ("region_name", region_lit),
                    ("script_args", _data(params.get("script_args", {}))),
                ],
            )
        return generic_task(ctx)
    if service == "s3" and operation in ("create_bucket", "ensure_bucket"):
        return _op(
            ctx,
            "S3CreateBucketOperator",
            [
                ("bucket_name", py_str_literal(_need(ctx, "params.bucket", params.get("bucket")))),
                ("region_name", py_str_literal(params.get("region", region))),
                ("aws_conn_id", "'aws_default'"),
            ],
        )
    if service == "athena":
        # The account-scoped bucket is the one Athena creates for a workgroup
        # that sets no result location of its own.
        output = _need(
            ctx,
            "params.output_location (or the account id, for the default results bucket)",
            params.get("output_location"),
            params.get("outputLocation"),
            f"s3://aws-athena-query-results-{account_id}-{region}/" if account_id else None,
        )
        return _op(
            ctx,
            "AthenaOperator",
            [
                ("query", py_str_literal(_need(ctx, "params.query", params.get("query")))),
                ("database", py_str_literal(params.get("database", "default"))),
                ("output_location", py_str_literal(output)),
                ("aws_conn_id", "'aws_default'"),
            ],
        )
    if service == "redshift" and operation == "execute_sql":
        return _op(
            ctx,
            "RedshiftDataOperator",
            [
                ("sql", py_str_literal(params.get("sql", ""))),
                ("cluster_identifier", py_str_literal(params.get("cluster", ""))),
                ("database", py_str_literal(params.get("database", "dev"))),
                ("aws_conn_id", "'aws_default'"),
            ],
        )
    if service == "lambda" and operation == "invoke":
        return _op(
            ctx,
            "LambdaInvokeFunctionOperator",
            [
                ("function_name", py_str_literal(params.get("function"))),
                ("payload", json_literal(params.get("payload", {}))),
                ("aws_conn_id", "'aws_default'"),
            ],
        )
    return generic_task(ctx)


def _glue_helper_task(ctx: TaskContext, call: str, helper: str) -> Emitted:
    return (
        f"{ctx.var} = PythonOperator(\n"
        f"    task_id={py_str_literal(ctx.task_id)},\n"
        f"    python_callable=lambda: {call},\n"
        f"    dag=dag,\n"
        f")",
        {helper},
    )


# --------------------------------------------------------------------------
# Snowflake
# --------------------------------------------------------------------------


def snowflake_task(ctx: TaskContext) -> Emitted:
    _, operation = ctx.service_and_operation
    params = ctx.params
    database = ctx.config.get("database")

    # Every identifier and column type reaches the SQL the operator runs at
    # task time, so each goes through validate_ident / validate_sql_type_name
    # (a value holding a quote, semicolon, parenthesis or comment marker raises
    # ValueError and never reaches the emitted SQL). py_str_literal below only
    # protects the Python layer.
    if operation == "create_database":
        db = validate_ident(_need(ctx, "params.database", params.get("database")))
        sql = f"CREATE DATABASE IF NOT EXISTS {db}"
    elif operation == "create_schema":
        db = validate_ident(
            _need(
                ctx,
                "the database (params.database or the provider's)",
                params.get("database"),
                database,
            )
        )
        schema = validate_ident(_need(ctx, "params.schema", params.get("schema")))
        sql = f"CREATE SCHEMA IF NOT EXISTS {db}.{schema}"
    elif operation == "create_table":
        db = validate_ident(
            _need(
                ctx,
                "the database (params.database or the provider's)",
                params.get("database"),
                database,
            )
        )
        schema = validate_ident(params.get("schema", "PUBLIC"))
        table = validate_ident(_need(ctx, "params.table", params.get("table")))
        columns = _need(ctx, "params.columns", params.get("columns"))
        for column in columns:
            if not (isinstance(column, dict) and column.get("name") and column.get("type")):
                raise ValueError(
                    f"task {ctx.task_id!r}: every create_table column needs a name and a type"
                )
        cols = ", ".join(
            f"{validate_ident(c['name'])} {validate_sql_type_name(c['type'])}" for c in columns
        )
        sql = f"CREATE TABLE IF NOT EXISTS {db}.{schema}.{table} ({cols})"
    elif operation in ("query", "run_query", "execute_sql"):
        sql = _need(ctx, "params.sql", params.get("sql"), params.get("query"))
    elif "sql" in params:
        sql = params["sql"]
    else:
        # An operation this renderer does not know and no SQL to run: log it
        # like every other provider's unmapped action, rather than send the
        # warehouse a made-up statement. (The common SQL operator, not
        # SnowflakeOperator: snowflake provider 6.0 removed it.)
        return generic_task(ctx)

    kwargs = [
        ("conn_id", py_str_literal(ctx.config.get("connection_id", "snowflake_default"))),
        ("sql", py_str_literal(sql)),
    ]
    hook = {key: ctx.config[key] for key in ("warehouse", "database") if ctx.config.get(key)}
    if hook:
        kwargs.append(("hook_params", repr(hook)))
    return _op(ctx, "SQLExecuteQueryOperator", kwargs)


#: Provider name (lower case) to its emitter.
EMITTERS: Dict[str, Callable[[TaskContext], Emitted]] = {
    "gcp": gcp_task,
    "aws": aws_task,
    "snowflake": snowflake_task,
}
