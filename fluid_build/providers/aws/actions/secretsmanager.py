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

# fluid_build/providers/aws/actions/secretsmanager.py
"""
AWS Secrets Manager actions.

Implements idempotent Secrets Manager operations including:
- Secret creation and updates
- Secret rotation configuration
- Secret retrieval
"""

import time
from typing import Any, Dict

from ..util.logging import duration_ms


def ensure_secret(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure secret exists in AWS Secrets Manager.

    Args:
        action: Secret configuration
            - secret_name: Name of the secret (required)
            - secret_value: Secret value (string or dict) (required)
            - description: Secret description (optional)
            - kms_key_id: KMS key for encryption (optional)
            - region: AWS region
            - tags: Resource tags

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

    secret_name = action.get("secret_name")
    secret_value = action.get("secret_value")
    description = action.get("description", "")
    kms_key_id = action.get("kms_key_id")
    region = action.get("region", "us-east-1")
    tags = action.get("tags", {})

    # Input validation
    if not secret_name:
        return {
            "status": "error",
            "error": "'secret_name' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    if secret_value is None:
        return {
            "status": "error",
            "error": "'secret_value' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        import json

        secrets = boto3.client("secretsmanager", region_name=region)

        changed = False

        # Convert secret_value to string if it's a dict
        if isinstance(secret_value, dict):
            secret_string = json.dumps(secret_value)
        else:
            secret_string = str(secret_value)

        # Check if secret exists
        try:
            response = secrets.describe_secret(SecretId=secret_name)
            secret_exists = True
            secret_arn = response["ARN"]

            # Update secret value
            secrets.put_secret_value(SecretId=secret_name, SecretString=secret_string)
            changed = True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "ResourceNotFoundException":
                secret_exists = False
            else:
                raise

        if not secret_exists:
            # Create secret
            create_params = {
                "Name": secret_name,
                "SecretString": secret_string,
            }

            if description:
                create_params["Description"] = description

            if kms_key_id:
                create_params["KmsKeyId"] = kms_key_id

            if tags:
                tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
                create_params["Tags"] = tag_list

            response = secrets.create_secret(**create_params)
            secret_arn = response["ARN"]
            changed = True

        return {
            "status": "changed" if changed else "ok",
            "secret_name": secret_name,
            "secret_arn": secret_arn,
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "secret_name": secret_name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def get_secret_value(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Retrieve secret value from AWS Secrets Manager.

    Args:
        action: Retrieval configuration
            - secret_name: Name of the secret (required)
            - version_id: Specific version ID (optional)
            - version_stage: Version stage (optional, default: AWSCURRENT)
            - region: AWS region

    Returns:
        Action result with status and secret value
    """
    start_time = time.time()

    try:
        import boto3
    except ImportError:
        return {
            "status": "error",
            "error": "boto3 library not available. Install with: pip install boto3",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    secret_name = action.get("secret_name")
    version_id = action.get("version_id")
    version_stage = action.get("version_stage", "AWSCURRENT")
    region = action.get("region", "us-east-1")

    # Input validation
    if not secret_name:
        return {
            "status": "error",
            "error": "'secret_name' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        import json

        secrets = boto3.client("secretsmanager", region_name=region)

        get_params = {"SecretId": secret_name}

        if version_id:
            get_params["VersionId"] = version_id
        else:
            get_params["VersionStage"] = version_stage

        response = secrets.get_secret_value(**get_params)

        secret_string = response.get("SecretString")

        # Try to parse as JSON
        try:
            secret_value = json.loads(secret_string)
        except (json.JSONDecodeError, TypeError):
            secret_value = secret_string

        return {
            "status": "ok",
            "secret_name": secret_name,
            "secret_value": secret_value,
            "version_id": response.get("VersionId"),
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "secret_name": secret_name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }
