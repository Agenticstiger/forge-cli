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

# fluid_build/providers/aws/util/names.py
"""
AWS resource name normalization utilities.
"""

import re
from typing import Optional


def normalize_database_name(name: str) -> str:
    """
    Normalize Glue database name to AWS requirements.

    Glue database names must:
    - Be lowercase
    - Contain only alphanumeric characters and underscores
    - Start with a letter
    - Be 1-255 characters

    Args:
        name: Input database name

    Returns:
        Normalized database name
    """
    # Convert to lowercase
    normalized = name.lower()

    # Replace invalid characters with underscores
    normalized = re.sub(r"[^a-z0-9_]", "_", normalized)

    # Ensure starts with letter
    if normalized and not normalized[0].isalpha():
        normalized = f"db_{normalized}"

    # Truncate to 255 characters
    return normalized[:255]


def normalize_table_name(name: str) -> str:
    """
    Normalize Glue table name to AWS requirements.

    Glue table names must:
    - Be lowercase
    - Contain only alphanumeric characters and underscores
    - Be 1-255 characters

    Args:
        name: Input table name

    Returns:
        Normalized table name
    """
    # Convert to lowercase
    normalized = name.lower()

    # Replace invalid characters with underscores
    normalized = re.sub(r"[^a-z0-9_]", "_", normalized)

    # Remove consecutive underscores
    normalized = re.sub(r"_+", "_", normalized)

    # Remove leading/trailing underscores
    normalized = normalized.strip("_")

    # Truncate to 255 characters
    return normalized[:255]


def normalize_bucket_name(name: str, account_id: Optional[str] = None) -> str:
    """
    Normalize S3 bucket name to AWS requirements.

    S3 bucket names must:
    - Be 3-63 characters
    - Be lowercase
    - Contain only lowercase letters, numbers, hyphens, and periods
    - Start and end with letter or number
    - Not be formatted as IP address

    Args:
        name: Input bucket name
        account_id: Optional account ID to ensure uniqueness

    Returns:
        Normalized bucket name
    """
    # Convert to lowercase
    normalized = name.lower()

    # Replace invalid characters with hyphens
    normalized = re.sub(r"[^a-z0-9.-]", "-", normalized)

    # Remove consecutive hyphens
    normalized = re.sub(r"-+", "-", normalized)

    # Remove leading/trailing hyphens and periods
    normalized = normalized.strip(".-")

    # Ensure starts and ends with alphanumeric
    if normalized and not normalized[0].isalnum():
        normalized = f"bucket-{normalized}"
    if normalized and not normalized[-1].isalnum():
        normalized = f"{normalized}-data"

    # Add account ID prefix if provided for uniqueness
    if account_id and not normalized.startswith(account_id):
        normalized = f"{account_id}-{normalized}"

    # Truncate to 63 characters
    if len(normalized) > 63:
        normalized = normalized[:63].rstrip(".-")

    # Ensure minimum length
    if len(normalized) < 3:
        normalized = f"{normalized}-bucket"

    return normalized


def normalize_lambda_name(name: str) -> str:
    """
    Normalize Lambda function name to AWS requirements.

    Lambda function names must:
    - Be 1-64 characters
    - Contain only letters, numbers, hyphens, and underscores

    Args:
        name: Input function name

    Returns:
        Normalized function name
    """
    # Replace invalid characters with underscores
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)

    # Remove consecutive underscores
    normalized = re.sub(r"_+", "_", normalized)

    # Truncate to 64 characters
    return normalized[:64]
