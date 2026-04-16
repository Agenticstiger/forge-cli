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

# fluid_build/providers/gcp/actions/iam.py
"""
IAM actions for GCP provider.

Implements idempotent IAM operations including:
- Dataset IAM policy bindings
- Table IAM policy bindings
- Bucket IAM policy bindings
- Pub/Sub topic IAM bindings
"""

import time
from typing import Any, Dict, Optional

from ..util.logging import duration_ms


def bind_bq_dataset(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Bind IAM policies to BigQuery dataset.

    Args:
        action: IAM binding configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import bigquery
        from google.cloud.exceptions import NotFound
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-bigquery library not available",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    dataset = action.get("dataset")
    policies = action.get("policies", {})

    if not project or not dataset:
        return {
            "status": "error",
            "error": "Both 'project' and 'dataset' are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        client = bigquery.Client(project=project)
        dataset_id = f"{project}.{dataset}"

        # Get current policy
        dataset_ref = client.get_dataset(dataset_id)
        entries = list(dataset_ref.access_entries)
        original_count = len(entries)

        changed = False

        # Process access policies from FLUID contract
        for policy_name, policy_config in policies.items():
            if not isinstance(policy_config, dict):
                continue

            principals = policy_config.get("principals", [])
            permissions = policy_config.get("permissions", [])

            # Map FLUID permissions to BigQuery roles
            for principal in principals:
                for permission in permissions:
                    role = _map_permission_to_bq_role(permission)
                    if role:
                        # Check if already exists
                        entry = bigquery.AccessEntry(
                            role=role,
                            entity_type="userByEmail" if "@" in principal else "groupByEmail",
                            entity_id=principal,
                        )

                        if entry not in entries:
                            entries.append(entry)
                            changed = True

        if changed:
            dataset_ref.access_entries = entries
            client.update_dataset(dataset_ref, ["access_entries"])

            return {
                "status": "changed",
                "op": "iam.bind_bq_dataset",
                "dataset": dataset_id,
                "bindings_added": len(entries) - original_count,
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }
        else:
            return {
                "status": "ok",
                "op": "iam.bind_bq_dataset",
                "dataset": dataset_id,
                "action": "no_changes",
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

    except NotFound:
        return {
            "status": "error",
            "error": f"Dataset not found: {dataset_id}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to bind IAM policy: {str(e)}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def bind_bq_table(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Bind IAM policies to BigQuery table.

    Note: BigQuery table-level IAM is limited. Most policies are dataset-level.
    """
    start_time = time.time()

    action.get("project")
    action.get("dataset")
    action.get("table")

    # BigQuery doesn't support table-level IAM in the same way
    # This is a placeholder for future fine-grained access control
    return {
        "status": "ok",
        "op": "iam.bind_bq_table",
        "action": "skipped",
        "reason": "BigQuery IAM is primarily dataset-level",
        "recommendation": "Use dataset-level IAM or authorized views",
        "duration_ms": duration_ms(start_time),
        "changed": False,
    }


def bind_gcs_bucket(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Bind IAM policies to Cloud Storage bucket.

    Args:
        action: IAM binding configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import storage
        from google.cloud.exceptions import NotFound
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-storage library not available",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    bucket_name = action.get("bucket")
    policies = action.get("policies", {})

    if not project or not bucket_name:
        return {
            "status": "error",
            "error": "Both 'project' and 'bucket' are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        client = storage.Client(project=project)
        bucket = client.get_bucket(bucket_name)

        # Get current IAM policy
        policy = bucket.get_iam_policy(requested_policy_version=3)
        len(policy.bindings)

        changed = False

        # Process access policies from FLUID contract
        for policy_name, policy_config in policies.items():
            if not isinstance(policy_config, dict):
                continue

            principals = policy_config.get("principals", [])
            permissions = policy_config.get("permissions", [])

            # Map FLUID permissions to GCS roles
            for permission in permissions:
                role = _map_permission_to_gcs_role(permission)
                if role:
                    # Format members correctly
                    members = set()
                    for principal in principals:
                        if "@" in principal:
                            if principal.endswith("gserviceaccount.com"):
                                members.add(f"serviceAccount:{principal}")
                            else:
                                members.add(f"user:{principal}")
                        else:
                            members.add(f"group:{principal}")

                    # Check if binding exists
                    existing_binding = None
                    for binding in policy.bindings:
                        if binding["role"] == role:
                            existing_binding = binding
                            break

                    if existing_binding:
                        # Add members to existing binding
                        original_members = set(existing_binding.get("members", []))
                        new_members = members - original_members
                        if new_members:
                            existing_binding["members"] = list(original_members | members)
                            changed = True
                    else:
                        # Create new binding
                        policy.bindings.append({"role": role, "members": list(members)})
                        changed = True

        if changed:
            bucket.set_iam_policy(policy)

            return {
                "status": "changed",
                "op": "iam.bind_gcs_bucket",
                "bucket": bucket_name,
                "bindings_count": len(policy.bindings),
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }
        else:
            return {
                "status": "ok",
                "op": "iam.bind_gcs_bucket",
                "bucket": bucket_name,
                "action": "no_changes",
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

    except NotFound:
        return {
            "status": "error",
            "error": f"Bucket not found: {bucket_name}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to bind IAM policy: {str(e)}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def bind_pubsub_topic(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Bind IAM policies to Pub/Sub topic.

    Args:
        action: IAM binding configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import pubsub_v1
        from google.cloud.exceptions import NotFound
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-pubsub library not available",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    topic_name = action.get("topic")
    policies = action.get("policies", {})

    if not project or not topic_name:
        return {
            "status": "error",
            "error": "Both 'project' and 'topic' are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        publisher = pubsub_v1.PublisherClient()
        topic_path = publisher.topic_path(project, topic_name)

        # Get current IAM policy
        policy = publisher.get_iam_policy(request={"resource": topic_path})
        len(policy.bindings)

        changed = False

        # Process access policies from FLUID contract
        for policy_name, policy_config in policies.items():
            if not isinstance(policy_config, dict):
                continue

            principals = policy_config.get("principals", [])
            permissions = policy_config.get("permissions", [])

            # Map FLUID permissions to Pub/Sub roles
            for permission in permissions:
                role = _map_permission_to_pubsub_role(permission)
                if role:
                    # Format members
                    members = []
                    for principal in principals:
                        if "@" in principal:
                            if principal.endswith("gserviceaccount.com"):
                                members.append(f"serviceAccount:{principal}")
                            else:
                                members.append(f"user:{principal}")
                        else:
                            members.append(f"group:{principal}")

                    # Check if binding exists
                    existing_binding = None
                    for binding in policy.bindings:
                        if binding.role == role:
                            existing_binding = binding
                            break

                    if existing_binding:
                        # Add members to existing binding
                        original_members = set(existing_binding.members)
                        new_members = set(members) - original_members
                        if new_members:
                            existing_binding.members.extend(list(new_members))
                            changed = True
                    else:
                        # Create new binding
                        from google.iam.v1 import policy_pb2

                        new_binding = policy_pb2.Binding(role=role, members=members)
                        policy.bindings.append(new_binding)
                        changed = True

        if changed:
            publisher.set_iam_policy(request={"resource": topic_path, "policy": policy})

            return {
                "status": "changed",
                "op": "iam.bind_pubsub_topic",
                "topic": topic_name,
                "bindings_count": len(policy.bindings),
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }
        else:
            return {
                "status": "ok",
                "op": "iam.bind_pubsub_topic",
                "topic": topic_name,
                "action": "no_changes",
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

    except NotFound:
        return {
            "status": "error",
            "error": f"Topic not found: {topic_name}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": f"Failed to bind IAM policy: {str(e)}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def _map_permission_to_bq_role(permission: str) -> Optional[str]:
    """Map FLUID permission to BigQuery IAM role."""
    permission_map = {
        "read": "READER",
        "select": "READER",
        "query": "READER",
        "write": "WRITER",
        "insert": "WRITER",
        "update": "WRITER",
        "delete": "WRITER",
        "admin": "OWNER",
        "owner": "OWNER",
    }
    return permission_map.get(permission.lower())


def _map_permission_to_gcs_role(permission: str) -> Optional[str]:
    """Map FLUID permission to GCS IAM role."""
    permission_map = {
        "read": "roles/storage.objectViewer",
        "view": "roles/storage.objectViewer",
        "list": "roles/storage.objectViewer",
        "write": "roles/storage.objectCreator",
        "create": "roles/storage.objectCreator",
        "delete": "roles/storage.objectAdmin",
        "admin": "roles/storage.admin",
        "owner": "roles/storage.admin",
    }
    return permission_map.get(permission.lower())


def _map_permission_to_pubsub_role(permission: str) -> Optional[str]:
    """Map FLUID permission to Pub/Sub IAM role."""
    permission_map = {
        "publish": "roles/pubsub.publisher",
        "write": "roles/pubsub.publisher",
        "subscribe": "roles/pubsub.subscriber",
        "read": "roles/pubsub.subscriber",
        "view": "roles/pubsub.viewer",
        "admin": "roles/pubsub.admin",
        "owner": "roles/pubsub.admin",
    }
    return permission_map.get(permission.lower())
