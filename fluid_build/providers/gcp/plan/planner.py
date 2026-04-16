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

# fluid_build/providers/gcp/plan/planner.py
"""
GCP provider planning engine.

Orchestrates contract-to-actions mapping across all GCP services.
Converts FLUID contract specifications into concrete GCP resource operations.
"""

import logging
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from ..util.logging import format_event
from ..util.names import normalize_bucket_name, normalize_dataset_name


def plan_actions(
    contract: Mapping[str, Any], project: str, region: str, logger: Optional[logging.Logger] = None
) -> List[Dict[str, Any]]:
    """
    Generate GCP actions from FLUID contract.

    Analyzes contract and produces ordered list of actions to:
    1. Create necessary datasets and buckets
    2. Set up IAM policies
    3. Deploy transformation logic (dbt/Dataform)
    4. Configure scheduling (Composer/Scheduler)
    5. Expose data products (tables, APIs, streams)

    Args:
        contract: FLUID contract specification
        project: GCP project ID
        region: GCP region
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

    # Phase 1: Infrastructure setup (datasets, buckets)
    infrastructure_actions = _plan_infrastructure(contract, project, region, logger)
    actions.extend(infrastructure_actions)

    # Phase 2: IAM and security policies
    iam_actions = _plan_iam_policies(contract, project, logger)
    actions.extend(iam_actions)

    # Phase 3: Build transformations (dbt, Dataform)
    build_actions = _plan_build_transformations(contract, project, region, logger)
    actions.extend(build_actions)

    # Phase 4: Expose data products
    expose_actions = _plan_exposures(contract, project, region, logger)
    actions.extend(expose_actions)

    # Phase 5: Scheduling and orchestration
    schedule_actions = _plan_scheduling(contract, project, region, logger)
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
    contract: Mapping[str, Any], project: str, region: str, logger: logging.Logger
) -> List[Dict[str, Any]]:
    """
    Plan infrastructure setup actions.

    Creates necessary datasets, buckets, and foundational resources.
    Supports both old (location.format/properties) and new (binding.format/location) structures.
    """
    actions = []
    datasets_created = set()
    buckets_created = set()

    # Analyze exposures to determine required infrastructure
    for exposure in contract.get("exposes", []):
        # Support both old and new contract structures
        # Old: exposure.location.format + exposure.location.properties
        # New: exposure.binding.format + exposure.binding.location
        location = exposure.get("location", {})
        binding = exposure.get("binding", {})

        if binding:
            # New 0.5.7 structure
            format_type = binding.get("format")
            properties = binding.get("location", {})
        else:
            # Old structure
            format_type = location.get("format")
            properties = location.get("properties", {})

        if format_type == "bigquery_table":
            # Ensure dataset exists
            dataset_project = properties.get("project", project)
            dataset_name = properties.get("dataset")

            if dataset_name and (dataset_project, dataset_name) not in datasets_created:
                normalized_dataset = normalize_dataset_name(dataset_name)

                # Support both 'region' (new v0.5.7) and 'location' (legacy) keys
                dataset_location = properties.get("region") or properties.get("location", "US")

                actions.append(
                    {
                        "op": "bq.ensure_dataset",
                        "id": f"dataset_{normalized_dataset}",
                        "project": dataset_project,
                        "dataset": normalized_dataset,
                        "location": dataset_location,
                        "description": f"Dataset for {contract.get('name', 'data product')}",
                        "labels": _get_resource_labels(contract, exposure),
                    }
                )

                datasets_created.add((dataset_project, dataset_name))

        elif format_type == "gcs_bucket":
            # Ensure bucket exists
            bucket_project = properties.get("project", project)
            bucket_name = properties.get("bucket")

            if bucket_name and (bucket_project, bucket_name) not in buckets_created:
                normalized_bucket = normalize_bucket_name(bucket_name, bucket_project)

                actions.append(
                    {
                        "op": "gcs.ensure_bucket",
                        "id": f"bucket_{normalized_bucket}",
                        "project": bucket_project,
                        "bucket": normalized_bucket,
                        "location": properties.get("location", region),
                        "storage_class": properties.get("storage_class", "STANDARD"),
                        "labels": _get_resource_labels(contract),
                    }
                )

                buckets_created.add((bucket_project, bucket_name))

    # Check build section for additional infrastructure needs
    # Support both 0.5.7 (builds array) and 0.4.0 (build object)
    from fluid_build.util.contract import get_primary_build

    build_config = get_primary_build(contract) or {}
    transformation = build_config.get("transformation", {})

    if transformation:
        engine = transformation.get("engine")

        # dbt/Dataform may need staging buckets
        if engine in ["dbt-bigquery", "dataform"]:
            staging_bucket = f"{project}-fluid-staging"
            if staging_bucket not in [b[1] for b in buckets_created]:
                actions.append(
                    {
                        "op": "gcs.ensure_bucket",
                        "id": "bucket_staging",
                        "project": project,
                        "bucket": staging_bucket,
                        "location": region,
                        "storage_class": "STANDARD",
                        "labels": {**_get_resource_labels(contract), "purpose": "staging"},
                    }
                )

    logger.debug(
        format_event(
            "infrastructure_planned",
            datasets=len(datasets_created),
            buckets=len(buckets_created),
            actions=len(actions),
        )
    )

    return actions


def _plan_iam_policies(
    contract: Mapping[str, Any], project: str, logger: logging.Logger
) -> List[Dict[str, Any]]:
    """
    Plan IAM policy actions.

    Converts FLUID policies to GCP IAM bindings.
    """
    actions = []

    metadata = contract.get("metadata", {})
    policies = metadata.get("policies", {})

    if not policies:
        return actions

    # For each exposure, apply relevant policies
    for exposure in contract.get("exposes", []):
        exposure.get("id") or exposure.get("exposeId")

        # Support both old and new structures
        location = exposure.get("location", {})
        binding = exposure.get("binding", {})

        if binding:
            format_type = binding.get("format")
            properties = binding.get("location", {})
        else:
            format_type = location.get("format")
            properties = location.get("properties", {})

        if format_type == "bigquery_table":
            dataset_project = properties.get("project", project)
            dataset_name = properties.get("dataset")
            table_name = properties.get("table")

            # Dataset-level IAM
            if dataset_name:
                actions.append(
                    {
                        "op": "iam.bind_bq_dataset",
                        "id": f"iam_dataset_{dataset_name}",
                        "project": dataset_project,
                        "dataset": dataset_name,
                        "policies": policies,
                    }
                )

            # Table-level IAM (if supported)
            if table_name and _should_apply_table_level_iam(policies):
                actions.append(
                    {
                        "op": "iam.bind_bq_table",
                        "id": f"iam_table_{table_name}",
                        "project": dataset_project,
                        "dataset": dataset_name,
                        "table": table_name,
                        "policies": policies,
                    }
                )

        elif format_type == "gcs_bucket":
            bucket_project = properties.get("project", project)
            bucket_name = properties.get("bucket")

            if bucket_name:
                actions.append(
                    {
                        "op": "iam.bind_gcs_bucket",
                        "id": f"iam_bucket_{bucket_name}",
                        "project": bucket_project,
                        "bucket": bucket_name,
                        "policies": policies,
                    }
                )

    logger.debug(format_event("iam_policies_planned", actions=len(actions)))

    return actions


def _plan_build_transformations(
    contract: Mapping[str, Any], project: str, region: str, logger: logging.Logger
) -> List[Dict[str, Any]]:
    """
    Plan build transformation actions.

    Sets up dbt, Dataform, or other transformation engines.
    """
    actions = []

    # Support both 0.5.7 (builds array) and 0.4.0 (build object)
    from fluid_build.util.contract import get_primary_build

    build_config = get_primary_build(contract) or {}
    transformation = build_config.get("transformation", {})

    if not transformation:
        return actions

    from .bq_modeler import plan_transformation_actions

    transformation_actions = plan_transformation_actions(
        transformation, contract, project, region, logger
    )
    actions.extend(transformation_actions)

    logger.debug(format_event("transformations_planned", actions=len(actions)))

    return actions


def _plan_exposures(
    contract: Mapping[str, Any], project: str, region: str, logger: logging.Logger
) -> List[Dict[str, Any]]:
    """
    Plan data product exposure actions.

    Creates tables, views, APIs, streams, etc.
    """
    actions = []

    for exposure in contract.get("exposes", []):
        exposure_id = exposure.get("id") or exposure.get("exposeId")
        exposure.get("type") or exposure.get("kind")

        # Support both old and new structures
        location = exposure.get("location", {})
        binding = exposure.get("binding", {})

        if binding:
            format_type = binding.get("format")
            properties = binding.get("location", {})
        else:
            format_type = location.get("format")
            properties = location.get("properties", {})

        if format_type == "bigquery_table":
            # Get schema from either old or new structure
            schema = exposure.get("schema", [])
            if not schema:
                # Try new 0.5.7 structure
                contract_def = exposure.get("contract", {})
                schema = contract_def.get("schema", [])

            actions.append(
                {
                    "op": "bq.ensure_table",
                    "id": f"table_{exposure_id}",
                    "project": properties.get("project", project),
                    "dataset": properties.get("dataset"),
                    "table": properties.get("table"),
                    "schema": schema,
                    "description": exposure.get("description"),
                    "labels": _get_resource_labels(contract, exposure),
                    "partitioning": properties.get("partitioning"),
                    "clustering": properties.get("clustering"),
                    "location": properties.get("region") or properties.get("location", "US"),
                    "contract": contract,  # Pass full contract for policy extraction
                }
            )

        elif format_type == "bigquery_view":
            actions.append(
                {
                    "op": "bq.ensure_view",
                    "id": f"view_{exposure_id}",
                    "project": properties.get("project", project),
                    "dataset": properties.get("dataset"),
                    "view": properties.get("view"),
                    "query": properties.get("query"),
                    "description": exposure.get("description"),
                    "labels": _get_resource_labels(contract, exposure),
                }
            )

        elif format_type == "pubsub_topic":
            actions.append(
                {
                    "op": "ps.ensure_topic",
                    "id": f"topic_{exposure_id}",
                    "project": project,
                    "topic": properties.get("topic"),
                    "labels": _get_resource_labels(contract, exposure),
                    "message_retention_duration": properties.get("message_retention_duration"),
                }
            )

    logger.debug(format_event("exposures_planned", actions=len(actions)))

    return actions


def _plan_scheduling(
    contract: Mapping[str, Any], project: str, region: str, logger: logging.Logger
) -> List[Dict[str, Any]]:
    """
    Plan scheduling and orchestration actions.

    Sets up Composer DAGs, Cloud Scheduler jobs, etc.
    """
    actions = []

    execution = contract.get("execution", {})
    trigger = execution.get("trigger", {})

    if not trigger:
        return actions

    from .schedule import plan_schedule_actions

    schedule_actions = plan_schedule_actions(trigger, contract, project, region, logger)
    actions.extend(schedule_actions)

    logger.debug(format_event("scheduling_planned", actions=len(actions)))

    return actions


def _get_resource_labels(
    contract: Mapping[str, Any], exposure: Optional[Mapping[str, Any]] = None
) -> Dict[str, str]:
    """
    Extract labels for GCP resources from contract metadata and exposure governance policies.

    Args:
        contract: FLUID contract
        exposure: Optional specific exposure to extract labels from

    Returns:
        Dictionary of labels for GCP resources
    """
    labels = {}

    # Standard labels from contract
    if contract.get("id"):
        labels["fluid_contract_id"] = _sanitize_label_value(contract["id"])

    if contract.get("name"):
        labels["fluid_contract_name"] = _sanitize_label_value(contract["name"])

    metadata = contract.get("metadata", {})

    if metadata.get("domain"):
        labels["fluid_domain"] = _sanitize_label_value(metadata["domain"])

    if metadata.get("layer"):
        labels["fluid_layer"] = _sanitize_label_value(metadata["layer"])

    if metadata.get("owner", {}).get("team"):
        labels["fluid_team"] = _sanitize_label_value(metadata["owner"]["team"])

    # Add custom labels from metadata
    custom_labels = metadata.get("labels", {})
    for key, value in custom_labels.items():
        sanitized_key = _sanitize_label_key(key)
        sanitized_value = _sanitize_label_value(str(value))
        if sanitized_key and sanitized_value:
            labels[sanitized_key] = sanitized_value

    # Add tags from contract (convert to labels)
    for tag in contract.get("tags", []):
        safe_tag = _sanitize_label_key(tag)
        if safe_tag:
            labels[f"tag_{safe_tag}"] = "true"

    # Add contract-level labels (v0.5.7 root labels)
    for key, value in contract.get("labels", {}).items():
        sanitized_key = _sanitize_label_key(key)
        sanitized_value = _sanitize_label_value(str(value))
        if sanitized_key and sanitized_value:
            labels[sanitized_key] = sanitized_value

    # Extract governance labels from exposure if provided
    if exposure:
        # Exposure-level labels
        for key, value in exposure.get("labels", {}).items():
            sanitized_key = _sanitize_label_key(key)
            sanitized_value = _sanitize_label_value(str(value))
            if sanitized_key and sanitized_value:
                labels[sanitized_key] = sanitized_value

        # Exposure-level tags (convert to labels)
        for tag in exposure.get("tags", []):
            safe_tag = _sanitize_label_key(tag)
            if safe_tag:
                labels[f"tag_{safe_tag}"] = "true"

        # Policy governance labels
        policy = exposure.get("policy", {})

        # Data classification
        if policy.get("classification"):
            labels["data_classification"] = _sanitize_label_value(policy["classification"])

        # Authentication method
        if policy.get("authn"):
            labels["authn_method"] = _sanitize_label_value(policy["authn"])

        # Policy labels
        for key, value in policy.get("labels", {}).items():
            sanitized_key = _sanitize_label_key(f"policy_{key}")
            sanitized_value = _sanitize_label_value(str(value))
            if sanitized_key and sanitized_value:
                labels[sanitized_key] = sanitized_value

        # Policy tags
        for tag in policy.get("tags", []):
            safe_tag = _sanitize_label_key(tag)
            if safe_tag:
                labels[f"policy_{safe_tag}"] = "true"

    return labels


def _sanitize_label_key(key: str) -> str:
    """Sanitize label key for GCP requirements."""
    import re

    # GCP label keys must be lowercase, start with letter, contain only letters, numbers, underscores, hyphens
    sanitized = re.sub(r"[^a-z0-9_-]", "_", key.lower())

    # Must start with letter
    if sanitized and not sanitized[0].isalpha():
        sanitized = f"label_{sanitized}"

    # Maximum 63 characters
    return sanitized[:63] if sanitized else ""


def _sanitize_label_value(value: str) -> str:
    """Sanitize label value for GCP requirements."""
    import re

    # GCP label values can contain lowercase letters, numbers, underscores, hyphens
    sanitized = re.sub(r"[^a-z0-9_-]", "_", value.lower())

    # Maximum 63 characters
    return sanitized[:63] if sanitized else ""


def _should_apply_table_level_iam(policies: Dict[str, Any]) -> bool:
    """
    Determine if table-level IAM should be applied.

    Table-level IAM is more granular but not always necessary.
    Apply when policies are complex or fine-grained access is needed.
    """
    # For now, apply table-level IAM if there are fine-grained policies
    if isinstance(policies, dict):
        # Check for role-based or column-level policies
        return any(key in policies for key in ["column_access", "row_access", "fine_grained"])

    return False
