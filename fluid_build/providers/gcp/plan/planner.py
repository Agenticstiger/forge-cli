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

from fluid_build.providers._planner_base import BasePlanner

from ..util.logging import format_event
from ..util.names import normalize_bucket_name, normalize_dataset_name


class GcpPlanner(BasePlanner):
    """GCP-specific planner — wires the 6-phase scaffold to the
    BigQuery / Cloud Storage / Composer phase functions below.

    The phase functions stay module-level (the test suite calls them
    individually); this class wires them into the canonical phase
    order owned by :class:`BasePlanner`.
    """

    _logger_name = "fluid.providers.gcp.planner"

    def __init__(
        self,
        *,
        project: str,
        region: str,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        super().__init__(logger=logger)
        self.project = project
        self.region = region

    def plan_infrastructure(self, contract):
        return _plan_infrastructure(contract, self.project, self.region, self.logger)

    def plan_iam(self, contract):
        return _plan_iam_policies(contract, self.project, self.logger)

    def plan_replace_snapshots(self, contract):
        return _plan_replace_snapshots(contract, self.project, self.region, self.logger)

    def plan_expose(self, contract, *, is_destructive):
        return _plan_exposures(
            contract,
            self.project,
            self.region,
            self.logger,
            is_destructive=is_destructive,
        )

    def plan_build(self, contract, *, is_destructive):
        return _plan_build_transformations(
            contract,
            self.project,
            self.region,
            self.logger,
            is_destructive=is_destructive,
        )

    def plan_schedule(self, contract):
        return _plan_scheduling(contract, self.project, self.region, self.logger)


def plan_actions(
    contract: Mapping[str, Any],
    project: str,
    region: str,
    logger: Optional[logging.Logger] = None,
    *,
    mode: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Generate GCP actions from FLUID contract.

    Back-compat shim — constructs a :class:`GcpPlanner` and calls
    :meth:`GcpPlanner.plan`. The 6-phase ordering, destructive-mode
    handling, and CTAS backup emission live in
    :class:`fluid_build.providers._planner_base.BasePlanner` so the
    GCP provider stays in lockstep with Snowflake / AWS.

    Phase order (set by ``BasePlanner.plan``):
    1. Infrastructure — datasets and buckets.
    2. IAM — service-account bindings, custom roles.
    3. Replace snapshots — pre-flight CTAS per bigquery_table
       (destructive modes only).
    4. Expose — tables, APIs, streams.
    5. Build — dbt / Dataform / SQL transforms.
    6. Schedule — Composer DAGs / Cloud Scheduler.

    For destructive modes (``replace`` / ``replace-and-build``),
    SQL builds emit ``CREATE OR REPLACE TABLE … AS SELECT`` (BigQuery's
    atomic replace) and a pre-flight CTAS backup is emitted per
    ``bigquery_table`` expose for rollback.

    Args:
        contract: FLUID contract specification
        project: GCP project ID
        region: GCP region
        logger: Optional logger instance
        mode: apply-time mode for destructive-vs-additive routing

    Returns:
        List of ordered actions to execute
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    contract_id = contract.get("id")
    if not contract_id:
        raise ValueError("Contract must have an 'id' field")

    logger.debug(format_event("planning_started", contract_id=contract_id, mode=mode))

    planner = GcpPlanner(project=project, region=region, logger=logger)
    actions = planner.plan(contract, mode=mode)

    logger.info(
        format_event(
            "planning_completed",
            contract_id=contract_id,
            total_actions=len(actions),
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
            # v0.7.x structure
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

                # Read 'region' from binding properties (v0.7.x canonical)
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
    # Support both v0.7.x builds array
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
    contract: Mapping[str, Any],
    project: str,
    region: str,
    logger: logging.Logger,
    *,
    is_destructive: bool = False,
) -> List[Dict[str, Any]]:
    """
    Plan build transformation actions.

    Sets up dbt, Dataform, or other transformation engines. When
    ``is_destructive`` is True, the action is stamped with
    ``mode="replace"`` so the dbt executor adds ``--full-refresh``
    and SQL transforms emit ``CREATE OR REPLACE TABLE … AS SELECT``.
    """
    actions = []
    apply_mode = "replace" if is_destructive else "amend"

    # Support both v0.7.x builds array
    from fluid_build.util.contract import get_primary_build

    build_config = get_primary_build(contract) or {}
    transformation = build_config.get("transformation", {})

    if transformation:
        from .bq_modeler import plan_transformation_actions

        transformation_actions = plan_transformation_actions(
            transformation, contract, project, region, logger
        )
        # Stamp ``mode`` on each emitted action so downstream executors
        # (the GCP action handler + bq_modeler runtime) can route additive
        # vs destructive paths consistently.
        for action in transformation_actions:
            action.setdefault("mode", apply_mode)
        actions.extend(transformation_actions)

    # Inline-SQL build path (mirrors the Snowflake planner's
    # ``_plan_build`` SQL emission). For each ``builds[]`` entry with
    # ``properties.sql`` AND outputs targeting a ``bigquery_table``
    # expose, emit ``bq.sql.execute`` actions wrapping the SQL into
    # INSERT INTO (additive) or CREATE OR REPLACE TABLE … AS SELECT
    # (destructive). Multi-output builds emit one action per output.
    for build_idx, build_entry in enumerate(contract.get("builds", []) or []):
        if not isinstance(build_entry, Mapping):
            continue
        props = build_entry.get("properties", {})
        if not isinstance(props, Mapping):
            props = {}
        sql_text = build_entry.get("sql") or props.get("sql")
        if not sql_text:
            continue
        outputs = list(build_entry.get("outputs") or [])
        if not outputs:
            outputs = [None]
        build_id = build_entry.get("id", f"build_{build_idx}")
        for out_id in outputs:
            wrapped = (
                _bq_wrap_sql_for_target(
                    sql_text=sql_text,
                    contract=contract,
                    target_output_id=out_id,
                    default_project=project,
                    is_destructive=is_destructive,
                )
                if out_id
                else sql_text
            )
            action_id = build_id if len(outputs) == 1 else f"{build_id}__{out_id}"
            actions.append(
                {
                    "id": action_id,
                    "op": "bq.sql.execute",
                    "phase": "build",
                    "project": project,
                    "sql": wrapped,
                    "mode": apply_mode,
                    "comment": build_entry.get("description") or build_entry.get("name"),
                }
            )

    logger.debug(format_event("transformations_planned", actions=len(actions), mode=apply_mode))

    return actions


def _bq_wrap_sql_for_target(
    *,
    sql_text: str,
    contract: Mapping[str, Any],
    target_output_id: str,
    default_project: str,
    is_destructive: bool,
) -> str:
    """Wrap a build's SQL with INSERT or CTAS for a BigQuery target.

    Mirrors :func:`fluid_build.providers.snowflake.plan.planner._wrap_sql_for_target`
    using BigQuery's backtick-quoted identifier syntax. Pass-through
    when the target expose isn't a ``bigquery_table`` binding or when
    the SQL already declares its own sink (INSERT/CREATE/MERGE/etc).
    """
    target_expose = next(
        (
            ex
            for ex in (contract.get("exposes") or [])
            if isinstance(ex, Mapping)
            and (ex.get("exposeId") == target_output_id or ex.get("id") == target_output_id)
        ),
        None,
    )
    if target_expose is None:
        return sql_text
    binding = target_expose.get("binding") or {}
    if (binding.get("format") or "").lower() not in (
        "bigquery_table",
        "bigquery-table",
    ):
        return sql_text
    location = binding.get("location") or {}
    proj = location.get("project") or default_project
    dataset = location.get("dataset") or location.get("schema")
    table = location.get("table") or target_output_id
    if not (proj and dataset and table):
        return sql_text
    upper_head = sql_text.lstrip().upper()[:32]
    for kw in ("INSERT", "CREATE", "MERGE", "UPDATE", "DELETE", "COPY", "TRUNCATE"):
        if upper_head.startswith(kw):
            return sql_text
    body = sql_text.rstrip().rstrip(";")
    fqn = f"`{proj}.{dataset}.{table}`"
    if is_destructive:
        return f"CREATE OR REPLACE TABLE {fqn} AS\n{body}"
    return f"INSERT INTO {fqn}\n{body}"


def _plan_replace_snapshots(
    contract: Mapping[str, Any],
    project: str,
    region: str,
    logger: logging.Logger,
) -> List[Dict[str, Any]]:
    """Pre-flight CTAS backups for ``--mode replace`` against BigQuery.

    For each ``bigquery_table`` expose, emit an action that runs
    ``CREATE TABLE IF NOT EXISTS <backup> AS SELECT * FROM <orig>``
    BEFORE the destructive replace fires. BigQuery has no CLONE so the
    backup is a real copy (storage cost applies); the
    ``IF NOT EXISTS`` guard keeps re-runs idempotent.

    The action carries a ``rollback_snapshot`` marker so apply.py's
    ``_rollback_writer`` can record the snapshot in
    ``.fluid/rollback-state.json`` for ``fluid rollback``.

    Skips exposes whose source table doesn't exist yet (first-run
    replace) — the IF NOT EXISTS gate also covers the pre-existence
    case but BigQuery raises if the SOURCE is absent. The backup
    action is marked ``allow_failure: True`` so a missing source on
    first-run replace soft-skips rather than aborting the plan.
    """
    import time as _time

    actions: List[Dict[str, Any]] = []
    backup_ts = int(_time.time())
    for expose in contract.get("exposes", []) or []:
        if not isinstance(expose, Mapping):
            continue
        binding = expose.get("binding") or {}
        if (binding.get("format") or "").lower() not in (
            "bigquery_table",
            "bigquery-table",
        ):
            continue
        location = binding.get("location") or {}
        proj = location.get("project") or project
        dataset = location.get("dataset") or location.get("schema")
        table = location.get("table") or expose.get("exposeId")
        if not (proj and dataset and table):
            continue
        backup = f"BACKUP_{table}_{backup_ts}"
        sql = (
            f"CREATE TABLE IF NOT EXISTS `{proj}.{dataset}.{backup}` AS "
            f"SELECT * FROM `{proj}.{dataset}.{table}`"
        )
        actions.append(
            {
                "id": f"snapshot_{expose.get('exposeId')}",
                "op": "bq.sql.execute",
                "phase": "snapshot",
                "project": proj,
                "dataset": dataset,
                "sql": sql,
                "comment": f"pre-flight backup of {proj}.{dataset}.{table}",
                "allow_failure": True,
                "rollback_snapshot": {
                    "backup_name": backup,
                    "product_id": contract.get("id"),
                    "expose_id": expose.get("exposeId"),
                    "location": {
                        "database": proj,
                        "schema": dataset,
                        "table": table,
                        "backup_table": backup,
                    },
                },
            }
        )
    return actions


def _plan_exposures(
    contract: Mapping[str, Any],
    project: str,
    region: str,
    logger: logging.Logger,
    *,
    is_destructive: bool = False,
) -> List[Dict[str, Any]]:
    """
    Plan data product exposure actions.

    Creates tables, views, APIs, streams, etc. When ``is_destructive``
    is True and the expose is a target of a SQL build (listed in
    ``builds[].outputs``), the ensure_table step is skipped because
    the build's CREATE OR REPLACE TABLE handles materialisation.
    """
    actions = []
    # Build the set of expose ids targeted by SQL builds; their
    # ensure_table is skipped under destructive modes.
    sql_build_targets: set = set()
    if is_destructive:
        for build_entry in contract.get("builds", []) or []:
            if not isinstance(build_entry, Mapping):
                continue
            outputs = build_entry.get("outputs") or []
            if outputs and (
                build_entry.get("sql") or (build_entry.get("properties") or {}).get("sql")
            ):
                sql_build_targets.update(outputs)

    for exposure in contract.get("exposes", []):
        exposure_id = exposure.get("id") or exposure.get("exposeId")
        exposure.get("type") or exposure.get("kind")

        # Skip ensure_table for destructive-mode SQL-build targets —
        # CREATE OR REPLACE TABLE in the build phase materialises them.
        if is_destructive and exposure_id in sql_build_targets:
            continue

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
                # Use v0.7.x structure
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
    if metadata.get("productType"):
        labels["fluid_product_type"] = _sanitize_label_value(metadata["productType"])

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

    # Add contract-level labels (v0.7.x root labels)
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
