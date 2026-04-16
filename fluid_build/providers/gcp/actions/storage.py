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

# fluid_build/providers/gcp/actions/storage.py
"""
Google Cloud Storage actions for GCP provider.

Implements idempotent Cloud Storage operations including:
- Bucket creation and management
- Lifecycle policy configuration
- IAM policy bindings
- Object operations and metadata
"""

import time
from typing import Any, Dict, List

from ..util.logging import duration_ms
from ..util.names import normalize_bucket_name


def ensure_bucket(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure Cloud Storage bucket exists with specified configuration.

    Creates bucket if it doesn't exist, updates configuration if changed.
    Idempotent operation - safe to run multiple times.

    Args:
        action: Bucket action configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        from google.cloud import storage
        from google.cloud.exceptions import Conflict, NotFound
    except ImportError:
        return {
            "status": "error",
            "error": "google-cloud-storage library not available. Install with: pip install google-cloud-storage",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    bucket_name = action.get("bucket")
    location = action.get("location", "US")
    storage_class = action.get("storage_class", "STANDARD")
    labels = action.get("labels", {})
    versioning = action.get("versioning", False)
    uniform_bucket_level_access = action.get("uniform_bucket_level_access", False)

    if not project or not bucket_name:
        return {
            "status": "error",
            "error": "Both 'project' and 'bucket' are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize Storage client
        client = storage.Client(project=project)

        # Normalize bucket name
        normalized_bucket = normalize_bucket_name(bucket_name)

        changed = False

        try:
            # Check if bucket exists
            bucket = client.get_bucket(normalized_bucket)

            # Compare and update if necessary
            update_needed = False

            # Check storage class
            if bucket.storage_class != storage_class:
                bucket.storage_class = storage_class
                update_needed = True

            # Check labels
            existing_labels = bucket.labels or {}
            if existing_labels != labels:
                bucket.labels = labels
                update_needed = True

            # Check versioning
            if bucket.versioning_enabled != versioning:
                bucket.versioning_enabled = versioning
                update_needed = True

            # Check uniform bucket-level access
            if (
                bucket.iam_configuration.uniform_bucket_level_access_enabled
                != uniform_bucket_level_access
            ):
                bucket.iam_configuration.uniform_bucket_level_access_enabled = (
                    uniform_bucket_level_access
                )
                update_needed = True

            if update_needed:
                bucket.patch()
                changed = True

            return {
                "status": "changed" if changed else "ok",
                "bucket_name": bucket.name,
                "location": bucket.location,
                "storage_class": bucket.storage_class,
                "labels": bucket.labels,
                "versioning_enabled": bucket.versioning_enabled,
                "uniform_bucket_level_access": bucket.iam_configuration.uniform_bucket_level_access_enabled,
                "created": bucket.time_created,
                "self_link": bucket.self_link,
                "duration_ms": duration_ms(start_time),
                "changed": changed,
            }

        except NotFound:
            # Bucket doesn't exist, create it
            bucket = client.bucket(normalized_bucket)
            bucket.storage_class = storage_class
            bucket.labels = labels
            bucket.versioning_enabled = versioning

            # Configure uniform bucket-level access
            if uniform_bucket_level_access:
                bucket.iam_configuration.uniform_bucket_level_access_enabled = True

            created_bucket = client.create_bucket(bucket, location=location)

            return {
                "status": "changed",
                "bucket_name": created_bucket.name,
                "location": created_bucket.location,
                "storage_class": created_bucket.storage_class,
                "labels": created_bucket.labels,
                "versioning_enabled": created_bucket.versioning_enabled,
                "uniform_bucket_level_access": created_bucket.iam_configuration.uniform_bucket_level_access_enabled,
                "created": created_bucket.time_created,
                "self_link": created_bucket.self_link,
                "duration_ms": duration_ms(start_time),
                "changed": True,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "bucket_name": normalized_bucket if "normalized_bucket" in locals() else None,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_lifecycle_policy(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure bucket lifecycle policy is configured.

    Creates or updates lifecycle rules for object management.

    Args:
        action: Lifecycle policy action configuration

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
            "error": "google-cloud-storage library not available. Install with: pip install google-cloud-storage",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    bucket_name = action.get("bucket")
    lifecycle_rules = action.get("lifecycle_rules", [])

    if not all([project, bucket_name]):
        return {
            "status": "error",
            "error": "Both 'project' and 'bucket' are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize Storage client
        client = storage.Client(project=project)

        # Normalize bucket name
        normalized_bucket = normalize_bucket_name(bucket_name)

        try:
            bucket = client.get_bucket(normalized_bucket)

            # Convert lifecycle rules to Storage format
            storage_rules = []
            for rule in lifecycle_rules:
                storage_rule = _convert_lifecycle_rule(rule)
                storage_rules.append(storage_rule)

            # Compare existing rules with new rules
            existing_rules = bucket.lifecycle_rules
            rules_changed = _are_lifecycle_rules_different(existing_rules, storage_rules)

            if rules_changed:
                bucket.lifecycle_rules = storage_rules
                bucket.patch()
                changed = True
            else:
                changed = False

            return {
                "status": "changed" if changed else "ok",
                "bucket_name": bucket.name,
                "lifecycle_rules_count": len(storage_rules),
                "duration_ms": duration_ms(start_time),
                "changed": changed,
            }

        except NotFound:
            return {
                "status": "error",
                "error": f"Bucket {normalized_bucket} does not exist",
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "bucket_name": normalized_bucket if "normalized_bucket" in locals() else None,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_bucket_iam_policy(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure IAM policy bindings are set on bucket.

    Adds or removes IAM policy bindings for bucket access control.

    Args:
        action: IAM policy action configuration

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
            "error": "google-cloud-storage library not available. Install with: pip install google-cloud-storage",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    bucket_name = action.get("bucket")
    bindings = action.get("bindings", [])

    if not all([project, bucket_name]):
        return {
            "status": "error",
            "error": "Both 'project' and 'bucket' are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize Storage client
        client = storage.Client(project=project)

        # Normalize bucket name
        normalized_bucket = normalize_bucket_name(bucket_name)

        try:
            bucket = client.get_bucket(normalized_bucket)

            # Get current IAM policy
            policy = bucket.get_iam_policy(requested_policy_version=3)

            changed = False

            # Apply bindings
            for binding in bindings:
                role = binding.get("role")
                members = binding.get("members", [])
                condition = binding.get("condition")

                if not role or not members:
                    continue

                # Check if binding already exists
                existing_binding = None
                for existing in policy.bindings:
                    if existing["role"] == role:
                        # Check if condition matches (if specified)
                        if condition:
                            if existing.get("condition") == condition:
                                existing_binding = existing
                                break
                        else:
                            if "condition" not in existing:
                                existing_binding = existing
                                break

                if existing_binding:
                    # Update existing binding
                    current_members = set(existing_binding["members"])
                    new_members = set(members)

                    if current_members != new_members:
                        existing_binding["members"] = list(new_members)
                        changed = True
                else:
                    # Add new binding
                    new_binding = {"role": role, "members": members}
                    if condition:
                        new_binding["condition"] = condition
                    policy.bindings.append(new_binding)
                    changed = True

            if changed:
                bucket.set_iam_policy(policy)

            return {
                "status": "changed" if changed else "ok",
                "bucket_name": bucket.name,
                "bindings_count": len(policy.bindings),
                "duration_ms": duration_ms(start_time),
                "changed": changed,
            }

        except NotFound:
            return {
                "status": "error",
                "error": f"Bucket {normalized_bucket} does not exist",
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "bucket_name": normalized_bucket if "normalized_bucket" in locals() else None,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def upload_object(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Upload object to Cloud Storage bucket.

    Uploads file content to specified bucket with metadata and encryption.

    Args:
        action: Object upload action configuration

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
            "error": "google-cloud-storage library not available. Install with: pip install google-cloud-storage",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    bucket_name = action.get("bucket")
    object_name = action.get("object")
    source_path = action.get("source_path")
    content = action.get("content")
    content_type = action.get("content_type")
    metadata = action.get("metadata", {})
    cache_control = action.get("cache_control")

    if not all([project, bucket_name, object_name]):
        return {
            "status": "error",
            "error": "Project, bucket, and object are all required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    if not source_path and not content:
        return {
            "status": "error",
            "error": "Either 'source_path' or 'content' must be provided",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize Storage client
        client = storage.Client(project=project)

        # Normalize bucket name
        normalized_bucket = normalize_bucket_name(bucket_name)

        try:
            bucket = client.get_bucket(normalized_bucket)
            blob = bucket.blob(object_name)

            # Set metadata if provided
            if metadata:
                blob.metadata = metadata

            if content_type:
                blob.content_type = content_type

            if cache_control:
                blob.cache_control = cache_control

            # Check if object exists and content has changed
            changed = True
            if blob.exists():
                if source_path:
                    # Compare file modification time and size
                    import os

                    try:
                        stat = os.stat(source_path)
                        if (
                            blob.updated
                            and blob.size == stat.st_size
                            and blob.updated.timestamp() >= stat.st_mtime
                        ):
                            changed = False
                    except OSError:
                        pass  # File doesn't exist, proceed with upload

            if changed:
                if source_path:
                    blob.upload_from_filename(source_path)
                else:
                    blob.upload_from_string(content, content_type=content_type)

            return {
                "status": "changed" if changed else "ok",
                "bucket_name": bucket.name,
                "object_name": blob.name,
                "size": blob.size,
                "content_type": blob.content_type,
                "updated": blob.updated,
                "md5_hash": blob.md5_hash,
                "self_link": blob.self_link,
                "duration_ms": duration_ms(start_time),
                "changed": changed,
            }

        except NotFound:
            return {
                "status": "error",
                "error": f"Bucket {normalized_bucket} does not exist",
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "bucket_name": normalized_bucket if "normalized_bucket" in locals() else None,
            "object_name": object_name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def delete_object(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Delete object from Cloud Storage bucket.

    Removes specified object from bucket.

    Args:
        action: Object deletion action configuration

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
            "error": "google-cloud-storage library not available. Install with: pip install google-cloud-storage",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    bucket_name = action.get("bucket")
    object_name = action.get("object")

    if not all([project, bucket_name, object_name]):
        return {
            "status": "error",
            "error": "Project, bucket, and object are all required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize Storage client
        client = storage.Client(project=project)

        # Normalize bucket name
        normalized_bucket = normalize_bucket_name(bucket_name)

        try:
            bucket = client.get_bucket(normalized_bucket)
            blob = bucket.blob(object_name)

            if blob.exists():
                blob.delete()
                changed = True
            else:
                changed = False

            return {
                "status": "changed" if changed else "ok",
                "bucket_name": bucket.name,
                "object_name": object_name,
                "duration_ms": duration_ms(start_time),
                "changed": changed,
            }

        except NotFound:
            return {
                "status": "error",
                "error": f"Bucket {normalized_bucket} does not exist",
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "bucket_name": normalized_bucket if "normalized_bucket" in locals() else None,
            "object_name": object_name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def copy_object(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Copy object within or between Cloud Storage buckets.

    Copies object with optional metadata updates.

    Args:
        action: Object copy action configuration

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
            "error": "google-cloud-storage library not available. Install with: pip install google-cloud-storage",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    project = action.get("project")
    source_bucket = action.get("source_bucket")
    source_object = action.get("source_object")
    dest_bucket = action.get("dest_bucket")
    dest_object = action.get("dest_object")
    metadata = action.get("metadata", {})

    if not all([project, source_bucket, source_object, dest_bucket, dest_object]):
        return {
            "status": "error",
            "error": "Project, source_bucket, source_object, dest_bucket, and dest_object are all required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        # Initialize Storage client
        client = storage.Client(project=project)

        # Normalize bucket names
        normalized_source_bucket = normalize_bucket_name(source_bucket)
        normalized_dest_bucket = normalize_bucket_name(dest_bucket)

        try:
            # Get source and destination buckets
            source_bucket_obj = client.get_bucket(normalized_source_bucket)
            dest_bucket_obj = client.get_bucket(normalized_dest_bucket)

            # Get source blob
            source_blob = source_bucket_obj.blob(source_object)

            if not source_blob.exists():
                return {
                    "status": "error",
                    "error": f"Source object {source_object} does not exist in bucket {normalized_source_bucket}",
                    "duration_ms": duration_ms(start_time),
                    "changed": False,
                }

            # Check if destination exists and is same
            dest_blob = dest_bucket_obj.blob(dest_object)
            changed = True

            if dest_blob.exists():
                # Compare metadata and content
                if (
                    source_blob.md5_hash == dest_blob.md5_hash
                    and source_blob.size == dest_blob.size
                ):
                    changed = False

            if changed:
                # Perform copy
                copied_blob = source_bucket_obj.copy_blob(source_blob, dest_bucket_obj, dest_object)

                # Update metadata if provided
                if metadata:
                    copied_blob.metadata = metadata
                    copied_blob.patch()
            else:
                copied_blob = dest_blob

            return {
                "status": "changed" if changed else "ok",
                "source_bucket": normalized_source_bucket,
                "source_object": source_object,
                "dest_bucket": normalized_dest_bucket,
                "dest_object": dest_object,
                "size": copied_blob.size,
                "content_type": copied_blob.content_type,
                "md5_hash": copied_blob.md5_hash,
                "duration_ms": duration_ms(start_time),
                "changed": changed,
            }

        except NotFound as e:
            return {
                "status": "error",
                "error": f"Bucket or object not found: {str(e)}",
                "duration_ms": duration_ms(start_time),
                "changed": False,
            }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


# Helper functions


def _convert_lifecycle_rule(rule_config: Dict[str, Any]) -> Dict[str, Any]:
    """Convert FLUID lifecycle rule to Storage format."""
    action = rule_config.get("action", {})
    condition = rule_config.get("condition", {})

    storage_rule = {"action": {"type": action.get("type", "Delete")}, "condition": {}}

    # Add storage class for SetStorageClass action
    if action.get("type") == "SetStorageClass":
        storage_rule["action"]["storageClass"] = action.get("storage_class", "NEARLINE")

    # Convert conditions
    if "age" in condition:
        storage_rule["condition"]["age"] = condition["age"]

    if "created_before" in condition:
        storage_rule["condition"]["createdBefore"] = condition["created_before"]

    if "matches_storage_class" in condition:
        storage_rule["condition"]["matchesStorageClass"] = condition["matches_storage_class"]

    if "matches_prefix" in condition:
        storage_rule["condition"]["matchesPrefix"] = condition["matches_prefix"]

    if "matches_suffix" in condition:
        storage_rule["condition"]["matchesSuffix"] = condition["matches_suffix"]

    if "num_newer_versions" in condition:
        storage_rule["condition"]["numNewerVersions"] = condition["num_newer_versions"]

    return storage_rule


def _are_lifecycle_rules_different(existing_rules: List[Dict], new_rules: List[Dict]) -> bool:
    """Compare existing lifecycle rules with new rules."""
    if len(existing_rules) != len(new_rules):
        return True

    # Convert to comparable format
    def normalize_rule(rule):
        return {"action": rule.get("action", {}), "condition": rule.get("condition", {})}

    existing_normalized = [normalize_rule(rule) for rule in existing_rules]
    new_normalized = [normalize_rule(rule) for rule in new_rules]

    return existing_normalized != new_normalized
