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

# fluid_build/providers/aws/util/s3_policies.py
"""
Smart S3 lifecycle policy generation.

Provides production-ready defaults for data lake and warehouse use cases.
"""

from typing import Any, Dict, List, Optional


def create_data_lake_lifecycle(
    prefix: str = "",
    intelligent_tiering_days: int = 30,
    glacier_days: int = 90,
    deep_archive_days: int = 365,
    expiration_days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Create lifecycle policy optimized for data lakes.

    Default strategy:
    - Day 0-30: S3 Standard (frequent access)
    - Day 30-90: Intelligent Tiering (automatic optimization)
    - Day 90-365: Glacier Instant Retrieval (archive with instant access)
    - Day 365+: Deep Archive (long-term cold storage)

    Args:
        prefix: Object prefix to apply policy to
        intelligent_tiering_days: Days before moving to Intelligent Tiering
        glacier_days: Days before moving to Glacier
        deep_archive_days: Days before moving to Deep Archive
        expiration_days: Days before expiring objects (None = no expiration)

    Returns:
        List of lifecycle rules
    """
    rules = []

    # Main lifecycle rule
    rule = {
        "Id": f"data-lake-lifecycle-{prefix or 'all'}",
        "Status": "Enabled",
        "Filter": {"Prefix": prefix} if prefix else {},
        "Transitions": [
            {"Days": intelligent_tiering_days, "StorageClass": "INTELLIGENT_TIERING"},
            {"Days": glacier_days, "StorageClass": "GLACIER_IR"},  # Glacier Instant Retrieval
            {"Days": deep_archive_days, "StorageClass": "DEEP_ARCHIVE"},
        ],
    }

    if expiration_days:
        rule["Expiration"] = {"Days": expiration_days}

    rules.append(rule)

    # Incomplete multipart upload cleanup
    rules.append(
        {
            "Id": "cleanup-incomplete-uploads",
            "Status": "Enabled",
            "Filter": {"Prefix": prefix} if prefix else {},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        }
    )

    # Delete markers cleanup
    rules.append(
        {
            "Id": "cleanup-delete-markers",
            "Status": "Enabled",
            "Filter": {"Prefix": prefix} if prefix else {},
            "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
            "ExpiredObjectDeleteMarker": True,
        }
    )

    return rules


def create_staging_lifecycle(
    prefix: str = "staging/",
    expiration_days: int = 7,
) -> List[Dict[str, Any]]:
    """
    Create lifecycle policy for temporary staging data.

    Automatically deletes old staging data to save costs.

    Args:
        prefix: Prefix for staging data
        expiration_days: Days before expiring staging data

    Returns:
        List of lifecycle rules
    """
    return [
        {
            "Id": "staging-expiration",
            "Status": "Enabled",
            "Filter": {"Prefix": prefix},
            "Expiration": {"Days": expiration_days},
        },
        {
            "Id": "staging-multipart-cleanup",
            "Status": "Enabled",
            "Filter": {"Prefix": prefix},
            "AbortIncompleteMultipartUpload": {
                "DaysAfterInitiation": 1  # Clean up quickly for staging
            },
        },
    ]


def create_log_lifecycle(
    prefix: str = "logs/",
    glacier_days: int = 30,
    expiration_days: int = 365,
) -> List[Dict[str, Any]]:
    """
    Create lifecycle policy for log data.

    Optimized for:
    - Recent logs accessible (30 days)
    - Archived logs for compliance (1 year)
    - Automatic cleanup after retention period

    Args:
        prefix: Prefix for log data
        glacier_days: Days before archiving to Glacier
        expiration_days: Days before expiring logs

    Returns:
        List of lifecycle rules
    """
    return [
        {
            "Id": "log-archival",
            "Status": "Enabled",
            "Filter": {"Prefix": prefix},
            "Transitions": [{"Days": glacier_days, "StorageClass": "GLACIER_IR"}],
            "Expiration": {"Days": expiration_days},
        }
    ]


def create_partition_lifecycle(
    partition_pattern: str = "year=*/month=*/day=*/",
    days_to_keep_standard: int = 30,
    days_to_keep_total: int = 730,  # 2 years
) -> List[Dict[str, Any]]:
    """
    Create lifecycle policy for partitioned data.

    Common for Hive/Athena partitioned tables.

    Args:
        partition_pattern: Partition path pattern
        days_to_keep_standard: Days in S3 Standard
        days_to_keep_total: Total days before expiration

    Returns:
        List of lifecycle rules
    """
    return [
        {
            "Id": "partition-lifecycle",
            "Status": "Enabled",
            "Filter": {"Prefix": partition_pattern.split("*")[0]},
            "Transitions": [
                {"Days": days_to_keep_standard, "StorageClass": "INTELLIGENT_TIERING"},
                {"Days": 90, "StorageClass": "GLACIER_IR"},
            ],
            "Expiration": {"Days": days_to_keep_total},
        }
    ]


def create_cost_optimized_lifecycle(
    prefix: str = "",
) -> List[Dict[str, Any]]:
    """
    Create aggressive cost-optimization lifecycle policy.

    Maximizes cost savings with:
    - Quick transition to Intelligent Tiering
    - Aggressive archival
    - Multipart upload cleanup

    Args:
        prefix: Object prefix

    Returns:
        List of lifecycle rules
    """
    return [
        {
            "Id": "cost-optimization",
            "Status": "Enabled",
            "Filter": {"Prefix": prefix} if prefix else {},
            "Transitions": [
                {
                    "Days": 7,  # Quick move to Intelligent Tiering
                    "StorageClass": "INTELLIGENT_TIERING",
                },
                {"Days": 60, "StorageClass": "GLACIER_IR"},  # Archive after 2 months
            ],
        },
        {
            "Id": "multipart-cleanup",
            "Status": "Enabled",
            "Filter": {"Prefix": prefix} if prefix else {},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
        },
    ]


def get_default_bucket_config(
    purpose: str = "data",
    enable_versioning: bool = True,
    enable_encryption: bool = True,
    block_public_access: bool = True,
) -> Dict[str, Any]:
    """
    Get production-ready bucket configuration.

    Args:
        purpose: Bucket purpose ("data", "staging", "logs")
        enable_versioning: Enable versioning
        enable_encryption: Enable encryption
        block_public_access: Block public access

    Returns:
        Bucket configuration dict
    """
    config = {
        "versioning": enable_versioning,
        "encryption": enable_encryption,
        "block_public_access": block_public_access,
    }

    # Add purpose-specific lifecycle
    if purpose == "data":
        config["lifecycle_rules"] = create_data_lake_lifecycle()
    elif purpose == "staging":
        config["lifecycle_rules"] = create_staging_lifecycle()
    elif purpose == "logs":
        config["lifecycle_rules"] = create_log_lifecycle()
    else:
        # Default: cost-optimized
        config["lifecycle_rules"] = create_cost_optimized_lifecycle()

    return config


def estimate_storage_cost(
    size_gb: float,
    days_standard: int = 30,
    days_intelligent: int = 60,
    days_glacier: int = 275,
    region: str = "us-east-1",
) -> Dict[str, float]:
    """
    Estimate monthly storage costs with lifecycle policy.

    Uses current AWS pricing (approximate).

    Args:
        size_gb: Data size in GB
        days_standard: Days in S3 Standard per month
        days_intelligent: Days in Intelligent Tiering per month
        days_glacier: Days in Glacier per month
        region: AWS region (affects pricing)

    Returns:
        Cost breakdown
    """
    # Pricing per GB-month (us-east-1, approximate)
    pricing = {
        "standard": 0.023,
        "intelligent_tiering": 0.0125,  # Average
        "glacier_ir": 0.004,
        "deep_archive": 0.00099,
    }

    # Calculate proportional costs
    _total_days = days_standard + days_intelligent + days_glacier  # noqa: F841

    cost_standard = size_gb * pricing["standard"] * (days_standard / 30)
    cost_intelligent = size_gb * pricing["intelligent_tiering"] * (days_intelligent / 30)
    cost_glacier = size_gb * pricing["glacier_ir"] * (days_glacier / 30)

    total_cost = cost_standard + cost_intelligent + cost_glacier

    # Cost without lifecycle (all Standard)
    cost_without_lifecycle = size_gb * pricing["standard"]

    savings = cost_without_lifecycle - total_cost
    savings_percent = (savings / cost_without_lifecycle) * 100 if cost_without_lifecycle > 0 else 0

    return {
        "cost_standard": round(cost_standard, 2),
        "cost_intelligent_tiering": round(cost_intelligent, 2),
        "cost_glacier": round(cost_glacier, 2),
        "total_cost": round(total_cost, 2),
        "cost_without_lifecycle": round(cost_without_lifecycle, 2),
        "savings": round(savings, 2),
        "savings_percent": round(savings_percent, 1),
    }
