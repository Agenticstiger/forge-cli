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

# fluid_build/providers/aws/plan/planner.py
"""
AWS provider planning engine.

Orchestrates contract-to-actions mapping across all AWS services.
Converts FLUID contract specifications into concrete AWS resource operations.
"""

import logging
import os
import re
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from ..util.logging import format_event
from ..util.names import normalize_database_name


def _resolve_env_templates(value: str) -> str:
    """
    Resolve ``{{ env.VAR }}`` templates from environment variables.

    Unresolvable templates (missing env var) are left as-is so the
    caller can decide whether to error or fall back.
    """
    if not isinstance(value, str) or "{{" not in value:
        return value

    def _replacer(m: re.Match) -> str:
        var = m.group(1).strip()
        return os.environ.get(var, m.group(0))  # keep original if unset

    return re.sub(r"\{\{\s*env\.(\S+?)\s*\}\}", _replacer, value)


def plan_actions(
    contract: Mapping[str, Any],
    account_id: str,
    region: str,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """
    Generate AWS actions from FLUID contract.

    Analyzes contract and produces ordered list of actions to:
    1. Create necessary S3 buckets and Glue databases
    2. Set up IAM roles and policies
    3. Deploy transformation logic (dbt/Lambda)
    4. Configure scheduling (EventBridge)
    5. Expose data products (Glue tables, Athena views, Redshift tables)

    Args:
        contract: FLUID contract specification
        account_id: AWS account ID
        region: AWS region
        logger: Optional logger instance

    Returns:
        List of ordered actions to execute
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    actions = []

    # Validate contract structure
    contract_id = contract.get("id")
    if not contract_id:
        raise ValueError("Contract must have an 'id' field")

    logger.debug(format_event("planning_started", contract_id=contract_id))

    # Phase 1: Infrastructure setup (S3 buckets, Glue databases)
    infrastructure_actions = _plan_infrastructure(contract, account_id, region, logger)
    actions.extend(infrastructure_actions)

    # Phase 2: IAM roles and policies
    iam_actions = _plan_iam_policies(contract, account_id, logger)
    actions.extend(iam_actions)

    # Phase 3: Build transformations (dbt, Lambda, Glue jobs)
    build_actions = _plan_build_transformations(contract, account_id, region, logger)
    actions.extend(build_actions)

    # Phase 4: Expose data products
    expose_actions = _plan_exposures(contract, account_id, region, logger)
    actions.extend(expose_actions)

    # Phase 5: Scheduling and orchestration (EventBridge, Step Functions)
    schedule_actions = _plan_scheduling(contract, account_id, region, logger)
    actions.extend(schedule_actions)

    logger.info(
        format_event(
            "planning_completed",
            contract_id=contract_id,
            total_actions=len(actions),
            infrastructure_actions=len(infrastructure_actions),
            iam_actions=len(iam_actions),
            build_actions=len(build_actions),
            expose_actions=len(expose_actions),
            schedule_actions=len(schedule_actions),
        )
    )

    return actions


def _plan_infrastructure(
    contract: Mapping[str, Any], account_id: str, region: str, logger: logging.Logger
) -> List[Dict[str, Any]]:
    """
    Plan infrastructure setup actions.

    Creates necessary S3 buckets, Glue databases, and foundational resources.
    """
    actions = []
    databases_created = set()
    buckets_created = set()

    # Analyze binding to determine platform
    for exposure in contract.get("exposes", []):
        if not isinstance(exposure, dict):
            continue
        binding = exposure.get("binding") or {}
        if not isinstance(binding, dict):
            continue
        platform = binding.get("platform", "").lower()

        # Handle different binding formats
        if platform in ["aws", "glue", "athena"]:
            # Ensure Glue database exists (support nested and flat formats)
            location = binding.get("location") or {}
            database = location.get("database") if isinstance(location, dict) else None
            database = database or binding.get("database")

            if database and database not in databases_created:
                normalized_db = normalize_database_name(database)

                # Resolve bucket from contract, fall back to convention
                raw_bucket = (
                    location.get("bucket") if isinstance(location, dict) else None
                ) or binding.get("bucket")
                bucket_name = _resolve_env_templates(raw_bucket) if raw_bucket else None
                # If template couldn't be resolved or no bucket specified, use default
                if not bucket_name or "{{" in bucket_name:
                    bucket_name = f"{account_id}-fluid-data"

                actions.append(
                    {
                        "op": "glue.ensure_database",
                        "id": f"database_{normalized_db}",
                        "database": normalized_db,
                        "description": f"Database for {contract.get('name', 'data product')}",
                        "location": f"s3://{bucket_name}/{normalized_db}/",
                        "tags": _get_resource_tags(contract),
                    }
                )

                databases_created.add(database)

                # Create S3 bucket for data
                if bucket_name not in buckets_created:
                    actions.append(
                        {
                            "op": "s3.ensure_bucket",
                            "id": f"bucket_{bucket_name}",
                            "bucket": bucket_name,
                            "region": region,
                            "tags": _get_resource_tags(contract),
                        }
                    )
                    buckets_created.add(bucket_name)

        elif platform == "s3":
            # Ensure S3 bucket exists
            raw_bucket = binding.get("bucket")
            bucket = _resolve_env_templates(raw_bucket) if raw_bucket else None

            if bucket and "{{" not in bucket and bucket not in buckets_created:
                actions.append(
                    {
                        "op": "s3.ensure_bucket",
                        "id": f"bucket_{bucket}",
                        "bucket": bucket,
                        "region": region,
                        "tags": _get_resource_tags(contract),
                    }
                )
                buckets_created.add(bucket)

        elif platform == "redshift":
            # Redshift schema creation handled in expose phase
            pass

    # Check build section for additional infrastructure needs
    from fluid_build.util.contract import get_primary_build

    get_primary_build(contract) or {}

    # Create staging bucket if needed
    staging_bucket = f"{account_id}-fluid-staging"
    if staging_bucket not in buckets_created:
        actions.append(
            {
                "op": "s3.ensure_bucket",
                "id": "bucket_staging",
                "bucket": staging_bucket,
                "region": region,
                "tags": {**_get_resource_tags(contract), "purpose": "staging"},
            }
        )

    logger.debug(
        format_event(
            "infrastructure_planned",
            databases=len(databases_created),
            buckets=len(buckets_created),
            actions=len(actions),
        )
    )

    return actions


def _plan_iam_policies(
    contract: Mapping[str, Any], account_id: str, logger: logging.Logger
) -> List[Dict[str, Any]]:
    """
    Plan IAM policy actions.

    Converts FLUID policies to AWS IAM roles and policies.
    """
    actions = []

    metadata = contract.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    policies = metadata.get("policies", {})

    if not policies:
        return actions

    # For each exposure, apply relevant IAM policies
    for exposure in contract.get("exposes", []):
        if not isinstance(exposure, dict):
            continue
        exposure.get("exposeId") or exposure.get("id")
        binding = exposure.get("binding") or {}
        if not isinstance(binding, dict):
            continue
        platform = binding.get("platform", "").lower()

        if platform in ["aws", "glue", "athena"]:
            database = binding.get("database")

            if database:
                actions.append(
                    {
                        "op": "iam.bind_glue_database",
                        "id": f"iam_database_{database}",
                        "database": database,
                        "policies": policies,
                    }
                )

        elif platform == "s3":
            bucket = binding.get("bucket")

            if bucket:
                actions.append(
                    {
                        "op": "iam.bind_s3_bucket",
                        "id": f"iam_bucket_{bucket}",
                        "bucket": bucket,
                        "policies": policies,
                    }
                )

    logger.debug(format_event("iam_policies_planned", actions=len(actions)))

    return actions


def _plan_build_transformations(
    contract: Mapping[str, Any], account_id: str, region: str, logger: logging.Logger
) -> List[Dict[str, Any]]:
    """
    Plan build transformation actions.

    Sets up dbt, Lambda functions, or Glue jobs for transformations.
    """
    actions = []

    from fluid_build.util.contract import get_primary_build

    build_config = get_primary_build(contract) or {}

    if not build_config:
        return actions

    # Check for engine type
    engine = build_config.get("engine")
    pattern = build_config.get("pattern")

    if engine in ["dbt-redshift", "dbt-athena"]:
        # dbt transformation planning
        actions.append(
            {
                "op": "dbt.prepare_profile",
                "id": "dbt_profile",
                "engine": engine,
                "account_id": account_id,
                "region": region,
            }
        )

    elif engine == "glue":
        # AWS Glue ETL job planning
        props = build_config.get("properties", {})
        job_name = props.get("job_name") or f"{contract.get('id', 'unnamed').replace('.', '-')}-etl"
        role = props.get("role")
        script_location = props.get("script_location")

        if role and script_location:
            actions.append(
                {
                    "op": "glue.ensure_job",
                    "id": f"glue_job_{job_name}",
                    "name": job_name,
                    "role": role,
                    "script_location": script_location,
                    "command_name": props.get("command_name", "glueetl"),
                    "glue_version": props.get("glue_version", "4.0"),
                    "worker_type": props.get("worker_type", "G.1X"),
                    "number_of_workers": props.get("number_of_workers", 10),
                    "timeout": props.get("timeout", 2880),
                    "max_retries": props.get("max_retries", 0),
                    "description": props.get(
                        "description", f"ETL job for {contract.get('name', '')}"
                    ),
                    "default_arguments": props.get("default_arguments", {}),
                    "extra_py_files": props.get("extra_py_files", []),
                    "extra_jars": props.get("extra_jars", []),
                    "connections": props.get("connections", []),
                    "temp_dir": props.get("temp_dir")
                    or f"s3://{account_id}-fluid-staging/glue-temp/",
                    "tags": _get_resource_tags(contract),
                }
            )
        else:
            logger.warning(
                format_event(
                    "glue_job_skipped",
                    reason="'role' and 'script_location' required in build.properties for engine: glue",
                )
            )

    elif pattern == "embedded-logic":
        # Inline SQL execution via Athena
        props = build_config.get("properties", {})
        sql = props.get("sql")

        if sql:
            actions.append(
                {
                    "op": "athena.execute_query",
                    "id": "embedded_query",
                    "sql": sql,
                    "workgroup": "primary",
                    "output_location": f"s3://{account_id}-fluid-staging/query-results/",
                }
            )

    logger.debug(format_event("transformations_planned", actions=len(actions)))

    return actions


def _plan_exposures(
    contract: Mapping[str, Any], account_id: str, region: str, logger: logging.Logger
) -> List[Dict[str, Any]]:
    """
    Plan data product exposure actions.

    Creates Glue tables (including Iceberg), Athena views, Redshift tables, etc.
    """
    from ..util.formats import get_iceberg_config, is_iceberg_format

    actions = []

    for exposure in contract.get("exposes", []):
        if not isinstance(exposure, dict):
            continue
        exposure_id = exposure.get("exposeId") or exposure.get("id")
        kind = exposure.get("kind", "table")
        binding = exposure.get("binding") or {}
        if not isinstance(binding, dict):
            continue
        platform = binding.get("platform", "").lower()
        contract_schema = exposure.get("contract", {}).get("schema", [])

        if platform in ["aws", "glue", "athena"]:
            location = binding.get("location", {})
            database = location.get("database") or binding.get("database")
            table = location.get("table") or binding.get("table")

            if kind == "table" and database and table:
                # Map FLUID schema to Glue schema
                glue_columns = _map_schema_to_glue(contract_schema)

                # Build S3 location — prefer contract bucket, resolve templates
                raw_bucket = location.get("bucket")
                bucket = _resolve_env_templates(raw_bucket) if raw_bucket else None
                if not bucket or "{{" in bucket:
                    bucket = f"{account_id}-fluid-data"
                path = location.get("path", f"{database}/{table}/")
                s3_location = f"s3://{bucket}/{path}"

                # Check if Iceberg format
                if is_iceberg_format(binding):
                    # Iceberg table
                    iceberg_config = get_iceberg_config(binding)

                    actions.append(
                        {
                            "op": "glue.ensure_iceberg_table",
                            "id": f"iceberg_table_{exposure_id}",
                            "database": database,
                            "table": table,
                            "columns": glue_columns,
                            "location": s3_location,
                            "icebergConfig": iceberg_config,
                            "description": exposure.get("description"),
                        }
                    )
                else:
                    # Standard table (Parquet/ORC/Avro/etc)
                    actions.append(
                        {
                            "op": "glue.ensure_table",
                            "id": f"table_{exposure_id}",
                            "database": database,
                            "table": table,
                            "columns": glue_columns,
                            "location": s3_location,
                            "input_format": binding.get("format", "parquet"),
                            "description": exposure.get("description"),
                        }
                    )

            elif kind == "view" and database:
                view_name = binding.get("view") or table
                query = binding.get("query")

                if query:
                    actions.append(
                        {
                            "op": "athena.create_view",
                            "id": f"view_{exposure_id}",
                            "database": database,
                            "view": view_name,
                            "query": query,
                        }
                    )

        elif platform == "redshift":
            cluster = binding.get("cluster")
            schema = binding.get("schema")
            table = binding.get("table")

            if schema:
                actions.append(
                    {
                        "op": "redshift.ensure_schema",
                        "id": f"schema_{schema}",
                        "cluster": cluster,
                        "schema": schema,
                    }
                )

            if kind == "table" and table:
                redshift_columns = _map_schema_to_redshift(contract_schema)

                actions.append(
                    {
                        "op": "redshift.ensure_table",
                        "id": f"table_{exposure_id}",
                        "cluster": cluster,
                        "schema": schema,
                        "table": table,
                        "columns": redshift_columns,
                    }
                )

    logger.debug(format_event("exposures_planned", actions=len(actions)))

    return actions


def _plan_scheduling(
    contract: Mapping[str, Any], account_id: str, region: str, logger: logging.Logger
) -> List[Dict[str, Any]]:
    """
    Plan scheduling and orchestration actions.

    Sets up EventBridge rules, Step Functions, etc.
    """
    actions = []

    execution = contract.get("execution", {})
    trigger = execution.get("trigger", {})

    if not trigger:
        return actions

    trigger_type = trigger.get("type")

    if trigger_type == "schedule":
        schedule_expr = trigger.get("schedule")

        if schedule_expr:
            # Convert FLUID schedule to EventBridge cron
            eventbridge_schedule = _convert_schedule_to_eventbridge(schedule_expr)

            actions.append(
                {
                    "op": "events.ensure_schedule",
                    "id": "schedule_rule",
                    "name": f"{contract.get('id', 'contract').replace('.', '-')}-schedule",
                    "schedule": eventbridge_schedule,
                    "description": f"Schedule for {contract.get('name')}",
                    "tags": _get_resource_tags(contract),
                }
            )

    logger.debug(format_event("scheduling_planned", actions=len(actions)))

    return actions


def _get_resource_tags(contract: Mapping[str, Any]) -> Dict[str, str]:
    """
    Extract tags for AWS resources from contract metadata.

    Args:
        contract: FLUID contract

    Returns:
        Dictionary of tags for AWS resources
    """
    tags = {}

    if contract.get("id"):
        tags["fluid:contract:id"] = contract["id"]

    if contract.get("name"):
        tags["fluid:contract:name"] = contract["name"]

    metadata = contract.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    if metadata.get("domain"):
        tags["fluid:domain"] = metadata["domain"]

    if metadata.get("layer"):
        tags["fluid:layer"] = metadata["layer"]

    owner = metadata.get("owner") or {}
    if isinstance(owner, dict) and owner.get("team"):
        tags["fluid:team"] = owner["team"]

    # Add custom tags from metadata
    custom_tags = metadata.get("tags") or {}
    if not isinstance(custom_tags, dict):
        custom_tags = {}
    for key, value in custom_tags.items():
        tags[key] = str(value)

    return tags


def _map_schema_to_glue(fluid_schema: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Map FLUID schema to Glue table columns."""
    type_mapping = {
        "string": "string",
        "text": "string",
        "integer": "bigint",
        "int": "bigint",
        "long": "bigint",
        "numeric": "double",
        "float": "double",
        "double": "double",
        "boolean": "boolean",
        "bool": "boolean",
        "timestamp": "timestamp",
        "date": "date",
        "binary": "binary",
    }

    columns = []
    for field in fluid_schema:
        glue_type = type_mapping.get(field.get("type", "string").lower(), "string")

        columns.append(
            {
                "Name": field["name"],
                "Type": glue_type,
                "Comment": field.get("description", ""),
            }
        )

    return columns


def _map_schema_to_redshift(fluid_schema: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Map FLUID schema to Redshift column definitions."""
    type_mapping = {
        "string": "VARCHAR(65535)",
        "text": "VARCHAR(65535)",
        "integer": "BIGINT",
        "int": "BIGINT",
        "long": "BIGINT",
        "numeric": "DOUBLE PRECISION",
        "float": "REAL",
        "double": "DOUBLE PRECISION",
        "boolean": "BOOLEAN",
        "bool": "BOOLEAN",
        "timestamp": "TIMESTAMP",
        "date": "DATE",
        "binary": "VARBYTE(65535)",
    }

    columns = []
    for field in fluid_schema:
        redshift_type = type_mapping.get(field.get("type", "string").lower(), "VARCHAR(65535)")

        column_def = {
            "name": field["name"],
            "type": redshift_type,
            "nullable": not field.get("required", False),
        }

        if field.get("description"):
            column_def["comment"] = field["description"]

        columns.append(column_def)

    return columns


def _convert_schedule_to_eventbridge(schedule: str) -> str:
    """Convert FLUID schedule expression to EventBridge cron expression."""
    # If already in cron format, return as-is
    if schedule.startswith("cron(") or schedule.startswith("rate("):
        return schedule

    # Common conversions
    conversions = {
        "hourly": "rate(1 hour)",
        "daily": "cron(0 0 * * ? *)",
        "weekly": "cron(0 0 ? * SUN *)",
        "monthly": "cron(0 0 1 * ? *)",
    }

    return conversions.get(schedule.lower(), schedule)
