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

# fluid_build/providers/aws/actions/s3.py
"""
S3 actions for AWS provider.

Implements idempotent S3 operations including:
- Bucket creation and management
- Lifecycle policies
- Versioning configuration
- Public access settings
"""

import time
from typing import Any, Dict

from ..util.logging import duration_ms


def ensure_bucket(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure S3 bucket exists with specified configuration.

    Creates bucket if it doesn't exist, updates configuration if changed.
    Idempotent operation - safe to run multiple times.

    Args:
        action: Bucket action configuration

    Returns:
        Action result with status and details
    """
    start_time = time.time()

    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        return {
            "status": "error",
            "error": "boto3 library not available. Install with: pip install boto3",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    bucket = action.get("bucket")
    region = action.get("region", "us-east-1")
    tags = action.get("tags", {})
    versioning = action.get("versioning", False)
    encryption = action.get("encryption", True)  # Enable by default for security
    public_access_block = action.get("block_public_access", True)

    if not bucket:
        return {
            "status": "error",
            "error": "'bucket' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    # Validate bucket name (S3 naming rules)
    if len(bucket) < 3 or len(bucket) > 63:
        return {
            "status": "error",
            "error": f"Bucket name must be between 3 and 63 characters, got {len(bucket)}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        s3 = boto3.client("s3", region_name=region)

        changed = False

        # Check if bucket exists
        try:
            s3.head_bucket(Bucket=bucket)
            bucket_exists = True
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "404":
                bucket_exists = False
            else:
                raise

        if not bucket_exists:
            # Create bucket
            if region == "us-east-1":
                s3.create_bucket(Bucket=bucket)
            else:
                s3.create_bucket(
                    Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region}
                )
            changed = True

            # Apply tags if provided
            if tags:
                tag_set = [{"Key": k, "Value": v} for k, v in tags.items()]
                s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": tag_set})

            # Enable versioning if requested
            if versioning:
                s3.put_bucket_versioning(
                    Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
                )

            # Enable encryption (default AES256)
            if encryption:
                s3.put_bucket_encryption(
                    Bucket=bucket,
                    ServerSideEncryptionConfiguration={
                        "Rules": [
                            {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                        ]
                    },
                )

            # Block public access by default (security best practice)
            if public_access_block:
                s3.put_public_access_block(
                    Bucket=bucket,
                    PublicAccessBlockConfiguration={
                        "BlockPublicAcls": True,
                        "IgnorePublicAcls": True,
                        "BlockPublicPolicy": True,
                        "RestrictPublicBuckets": True,
                    },
                )

        return {
            "status": "changed" if changed else "ok",
            "bucket": bucket,
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "bucket": bucket,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_prefix(action: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure S3 prefix (folder) exists."""
    start_time = time.time()

    try:
        import boto3
    except ImportError:
        return {
            "status": "error",
            "error": "boto3 not available",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    bucket = action.get("bucket")
    prefix = action.get("prefix", "").rstrip("/") + "/"

    if not bucket:
        return {"status": "error", "error": "'bucket' required", "changed": False}

    try:
        s3 = boto3.client("s3")

        # Create empty object to represent folder
        s3.put_object(Bucket=bucket, Key=prefix, Body=b"")

        return {
            "status": "changed",
            "bucket": bucket,
            "prefix": prefix,
            "duration_ms": duration_ms(start_time),
            "changed": True,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_lifecycle(action: Dict[str, Any]) -> Dict[str, Any]:
    """Configure S3 bucket lifecycle policies."""
    start_time = time.time()

    try:
        import boto3
    except ImportError:
        return {
            "status": "error",
            "error": "boto3 not available",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    bucket = action.get("bucket")
    rules = action.get("rules", [])

    if not bucket:
        return {"status": "error", "error": "'bucket' required", "changed": False}

    try:
        s3 = boto3.client("s3")

        # Put lifecycle configuration
        s3.put_bucket_lifecycle_configuration(
            Bucket=bucket, LifecycleConfiguration={"Rules": rules}
        )

        return {
            "status": "changed",
            "bucket": bucket,
            "rules_count": len(rules),
            "duration_ms": duration_ms(start_time),
            "changed": True,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_versioning(action: Dict[str, Any]) -> Dict[str, Any]:
    """Configure S3 bucket versioning."""
    start_time = time.time()

    try:
        import boto3
    except ImportError:
        return {
            "status": "error",
            "error": "boto3 not available",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    bucket = action.get("bucket")
    enabled = action.get("enabled", True)

    if not bucket:
        return {"status": "error", "error": "'bucket' required", "changed": False}

    try:
        s3 = boto3.client("s3")

        # Check current versioning status
        current = s3.get_bucket_versioning(Bucket=bucket)
        current_status = current.get("Status", "Disabled")

        desired_status = "Enabled" if enabled else "Suspended"

        if current_status != desired_status:
            s3.put_bucket_versioning(
                Bucket=bucket, VersioningConfiguration={"Status": desired_status}
            )
            changed = True
        else:
            changed = False

        return {
            "status": "changed" if changed else "ok",
            "bucket": bucket,
            "versioning": desired_status,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }
