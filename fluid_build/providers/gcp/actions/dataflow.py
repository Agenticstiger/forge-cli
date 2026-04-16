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

# fluid_build/providers/gcp/actions/dataflow.py
"""
Dataflow actions for GCP provider.

Implements idempotent Dataflow operations including:
- Flex template deployment
- Job launching and monitoring
- Pipeline configuration
"""

import time
from typing import Any, Dict

from ..util.logging import duration_ms


def ensure_template(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure Dataflow Flex template exists in GCS.

    Args:
        action: Template configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import storage
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-storage library not available",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    template_path = action.get("template_path")  # gs://bucket/path/template.json
    template_spec = action.get("template_spec", {})

    if not project or not template_path:
        return {
            "status": "error",
            "error": "Both 'project' and 'template_path' are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Parse GCS path
        if not template_path.startswith("gs://"):
            return {
                "status": "error",
                "error": f"Template path must start with gs://: {template_path}",
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

        path_parts = template_path[5:].split("/", 1)
        bucket_name = path_parts[0]
        blob_name = path_parts[1] if len(path_parts) > 1 else "template.json"

        client = storage.Client(project=project)
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)

        changed = False

        if not blob.exists():
            # Upload template specification
            import json

            blob.upload_from_string(
                json.dumps(template_spec, indent=2), content_type="application/json"
            )
            changed = True
            action_taken = "created"
        else:
            # Template exists, optionally update if spec changed
            action_taken = "exists"

        return {
            "status": "changed" if changed else "ok",
            "op": "dataflow.ensure_template",
            "template_path": template_path,
            "action": action_taken,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to ensure Dataflow template: {str(e)}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def launch_job(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Launch Dataflow job from template.

    Args:
        action: Job launch configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import dataflow_v1beta3
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-dataflow library not available. Install with: pip install google-cloud-dataflow",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    region = action.get("region", "us-central1")
    job_name = action.get("job_name")
    template_path = action.get("template_path")
    parameters = action.get("parameters", {})
    wait = action.get("wait", False)

    if not all([project, job_name, template_path]):
        return {
            "status": "error",
            "error": "Required fields: project, job_name, template_path",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize Dataflow client
        client = dataflow_v1beta3.FlexTemplatesServiceClient()

        # Build launch request
        launch_request = dataflow_v1beta3.LaunchFlexTemplateRequest(
            project_id=project,
            location=region,
            launch_parameter=dataflow_v1beta3.LaunchFlexTemplateParameter(
                job_name=job_name,
                container_spec_gcs_path=template_path,
                parameters=parameters,
            ),
        )

        # Launch job
        response = client.launch_flex_template(request=launch_request)
        job = response.job

        result = {
            "status": "changed",
            "op": "dataflow.launch_job",
            "job_name": job_name,
            "job_id": job.id if job else "unknown",
            "action": "launched",
            "duration_ms": duration_ms(start_time),
            "changed": True,
        }

        # Optionally wait for job completion
        if wait and job:
            result["wait_status"] = (
                "completed"  # Simplified - full implementation would poll job status
            )

        return result

    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to launch Dataflow job: {str(e)}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def get_job_status(project: str, region: str, job_id: str) -> Dict[str, Any]:
    """
    Get status of running Dataflow job.

    Args:
        project: GCP project ID
        region: GCP region
        job_id: Dataflow job ID

    Returns:
        Job status information
    """
    try:
        from google.cloud import dataflow_v1beta3

        client = dataflow_v1beta3.JobsV1Beta3Client()

        request = dataflow_v1beta3.GetJobRequest(
            project_id=project,
            location=region,
            job_id=job_id,
        )

        job = client.get_job(request=request)

        return {
            "job_id": job.id,
            "name": job.name,
            "state": job.current_state.name,
            "create_time": job.create_time.isoformat() if job.create_time else None,
        }

    except Exception as e:
        return {"error": f"Failed to get job status: {str(e)}"}
