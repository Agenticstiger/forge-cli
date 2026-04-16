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

"""
Airflow DAG Generator for FLUID 0.7.0+ Provider Actions

Generates Airflow DAGs from declarative provider actions.
Supports all action types defined in FLUID 0.7.1 schema.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional


class AirflowDAGGenerator:
    """Generates Airflow DAGs from FLUID provider actions."""

    # Map action types to Airflow operators
    ACTION_OPERATOR_MAP = {
        "provisionDataset": "BashOperator",
        "grantAccess": "BashOperator",
        "revokeAccess": "BashOperator",
        "scheduleTask": "PythonOperator",
        "registerSchema": "BashOperator",
        "createView": "BigQueryOperator",
        "updatePolicy": "BashOperator",
        "publishEvent": "PythonOperator",
        "custom": "BashOperator",
    }

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger(__name__)

    def generate_dag(
        self,
        contract: Dict[str, Any],
        dag_id: Optional[str] = None,
        schedule: Optional[str] = None,
        output_path: Optional[str] = None,
    ) -> str:
        """
        Generate complete Airflow DAG Python code.

        Args:
            contract: FLUID contract (0.7.0+)
            dag_id: Override DAG ID (default: from contract.id)
            schedule: Override schedule (default: from contract.orchestration or @daily)
            output_path: Optional path to write DAG file

        Returns:
            Python code for Airflow DAG
        """
        from ..forge.core.provider_actions import ProviderActionParser

        # Parse provider actions
        parser = ProviderActionParser(logger=self.logger)
        actions = parser.parse(contract)

        if not actions:
            self.logger.warning("No provider actions found in contract")
            return self._generate_empty_dag(contract, dag_id, schedule)

        # Extract metadata
        if not dag_id:
            dag_id = contract.get("id", "fluid_dag").replace(".", "_")

        if not schedule:
            orchestration = contract.get("orchestration", {})
            schedule = orchestration.get("schedule", "@daily")

        # Generate DAG code
        dag_code = self._generate_dag_header(dag_id, schedule, contract)
        dag_code += "\n\n"

        # Generate tasks
        task_definitions = []
        for action in actions:
            task_def = self._generate_task(action)
            task_definitions.append(task_def)

        dag_code += "\n\n".join(task_definitions)
        dag_code += "\n\n"

        # Generate dependencies
        dag_code += self._generate_dependencies(actions)

        # Write to file if path provided
        if output_path:
            with open(output_path, "w") as f:
                f.write(dag_code)
            self.logger.info(f"DAG written to {output_path}")

        return dag_code

    def _generate_dag_header(self, dag_id: str, schedule: str, contract: Dict[str, Any]) -> str:
        """Generate DAG definition header."""
        description = contract.get(
            "description", f"FLUID data product: {contract.get('name', dag_id)}"
        )
        name = contract.get("name", dag_id)
        domain = contract.get("domain", "unknown")

        return f'''"""
Airflow DAG for FLUID Data Product: {name}

Auto-generated from FLUID contract v{contract.get("fluidVersion", "0.7.0")}
Generated at: {datetime.now().isoformat()}

Domain: {domain}
Description: {description}
"""
from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator
from airflow.utils.dates import days_ago
from datetime import datetime, timedelta

# DAG configuration
default_args = {{
    'owner': 'fluid',
    'depends_on_past': False,
    'email_on_failure': True,
    'email_on_retry': False,
    'retries': 3,
    'retry_delay': timedelta(minutes=5),
}}

# DAG definition
dag = DAG(
    dag_id="{dag_id}",
    description="""{description}""",
    schedule_interval="{schedule}",
    start_date=days_ago(1),
    catchup=False,
    tags=["fluid", "data-product", "{contract.get('kind', 'DataProduct').lower()}", "{domain}"],
    default_args=default_args
)'''

    def _generate_task(self, action) -> str:
        """Generate task definition for a provider action."""
        from ..forge.core.provider_actions import ActionType

        task_id = action.action_id.replace("-", "_").replace(".", "_")

        if action.action_type == ActionType.PROVISION_DATASET:
            return self._generate_provision_task(action, task_id)
        elif action.action_type == ActionType.SCHEDULE_TASK:
            return self._generate_schedule_task(action, task_id)
        elif action.action_type == ActionType.GRANT_ACCESS:
            return self._generate_grant_task(action, task_id)
        elif action.action_type == ActionType.REGISTER_SCHEMA:
            return self._generate_register_schema_task(action, task_id)
        elif action.action_type == ActionType.CREATE_VIEW:
            return self._generate_create_view_task(action, task_id)
        else:
            return self._generate_generic_task(action, task_id)

    def _generate_provision_task(self, action, task_id: str) -> str:
        """Generate dataset provisioning task."""
        params = action.params
        expose_id = params.get("exposeId", "unknown")
        provider = action.provider

        # Generate provider-specific command
        if provider == "gcp":
            location = params.get("binding", {}).get("location", {})
            project = location.get("project", "{{ var.value.gcp_project }}")
            dataset = location.get("dataset", expose_id)
            command = f"bq mk --project_id={project} --dataset {dataset} || true"
        elif provider == "aws":
            command = "aws s3 mb s3://{{ var.value.s3_bucket }} || true"
        else:
            command = f"echo 'Provision {expose_id} on {provider}'"

        return f"""
# Provision dataset: {expose_id}
{task_id} = BashOperator(
    task_id="{task_id}",
    bash_command="{command}",
    dag=dag
)"""

    def _generate_schedule_task(self, action, task_id: str) -> str:
        """Generate scheduled task (e.g., dbt run)."""
        params = action.params
        engine = params.get("engine", "dbt")
        script = params.get("script", "")
        build_id = params.get("buildId", "build")

        if engine == "dbt":
            command = f"dbt run --models {script or build_id}"
        elif engine == "sql":
            command = f"echo 'Execute SQL: {script}'"
        else:
            command = script or f"echo 'Run {build_id}'"

        return f"""
# Schedule task: {build_id}
{task_id} = BashOperator(
    task_id="{task_id}",
    bash_command="{command}",
    dag=dag
)"""

    def _generate_grant_task(self, action, task_id: str) -> str:
        """Generate access grant task."""
        params = action.params
        principal = params.get("principal", "unknown")
        role = params.get("role", "viewer")
        expose_id = params.get("exposeId", "unknown")

        command = f"echo 'Grant {role} to {principal} on {expose_id}'"

        return f"""
# Grant access: {expose_id} to {principal}
{task_id} = BashOperator(
    task_id="{task_id}",
    bash_command="{command}",
    dag=dag
)"""

    def _generate_register_schema_task(self, action, task_id: str) -> str:
        """Generate schema registration task."""
        params = action.params
        schema_name = params.get("schemaName", "unknown")

        return f"""
# Register schema: {schema_name}
{task_id} = BashOperator(
    task_id="{task_id}",
    bash_command="echo 'Register schema: {schema_name}'",
    dag=dag
)"""

    def _generate_create_view_task(self, action, task_id: str) -> str:
        """Generate create view task."""
        params = action.params
        view_name = params.get("viewName", "unknown")

        return f"""
# Create view: {view_name}
{task_id} = BashOperator(
    task_id="{task_id}",
    bash_command="echo 'Create view: {view_name}'",
    dag=dag
)"""

    def _generate_generic_task(self, action, task_id: str) -> str:
        """Generate generic task."""
        description = action.description or f"Execute {action.action_type.value}"

        return f"""
# {description}
{task_id} = BashOperator(
    task_id="{task_id}",
    bash_command="echo '{description}'",
    dag=dag
)"""

    def _generate_dependencies(self, actions: List) -> str:
        """Generate task dependencies based on action.depends_on."""
        dep_code = "# Task dependencies\n"

        for action in actions:
            task_id = action.action_id.replace("-", "_").replace(".", "_")

            if action.depends_on:
                for dep in action.depends_on:
                    dep_task_id = dep.replace("-", "_").replace(".", "_")
                    dep_code += f"{dep_task_id} >> {task_id}\n"

        if dep_code == "# Task dependencies\n":
            dep_code += "# No dependencies specified\n"

        return dep_code

    def _generate_empty_dag(
        self, contract: Dict[str, Any], dag_id: Optional[str], schedule: Optional[str]
    ) -> str:
        """Generate placeholder DAG when no actions found."""
        if not dag_id:
            dag_id = contract.get("id", "fluid_dag").replace(".", "_")
        if not schedule:
            schedule = "@daily"

        return f'''"""
Empty Airflow DAG - No provider actions found
"""
from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.utils.dates import days_ago

dag = DAG(
    dag_id="{dag_id}",
    schedule_interval="{schedule}",
    start_date=days_ago(1),
    catchup=False,
    tags=["fluid", "placeholder"]
)

placeholder = BashOperator(
    task_id="no_actions_found",
    bash_command="echo 'No provider actions found in contract'",
    dag=dag
)
'''


def generate_airflow_dag(
    contract: Dict[str, Any],
    output_path: Optional[str] = None,
    dag_id: Optional[str] = None,
    schedule: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> str:
    """
    Convenience function to generate Airflow DAG from FLUID contract.

    Args:
        contract: FLUID contract dict
        output_path: Optional path to write DAG file
        dag_id: Optional DAG ID override
        schedule: Optional schedule override
        logger: Optional logger instance

    Returns:
        DAG Python code
    """
    generator = AirflowDAGGenerator(logger=logger)
    return generator.generate_dag(
        contract, dag_id=dag_id, schedule=schedule, output_path=output_path
    )
