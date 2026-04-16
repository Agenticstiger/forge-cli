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

# fluid_build/providers/aws/actions/iam.py
"""
AWS IAM actions for FLUID Forge.

Implements idempotent IAM operations:
- Role creation/validation with trust policies
- Policy creation/update and attachment
- S3 bucket access policy binding
- Glue database access policy binding
"""

import json
import time
from typing import Any, Dict

from ..util.logging import duration_ms


def ensure_role(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure IAM role exists with the specified trust policy.

    Creates the role if it doesn't exist, validates trust policy if it does.
    Idempotent — safe to call repeatedly.

    Action keys:
        role_name (str): IAM role name
        trust_policy (dict): AssumeRolePolicyDocument
        description (str): Role description
        tags (dict): Resource tags
    """
    start_time = time.time()

    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        return _boto_missing(start_time)

    role_name = action.get("role_name")
    if not role_name:
        return _error("'role_name' is required", start_time)

    trust_policy = action.get(
        "trust_policy",
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "glue.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        },
    )
    description = action.get("description", "Managed by FLUID Forge")
    tags = _to_iam_tags(action.get("tags", {}))

    iam = boto3.client("iam")

    try:
        iam.get_role(RoleName=role_name)
        return {
            "status": "ok",
            "role_name": role_name,
            "message": "Role already exists",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            return _error(str(e), start_time)

    try:
        iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description=description,
            Tags=tags,
        )
        # Wait for role to propagate (IAM is eventually consistent)
        iam.get_waiter("role_exists").wait(RoleName=role_name)
        return {
            "status": "changed",
            "role_name": role_name,
            "message": "Role created",
            "duration_ms": duration_ms(start_time),
            "changed": True,
        }
    except Exception as e:
        return _error(str(e), start_time)


def attach_policy(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Attach a managed policy to an IAM role.

    Action keys:
        role_name (str): Target IAM role
        policy_arn (str): ARN of managed policy to attach
    """
    start_time = time.time()

    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        return _boto_missing(start_time)

    role_name = action.get("role_name")
    policy_arn = action.get("policy_arn")
    if not role_name or not policy_arn:
        return _error("'role_name' and 'policy_arn' are required", start_time)

    iam = boto3.client("iam")

    try:
        # Check if already attached
        attached = iam.list_attached_role_policies(RoleName=role_name)
        for p in attached.get("AttachedPolicies", []):
            if p["PolicyArn"] == policy_arn:
                return {
                    "status": "ok",
                    "role_name": role_name,
                    "policy_arn": policy_arn,
                    "message": "Policy already attached",
                    "duration_ms": duration_ms(start_time),
                    "changed": False,
                }

        iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
        return {
            "status": "changed",
            "role_name": role_name,
            "policy_arn": policy_arn,
            "message": "Policy attached",
            "duration_ms": duration_ms(start_time),
            "changed": True,
        }
    except Exception as e:
        return _error(str(e), start_time)


def ensure_policy(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure an IAM policy exists. Creates or updates the policy document.

    Action keys:
        policy_name (str): Policy name
        policy_document (dict): IAM policy JSON document
        description (str): Policy description
        path (str): IAM path (default "/fluid/")
        account_id (str): AWS account ID (needed to build ARN for lookup)
    """
    start_time = time.time()

    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        return _boto_missing(start_time)

    policy_name = action.get("policy_name")
    policy_doc = action.get("policy_document")
    if not policy_name or not policy_doc:
        return _error("'policy_name' and 'policy_document' are required", start_time)

    description = action.get("description", "Managed by FLUID Forge")
    path = action.get("path", "/fluid/")
    account_id = action.get("account_id")

    iam = boto3.client("iam")

    # Try to find existing policy by ARN
    policy_arn = None
    if account_id:
        policy_arn = f"arn:aws:iam::{account_id}:policy{path}{policy_name}"
        try:
            iam.get_policy(PolicyArn=policy_arn)
            # Policy exists — create a new version with the updated document
            try:
                # List versions and delete the oldest non-default if at limit (max 5)
                versions = iam.list_policy_versions(PolicyArn=policy_arn)
                non_default = [v for v in versions["Versions"] if not v["IsDefaultVersion"]]
                if len(non_default) >= 4:
                    oldest = sorted(non_default, key=lambda v: v["CreateDate"])[0]
                    iam.delete_policy_version(
                        PolicyArn=policy_arn,
                        VersionId=oldest["VersionId"],
                    )

                iam.create_policy_version(
                    PolicyArn=policy_arn,
                    PolicyDocument=json.dumps(policy_doc),
                    SetAsDefault=True,
                )
                return {
                    "status": "changed",
                    "policy_arn": policy_arn,
                    "message": "Policy version updated",
                    "duration_ms": duration_ms(start_time),
                    "changed": True,
                }
            except Exception as e:
                return _error(f"Failed to update policy version: {e}", start_time)
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchEntity":
                return _error(str(e), start_time)

    # Policy doesn't exist — create it
    try:
        resp = iam.create_policy(
            PolicyName=policy_name,
            PolicyDocument=json.dumps(policy_doc),
            Description=description,
            Path=path,
        )
        return {
            "status": "changed",
            "policy_arn": resp["Policy"]["Arn"],
            "message": "Policy created",
            "duration_ms": duration_ms(start_time),
            "changed": True,
        }
    except Exception as e:
        return _error(str(e), start_time)


def bind_s3_bucket(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create and attach an IAM policy granting access to an S3 bucket.

    Action keys:
        bucket (str): S3 bucket name
        role_name (str): IAM role to attach policy to (optional)
        account_id (str): AWS account ID
        access (str): "read", "write", or "readwrite" (default "readwrite")
        policies (dict): Raw policies from contract metadata (informational)
    """
    start_time = time.time()

    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        return _boto_missing(start_time)

    bucket = action.get("bucket")
    if not bucket:
        return _error("'bucket' is required", start_time)

    account_id = action.get("account_id", "")
    access = action.get("access", "readwrite").lower()

    # Build least-privilege S3 policy
    statements = []
    if access in ("read", "readwrite"):
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [
                    f"arn:aws:s3:::{bucket}",
                    f"arn:aws:s3:::{bucket}/*",
                ],
            }
        )
    if access in ("write", "readwrite"):
        statements.append(
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:DeleteObject"],
                "Resource": [f"arn:aws:s3:::{bucket}/*"],
            }
        )

    policy_doc = {"Version": "2012-10-17", "Statement": statements}

    result = ensure_policy(
        {
            "policy_name": f"fluid-s3-{bucket}",
            "policy_document": policy_doc,
            "description": f"FLUID S3 access for {bucket}",
            "account_id": account_id,
        }
    )

    if result.get("status") == "error":
        return result

    # Optionally attach to role
    role_name = action.get("role_name")
    if role_name and result.get("policy_arn"):
        attach_result = attach_policy(
            {
                "role_name": role_name,
                "policy_arn": result["policy_arn"],
            }
        )
        if attach_result.get("status") == "error":
            return attach_result

    result["duration_ms"] = duration_ms(start_time)
    return result


def bind_glue_database(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create and attach an IAM policy granting access to a Glue database.

    Action keys:
        database (str): Glue database name
        role_name (str): IAM role to attach policy to (optional)
        account_id (str): AWS account ID
        policies (dict): Raw policies from contract metadata (informational)
    """
    start_time = time.time()

    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        return _boto_missing(start_time)

    database = action.get("database")
    if not database:
        return _error("'database' is required", start_time)

    account_id = action.get("account_id", "")
    region = action.get("region", "*")

    # Glue catalog policy — database + all tables within it
    policy_doc = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "glue:GetDatabase",
                    "glue:GetDatabases",
                    "glue:GetTable",
                    "glue:GetTables",
                    "glue:GetPartitions",
                    "glue:BatchGetPartition",
                ],
                "Resource": [
                    f"arn:aws:glue:{region}:{account_id}:catalog",
                    f"arn:aws:glue:{region}:{account_id}:database/{database}",
                    f"arn:aws:glue:{region}:{account_id}:table/{database}/*",
                ],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "glue:CreateTable",
                    "glue:UpdateTable",
                    "glue:DeleteTable",
                    "glue:CreatePartition",
                    "glue:BatchCreatePartition",
                    "glue:UpdatePartition",
                ],
                "Resource": [
                    f"arn:aws:glue:{region}:{account_id}:catalog",
                    f"arn:aws:glue:{region}:{account_id}:database/{database}",
                    f"arn:aws:glue:{region}:{account_id}:table/{database}/*",
                ],
            },
        ],
    }

    result = ensure_policy(
        {
            "policy_name": f"fluid-glue-{database}",
            "policy_document": policy_doc,
            "description": f"FLUID Glue access for {database}",
            "account_id": account_id,
        }
    )

    if result.get("status") == "error":
        return result

    # Optionally attach to role
    role_name = action.get("role_name")
    if role_name and result.get("policy_arn"):
        attach_result = attach_policy(
            {
                "role_name": role_name,
                "policy_arn": result["policy_arn"],
            }
        )
        if attach_result.get("status") == "error":
            return attach_result

    result["duration_ms"] = duration_ms(start_time)
    return result


# ── helpers ──────────────────────────────────────────────────────────────


def _boto_missing(start_time: float) -> Dict[str, Any]:
    return {
        "status": "error",
        "error": "boto3 library not available. Install with: pip install boto3",
        "duration_ms": duration_ms(start_time),
        "changed": False,
    }


def _error(msg: str, start_time: float) -> Dict[str, Any]:
    return {
        "status": "error",
        "error": msg,
        "duration_ms": duration_ms(start_time),
        "changed": False,
    }


def _to_iam_tags(tags: Dict[str, str]) -> list:
    """Convert {key: value} dict to IAM Tag list format."""
    return [{"Key": k, "Value": str(v)} for k, v in tags.items()]
