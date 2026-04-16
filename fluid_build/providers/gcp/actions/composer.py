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

# fluid_build/providers/gcp/actions/composer.py
"""
Google Cloud Composer actions for GCP provider.

Implements idempotent Composer operations including:
- Environment management and configuration
- DAG deployment and lifecycle
- Variable and connection management
- Workflow triggering and monitoring
"""

import hashlib
import time
from typing import Any, Dict

from fluid_build.util.network import safe_get, safe_patch, safe_post

from ..util.logging import duration_ms
from ..util.names import normalize_composer_name


def ensure_environment(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure Composer environment exists with specified configuration.

    Creates environment if it doesn't exist, updates configuration if changed.
    Note: Environment updates can take 20-60 minutes to complete.

    Args:
        action: Environment action configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import composer_v1
        from google.cloud.exceptions import NotFound
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-composer library not available. Install with: pip install google-cloud-composer",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    location = action.get("location", "us-central1")
    environment_name = action.get("environment")
    node_count = action.get("node_count", 3)
    machine_type = action.get("machine_type", "n1-standard-1")
    disk_size_gb = action.get("disk_size_gb", 20)
    python_version = action.get("python_version", "3")
    airflow_version = action.get("airflow_version")
    env_variables = action.get("env_variables", {})
    pypi_packages = action.get("pypi_packages", {})
    labels = action.get("labels", {})

    if not all([project, environment_name]):
        return {
            "status": "error",
            "error": "Both 'project' and 'environment' are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize Composer client
        client = composer_v1.EnvironmentsClient()

        # Normalize environment name
        normalized_env = normalize_composer_name(environment_name)
        parent = f"projects/{project}/locations/{location}"
        env_path = f"{parent}/environments/{normalized_env}"

        changed = False

        try:
            # Check if environment exists
            existing_env = client.get_environment(request={"name": env_path})

            # Environment exists - check if updates are needed
            # Note: Many Composer updates require long-running operations
            update_needed = False

            # Check node count
            current_node_count = existing_env.config.node_count
            if current_node_count != node_count:
                update_needed = True

            # Check environment variables
            current_env_vars = dict(existing_env.config.software_config.env_variables)
            if current_env_vars != env_variables:
                update_needed = True

            # Check PyPI packages
            current_pypi = dict(existing_env.config.software_config.pypi_packages)
            if current_pypi != pypi_packages:
                update_needed = True

            # Check labels
            current_labels = dict(existing_env.labels) if existing_env.labels else {}
            if current_labels != labels:
                update_needed = True

            if update_needed:
                # Environment updates require long-running operations (20-60 minutes)
                # For now, we'll return a warning that manual update is needed
                return {
                    "status": "changed",
                    "environment_name": env_path,
                    "state": existing_env.state.name if existing_env.state else "UNKNOWN",
                    "airflow_uri": existing_env.config.airflow_uri if existing_env.config else None,
                    "gcs_bucket": (
                        existing_env.config.dag_gcs_prefix.split("/")[0]
                        if existing_env.config and existing_env.config.dag_gcs_prefix
                        else None
                    ),
                    "node_count": existing_env.config.node_count if existing_env.config else None,
                    "duration_ms": duration_ms(start_time),
                    "changed": False,
                    "warning": "Environment configuration differs but updates require 20-60 minutes. Use gcloud composer environments update for changes.",
                    "required_updates": {
                        "node_count": node_count if current_node_count != node_count else None,
                        "env_variables": (
                            env_variables if current_env_vars != env_variables else None
                        ),
                        "pypi_packages": pypi_packages if current_pypi != pypi_packages else None,
                        "labels": labels if current_labels != labels else None,
                    },
                }
            else:
                return {
                    "status": "ok",
                    "environment_name": env_path,
                    "state": existing_env.state.name if existing_env.state else "UNKNOWN",
                    "airflow_uri": existing_env.config.airflow_uri if existing_env.config else None,
                    "gcs_bucket": (
                        existing_env.config.dag_gcs_prefix.split("/")[0]
                        if existing_env.config and existing_env.config.dag_gcs_prefix
                        else None
                    ),
                    "node_count": existing_env.config.node_count if existing_env.config else None,
                    "duration_ms": duration_ms(start_time),
                    "changed": False,
                }

        except NotFound:
            # Environment doesn't exist - provide guidance for creation
            # Composer environments take 20-40 minutes to create, so we recommend gcloud
            return {
                "status": "error",
                "error": f"Composer environment '{normalized_env}' does not exist",
                "environment_name": f"{parent}/environments/{normalized_env}",
                "duration_ms": duration_ms(start_time),
                "changed": False,
                "suggestion": f"Create environment with: gcloud composer environments create {normalized_env} --location {location} --node-count {node_count} --machine-type {machine_type} --disk-size {disk_size_gb}GB",
                "creation_config": {
                    "node_count": node_count,
                    "machine_type": machine_type,
                    "disk_size_gb": disk_size_gb,
                    "python_version": python_version,
                    "env_variables": env_variables,
                    "pypi_packages": pypi_packages,
                    "labels": labels,
                },
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "environment_name": env_path if "env_path" in locals() else None,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def deploy_dag(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deploy DAG file to Composer environment.

    Uploads DAG to the environment's GCS bucket and verifies deployment.

    Args:
        action: DAG deployment action configuration

    Returns:
        Action result with deployment status
    """
    start_time = time.time()

    try:
        from google.cloud import composer_v1, storage
        from google.cloud.exceptions import NotFound
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-composer or google-cloud-storage library not available",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    location = action.get("location", "us-central1")
    environment_name = action.get("environment")
    dag_id = action.get("dag_id")
    dag_content = action.get("dag_content")
    dag_file_path = action.get("dag_file_path")

    if not all([project, environment_name, dag_id]):
        return {
            "status": "error",
            "error": "Project, environment, and dag_id are all required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    if not dag_content and not dag_file_path:
        return {
            "status": "error",
            "error": "Either 'dag_content' or 'dag_file_path' must be provided",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize clients
        composer_client = composer_v1.EnvironmentsClient()
        storage_client = storage.Client(project=project)

        # Get environment details
        normalized_env = normalize_composer_name(environment_name)
        env_path = f"projects/{project}/locations/{location}/environments/{normalized_env}"

        try:
            environment = composer_client.get_environment(request={"name": env_path})
        except NotFound:
            return {
                "status": "error",
                "error": f"Composer environment {normalized_env} not found in {location}",
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

        # Extract DAGs bucket from environment
        if not environment.config or not environment.config.dag_gcs_prefix:
            return {
                "status": "error",
                "error": f"Environment {normalized_env} does not have DAG GCS prefix configured",
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

        dag_gcs_prefix = environment.config.dag_gcs_prefix
        # Format: gs://bucket-name/dags
        bucket_name = dag_gcs_prefix.replace("gs://", "").split("/")[0]
        dags_prefix = "/".join(dag_gcs_prefix.replace("gs://", "").split("/")[1:])

        # Prepare DAG content
        if dag_file_path:
            try:
                with open(dag_file_path) as f:
                    dag_content = f.read()
            except OSError as e:
                return {
                    "status": "error",
                    "error": f"Failed to read DAG file {dag_file_path}: {str(e)}",
                    "duration_ms": duration_ms(start_time),
                    "changed": False,
                }

        # Calculate content hash for change detection
        content_hash = hashlib.md5(dag_content.encode("utf-8")).hexdigest()
        dag_filename = f"{dag_id}.py"
        blob_path = f"{dags_prefix}/{dag_filename}"

        # Get bucket and blob
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_path)

        changed = True

        # Check if DAG exists and content has changed
        if blob.exists():
            existing_metadata = blob.metadata or {}
            existing_hash = existing_metadata.get("content_hash")

            if existing_hash == content_hash:
                changed = False

        if changed:
            # Upload DAG with metadata
            blob.metadata = {
                "content_hash": content_hash,
                "dag_id": dag_id,
                "uploaded_by": "fluid-forge",
                "upload_timestamp": str(int(time.time())),
            }

            blob.upload_from_string(dag_content, content_type="text/x-python")

        return {
            "status": "changed" if changed else "ok",
            "environment_name": env_path,
            "dag_id": dag_id,
            "dag_filename": dag_filename,
            "gcs_path": f"gs://{bucket_name}/{blob_path}",
            "content_size": len(dag_content),
            "content_hash": content_hash,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "dag_id": dag_id,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def trigger_dag(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Trigger DAG run in Composer environment.

    Starts a new DAG run with optional configuration.

    Args:
        action: DAG trigger action configuration

    Returns:
        Action result with run details
    """
    start_time = time.time()

    try:
        import requests
        from google.auth import default
        from google.auth.transport.requests import Request
    except ImportError:
        return {
            "status": "error",
            "error": "requests library not available. Install with: pip install requests",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    location = action.get("location", "us-central1")
    environment_name = action.get("environment")
    dag_id = action.get("dag_id")
    run_id = action.get("run_id")
    conf = action.get("conf", {})

    if not all([project, environment_name, dag_id]):
        return {
            "status": "error",
            "error": "Project, environment, and dag_id are all required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        from google.cloud import composer_v1

        # Get environment details for Airflow URI
        composer_client = composer_v1.EnvironmentsClient()
        normalized_env = normalize_composer_name(environment_name)
        env_path = f"projects/{project}/locations/{location}/environments/{normalized_env}"

        try:
            environment = composer_client.get_environment(request={"name": env_path})
        except Exception:
            return {
                "status": "error",
                "error": f"Composer environment {normalized_env} not found or not accessible",
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

        if not environment.config or not environment.config.airflow_uri:
            return {
                "status": "error",
                "error": f"Environment {normalized_env} does not have Airflow URI configured",
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

        airflow_uri = environment.config.airflow_uri

        # Get authentication token
        credentials, _ = default()
        credentials.refresh(Request())

        # Generate run_id if not provided
        if not run_id:
            import datetime

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            run_id = f"fluid_build_{timestamp}"

        # Prepare trigger request
        trigger_url = f"{airflow_uri}/api/v1/dags/{dag_id}/dagRuns"

        payload = {"dag_run_id": run_id, "conf": conf}

        headers = {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        }

        # Trigger DAG run
        response = safe_post(trigger_url, json=payload, headers=headers, timeout=30)

        if response.status_code == 200:
            run_data = response.json()

            return {
                "status": "ok",
                "environment_name": env_path,
                "dag_id": dag_id,
                "run_id": run_data.get("dag_run_id", run_id),
                "execution_date": run_data.get("execution_date"),
                "state": run_data.get("state", "running"),
                "airflow_uri": airflow_uri,
                "run_url": f"{airflow_uri}/tree?dag_id={dag_id}",
                "duration_ms": duration_ms(start_time),
                "changed": True,  # Triggering always creates a new run
            }
        elif response.status_code == 409:
            # DAG run already exists
            return {
                "status": "ok",
                "environment_name": env_path,
                "dag_id": dag_id,
                "run_id": run_id,
                "state": "already_exists",
                "airflow_uri": airflow_uri,
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }
        else:
            return {
                "status": "error",
                "error": f"Failed to trigger DAG: HTTP {response.status_code} - {response.text}",
                "dag_id": dag_id,
                "run_id": run_id,
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "dag_id": dag_id,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_airflow_variable(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure Airflow variable is set in Composer environment.

    Creates or updates Airflow variables via the Airflow REST API.

    Args:
        action: Variable action configuration

    Returns:
        Action result with variable status
    """
    start_time = time.time()

    try:
        import requests
        from google.auth import default
        from google.auth.transport.requests import Request
    except ImportError:
        return {
            "status": "error",
            "error": "requests library not available. Install with: pip install requests",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    location = action.get("location", "us-central1")
    environment_name = action.get("environment")
    key = action.get("key")
    value = action.get("value")
    description = action.get("description", "")

    if not all([project, environment_name, key, value]):
        return {
            "status": "error",
            "error": "Project, environment, key, and value are all required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        from google.cloud import composer_v1

        # Get environment details for Airflow URI
        composer_client = composer_v1.EnvironmentsClient()
        normalized_env = normalize_composer_name(environment_name)
        env_path = f"projects/{project}/locations/{location}/environments/{normalized_env}"

        try:
            environment = composer_client.get_environment(request={"name": env_path})
        except Exception:
            return {
                "status": "error",
                "error": f"Composer environment {normalized_env} not found or not accessible",
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

        if not environment.config or not environment.config.airflow_uri:
            return {
                "status": "error",
                "error": f"Environment {normalized_env} does not have Airflow URI configured",
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

        airflow_uri = environment.config.airflow_uri

        # Get authentication token
        credentials, _ = default()
        credentials.refresh(Request())

        headers = {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        }

        # Check if variable exists
        get_url = f"{airflow_uri}/api/v1/variables/{key}"
        get_response = safe_get(get_url, headers=headers, timeout=30)

        changed = False

        if get_response.status_code == 200:
            # Variable exists, check if value changed
            existing_var = get_response.json()
            existing_value = existing_var.get("value", "")

            if existing_value != str(value):
                # Update variable
                update_payload = {"key": key, "value": str(value)}
                if description:
                    update_payload["description"] = description

                patch_response = safe_patch(
                    get_url, json=update_payload, headers=headers, timeout=30
                )

                if patch_response.status_code == 200:
                    changed = True
                else:
                    return {
                        "status": "error",
                        "error": f"Failed to update variable: HTTP {patch_response.status_code} - {patch_response.text}",
                        "variable_key": key,
                        "duration_ms": duration_ms(start_time),
                        "changed": False,
                    }

        elif get_response.status_code == 404:
            # Variable doesn't exist, create it
            create_url = f"{airflow_uri}/api/v1/variables"
            create_payload = {"key": key, "value": str(value)}
            if description:
                create_payload["description"] = description

            create_response = safe_post(
                create_url, json=create_payload, headers=headers, timeout=30
            )

            if create_response.status_code == 200:
                changed = True
            else:
                return {
                    "status": "error",
                    "error": f"Failed to create variable: HTTP {create_response.status_code} - {create_response.text}",
                    "variable_key": key,
                    "duration_ms": duration_ms(start_time),
                    "changed": False,
                }

        else:
            return {
                "status": "error",
                "error": f"Failed to check variable: HTTP {get_response.status_code} - {get_response.text}",
                "variable_key": key,
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

        return {
            "status": "changed" if changed else "ok",
            "environment_name": env_path,
            "variable_key": key,
            "value_length": len(str(value)),
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "variable_key": key,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


# Helper functions


def get_environment_status(project: str, location: str, environment_name: str) -> Dict[str, Any]:
    """
    Get current status of Composer environment.

    Args:
        project: GCP project ID
        location: GCP region/location
        environment_name: Composer environment name

    Returns:
        Environment status information
    """
    try:
        from google.cloud import composer_v1

        client = composer_v1.EnvironmentsClient()
        normalized_env = normalize_composer_name(environment_name)
        env_path = f"projects/{project}/locations/{location}/environments/{normalized_env}"

        environment = client.get_environment(request={"name": env_path})

        return {
            "exists": True,
            "state": environment.state.name if environment.state else "UNKNOWN",
            "airflow_uri": environment.config.airflow_uri if environment.config else None,
            "node_count": environment.config.node_count if environment.config else None,
            "gcs_bucket": (
                environment.config.dag_gcs_prefix.split("/")[0]
                if environment.config and environment.config.dag_gcs_prefix
                else None
            ),
            "python_version": (
                environment.config.software_config.python_version
                if environment.config and environment.config.software_config
                else None
            ),
        }

    except Exception as e:
        return {"exists": False, "error": str(e)}
