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

# fluid_build/providers/gcp/plan/bq_modeler.py
"""
BigQuery transformation modeling for GCP provider.

Maps FLUID build.transformation specifications to concrete
dbt, Dataform, or SQL execution actions.
"""

import logging
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from ..util.logging import format_event


def plan_transformation_actions(
    transformation: Mapping[str, Any],
    contract: Mapping[str, Any],
    project: str,
    region: str,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """
    Plan transformation actions based on engine type.

    Supports:
    - dbt-bigquery: Full dbt workflow with profiles and dependencies
    - dataform: Dataform compilation and execution
    - sql: Direct SQL execution

    Args:
        transformation: Transformation configuration from contract
        contract: Full FLUID contract for context
        project: GCP project ID
        region: GCP region
        logger: Optional logger instance

    Returns:
        List of transformation actions
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    engine = transformation.get("engine")
    transformation.get("properties", {})

    logger.debug(
        format_event(
            "transformation_planning_started", engine=engine, contract_id=contract.get("id")
        )
    )

    if engine == "dbt-bigquery":
        return _plan_dbt_actions(transformation, contract, project, region, logger)
    elif engine == "dataform":
        return _plan_dataform_actions(transformation, contract, project, region, logger)
    elif engine == "sql":
        return _plan_sql_actions(transformation, contract, project, region, logger)
    else:
        logger.warning(
            format_event(
                "unknown_transformation_engine", engine=engine, contract_id=contract.get("id")
            )
        )
        return []


def _plan_dbt_actions(
    transformation: Mapping[str, Any],
    contract: Mapping[str, Any],
    project: str,
    region: str,
    logger: logging.Logger,
) -> List[Dict[str, Any]]:
    """
    Plan dbt-bigquery transformation actions.

    Creates actions for:
    1. Profile preparation
    2. Dependencies installation
    3. Model compilation and execution
    4. Test execution
    """
    actions = []
    properties = transformation.get("properties", {})

    # Extract dbt configuration
    dbt_project = properties.get("project", project)
    dbt_dataset = properties.get("dataset", "analytics")
    dbt_target = properties.get("target", "prod")
    dbt_threads = properties.get("threads", 4)

    # Working directory for dbt project
    work_dir = properties.get("work_dir", f"./dbt/{contract.get('id', 'project')}")

    # 1. Prepare dbt profile
    actions.append(
        {
            "op": "dbt.prepare_profile",
            "id": "dbt_prepare_profile",
            "project": dbt_project,
            "dataset": dbt_dataset,
            "target": dbt_target,
            "work_dir": work_dir,
            "threads": dbt_threads,
            "profiles_dir": properties.get("profiles_dir", "~/.dbt"),
            "keyfile_path": properties.get("keyfile_path"),  # Optional service account key
            "timeout": properties.get("timeout", 300),
        }
    )

    # 2. Install dbt dependencies
    if properties.get("install_deps", True):
        actions.append(
            {
                "op": "dbt.install_deps",
                "id": "dbt_install_deps",
                "work_dir": work_dir,
                "timeout": properties.get("deps_timeout", 300),
            }
        )

    # 3. Run dbt seed (if configured)
    if properties.get("run_seed", False):
        actions.append(
            {
                "op": "dbt.seed",
                "id": "dbt_seed",
                "work_dir": work_dir,
                "target": dbt_target,
                "select": properties.get("seed_select"),
                "timeout": properties.get("seed_timeout", 600),
            }
        )

    # 4. Run dbt models
    actions.append(
        {
            "op": "dbt.run",
            "id": "dbt_run",
            "work_dir": work_dir,
            "target": dbt_target,
            "select": properties.get("select"),  # Optional model selection
            "exclude": properties.get("exclude"),  # Optional model exclusion
            "vars": properties.get("vars", {}),  # dbt variables
            "full_refresh": properties.get("full_refresh", False),
            "fail_fast": properties.get("fail_fast", True),
            "timeout": properties.get("run_timeout", 1800),  # 30 minutes default
        }
    )

    # 5. Run dbt tests
    if properties.get("run_tests", True):
        actions.append(
            {
                "op": "dbt.test",
                "id": "dbt_test",
                "work_dir": work_dir,
                "target": dbt_target,
                "select": properties.get("test_select"),
                "exclude": properties.get("test_exclude"),
                "fail_fast": properties.get("test_fail_fast", True),
                "timeout": properties.get("test_timeout", 900),  # 15 minutes default
            }
        )

    # 6. Generate dbt docs (optional)
    if properties.get("generate_docs", False):
        actions.append(
            {
                "op": "dbt.docs_generate",
                "id": "dbt_docs_generate",
                "work_dir": work_dir,
                "target": dbt_target,
                "timeout": properties.get("docs_timeout", 300),
            }
        )

    logger.debug(
        format_event(
            "dbt_actions_planned",
            actions_count=len(actions),
            project=dbt_project,
            dataset=dbt_dataset,
            target=dbt_target,
        )
    )

    return actions


def _plan_dataform_actions(
    transformation: Mapping[str, Any],
    contract: Mapping[str, Any],
    project: str,
    region: str,
    logger: logging.Logger,
) -> List[Dict[str, Any]]:
    """
    Plan Dataform transformation actions.

    Creates actions for:
    1. Workspace initialization
    2. Code compilation
    3. Workflow execution
    """
    actions = []
    properties = transformation.get("properties", {})

    # Extract Dataform configuration
    dataform_project = properties.get("project", project)
    repository_id = properties.get("repository_id")
    workspace_id = properties.get("workspace_id")
    location = properties.get("location", region)

    if not repository_id:
        raise ValueError("Dataform engine requires 'repository_id' in properties")

    # 1. Initialize or update workspace
    if workspace_id:
        actions.append(
            {
                "op": "dataform.ensure_workspace",
                "id": "dataform_ensure_workspace",
                "project": dataform_project,
                "location": location,
                "repository_id": repository_id,
                "workspace_id": workspace_id,
            }
        )

    # 2. Compile Dataform code
    actions.append(
        {
            "op": "dataform.compile",
            "id": "dataform_compile",
            "project": dataform_project,
            "location": location,
            "repository_id": repository_id,
            "workspace_id": workspace_id,
            "git_commitish": properties.get("git_commitish", "main"),
            "timeout": properties.get("compile_timeout", 300),
        }
    )

    # 3. Execute Dataform workflow
    actions.append(
        {
            "op": "dataform.run",
            "id": "dataform_run",
            "project": dataform_project,
            "location": location,
            "repository_id": repository_id,
            "workspace_id": workspace_id,
            "git_commitish": properties.get("git_commitish", "main"),
            "invocation_config": {
                "included_targets": properties.get("included_targets", []),
                "excluded_targets": properties.get("excluded_targets", []),
                "transitive_dependencies": properties.get("transitive_dependencies", True),
                "transitive_dependents": properties.get("transitive_dependents", False),
                "fully_refresh_incremental_tables": properties.get("full_refresh", False),
            },
            "timeout": properties.get("run_timeout", 1800),
        }
    )

    logger.debug(
        format_event(
            "dataform_actions_planned",
            actions_count=len(actions),
            project=dataform_project,
            repository_id=repository_id,
            location=location,
        )
    )

    return actions


def _plan_sql_actions(
    transformation: Mapping[str, Any],
    contract: Mapping[str, Any],
    project: str,
    region: str,
    logger: logging.Logger,
) -> List[Dict[str, Any]]:
    """
    Plan direct SQL transformation actions.

    Executes SQL statements directly against BigQuery.
    """
    actions = []
    properties = transformation.get("properties", {})

    # Extract SQL configuration
    sql_project = properties.get("project", project)
    sql_location = properties.get("location", "US")
    sql_statements = properties.get("sql_statements", [])
    sql_files = properties.get("sql_files", [])

    # Execute individual SQL statements
    for i, statement in enumerate(sql_statements):
        actions.append(
            {
                "op": "bq.execute_sql",
                "id": f"sql_statement_{i}",
                "project": sql_project,
                "location": sql_location,
                "sql": statement,
                "timeout": properties.get("statement_timeout", 600),
                "dry_run": properties.get("dry_run", False),
                "use_legacy_sql": properties.get("use_legacy_sql", False),
            }
        )

    # Execute SQL files
    for i, sql_file in enumerate(sql_files):
        actions.append(
            {
                "op": "bq.execute_sql_file",
                "id": f"sql_file_{i}",
                "project": sql_project,
                "location": sql_location,
                "sql_file": sql_file,
                "timeout": properties.get("file_timeout", 600),
                "dry_run": properties.get("dry_run", False),
                "use_legacy_sql": properties.get("use_legacy_sql", False),
            }
        )

    logger.debug(
        format_event(
            "sql_actions_planned",
            actions_count=len(actions),
            statements_count=len(sql_statements),
            files_count=len(sql_files),
            project=sql_project,
        )
    )

    return actions


def validate_transformation_config(
    transformation: Mapping[str, Any], contract: Mapping[str, Any]
) -> List[str]:
    """
    Validate transformation configuration.

    Args:
        transformation: Transformation configuration
        contract: Full FLUID contract

    Returns:
        List of validation error messages (empty if valid)
    """
    errors = []

    engine = transformation.get("engine")
    if not engine:
        errors.append("Transformation engine is required")
        return errors

    properties = transformation.get("properties", {})

    if engine == "dbt-bigquery":
        errors.extend(_validate_dbt_config(properties))
    elif engine == "dataform":
        errors.extend(_validate_dataform_config(properties))
    elif engine == "sql":
        errors.extend(_validate_sql_config(properties))
    else:
        errors.append(f"Unknown transformation engine: {engine}")

    return errors


def _validate_dbt_config(properties: Dict[str, Any]) -> List[str]:
    """Validate dbt-bigquery configuration."""
    errors = []

    # Check for required BigQuery credentials
    if not properties.get("project") and not properties.get("keyfile_path"):
        errors.append("dbt-bigquery requires either 'project' or 'keyfile_path'")

    # Validate numeric settings
    threads = properties.get("threads", 4)
    if not isinstance(threads, int) or threads <= 0:
        errors.append("dbt 'threads' must be a positive integer")

    # Validate timeout settings
    for timeout_field in ["timeout", "run_timeout", "test_timeout"]:
        timeout = properties.get(timeout_field)
        if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
            errors.append(f"dbt '{timeout_field}' must be a positive integer")

    return errors


def _validate_dataform_config(properties: Dict[str, Any]) -> List[str]:
    """Validate Dataform configuration."""
    errors = []

    if not properties.get("repository_id"):
        errors.append("Dataform requires 'repository_id'")

    # Validate timeout settings
    for timeout_field in ["compile_timeout", "run_timeout"]:
        timeout = properties.get(timeout_field)
        if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
            errors.append(f"Dataform '{timeout_field}' must be a positive integer")

    return errors


def _validate_sql_config(properties: Dict[str, Any]) -> List[str]:
    """Validate direct SQL configuration."""
    errors = []

    sql_statements = properties.get("sql_statements", [])
    sql_files = properties.get("sql_files", [])

    if not sql_statements and not sql_files:
        errors.append("SQL engine requires either 'sql_statements' or 'sql_files'")

    if sql_statements and not isinstance(sql_statements, list):
        errors.append("'sql_statements' must be a list")

    if sql_files and not isinstance(sql_files, list):
        errors.append("'sql_files' must be a list")

    return errors
