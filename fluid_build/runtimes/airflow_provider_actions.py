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
import shlex
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..providers.common.codegen_utils import (
    escape_for_docstring,
    py_str_literal,
    sanitize_identifier,
)


def _safe_comment(value: Any) -> str:
    """Collapse ``value`` to a single line for a generated ``#`` comment.

    A ``#`` comment is terminated by a newline, so an embedded CR/LF in a
    contract-supplied description would end the comment and put the
    remainder of the value at module scope — executed when Airflow parses
    the DAG. Collapsing whitespace is sufficient and keeps the comment
    readable; the value is cosmetic, so it is not escaped further.
    """
    return " ".join(escape_for_docstring(value).split()) or "task"


def _bash_task(task_id: str, comment: str, command: str) -> str:
    """Render a ``BashOperator`` block with both injection layers applied.

    Every ``bash_command`` in this module is built from
    ``contract.fluid.yaml`` values, which are untrusted. Two independent
    layers, mirroring ``cli/scaffold_composer._build_pipeline_bash_commands``:

    1. Callers ``shlex.quote`` each interpolated *value* → at run time the
       shell sees it as one inert token, never a metacharacter it executes.
       Airflow's own docs are explicit that ``BashOperator`` "does not
       perform any escaping or sanitization of the command".
    2. ``py_str_literal`` (``repr``) on the *whole* command here → at
       DAG-parse time it is a fully escaped Python literal, so an embedded
       quote or newline cannot break out of ``bash_command=<literal>`` and
       inject a top-level statement Airflow would run.

    ``sanitize_identifier`` covers the third surface: ``task_id`` becomes a
    Python *variable name*, so it must be a legal identifier.
    """
    ident = sanitize_identifier(task_id)
    return f"""
# {_safe_comment(comment)}
{ident} = BashOperator(
    task_id={py_str_literal(ident)},
    bash_command={py_str_literal(command)},
    dag=dag
)"""


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
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(dag_code)
            self.logger.info(f"DAG written to {output_path}")

        return dag_code

    def _generate_dag_header(self, dag_id: str, schedule: str, contract: Dict[str, Any]) -> str:
        """Generate DAG definition header."""
        # Function-local: schema_manager pulls in jsonschema, and the CLI's
        # startup budget (tests/perf/test_startup_budget.py) forbids that on
        # the ``--help`` path.
        from ..schema_manager import FluidSchemaManager

        description = contract.get(
            "description", f"FLUID data product: {contract.get('name', dag_id)}"
        )
        name = contract.get("name", dag_id)
        domain = contract.get("domain", "unknown")
        # Cosmetic: this lands in the generated DAG's docstring only. Still
        # worth resolving dynamically, because the hardcoded "0.7.0" stamped
        # every version-less contract with a schema version five releases old.
        fluid_version = contract.get("fluidVersion") or FluidSchemaManager.latest_bundled_version()
        kind = str(contract.get("kind", "DataProduct")).lower()

        # The header is the second injection surface: every field below comes
        # from the contract. Docstring fields route through
        # ``escape_for_docstring`` (they must not be able to close the ``"""``
        # delimiter) and every ``DAG(...)`` kwarg through ``py_str_literal``.
        d_name = escape_for_docstring(name)
        d_version = escape_for_docstring(fluid_version)
        d_domain = escape_for_docstring(domain)
        d_description = escape_for_docstring(description)

        return f'''"""
Airflow DAG for FLUID Data Product: {d_name}

Auto-generated from FLUID contract v{d_version}
Generated at: {datetime.now().isoformat()}

Domain: {d_domain}
Description: {d_description}
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
    dag_id={py_str_literal(dag_id)},
    description={py_str_literal(description)},
    schedule_interval={py_str_literal(schedule)},
    start_date=days_ago(1),
    catchup=False,
    tags=["fluid", "data-product", {py_str_literal(kind)}, {py_str_literal(domain)}],
    default_args=default_args
)'''

    def _generate_task(self, action) -> str:
        """Generate task definition for a provider action."""
        from ..forge.core.provider_actions import ActionType

        task_id = sanitize_identifier(action.action_id)

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
            command = (
                f"bq mk --project_id={shlex.quote(str(project))} "
                f"--dataset {shlex.quote(str(dataset))} || true"
            )
        elif provider == "aws":
            command = "aws s3 mb s3://{{ var.value.s3_bucket }} || true"
        else:
            command = f"echo {shlex.quote(f'Provision {expose_id} on {provider}')}"

        return _bash_task(task_id, f"Provision dataset: {expose_id}", command)

    def _generate_schedule_task(self, action, task_id: str) -> str:
        """Generate scheduled task (e.g., dbt run)."""
        params = action.params
        engine = params.get("engine", "dbt")
        script = params.get("script", "")
        build_id = params.get("buildId", "build")

        if engine == "dbt":
            # ``--select`` since dbt 0.21; ``--models`` warns on dbt-core 1.10
            # (ModelParamUsageDeprecation) and is a hard error on dbt v2.
            command = f"dbt run --select {shlex.quote(str(script or build_id))}"
        elif engine == "sql":
            command = f"echo {shlex.quote(f'Execute SQL: {script}')}"
        elif script:
            # ``script`` is NOT a schema field -- ``$defs.build`` sets
            # ``additionalProperties: false`` and declares no ``script`` in any
            # shipped schema (0.7.1-0.7.6), and this generator does not validate
            # before emitting. The only shipped usage
            # (examples/0.7.1/provider-actions-workflow.yaml) is a dbt *model
            # identifier*, not a command line. So quote it like every other
            # branch rather than trusting it to be a well-formed command.
            command = shlex.quote(str(script))
        else:
            command = f"echo {shlex.quote(f'Run {build_id}')}"

        return _bash_task(task_id, f"Schedule task: {build_id}", command)

    def _generate_grant_task(self, action, task_id: str) -> str:
        """Generate access grant task."""
        params = action.params
        principal = params.get("principal", "unknown")
        role = params.get("role", "viewer")
        expose_id = params.get("exposeId", "unknown")

        command = f"echo {shlex.quote(f'Grant {role} to {principal} on {expose_id}')}"

        return _bash_task(task_id, f"Grant access: {expose_id} to {principal}", command)

    def _generate_register_schema_task(self, action, task_id: str) -> str:
        """Generate schema registration task."""
        params = action.params
        schema_name = params.get("schemaName", "unknown")

        command = f"echo {shlex.quote(f'Register schema: {schema_name}')}"

        return _bash_task(task_id, f"Register schema: {schema_name}", command)

    def _generate_create_view_task(self, action, task_id: str) -> str:
        """Generate create view task."""
        params = action.params
        view_name = params.get("viewName", "unknown")

        command = f"echo {shlex.quote(f'Create view: {view_name}')}"

        return _bash_task(task_id, f"Create view: {view_name}", command)

    def _generate_generic_task(self, action, task_id: str) -> str:
        """Generate generic task."""
        description = action.description or f"Execute {action.action_type.value}"

        command = f"echo {shlex.quote(str(description))}"

        return _bash_task(task_id, str(description), command)

    def _generate_dependencies(self, actions: List) -> str:
        """Generate task dependencies based on action.depends_on."""
        dep_code = "# Task dependencies\n"

        for action in actions:
            task_id = sanitize_identifier(action.action_id)

            if action.depends_on:
                for dep in action.depends_on:
                    dep_task_id = sanitize_identifier(dep)
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
    dag_id={py_str_literal(dag_id)},
    schedule_interval={py_str_literal(schedule)},
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
