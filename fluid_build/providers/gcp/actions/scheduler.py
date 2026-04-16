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

# fluid_build/providers/gcp/actions/scheduler.py
"""
Cloud Scheduler actions for GCP provider.

Implements idempotent Cloud Scheduler operations including:
- Job creation and updates
- Schedule configuration
- Target configuration (HTTP, Pub/Sub, App Engine)
"""

import time
from typing import Any, Dict

from ..util.logging import duration_ms


def ensure_job(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure Cloud Scheduler job exists with specified configuration.

    Args:
        action: Scheduler job configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import scheduler_v1
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-scheduler library not available. Install with: pip install google-cloud-scheduler",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    region = action.get("region", "us-central1")
    job_name = action.get("job_name")
    schedule = action.get("schedule")  # Cron format
    target = action.get("target", {})
    timezone = action.get("timezone", "UTC")

    if not all([project, job_name, schedule]):
        return {
            "status": "error",
            "error": "Required fields: project, job_name, schedule",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        client = scheduler_v1.CloudSchedulerClient()
        parent = f"projects/{project}/locations/{region}"
        job_path = f"{parent}/jobs/{job_name}"

        changed = False

        try:
            # Check if job exists
            existing_job = client.get_job(name=job_path)

            # Compare configuration and update if needed
            update_needed = False

            if existing_job.schedule != schedule:
                update_needed = True

            if existing_job.time_zone != timezone:
                update_needed = True

            if update_needed:
                # Update job
                job = _build_scheduler_job(job_path, schedule, target, timezone)
                client.update_job(job=job)
                changed = True
                action_taken = "updated"
            else:
                action_taken = "exists"

        except Exception:
            # Job doesn't exist, create it
            job = _build_scheduler_job(job_path, schedule, target, timezone)

            client.create_job(
                parent=parent,
                job=job,
            )

            changed = True
            action_taken = "created"

        return {
            "status": "changed" if changed else "ok",
            "op": "scheduler.ensure_job",
            "job": job_name,
            "schedule": schedule,
            "action": action_taken,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to ensure Cloud Scheduler job: {str(e)}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def pause_job(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pause a Cloud Scheduler job.

    Args:
        action: Job configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import scheduler_v1
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-scheduler library not available",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    region = action.get("region", "us-central1")
    job_name = action.get("job_name")

    if not all([project, job_name]):
        return {
            "status": "error",
            "error": "Required fields: project, job_name",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        client = scheduler_v1.CloudSchedulerClient()
        parent = f"projects/{project}/locations/{region}"
        job_path = f"{parent}/jobs/{job_name}"

        client.pause_job(name=job_path)

        return {
            "status": "changed",
            "op": "scheduler.pause_job",
            "job": job_name,
            "action": "paused",
            "duration_ms": duration_ms(start_time),
            "changed": True,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to pause scheduler job: {str(e)}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def resume_job(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Resume a paused Cloud Scheduler job.

    Args:
        action: Job configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import scheduler_v1
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-scheduler library not available",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    region = action.get("region", "us-central1")
    job_name = action.get("job_name")

    if not all([project, job_name]):
        return {
            "status": "error",
            "error": "Required fields: project, job_name",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        client = scheduler_v1.CloudSchedulerClient()
        parent = f"projects/{project}/locations/{region}"
        job_path = f"{parent}/jobs/{job_name}"

        client.resume_job(name=job_path)

        return {
            "status": "changed",
            "op": "scheduler.resume_job",
            "job": job_name,
            "action": "resumed",
            "duration_ms": duration_ms(start_time),
            "changed": True,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to resume scheduler job: {str(e)}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def _build_scheduler_job(name: str, schedule: str, target: Dict[str, Any], timezone: str):
    """Build Cloud Scheduler job object."""
    from google.cloud import scheduler_v1

    job = scheduler_v1.Job(
        name=name,
        schedule=schedule,
        time_zone=timezone,
    )

    # Configure target based on type
    target_type = target.get("type", "http")

    if target_type == "http":
        job.http_target = scheduler_v1.HttpTarget(
            uri=target.get("uri"),
            http_method=target.get("method", "POST"),
        )
    elif target_type == "pubsub":
        job.pubsub_target = scheduler_v1.PubsubTarget(
            topic_name=target.get("topic"),
            data=target.get("data", b""),
        )
    elif target_type == "app_engine":
        job.app_engine_http_target = scheduler_v1.AppEngineHttpTarget(
            relative_uri=target.get("relative_uri", "/"),
            http_method=target.get("method", "POST"),
        )

    return job
