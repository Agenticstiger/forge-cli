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
    dag_id='{sanitize_identifier(contract_id)}',
    default_args=default_args,
    description='{contract_name}',
    schedule_interval='{airflow_schedule}',
    start_date=datetime(2026, 1, 1, tzinfo=ZoneInfo('{timezone}')),
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
    """Generate a GCP-specific Airflow task."""
    task_id = task.get("taskId")
    action = task.get("action", "")
    params = task.get("params", {})
    project = config.get("project", "my-project")
    region = config.get("region", "us-central1")

    action_parts = action.split(".")
    if len(action_parts) >= 3:
        service = action_parts[1]
        operation = action_parts[2]

        if service == "bigquery":
            if operation == "create_dataset":
                dataset_id = params.get("dataset_id", "unknown_dataset")
                location = params.get("location", region)
                return f"""{task_id} = BigQueryCreateEmptyDatasetOperator(
    task_id='{task_id}',
    dataset_id='{dataset_id}',
    project_id='{project}',
    location='{location}',
    dag=dag,
)"""
            elif operation in ("query", "run_query"):
                query_sql = params.get("query", "SELECT 1")
                bq_config = {"query": {"query": query_sql, "useLegacySql": False}}
                return f"""{task_id} = BigQueryInsertJobOperator(
    task_id='{task_id}',
    configuration={bq_config!r},
    project_id='{project}',
    location='{region}',
    dag=dag,
)"""
        elif service in ("gcs", "storage"):
            if operation in ("create_bucket", "ensure_bucket"):
                bucket_name = params.get("bucket", "unknown-bucket")
                return f"""{task_id} = GCSCreateBucketOperator(
    task_id='{task_id}',
    bucket_name='{bucket_name}',
    project_id='{project}',
    location='{region}',
    dag=dag,
)"""

    return _generate_generic_task(task)


def _generate_aws_task(task: Dict[str, Any], config: Dict[str, Any]) -> str:
    """Generate an AWS-specific Airflow task."""
    task_id = task.get("taskId")
    action = task.get("action", "")
    params = task.get("params", {})
    region = config.get("region", "us-east-1")

    action_parts = action.split(".")
    if len(action_parts) >= 3:
        service = action_parts[1]
        operation = action_parts[2]

        if service == "glue":
            job_name = params.get("job_name", task_id)
            return f"""{task_id} = GlueJobOperator(
    task_id='{task_id}',
    job_name='{job_name}',
    region_name='{region}',
    script_args={params.get('script_args', {})!r},
    dag=dag,
)"""
        elif service == "s3":
            if operation in ("create_bucket", "ensure_bucket"):
                bucket_name = params.get("bucket", "unknown-bucket")
                return f"""{task_id} = S3CreateBucketOperator(
    task_id='{task_id}',
    bucket_name='{bucket_name}',
    region_name='{region}',
    dag=dag,
)"""
        elif service == "athena":
            query = params.get("query", "SELECT 1")
            database = params.get("database", "default")
            output = params.get("output_location", "s3://results/")
            return f"""{task_id} = AthenaOperator(
    task_id='{task_id}',
    query='{query}',
    database='{database}',
    output_location='{output}',
    dag=dag,
)"""

    return _generate_generic_task(task)


def _generate_snowflake_task(task: Dict[str, Any], config: Dict[str, Any]) -> str:
    """Generate a Snowflake-specific Airflow task."""
    task_id = task.get("taskId")
    action = task.get("action", "")
    params = task.get("params", {})
    conn_id = config.get("connection_id", "snowflake_default")

    action_parts = action.split(".")
    if len(action_parts) >= 3:
        operation = action_parts[2]

        if operation in ("query", "run_query", "execute_sql"):
            sql = params.get("sql", params.get("query", "SELECT 1"))
            return f"""{task_id} = SnowflakeOperator(
    task_id='{task_id}',
    sql=\"\"\"{sql}\"\"\",
    snowflake_conn_id='{conn_id}',
    dag=dag,
)"""

    # Fallback: wrap action in SnowflakeOperator SQL call
    sql = params.get("sql", f"-- TODO: implement {action}")
    return f"""{task_id} = SnowflakeOperator(
    task_id='{task_id}',
    sql=\"\"\"{sql}\"\"\",
    snowflake_conn_id='{conn_id}',
    dag=dag,
)"""


def _generate_generic_task(task: Dict[str, Any]) -> str:
    """Generate a generic Python task (no provider-specific operators)."""
    task_id = task.get("taskId")
    action = task.get("action")
    params = task.get("params", {})

    return f"""{task_id} = PythonOperator(
    task_id='{task_id}',
    python_callable=lambda: logger.info('Action: {action}, Params: {params!r}'),
    dag=dag,
)"""
