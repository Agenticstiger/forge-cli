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

# fluid_build/providers/gcp/util/auth.py
"""
GCP authentication utilities and diagnostics.

Handles Application Default Credentials, service account keys,
Workload Identity Federation, and authentication reporting.
"""

import json
import os
from typing import Any, Dict, Optional


def get_auth_report(project: Optional[str] = None, region: Optional[str] = None) -> Dict[str, Any]:
    """
    Generate comprehensive authentication and environment report.

    Useful for diagnostics and troubleshooting auth issues.

    Args:
        project: Optional project override
        region: Optional region override

    Returns:
        Authentication status report
    """
    report = {
        "provider": "gcp",
        "timestamp": _utc_timestamp(),
    }

    try:
        # Check for service account key file
        sa_key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if sa_key_path:
            report["auth_method"] = "service_account_key"
            report["service_account_key_path"] = sa_key_path
            report["key_file_exists"] = os.path.exists(sa_key_path)

            # Try to read key info (without exposing private key)
            if report["key_file_exists"]:
                try:
                    with open(sa_key_path) as f:
                        key_data = json.load(f)
                    report["service_account_email"] = key_data.get("client_email")
                    report["project_id_from_key"] = key_data.get("project_id")
                    report["key_type"] = key_data.get("type")
                except Exception as e:
                    report["key_read_error"] = str(e)
        else:
            report["auth_method"] = "adc"

        # Try to get credentials and project info
        try:
            from google.auth import default
            from google.auth.transport.requests import Request

            credentials, adc_project = default()

            # Refresh credentials to check validity
            if hasattr(credentials, "refresh"):
                try:
                    credentials.refresh(Request())
                    report["credentials_valid"] = True
                except Exception as e:
                    report["credentials_valid"] = False
                    report["credentials_error"] = str(e)

            report["adc_project"] = adc_project
            report["credentials_type"] = type(credentials).__name__

            # Check for specific credential types
            if hasattr(credentials, "service_account_email"):
                report["service_account_email"] = credentials.service_account_email

            if hasattr(credentials, "token"):
                report["has_token"] = bool(credentials.token)

        except ImportError:
            report["error"] = "google-auth library not available"
            report["install_command"] = "pip install google-auth"

        except Exception as e:
            report["credentials_error"] = str(e)
            report["credentials_valid"] = False

        # Environment variables
        report["environment"] = {
            "GOOGLE_APPLICATION_CREDENTIALS": os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
            "GOOGLE_CLOUD_PROJECT": os.environ.get("GOOGLE_CLOUD_PROJECT"),
            "FLUID_PROJECT": os.environ.get("FLUID_PROJECT"),
            "FLUID_REGION": os.environ.get("FLUID_REGION"),
            "GCLOUD_PROJECT": os.environ.get("GCLOUD_PROJECT"),
        }

        # Resolved configuration
        from .config import resolve_project_and_region

        try:
            resolved_project, resolved_region = resolve_project_and_region(project, region)
            report["resolved_project"] = resolved_project
            report["resolved_region"] = resolved_region
        except Exception as e:
            report["config_error"] = str(e)

        # Check gcloud CLI availability
        report["gcloud_available"] = _check_gcloud_available()
        if report["gcloud_available"]:
            report["gcloud_info"] = _get_gcloud_info()

        # Workload Identity Federation detection
        if _is_workload_identity():
            report["workload_identity"] = True
            report["metadata_service_available"] = _check_metadata_service()

        report["status"] = "ok"

    except Exception as e:
        report["status"] = "error"
        report["error"] = str(e)

    return report


def _utc_timestamp() -> str:
    """Generate UTC timestamp string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _check_gcloud_available() -> bool:
    """Check if gcloud CLI is available."""
    import subprocess

    try:
        result = subprocess.run(["gcloud", "version"], capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _get_gcloud_info() -> Dict[str, Any]:
    """Get gcloud CLI configuration information."""
    import subprocess

    info = {}

    try:
        # Get active account
        result = subprocess.run(
            ["gcloud", "config", "get-value", "account"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            info["active_account"] = result.stdout.strip()

        # Get active project
        result = subprocess.run(
            ["gcloud", "config", "get-value", "project"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            info["active_project"] = result.stdout.strip()

        # Get configurations
        result = subprocess.run(
            ["gcloud", "config", "configurations", "list", "--format=json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            configs = json.loads(result.stdout)
            info["configurations"] = [
                {
                    "name": config.get("name"),
                    "is_active": config.get("is_active"),
                    "account": config.get("properties", {}).get("core", {}).get("account"),
                    "project": config.get("properties", {}).get("core", {}).get("project"),
                }
                for config in configs
            ]

    except (subprocess.SubprocessError, json.JSONDecodeError, subprocess.TimeoutExpired):
        pass

    return info


def _is_workload_identity() -> bool:
    """Check if running with Workload Identity Federation."""
    return (
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_NAME") is not None
        or os.environ.get("GOOG_SERVICE_ACCOUNT_NAME") is not None
        or os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")
    )


def _check_metadata_service() -> bool:
    """Check if GCP metadata service is available."""
    try:
        import urllib.request

        request = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/project/project-id",
            headers={"Metadata-Flavor": "Google"},
        )

        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status == 200

    except Exception:
        return False


def validate_credentials(project: str) -> Dict[str, Any]:
    """
    Validate credentials against a specific project.

    Args:
        project: GCP project ID to test access against

    Returns:
        Validation result with permissions info
    """
    result = {
        "project": project,
        "valid": False,
        "permissions": {},
    }

    try:
        from google.auth import default
        from google.auth.transport.requests import Request

        credentials, _ = default()

        # Refresh credentials
        if hasattr(credentials, "refresh"):
            credentials.refresh(Request())

        # Test basic project access
        result["permissions"]["project.get"] = _test_project_access(project, credentials)

        # Test service-specific permissions
        result["permissions"]["bigquery"] = _test_bigquery_access(project, credentials)
        result["permissions"]["storage"] = _test_storage_access(project, credentials)
        result["permissions"]["pubsub"] = _test_pubsub_access(project, credentials)

        result["valid"] = any(result["permissions"].values())

    except Exception as e:
        result["error"] = str(e)

    return result


def _test_project_access(project: str, credentials) -> bool:
    """Test basic project access."""
    try:
        from googleapiclient.discovery import build

        service = build("cloudresourcemanager", "v1", credentials=credentials)
        service.projects().get(projectId=project).execute()
        return True

    except Exception:
        return False


def _test_bigquery_access(project: str, credentials) -> bool:
    """Test BigQuery access."""
    try:
        from google.cloud import bigquery

        client = bigquery.Client(project=project, credentials=credentials)
        list(client.list_datasets(max_results=1))
        return True

    except Exception:
        return False


def _test_storage_access(project: str, credentials) -> bool:
    """Test Cloud Storage access."""
    try:
        from google.cloud import storage

        client = storage.Client(project=project, credentials=credentials)
        list(client.list_buckets(max_results=1))
        return True

    except Exception:
        return False


def _test_pubsub_access(project: str, credentials) -> bool:
    """Test Pub/Sub access."""
    try:
        from google.cloud import pubsub_v1

        publisher = pubsub_v1.PublisherClient(credentials=credentials)
        publisher.list_topics(request={"project": f"projects/{project}"})
        return True

    except Exception:
        return False
