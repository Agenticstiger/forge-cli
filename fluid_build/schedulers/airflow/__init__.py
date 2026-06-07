# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Airflow schedule engine.

Generates Airflow DAG Python files from FLUID contracts.  Provider-aware:
dispatches to GCP, AWS, or Snowflake operator generators based on the
``provider`` parameter.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fluid_build.engines.base import Severity, ValidationIssue
from fluid_build.providers.common.codegen_utils import (
    convert_schedule_to_airflow,
    generate_file_header,
    generate_task_dependencies_code,
    py_str_literal,
    sanitize_identifier,
)
from fluid_build.schedulers.base import ScheduleEngine, ScheduleGenerationResult, ScheduleIntent
from fluid_build.schedulers.registry import register_scheduler


@register_scheduler
class AirflowScheduler(ScheduleEngine):
    """Airflow DAG generator — works with any cloud provider."""

    name = "airflow"
    supported_platforms = None  # platform-agnostic

    def generate(
        self,
        contract: Dict[str, Any],
        *,
        provider: Optional[str] = None,
        provider_config: Optional[Dict[str, Any]] = None,
        schedule_intent: Optional[ScheduleIntent] = None,
    ) -> ScheduleGenerationResult:
        orchestration = contract.get("orchestration", {})
        tasks = orchestration.get("tasks", [])
        contract_id = contract.get("id", "unknown")
        contract_name = contract.get("name", contract_id)
        schedule = orchestration.get("schedule", "0 2 * * *")
        timezone = orchestration.get("timezone", "UTC")

        if schedule_intent:
            schedule = schedule_intent.schedule or schedule
            timezone = schedule_intent.timezone or timezone
            if schedule_intent.tasks:
                tasks = schedule_intent.tasks

        provider = provider or contract.get("provider", "")
        provider_config = provider_config or {}
        provider_tasks = [t for t in tasks if t.get("type") == "provider_action"]

        dag_code = generate_file_header(
            contract_id=contract_id,
            contract_name=contract_name,
            provider=provider or "generic",
            schedule=schedule,
            timezone=timezone,
        )
        dag_code += "\n\n"
        dag_code += _generate_imports(provider)
        dag_code += "\n\n"
        dag_code += _generate_dag_definition(
            contract_id, contract_name, schedule, timezone, provider
        )
        dag_code += "\n\n"
        dag_code += _generate_task_definitions(provider_tasks, provider, provider_config)
        dag_code += "\n\n"
        dag_code += generate_task_dependencies_code(provider_tasks, syntax="airflow")

        dag_filename = f"{sanitize_identifier(contract_id)}_dag.py"
        return {dag_filename: dag_code}

    def validate(
        self,
        contract: Dict[str, Any],
    ) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        orchestration = contract.get("orchestration")
        if not orchestration:
            issues.append(
                ValidationIssue(
                    message="Contract missing 'orchestration' section",
                    severity=Severity.ERROR,
                    field="orchestration",
                )
            )
            return issues

        tasks = orchestration.get("tasks")
        if not tasks:
            issues.append(
                ValidationIssue(
                    message="Orchestration has no tasks",
                    severity=Severity.WARNING,
                    field="orchestration.tasks",
                )
            )

        # Check for duplicate task IDs
        task_ids = [t.get("taskId") for t in (tasks or [])]
        if len(task_ids) != len(set(task_ids)):
            issues.append(
                ValidationIssue(
                    message="Duplicate task IDs found in orchestration",
                    severity=Severity.ERROR,
                    field="orchestration.tasks",
                )
            )

        # Validate dependencies reference existing tasks
        task_id_set = set(task_ids)
        for task in tasks or []:
            for dep in task.get("dependsOn", []):
                if dep not in task_id_set:
                    issues.append(
                        ValidationIssue(
                            message=f"Task '{task.get('taskId')}' depends on non-existent task '{dep}'",
                            severity=Severity.ERROR,
                            field=f"orchestration.tasks[{task.get('taskId')}].dependsOn",
                        )
                    )

        return issues


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _generate_imports(provider: Optional[str]) -> str:
    """Generate import statements based on provider."""
    base = """from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from airflow import DAG
from airflow.operators.python import PythonOperator
import json
import logging

logger = logging.getLogger(__name__)"""

    provider_lower = (provider or "").lower()
    if provider_lower == "gcp":
        base += """
from airflow.providers.google.cloud.operators.bigquery import (
    BigQueryCreateEmptyDatasetOperator,
    BigQueryCreateEmptyTableOperator,
    BigQueryInsertJobOperator,
)
from airflow.providers.google.cloud.operators.gcs import (
    GCSCreateBucketOperator,
)"""
    elif provider_lower == "aws":
        base += """
from airflow.providers.amazon.aws.operators.glue import GlueJobOperator
from airflow.providers.amazon.aws.operators.s3 import S3CreateBucketOperator
from airflow.providers.amazon.aws.operators.athena import AthenaOperator
from airflow.providers.amazon.aws.operators.step_function import StepFunctionStartExecutionOperator"""
    elif provider_lower == "snowflake":
        base += """
from airflow.providers.snowflake.operators.snowflake import SnowflakeOperator"""

    return base


def _generate_dag_definition(
    contract_id: str,
    contract_name: str,
    schedule: str,
    timezone: str,
    provider: Optional[str],
) -> str:
    """Generate DAG definition with default arguments."""
    airflow_schedule = convert_schedule_to_airflow(schedule)
    tags = ["fluid", "auto-generated"]
    if provider:
        tags.append(provider.lower())
    tags.append(contract_id)

    # All contract-derived values are emitted as repr()-escaped literals so a
    # malicious name/timezone/schedule cannot break out and inject code that
    # Airflow would run at DAG-parse time. dag_id is an identifier (no quotes).
    return f"""# Default DAG arguments
default_args = {{
    'owner': 'fluid-forge',
    'depends_on_past': False,
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 3,
    'retry_delay': timedelta(minutes=5),
    'execution_timeout': timedelta(hours=2),
}}

# DAG definition
dag = DAG(
    dag_id={py_str_literal(sanitize_identifier(contract_id))},
    default_args=default_args,
    description={py_str_literal(contract_name)},
    schedule_interval={py_str_literal(airflow_schedule)},
    start_date=datetime(2026, 1, 1, tzinfo=ZoneInfo({py_str_literal(timezone)})),
    catchup=False,
    tags={tags!r},
)"""


def _generate_task_definitions(
    tasks: List[Dict[str, Any]],
    provider: Optional[str],
    provider_config: Dict[str, Any],
) -> str:
    """Generate task definitions, dispatching by provider."""
    task_code = "# Task definitions\n"
    provider_lower = (provider or "").lower()

    for task in tasks:
        if provider_lower == "gcp":
            task_code += _generate_gcp_task(task, provider_config)
        elif provider_lower == "aws":
            task_code += _generate_aws_task(task, provider_config)
        elif provider_lower == "snowflake":
            task_code += _generate_snowflake_task(task, provider_config)
        else:
            task_code += _generate_generic_task(task)
        task_code += "\n\n"

    return task_code.rstrip()


def _generate_gcp_task(task: Dict[str, Any], config: Dict[str, Any]) -> str:
    """Generate a GCP-specific Airflow task.

    Every contract/config-derived value is emitted via ``sanitize_identifier``
    (the LHS variable name) or ``py_str_literal`` (string kwargs) so untrusted
    contract content cannot inject code into the generated DAG. Dict configs go
    out via ``!r`` (repr of a dict is itself a safe, fully-escaped literal).
    """
    task_id = task.get("taskId")
    action = task.get("action", "")
    params = task.get("params", {})
    project = config.get("project", "my-project")
    region = config.get("region", "us-central1")

    var = sanitize_identifier(task_id)
    task_id_lit = py_str_literal(task_id)
    project_lit = py_str_literal(project)
    region_lit = py_str_literal(region)

    action_parts = action.split(".")
    if len(action_parts) >= 3:
        service = action_parts[1]
        operation = action_parts[2]

        if service == "bigquery":
            if operation == "create_dataset":
                dataset_lit = py_str_literal(params.get("dataset_id", "unknown_dataset"))
                location_lit = py_str_literal(params.get("location", region))
                return f"""{var} = BigQueryCreateEmptyDatasetOperator(
    task_id={task_id_lit},
    dataset_id={dataset_lit},
    project_id={project_lit},
    location={location_lit},
    dag=dag,
)"""
            elif operation in ("query", "run_query"):
                query_sql = params.get("query", "SELECT 1")
                bq_config = {"query": {"query": query_sql, "useLegacySql": False}}
                return f"""{var} = BigQueryInsertJobOperator(
    task_id={task_id_lit},
    configuration={bq_config!r},
    project_id={project_lit},
    location={region_lit},
    dag=dag,
)"""
        elif service in ("gcs", "storage"):
            if operation in ("create_bucket", "ensure_bucket"):
                bucket_lit = py_str_literal(params.get("bucket", "unknown-bucket"))
                return f"""{var} = GCSCreateBucketOperator(
    task_id={task_id_lit},
    bucket_name={bucket_lit},
    project_id={project_lit},
    location={region_lit},
    dag=dag,
)"""

    return _generate_generic_task(task)


def _generate_aws_task(task: Dict[str, Any], config: Dict[str, Any]) -> str:
    """Generate an AWS-specific Airflow task."""
    task_id = task.get("taskId")
    action = task.get("action", "")
    params = task.get("params", {})
    region = config.get("region", "us-east-1")

    var = sanitize_identifier(task_id)
    task_id_lit = py_str_literal(task_id)
    region_lit = py_str_literal(region)

    action_parts = action.split(".")
    if len(action_parts) >= 3:
        service = action_parts[1]
        operation = action_parts[2]

        if service == "glue":
            job_name_lit = py_str_literal(params.get("job_name", task_id))
            return f"""{var} = GlueJobOperator(
    task_id={task_id_lit},
    job_name={job_name_lit},
    region_name={region_lit},
    script_args={params.get("script_args", {})!r},
    dag=dag,
)"""
        elif service == "s3":
            if operation in ("create_bucket", "ensure_bucket"):
                bucket_lit = py_str_literal(params.get("bucket", "unknown-bucket"))
                return f"""{var} = S3CreateBucketOperator(
    task_id={task_id_lit},
    bucket_name={bucket_lit},
    region_name={region_lit},
    dag=dag,
)"""
        elif service == "athena":
            query_lit = py_str_literal(params.get("query", "SELECT 1"))
            database_lit = py_str_literal(params.get("database", "default"))
            output_lit = py_str_literal(params.get("output_location", "s3://results/"))
            return f"""{var} = AthenaOperator(
    task_id={task_id_lit},
    query={query_lit},
    database={database_lit},
    output_location={output_lit},
    dag=dag,
)"""

    return _generate_generic_task(task)


def _generate_snowflake_task(task: Dict[str, Any], config: Dict[str, Any]) -> str:
    """Generate a Snowflake-specific Airflow task."""
    task_id = task.get("taskId")
    action = task.get("action", "")
    params = task.get("params", {})
    conn_id = config.get("connection_id", "snowflake_default")

    var = sanitize_identifier(task_id)
    task_id_lit = py_str_literal(task_id)
    conn_lit = py_str_literal(conn_id)

    action_parts = action.split(".")
    if len(action_parts) >= 3:
        operation = action_parts[2]

        if operation in ("query", "run_query", "execute_sql"):
            # repr() escapes embedded quotes / triple-quotes / newlines, so SQL
            # cannot terminate the literal and inject a top-level statement.
            sql_lit = py_str_literal(params.get("sql", params.get("query", "SELECT 1")))
            return f"""{var} = SnowflakeOperator(
    task_id={task_id_lit},
    sql={sql_lit},
    snowflake_conn_id={conn_lit},
    dag=dag,
)"""

    # Fallback: wrap action in SnowflakeOperator SQL call
    sql_lit = py_str_literal(params.get("sql", f"-- TODO: implement {action}"))
    return f"""{var} = SnowflakeOperator(
    task_id={task_id_lit},
    sql={sql_lit},
    snowflake_conn_id={conn_lit},
    dag=dag,
)"""


def _generate_generic_task(task: Dict[str, Any]) -> str:
    """Generate a generic Python task (no provider-specific operators).

    **Quote safety:** the previous implementation interpolated
    ``{params!r}`` (Python repr) inside a single-quoted string literal.
    ``repr({'model': 'x'})`` returns ``"{'model': 'x'}"`` — a string
    containing single quotes. When embedded in the lambda body as
    ``'Action: foo, Params: {'model': 'x'}'`` the Python parser sees
    the first inner ``'`` as a string terminator and fails with
    ``SyntaxError: invalid syntax. Perhaps you forgot a comma?``
    Airflow's scheduler rejects the DAG file at parse time; the DAG
    never appears in ``airflow dags list``.

    **Fix:** serialize via ``json.dumps`` (produces double-quoted
    JSON, which is safe inside single-quoted Python strings) and
    use ``repr()`` on the task_id + action strings so any embedded
    quotes get properly escaped by Python's own repr machinery.
    """
    import json

    task_id = task.get("taskId") or "unnamed_task"
    action = task.get("action") or ""
    params = task.get("params", {})

    # repr() handles arbitrary quote content correctly by choosing the
    # appropriate quote style and escaping where needed — safer than a
    # naive f-string wrapping.
    task_id_literal = repr(task_id)
    action_literal = repr(action)
    # json.dumps produces double-quoted JSON, embedded here inside
    # the outer single-quoted f-string payload via repr().
    params_json = repr(json.dumps(params, sort_keys=True))

    # The Python identifier in ``<id> = PythonOperator(...)`` must be a legal
    # Python name AND must match the identifier the dependency wiring emits, so
    # route it through the shared ``sanitize_identifier`` (deterministic — the
    # same task_id maps to the same variable in both the definition and the
    # ``a >> b`` wiring).
    py_id = sanitize_identifier(task_id)

    return f"""{py_id} = PythonOperator(
    task_id={task_id_literal},
    python_callable=lambda: logger.info(
        'Action: ' + {action_literal} + ', Params: ' + {params_json}
    ),
    dag=dag,
)"""
