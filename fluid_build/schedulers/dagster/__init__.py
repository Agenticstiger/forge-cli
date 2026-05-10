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

"""Dagster schedule engine.

Generates Dagster pipeline definitions from FLUID contracts.  Provider-aware:
generates GCP, AWS, or Snowflake resource/op definitions based on the
``provider`` parameter.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from fluid_build.engines.base import Severity, ValidationIssue
from fluid_build.providers.common.codegen_utils import (
    convert_schedule_to_cron,
    sanitize_identifier,
)
from fluid_build.schedulers.base import ScheduleEngine, ScheduleGenerationResult, ScheduleIntent
from fluid_build.schedulers.registry import register_scheduler


@register_scheduler
class DagsterScheduler(ScheduleEngine):
    """Dagster pipeline generator — works with any cloud provider."""

    name = "dagster"
    supported_platforms = None

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

        code = _generate_header(contract_id, contract_name, schedule, timezone, provider)
        code += "\n\n"
        code += _generate_imports(provider)
        code += "\n\n"
        code += _generate_resources(provider, provider_config)
        code += "\n\n"
        code += _generate_ops(provider_tasks, provider, provider_config)
        code += "\n\n"
        code += _generate_job(contract_id, provider_tasks, schedule, timezone, provider)

        filename = f"{sanitize_identifier(contract_id)}_pipeline.py"
        return {filename: code}

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

        task_ids = [t.get("taskId") for t in (tasks or [])]
        if len(task_ids) != len(set(task_ids)):
            issues.append(
                ValidationIssue(
                    message="Duplicate task IDs found in orchestration",
                    severity=Severity.ERROR,
                    field="orchestration.tasks",
                )
            )

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


def _sanitize_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _generate_header(
    contract_id: str,
    contract_name: str,
    schedule: str,
    timezone: str,
    provider: Optional[str],
) -> str:
    return f'''"""
FLUID Generated Dagster Pipeline: {contract_name}

Contract ID: {contract_id}
Schedule: {schedule}
Timezone: {timezone}
Provider: {(provider or "generic").upper()}

Auto-generated by FLUID Forge - DO NOT EDIT MANUALLY
Generated: {datetime.utcnow().isoformat()}Z
"""'''


def _generate_imports(provider: Optional[str]) -> str:
    base = """from dagster import (
    op,
    job,
    resource,
    In,
    Out,
    Nothing,
    ScheduleDefinition,
)
import json
import logging

logger = logging.getLogger(__name__)"""

    provider_lower = (provider or "").lower()
    if provider_lower == "gcp":
        base += "\nfrom google.cloud import bigquery, storage"
    elif provider_lower == "aws":
        base += "\nimport boto3"
    elif provider_lower == "snowflake":
        base += "\nimport snowflake.connector"

    return base


def _generate_resources(provider: Optional[str], config: Dict[str, Any]) -> str:
    provider_lower = (provider or "").lower()

    if provider_lower == "gcp":
        project = config.get("project", "my-project")
        region = config.get("region", "us-central1")
        return f'''# GCP Resources
@resource
def gcp_config():
    """GCP configuration resource."""
    return {{"project": "{project}", "region": "{region}"}}

@resource
def bigquery_client(context):
    """BigQuery client resource."""
    cfg = context.resources.gcp_config
    return bigquery.Client(project=cfg["project"])'''

    elif provider_lower == "aws":
        region = config.get("region", "us-east-1")
        return f'''# AWS Resources
@resource
def aws_config():
    """AWS configuration resource."""
    return {{"region": "{region}"}}

@resource
def boto_session(context):
    """Boto3 session resource."""
    cfg = context.resources.aws_config
    return boto3.Session(region_name=cfg["region"])'''

    elif provider_lower == "snowflake":
        conn_id = config.get("connection_id", "snowflake_default")
        return f'''# Snowflake Resources
@resource
def snowflake_config():
    """Snowflake configuration resource."""
    return {{"connection_id": "{conn_id}"}}'''

    return """# Generic Resources
@resource
def pipeline_config():
    \"\"\"Pipeline configuration resource.\"\"\"
    return {}"""


def _generate_ops(
    tasks: List[Dict[str, Any]],
    provider: Optional[str],
    config: Dict[str, Any],
) -> str:
    ops_code = "# Pipeline Ops\n"
    for task in tasks:
        ops_code += _generate_single_op(task, provider, config)
        ops_code += "\n\n"
    return ops_code.rstrip()


def _generate_single_op(
    task: Dict[str, Any],
    provider: Optional[str],
    config: Dict[str, Any],
) -> str:
    task_id = task.get("taskId")
    action = task.get("action", "")
    params = task.get("params", {})
    depends_on = task.get("dependsOn", [])

    if depends_on:
        dep_items = ", ".join([f'"dep_{d}": In(Nothing)' for d in depends_on])
        ins_def = f"ins={{{dep_items}}}, "
    else:
        ins_def = ""

    provider_lower = (provider or "").lower()
    action_parts = action.split(".")

    if len(action_parts) >= 3 and provider_lower == "gcp":
        service = action_parts[1]
        if service == "bigquery":
            return _generate_bq_op(task_id, action_parts[2], params, ins_def)

    if len(action_parts) >= 3 and provider_lower == "snowflake":
        sql = params.get("sql", params.get("query", f"-- TODO: {action}"))
        return f'''@op({ins_def}required_resource_keys={{"snowflake_config"}})
def {task_id}(context):
    """Execute: {action}"""
    # TODO: connect via snowflake.connector and execute
    context.log.info("SQL: {sql}")
    return True'''

    return f'''@op({ins_def})
def {task_id}(context):
    """Generic op: {action}"""
    context.log.info("Action: {action}")
    context.log.info("Params: {params!r}")
    return True'''


def _generate_bq_op(task_id: str, operation: str, params: Dict[str, Any], ins_def: str) -> str:
    if operation == "create_dataset":
        dataset_id = params.get("dataset_id", "unknown_dataset")
        return f'''@op({ins_def}required_resource_keys={{"bigquery_client", "gcp_config"}})
def {task_id}(context):
    """Create BigQuery dataset: {dataset_id}"""
    client = context.resources.bigquery_client
    cfg = context.resources.gcp_config
    dataset = bigquery.Dataset(f"{{cfg['project']}}.{dataset_id}")
    dataset = client.create_dataset(dataset, exists_ok=True)
    context.log.info(f"Created dataset {{dataset.dataset_id}}")
    return dataset.dataset_id'''

    elif operation in ("query", "run_query"):
        query_sql = params.get("query", "SELECT 1").replace('"', '\\"')
        return f'''@op({ins_def}required_resource_keys={{"bigquery_client"}})
def {task_id}(context):
    """Run BigQuery query"""
    client = context.resources.bigquery_client
    results = client.query("{query_sql}").result()
    context.log.info(f"Query completed: {{results.total_rows}} rows")
    return results.total_rows'''

    return f'''@op({ins_def}required_resource_keys={{"bigquery_client"}})
def {task_id}(context):
    """BigQuery op: {operation}"""
    context.log.info("Operation: {operation}, Params: {params!r}")
    return True'''


def _generate_job(
    contract_id: str,
    tasks: List[Dict[str, Any]],
    schedule: str,
    timezone: str,
    provider: Optional[str],
) -> str:
    op_calls = []
    for task in tasks:
        task_id = task.get("taskId")
        depends_on = task.get("dependsOn", [])
        if depends_on:
            dep_args = ", ".join([f"dep_{d}={d}_result" for d in depends_on])
            op_calls.append(f"    {task_id}_result = {task_id}({dep_args})")
        else:
            op_calls.append(f"    {task_id}_result = {task_id}()")

    op_calls_str = "\n".join(op_calls)
    cron_schedule = convert_schedule_to_cron(schedule)
    job_name = _sanitize_name(contract_id)

    provider_lower = (provider or "").lower()
    if provider_lower == "gcp":
        resource_defs = '"gcp_config": gcp_config, "bigquery_client": bigquery_client'
    elif provider_lower == "aws":
        resource_defs = '"aws_config": aws_config, "boto_session": boto_session'
    elif provider_lower == "snowflake":
        resource_defs = '"snowflake_config": snowflake_config'
    else:
        resource_defs = '"pipeline_config": pipeline_config'

    return f'''# Job definition
@job(
    resource_defs={{{resource_defs}}},
    tags={{"fluid": "auto-generated", "contract_id": "{contract_id}", "provider": "{provider or "generic"}"}},
)
def {job_name}():
    """{contract_id} pipeline."""
{op_calls_str}

# Schedule
{job_name}_schedule = ScheduleDefinition(
    job={job_name},
    cron_schedule="{cron_schedule}",
    execution_timezone="{timezone}",
)'''
