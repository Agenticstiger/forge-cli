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

# fluid_build/providers/gcp/util/names.py
"""
GCP resource naming utilities.

Handles resource naming conventions, validation, and normalization
for GCP services with proper character restrictions and length limits.
"""

import re
from typing import List, Optional


class NamingError(ValueError):
    """Exception raised for invalid resource names."""

    pass


def normalize_dataset_name(name: str) -> str:
    """
    Normalize a name for BigQuery dataset usage.

    BigQuery dataset names:
    - Must contain only letters, numbers, and underscores
    - Must start with a letter or underscore
    - Maximum 1024 characters

    Args:
        name: Input name to normalize

    Returns:
        Normalized dataset name

    Raises:
        NamingError: If name cannot be normalized
    """
    if not name:
        raise NamingError("Dataset name cannot be empty")

    # Remove invalid characters and replace with underscores
    normalized = re.sub(r"[^a-zA-Z0-9_]", "_", name)

    # Ensure it starts with letter or underscore
    if normalized[0].isdigit():
        normalized = f"_{normalized}"

    # Trim to maximum length
    if len(normalized) > 1024:
        normalized = normalized[:1024]
        # Ensure we don't end with underscore after truncation
        normalized = normalized.rstrip("_")

    if not normalized:
        raise NamingError(f"Cannot normalize dataset name: {name}")

    return normalized


def normalize_table_name(name: str) -> str:
    """
    Normalize a name for BigQuery table usage.

    BigQuery table names follow same rules as datasets.

    Args:
        name: Input name to normalize

    Returns:
        Normalized table name
    """
    return normalize_dataset_name(name)  # Same rules


def normalize_bucket_name(name: str, project: Optional[str] = None) -> str:
    """
    Normalize a name for Cloud Storage bucket usage.

    Cloud Storage bucket names:
    - Must be 3-63 characters long
    - Must contain only lowercase letters, numbers, and hyphens
    - Must start and end with alphanumeric character
    - Cannot contain consecutive periods
    - Cannot be formatted as IP address

    Args:
        name: Input name to normalize
        project: Optional project ID to make name unique

    Returns:
        Normalized bucket name

    Raises:
        NamingError: If name cannot be normalized
    """
    if not name:
        raise NamingError("Bucket name cannot be empty")

    # Convert to lowercase and replace invalid characters
    normalized = re.sub(r"[^a-z0-9\-]", "-", name.lower())

    # Remove consecutive hyphens
    normalized = re.sub(r"-+", "-", normalized)

    # Ensure starts and ends with alphanumeric
    normalized = normalized.strip("-")
    if not normalized:
        raise NamingError(f"Cannot normalize bucket name: {name}")

    # Add project prefix if provided to ensure uniqueness
    if project:
        normalized = f"{project}-{normalized}"

    # Ensure length constraints
    if len(normalized) < 3:
        normalized = f"{normalized}-bucket"

    if len(normalized) > 63:
        # Try to truncate intelligently
        if project:
            # Remove project prefix and retry
            normalized = normalized[len(project) + 1 :]
            if len(normalized) > 50:  # Leave room for project prefix
                normalized = normalized[:50]
            normalized = f"{project}-{normalized}"
        else:
            normalized = normalized[:63]

        # Clean up after truncation
        normalized = normalized.rstrip("-")

    # Final validation
    if len(normalized) < 3 or len(normalized) > 63:
        raise NamingError(f"Bucket name length must be 3-63 characters: {normalized}")

    if not re.match(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$", normalized):
        raise NamingError(f"Invalid bucket name format: {normalized}")

    # Check for IP address format
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", normalized):
        raise NamingError(f"Bucket name cannot be formatted as IP address: {normalized}")

    return normalized


def normalize_topic_name(name: str) -> str:
    """
    Normalize a name for Pub/Sub topic usage.

    Pub/Sub topic names:
    - Must be 3-255 characters long
    - Must start with a letter
    - Can contain letters, numbers, hyphens, periods, underscores, tildes, percent signs, plus signs

    Args:
        name: Input name to normalize

    Returns:
        Normalized topic name
    """
    if not name:
        raise NamingError("Topic name cannot be empty")

    # Remove invalid characters
    normalized = re.sub(r"[^a-zA-Z0-9\-\._~%+]", "_", name)

    # Ensure starts with letter
    if not normalized[0].isalpha():
        normalized = f"topic_{normalized}"

    # Ensure length constraints
    if len(normalized) < 3:
        normalized = f"{normalized}_topic"

    if len(normalized) > 255:
        normalized = normalized[:255]

    return normalized


def normalize_subscription_name(name: str) -> str:
    """
    Normalize a name for Pub/Sub subscription usage.

    Same rules as topic names.

    Args:
        name: Input name to normalize

    Returns:
        Normalized subscription name
    """
    return normalize_topic_name(name)


def normalize_composer_name(name: str) -> str:
    """
    Normalize a name for Cloud Composer environment usage.

    Cloud Composer environment names:
    - Must be 1-63 characters long
    - Must contain only lowercase letters, numbers, and hyphens
    - Must start and end with a letter or number

    Args:
        name: Input name to normalize

    Returns:
        Normalized composer environment name
    """
    if not name:
        raise NamingError("Composer environment name cannot be empty")

    # Convert to lowercase and replace invalid characters
    normalized = re.sub(r"[^a-z0-9\-]", "-", name.lower())

    # Remove consecutive hyphens
    normalized = re.sub(r"-+", "-", normalized)

    # Ensure starts and ends with alphanumeric
    normalized = normalized.strip("-")

    # Ensure starts with letter or number
    if not normalized or not (normalized[0].isalnum()):
        normalized = f"env-{normalized}" if normalized else "composer-env"

    # Ensure length constraints
    if len(normalized) < 1:
        normalized = "composer-env"

    if len(normalized) > 63:
        normalized = normalized[:63].rstrip("-")

    if len(normalized) < 1:
        raise NamingError(f"Cannot create valid composer environment name from: {name}")

    return normalized


def normalize_pubsub_name(name: str) -> str:
    """
    Normalize a name for Pub/Sub topic/subscription usage.

    This is an alias for normalize_topic_name for consistency.

    Args:
        name: Input name to normalize

    Returns:
        Normalized Pub/Sub name
    """
    return normalize_topic_name(name)


def normalize_job_name(name: str) -> str:
    """
    Normalize a name for job usage (Dataflow, etc.).

    Job names:
    - Must be 4-63 characters long
    - Must contain only lowercase letters, numbers, and hyphens
    - Must start with lowercase letter
    - Must end with lowercase letter or number

    Args:
        name: Input name to normalize

    Returns:
        Normalized job name
    """
    if not name:
        raise NamingError("Job name cannot be empty")

    # Convert to lowercase and replace invalid characters
    normalized = re.sub(r"[^a-z0-9\-]", "-", name.lower())

    # Remove consecutive hyphens
    normalized = re.sub(r"-+", "-", normalized)

    # Ensure starts with letter
    if not normalized[0].isalpha():
        normalized = f"job-{normalized}"

    # Ensure ends with letter or number
    normalized = normalized.rstrip("-")

    # Ensure length constraints
    if len(normalized) < 4:
        normalized = f"{normalized}-job"

    if len(normalized) > 63:
        normalized = normalized[:63].rstrip("-")

    if len(normalized) < 4:
        raise NamingError(f"Cannot create valid job name from: {name}")

    return normalized


def validate_name(name: str, resource_type: str) -> bool:
    """
    Validate a resource name for a specific GCP resource type.

    Args:
        name: Resource name to validate
        resource_type: Type of GCP resource (dataset, table, bucket, topic, etc.)

    Returns:
        True if name is valid for the resource type
    """
    try:
        if resource_type in ["dataset", "table"]:
            # BigQuery naming rules
            if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name):
                return False
            return len(name) <= 1024

        elif resource_type == "bucket":
            # Cloud Storage naming rules
            if len(name) < 3 or len(name) > 63:
                return False
            if not re.match(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$", name):
                return False
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", name):
                return False
            return True

        elif resource_type in ["topic", "subscription"]:
            # Pub/Sub naming rules
            if len(name) < 3 or len(name) > 255:
                return False
            if not name[0].isalpha():
                return False
            return re.match(r"^[a-zA-Z][a-zA-Z0-9\-\._~%+]*$", name) is not None

        elif resource_type == "job":
            # Job naming rules
            if len(name) < 4 or len(name) > 63:
                return False
            if not name[0].islower() or not name[0].isalpha():
                return False
            if name[-1] == "-":
                return False
            return re.match(r"^[a-z][a-z0-9\-]*[a-z0-9]$", name) is not None

        else:
            # Unknown resource type
            return False

    except Exception:
        return False


def generate_unique_name(
    base_name: str,
    resource_type: str,
    existing_names: Optional[List[str]] = None,
    project: Optional[str] = None,
) -> str:
    """
    Generate a unique resource name based on base name and resource type.

    Args:
        base_name: Base name to use
        resource_type: Type of GCP resource
        existing_names: List of existing names to avoid conflicts
        project: Optional project ID for uniqueness

    Returns:
        Unique normalized resource name
    """
    existing_names = existing_names or []

    # Normalize the base name
    if resource_type in ["dataset", "table"]:
        normalized = normalize_dataset_name(base_name)
    elif resource_type == "bucket":
        normalized = normalize_bucket_name(base_name, project)
    elif resource_type in ["topic", "subscription"]:
        normalized = normalize_topic_name(base_name)
    elif resource_type == "job":
        normalized = normalize_job_name(base_name)
    else:
        raise NamingError(f"Unknown resource type: {resource_type}")

    # Check for uniqueness
    if normalized not in existing_names:
        return normalized

    # Generate unique variant
    counter = 1
    max_attempts = 1000

    while counter < max_attempts:
        suffix = (
            f"_{counter}"
            if resource_type in ["dataset", "table", "topic", "subscription"]
            else f"-{counter}"
        )
        candidate = f"{normalized}{suffix}"

        # Ensure still valid after adding suffix
        try:
            if resource_type in ["dataset", "table"]:
                if len(candidate) > 1024:
                    # Truncate base name to make room for suffix
                    truncated_base = normalized[: 1024 - len(suffix)]
                    candidate = f"{truncated_base}{suffix}"
            elif resource_type == "bucket":
                if len(candidate) > 63:
                    truncated_base = normalized[: 63 - len(suffix)]
                    candidate = f"{truncated_base}{suffix}"
            elif resource_type in ["topic", "subscription"]:
                if len(candidate) > 255:
                    truncated_base = normalized[: 255 - len(suffix)]
                    candidate = f"{truncated_base}{suffix}"
            elif resource_type == "job":
                if len(candidate) > 63:
                    truncated_base = normalized[: 63 - len(suffix)]
                    candidate = f"{truncated_base}{suffix}"

            if validate_name(candidate, resource_type) and candidate not in existing_names:
                return candidate

        except Exception:
            pass  # Try next counter

        counter += 1

    raise NamingError(
        f"Could not generate unique name for {resource_type} after {max_attempts} attempts"
    )


def get_resource_path(resource_type: str, project: str, **kwargs) -> str:
    """
    Generate full GCP resource path/name.

    Args:
        resource_type: Type of GCP resource
        project: GCP project ID
        **kwargs: Additional resource identifiers

    Returns:
        Full resource path
    """
    if resource_type == "dataset":
        dataset = kwargs.get("dataset")
        if not dataset:
            raise ValueError("Dataset name required")
        return f"projects/{project}/datasets/{dataset}"

    elif resource_type == "table":
        dataset = kwargs.get("dataset")
        table = kwargs.get("table")
        if not dataset or not table:
            raise ValueError("Dataset and table names required")
        return f"projects/{project}/datasets/{dataset}/tables/{table}"

    elif resource_type == "bucket":
        bucket = kwargs.get("bucket")
        if not bucket:
            raise ValueError("Bucket name required")
        return f"gs://{bucket}"

    elif resource_type == "topic":
        topic = kwargs.get("topic")
        if not topic:
            raise ValueError("Topic name required")
        return f"projects/{project}/topics/{topic}"

    elif resource_type == "subscription":
        subscription = kwargs.get("subscription")
        if not subscription:
            raise ValueError("Subscription name required")
        return f"projects/{project}/subscriptions/{subscription}"

    else:
        raise ValueError(f"Unknown resource type: {resource_type}")


def extract_name_from_path(resource_path: str) -> str:
    """
    Extract resource name from full GCP resource path.

    Args:
        resource_path: Full GCP resource path

    Returns:
        Resource name component
    """
    if not resource_path:
        return ""

    # Handle different path formats
    if resource_path.startswith("gs://"):
        # GCS bucket path
        return resource_path[5:].split("/")[0]

    if "/topics/" in resource_path:
        return resource_path.split("/topics/")[-1]

    if "/subscriptions/" in resource_path:
        return resource_path.split("/subscriptions/")[-1]

    if "/datasets/" in resource_path:
        if "/tables/" in resource_path:
            return resource_path.split("/tables/")[-1]
        else:
            return resource_path.split("/datasets/")[-1]

    # Default: return last component
    return resource_path.split("/")[-1]
