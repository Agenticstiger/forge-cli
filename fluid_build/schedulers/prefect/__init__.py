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

"""Prefect schedule engine.

Generates Prefect flow definitions from FLUID contracts.  Provider-aware:
generates GCP, AWS, or Snowflake task implementations based on the
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
class PrefectScheduler(ScheduleEngine):
    """Prefect flow generator — works with any cloud provider."""

    name = "prefect"
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
        code += _generate_config(provider, provider_config)
        code += "\n\n"
        code += _generate_tasks(provider_tasks, provider, provider_config)
        code += "\n\n"
        code += _generate_flow(contract_id, contract_name, provider_tasks)
        code += "\n\n"
        code += _generate_deployment(contract_id, contract_name, schedule, timezone)

        filename = f"{sanitize_identifier(contract_id)}_flow.py"
        return {filename: code}

    def validate(
        self,
        contract: Dict[str, Any],
    ) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        orchestration = contract.get("orchestration")
        if not orchestration:
            issues.append(ValidationIssue(
                message="Contract missing 'orchestration' section",
                severity=Severity.ERROR,
                field="orchestration",
            ))
            return issues

        tasks = orchestration.get("tasks")
        if not tasks:
            issues.append(ValidationIssue(
                message="Orchestration has no tasks",
                severity=Severity.WARNING,
                field="orchestration.tasks",
            ))

        task_ids = [t.get("taskId") for t in (tasks or [])]
        if len(task_ids) != len(set(task_ids)):
            issues.append(ValidationIssue(
                message="Duplicate task IDs found in orchestration",
                severity=Severity.ERROR,
                field="orchestration.tasks",
            ))

        task_id_set = set(task_ids)
        for task in (tasks or []):
            for dep in task.get("dependsOn", []):
                if dep not in task_id_set:
                    issues.append(ValidationIssue(
                        message=f"Task '{task.get('taskId')}' depends on non-existent task '{dep}'",
                        severity=Severity.ERROR,
                        field=f"orchestration.tasks[{task.get('taskId')}].dependsOn",
                    ))

        return issues


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sanitize_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _generate_header(
    contract_id: str, contract_name: str, schedule: str, timezone: str,
    provider: Optional[str],
) -> str:
    return f'''"""
FLUID Generated Prefect Flow: {contract_name}

Contract ID: {contract_id}
Schedule: {schedule}
Timezone: {timezone}
Provider: {(provider or "generic").upper()}

Auto-generated by FLUID Forge - DO NOT EDIT MANUALLY
Generated: {datetime.utcnow().isoformat()}Z
"""'''


def _generate_imports(provider: Optional[str]) -> str:
    base = """from prefect import flow, task
from prefect.deployments import Deployment
from prefect.server.schemas.schedules import CronSchedule
import logging
from datetime import timedelta

logger = logging.getLogger(__name__)"""

    provider_lower = (provider or "").lower()
    if provider_lower == "gcp":
        base += "\nfrom google.cloud import bigquery, storage"
    elif provider_lower == "aws":
        base += "\nimport boto3"
    elif provider_lower == "snowflake":
        base += "\nimport snowflake.connector"

    return base


def _generate_config(provider: Optional[str], config: Dict[str, Any]) -> str:
    provider_lower = (provider or "").lower()

    if provider_lower == "gcp":
        project = config.get("project", "my-project")
        region = config.get("region", "us-central1")
        return f"""# GCP Configuration
GCP_PROJECT = "{project}"
GCP_REGION = "{region}" """

    elif provider_lower == "aws":
        region = config.get("region", "us-east-1")
        return f"""# AWS Configuration
AWS_REGION = "{region}" """

    elif provider_lower == "snowflake":
        return """# Snowflake Configuration
# Set via environment variables or Prefect blocks"""

    return "# Configuration\n# Set provider-specific config via environment variables"


def _generate_tasks(
    tasks: List[Dict[str, Any]], provider: Optional[str], config: Dict[str, Any],
) -> str:
    tasks_code = "# Prefect Tasks\n"
    for task_spec in tasks:
        tasks_code += _generate_single_task(task_spec, provider, config)
        tasks_code += "\n\n"
    return tasks_code.rstrip()


def _generate_single_task(
    task_spec: Dict[str, Any], provider: Optional[str], config: Dict[str, Any],
) -> str:
    task_id = task_spec.get("taskId")
    action = task_spec.get("action", "")
    params = task_spec.get("params", {})
    provider_lower = (provider or "").lower()

    action_parts = action.split(".")
    if len(action_parts) >= 3:
        service = action_parts[1]
        operation = action_parts[2]

        if provider_lower == "gcp" and service == "bigquery":
            if operation == "create_dataset":
                dataset_id = params.get("dataset_id", "unknown_dataset")
                return f'''@task(retries=3, retry_delay_seconds=30, timeout_seconds=600)
def {task_id}():
    """Create BigQuery dataset: {dataset_id}"""
    client = bigquery.Client(project=GCP_PROJECT)
    dataset = bigquery.Dataset(f"{{GCP_PROJECT}}.{dataset_id}")
    dataset = client.create_dataset(dataset, exists_ok=True)
    logger.info(f"Created dataset {{dataset.dataset_id}}")
    return dataset.dataset_id'''

            elif operation in ("query", "run_query"):
                query_sql = params.get("query", "SELECT 1").replace('"', '\\"')
                return f'''@task(retries=3, retry_delay_seconds=30, timeout_seconds=1800)
def {task_id}():
    """Run BigQuery query"""
    client = bigquery.Client(project=GCP_PROJECT)
    results = client.query("{query_sql}").result()
    logger.info(f"Query completed: {{results.total_rows}} rows")
    return results.total_rows'''

        if provider_lower == "snowflake":
            sql = params.get("sql", params.get("query", f"-- TODO: {action}"))
            return f'''@task(retries=3, retry_delay_seconds=30, timeout_seconds=600)
def {task_id}():
    """Execute Snowflake: {action}"""
    # TODO: connect via snowflake.connector
    logger.info("SQL: {sql}")
    return True'''

    return f'''@task(retries=2, retry_delay_seconds=30)
def {task_id}():
    """Generic task: {action}"""
    logger.info("Action: {action}")
    logger.info("Params: {params!r}")
    return True'''


def _generate_flow(contract_id: str, contract_name: str, tasks: List[Dict[str, Any]]) -> str:
    flow_name = _sanitize_name(contract_id)
    task_executions = []
    for task_spec in tasks:
        task_id = task_spec.get("taskId")
        task_executions.append(f"    {task_id}_result = {task_id}()")

    task_exec_str = "\n".join(task_executions)

    return f'''# Flow definition
@flow(
    name="{flow_name}",
    description="{contract_name}",
    retries=1,
    retry_delay_seconds=60,
)
def {flow_name}_flow():
    """{contract_name} flow."""
{task_exec_str}
    logger.info("Flow completed successfully")
    return True'''


def _generate_deployment(contract_id: str, contract_name: str, schedule: str, timezone: str) -> str:
    flow_name = _sanitize_name(contract_id)
    cron_schedule = convert_schedule_to_cron(schedule)

    return f'''# Deployment configuration
if __name__ == "__main__":
    deployment = Deployment.build_from_flow(
        flow={flow_name}_flow,
        name="{flow_name}-deployment",
        version="1.0",
        tags=["fluid", "auto-generated", "{contract_id}"],
        schedule=CronSchedule(
            cron="{cron_schedule}",
            timezone="{timezone}",
        ),
        work_queue_name="default",
    )
    deployment.apply()
    print(f"Deployment created: {{deployment.name}}")'''
