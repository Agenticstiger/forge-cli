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

# fluid_build/providers/gcp/plan/schedule.py
"""
Scheduling and orchestration planning for GCP provider.

Maps FLUID execution.trigger specifications to concrete
Composer DAGs, Cloud Scheduler jobs, and Cloud Run services.
"""

import logging
from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from ..util.logging import format_event
from ..util.names import normalize_job_name


def plan_schedule_actions(
    trigger: Mapping[str, Any],
    contract: Mapping[str, Any],
    project: str,
    region: str,
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """
    Plan scheduling and orchestration actions.

    Supports:
    - schedule: Cron-based scheduling via Composer or Cloud Scheduler
    - event: Event-driven triggers via Pub/Sub and Cloud Run
    - manual: Manual execution setup

    Args:
        trigger: Trigger configuration from contract execution
        contract: Full FLUID contract for context
        project: GCP project ID
        region: GCP region
        logger: Optional logger instance

    Returns:
        List of scheduling actions
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    trigger_type = trigger.get("type")
    trigger.get("properties", {})

    logger.debug(
        format_event(
            "schedule_planning_started", trigger_type=trigger_type, contract_id=contract.get("id")
        )
    )

    if trigger_type == "schedule":
        return _plan_scheduled_execution(trigger, contract, project, region, logger)
    elif trigger_type == "event":
        return _plan_event_driven_execution(trigger, contract, project, region, logger)
    elif trigger_type == "manual":
        return _plan_manual_execution(trigger, contract, project, region, logger)
    else:
        logger.warning(
            format_event(
                "unknown_trigger_type", trigger_type=trigger_type, contract_id=contract.get("id")
            )
        )
        return []


def _plan_scheduled_execution(
    trigger: Mapping[str, Any],
    contract: Mapping[str, Any],
    project: str,
    region: str,
    logger: logging.Logger,
) -> List[Dict[str, Any]]:
    """
    Plan scheduled execution via Composer or Cloud Scheduler.

    Preference order:
    1. Cloud Composer (if environment specified)
    2. Cloud Scheduler + Cloud Run (serverless)
    """
    actions = []
    properties = trigger.get("properties", {})

    cron_expression = trigger.get("cron")
    if not cron_expression:
        logger.error(format_event("missing_cron_expression", contract_id=contract.get("id")))
        return actions

    # Check if Composer environment is specified
    composer_env = properties.get("composer_environment")
    composer_location = properties.get("composer_location", region)

    if composer_env:
        # Use Cloud Composer for orchestration
        actions.extend(
            _plan_composer_dag(trigger, contract, project, composer_location, composer_env, logger)
        )
    else:
        # Use Cloud Scheduler + Cloud Run for serverless execution
        actions.extend(_plan_scheduler_job(trigger, contract, project, region, logger))

    return actions


def _plan_composer_dag(
    trigger: Mapping[str, Any],
    contract: Mapping[str, Any],
    project: str,
    location: str,
    environment: str,
    logger: logging.Logger,
) -> List[Dict[str, Any]]:
    """
    Plan Composer DAG deployment and configuration.
    """
    actions = []
    properties = trigger.get("properties", {})

    contract_id = contract.get("id", "unknown")
    dag_id = properties.get("dag_id", f"fluid_{contract_id}")

    # 1. Deploy DAG to Composer environment
    actions.append(
        {
            "op": "composer.deploy_dag",
            "id": f"composer_dag_{dag_id}",
            "project": project,
            "location": location,
            "environment": environment,
            "dag_id": dag_id,
            "schedule_interval": trigger.get("cron"),
            "description": f"FLUID data product pipeline for {contract.get('name', contract_id)}",
            "tags": [
                "fluid",
                contract.get("metadata", {}).get("domain", "unknown"),
                contract.get("metadata", {}).get("layer", "unknown"),
            ],
            "default_args": {
                "owner": contract.get("metadata", {})
                .get("owner", {})
                .get("email", "fluid@company.com"),
                "depends_on_past": properties.get("depends_on_past", False),
                "email_on_failure": properties.get("email_on_failure", True),
                "email_on_retry": properties.get("email_on_retry", False),
                "retries": properties.get("retries", 2),
                "retry_delay_minutes": properties.get("retry_delay_minutes", 5),
            },
            "max_active_runs": properties.get("max_active_runs", 1),
            "catchup": properties.get("catchup", False),
            "start_date": properties.get("start_date"),
        }
    )

    # 2. Set Composer environment variables (if needed)
    dag_variables = properties.get("variables", {})
    if dag_variables:
        actions.append(
            {
                "op": "composer.ensure_variables",
                "id": f"composer_vars_{dag_id}",
                "project": project,
                "location": location,
                "environment": environment,
                "variables": {
                    f"fluid_{dag_id}_project": project,
                    f"fluid_{dag_id}_region": location,
                    **dag_variables,
                },
            }
        )

    # 3. Optionally trigger initial DAG run
    if properties.get("trigger_initial_run", False):
        actions.append(
            {
                "op": "composer.trigger_dag",
                "id": f"composer_trigger_{dag_id}",
                "project": project,
                "location": location,
                "environment": environment,
                "dag_id": dag_id,
                "run_id": f"initial_{int(__import__('time').time())}",
                "conf": properties.get("initial_run_conf", {}),
            }
        )

    logger.debug(
        format_event(
            "composer_dag_planned",
            dag_id=dag_id,
            environment=environment,
            actions_count=len(actions),
        )
    )

    return actions


def _plan_scheduler_job(
    trigger: Mapping[str, Any],
    contract: Mapping[str, Any],
    project: str,
    region: str,
    logger: logging.Logger,
) -> List[Dict[str, Any]]:
    """
    Plan Cloud Scheduler job with Cloud Run target.
    """
    actions = []
    properties = trigger.get("properties", {})

    contract_id = contract.get("id", "unknown")
    job_name = normalize_job_name(properties.get("job_name", f"fluid-{contract_id}"))
    service_name = normalize_job_name(properties.get("service_name", f"fluid-{contract_id}"))

    # 1. Ensure Cloud Run service exists
    actions.append(
        {
            "op": "run.ensure_service",
            "id": f"run_service_{service_name}",
            "project": project,
            "region": region,
            "service_name": service_name,
            "image": properties.get("image", "gcr.io/fluid-forge/runner:latest"),
            "cpu": properties.get("cpu", "1"),
            "memory": properties.get("memory", "512Mi"),
            "concurrency": properties.get("concurrency", 1),
            "timeout": properties.get("timeout", 300),
            "env_vars": {
                "FLUID_PROJECT": project,
                "FLUID_REGION": region,
                "FLUID_CONTRACT_ID": contract_id,
                **properties.get("env_vars", {}),
            },
            "labels": {
                "fluid-contract-id": contract_id.replace(".", "-"),
                "fluid-domain": contract.get("metadata", {}).get("domain", "unknown"),
                "managed-by": "fluid-forge",
            },
            "service_account": properties.get("service_account"),
            "vpc_connector": properties.get("vpc_connector"),
            "max_instances": properties.get("max_instances", 10),
            "min_instances": properties.get("min_instances", 0),
        }
    )

    # 2. Create Cloud Scheduler job
    actions.append(
        {
            "op": "scheduler.ensure_job",
            "id": f"scheduler_job_{job_name}",
            "project": project,
            "location": region,
            "job_name": job_name,
            "description": f"Scheduled execution for FLUID contract {contract_id}",
            "schedule": trigger.get("cron"),
            "timezone": properties.get("timezone", "UTC"),
            "target": {
                "type": "http",
                "http_target": {
                    "uri": f"https://{service_name}-{_hash_project_region(project, region)}-{region}.a.run.app/execute",
                    "http_method": "POST",
                    "headers": {
                        "Content-Type": "application/json",
                    },
                    "body": _encode_job_body(
                        {
                            "contract_id": contract_id,
                            "execution_mode": "scheduled",
                            "trigger_time": "{{.ScheduledTime}}",
                        }
                    ),
                    "oidc_token": {
                        "service_account_email": properties.get("scheduler_service_account"),
                        "audience": f"https://{service_name}-{_hash_project_region(project, region)}-{region}.a.run.app",
                    },
                },
            },
            "retry_config": {
                "retry_count": properties.get("retry_count", 3),
                "max_retry_duration": f"{properties.get('max_retry_duration', 300)}s",
                "min_backoff_duration": f"{properties.get('min_backoff_duration', 5)}s",
                "max_backoff_duration": f"{properties.get('max_backoff_duration', 300)}s",
                "max_doublings": properties.get("max_doublings", 3),
            },
            "attempt_deadline": f"{properties.get('attempt_deadline', 300)}s",
        }
    )

    logger.debug(
        format_event(
            "scheduler_job_planned",
            job_name=job_name,
            service_name=service_name,
            actions_count=len(actions),
        )
    )

    return actions


def _plan_event_driven_execution(
    trigger: Mapping[str, Any],
    contract: Mapping[str, Any],
    project: str,
    region: str,
    logger: logging.Logger,
) -> List[Dict[str, Any]]:
    """
    Plan event-driven execution via Pub/Sub and Cloud Run.
    """
    actions = []
    properties = trigger.get("properties", {})

    contract_id = contract.get("id", "unknown")
    topic_name = properties.get("topic_name", f"fluid-{contract_id}-events")
    subscription_name = properties.get("subscription_name", f"fluid-{contract_id}-sub")
    service_name = normalize_job_name(properties.get("service_name", f"fluid-{contract_id}"))

    # 1. Ensure Pub/Sub topic exists
    actions.append(
        {
            "op": "ps.ensure_topic",
            "id": f"topic_{topic_name}",
            "project": project,
            "topic": topic_name,
            "labels": {
                "fluid-contract-id": contract_id.replace(".", "-"),
                "managed-by": "fluid-forge",
            },
            "message_retention_duration": properties.get("message_retention", "604800s"),  # 7 days
        }
    )

    # 2. Ensure Cloud Run service exists
    actions.append(
        {
            "op": "run.ensure_service",
            "id": f"run_service_{service_name}",
            "project": project,
            "region": region,
            "service_name": service_name,
            "image": properties.get("image", "gcr.io/fluid-forge/runner:latest"),
            "cpu": properties.get("cpu", "1"),
            "memory": properties.get("memory", "512Mi"),
            "concurrency": properties.get("concurrency", 1000),  # Higher for event processing
            "timeout": properties.get("timeout", 300),
            "env_vars": {
                "FLUID_PROJECT": project,
                "FLUID_REGION": region,
                "FLUID_CONTRACT_ID": contract_id,
                **properties.get("env_vars", {}),
            },
            "labels": {
                "fluid-contract-id": contract_id.replace(".", "-"),
                "execution-type": "event-driven",
                "managed-by": "fluid-forge",
            },
            "service_account": properties.get("service_account"),
            "max_instances": properties.get("max_instances", 100),
            "min_instances": properties.get("min_instances", 0),
        }
    )

    # 3. Create Pub/Sub push subscription to Cloud Run
    actions.append(
        {
            "op": "ps.ensure_subscription",
            "id": f"subscription_{subscription_name}",
            "project": project,
            "topic": topic_name,
            "subscription": subscription_name,
            "push_config": {
                "push_endpoint": f"https://{service_name}-{_hash_project_region(project, region)}-{region}.a.run.app/pubsub",
                "oidc_token": {
                    "service_account_email": properties.get("pubsub_service_account"),
                    "audience": f"https://{service_name}-{_hash_project_region(project, region)}-{region}.a.run.app",
                },
                "attributes": {
                    "x-goog-version": "v1",
                    "content-type": "application/json",
                },
            },
            "ack_deadline_seconds": properties.get("ack_deadline", 60),
            "retain_acked_messages": properties.get("retain_acked_messages", False),
            "message_retention_duration": properties.get("subscription_retention", "604800s"),
            "dead_letter_policy": (
                {
                    "dead_letter_topic": properties.get("dead_letter_topic"),
                    "max_delivery_attempts": properties.get("max_delivery_attempts", 5),
                }
                if properties.get("dead_letter_topic")
                else None
            ),
            "filter": properties.get("message_filter"),
            "labels": {
                "fluid-contract-id": contract_id.replace(".", "-"),
                "managed-by": "fluid-forge",
            },
        }
    )

    logger.debug(
        format_event(
            "event_driven_execution_planned",
            topic_name=topic_name,
            subscription_name=subscription_name,
            service_name=service_name,
            actions_count=len(actions),
        )
    )

    return actions


def _plan_manual_execution(
    trigger: Mapping[str, Any],
    contract: Mapping[str, Any],
    project: str,
    region: str,
    logger: logging.Logger,
) -> List[Dict[str, Any]]:
    """
    Plan manual execution setup.

    Creates Cloud Run service that can be invoked manually or via API.
    """
    actions = []
    properties = trigger.get("properties", {})

    contract_id = contract.get("id", "unknown")
    service_name = normalize_job_name(properties.get("service_name", f"fluid-{contract_id}"))

    # Create Cloud Run service for manual execution
    actions.append(
        {
            "op": "run.ensure_service",
            "id": f"run_service_{service_name}",
            "project": project,
            "region": region,
            "service_name": service_name,
            "image": properties.get("image", "gcr.io/fluid-forge/runner:latest"),
            "cpu": properties.get("cpu", "2"),
            "memory": properties.get("memory", "1Gi"),
            "concurrency": properties.get("concurrency", 1),
            "timeout": properties.get("timeout", 600),  # 10 minutes for manual jobs
            "env_vars": {
                "FLUID_PROJECT": project,
                "FLUID_REGION": region,
                "FLUID_CONTRACT_ID": contract_id,
                **properties.get("env_vars", {}),
            },
            "labels": {
                "fluid-contract-id": contract_id.replace(".", "-"),
                "execution-type": "manual",
                "managed-by": "fluid-forge",
            },
            "service_account": properties.get("service_account"),
            "max_instances": properties.get("max_instances", 10),
            "min_instances": properties.get("min_instances", 0),
            "ingress": "all",  # Allow external invocation
            "allow_unauthenticated": properties.get("allow_unauthenticated", False),
        }
    )

    logger.debug(
        format_event(
            "manual_execution_planned", service_name=service_name, actions_count=len(actions)
        )
    )

    return actions


def _hash_project_region(project: str, region: str) -> str:
    """
    Generate deterministic hash for project-region combination.

    Used to create consistent Cloud Run service URLs.
    """
    import hashlib

    combined = f"{project}-{region}"
    return hashlib.md5(combined.encode()).hexdigest()[:8]


def _encode_job_body(data: Dict[str, Any]) -> str:
    """
    Encode job data as base64 JSON for Cloud Scheduler.
    """
    import base64
    import json

    json_str = json.dumps(data)
    return base64.b64encode(json_str.encode()).decode()


def validate_trigger_config(trigger: Mapping[str, Any], contract: Mapping[str, Any]) -> List[str]:
    """
    Validate trigger configuration.

    Args:
        trigger: Trigger configuration
        contract: Full FLUID contract

    Returns:
        List of validation error messages (empty if valid)
    """
    errors = []

    trigger_type = trigger.get("type")
    if not trigger_type:
        errors.append("Trigger type is required")
        return errors

    if trigger_type == "schedule":
        if not trigger.get("cron"):
            errors.append("Schedule trigger requires 'cron' expression")

        # Validate cron expression format (basic check)
        cron = trigger.get("cron", "")
        if cron and len(cron.split()) not in [5, 6]:
            errors.append("Cron expression should have 5 or 6 fields")

    elif trigger_type == "event":
        properties = trigger.get("properties", {})
        if not properties.get("topic_name"):
            errors.append("Event trigger should specify 'topic_name' in properties")

    elif trigger_type == "manual":
        # Manual triggers have minimal requirements
        pass

    else:
        errors.append(f"Unknown trigger type: {trigger_type}")

    return errors
