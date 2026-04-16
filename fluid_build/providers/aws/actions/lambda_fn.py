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

# fluid_build/providers/aws/actions/lambda_fn.py
"""AWS Lambda function actions."""

import time
from typing import Any, Dict

from ..util.logging import duration_ms


def ensure_function(action: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure Lambda function exists with full configuration support."""
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

    function_name = action.get("name")
    runtime = action.get("runtime", "python3.11")
    handler = action.get("handler", "index.handler")
    role_arn = action.get("role_arn")
    code = action.get("code", {})
    environment = action.get("environment", {})
    timeout = action.get("timeout", 30)
    memory_size = action.get("memory_size", 128)
    layers = action.get("layers", [])
    tags = action.get("tags", {})
    region = action.get("region", "us-east-1")

    # Input validation
    if not function_name:
        return {
            "status": "error",
            "error": "'name' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    if not role_arn:
        return {
            "status": "error",
            "error": "'role_arn' is required for Lambda function",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    # Validate memory size (128 MB to 10,240 MB in 1 MB increments)
    if memory_size < 128 or memory_size > 10240:
        return {
            "status": "error",
            "error": f"'memory_size' must be between 128 and 10240 MB, got {memory_size}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    # Validate timeout (1 to 900 seconds)
    if timeout < 1 or timeout > 900:
        return {
            "status": "error",
            "error": f"'timeout' must be between 1 and 900 seconds, got {timeout}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        lambda_client = boto3.client("lambda", region_name=region)

        changed = False

        # Check if function exists
        try:
            response = lambda_client.get_function(FunctionName=function_name)
            function_exists = True

            # Update function configuration if needed
            config_changed = False
            update_config = {}

            current_config = response["Configuration"]

            if current_config.get("Timeout") != timeout:
                update_config["Timeout"] = timeout
                config_changed = True

            if current_config.get("MemorySize") != memory_size:
                update_config["MemorySize"] = memory_size
                config_changed = True

            if config_changed:
                lambda_client.update_function_configuration(
                    FunctionName=function_name, **update_config
                )
                changed = True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "ResourceNotFoundException":
                function_exists = False
            else:
                raise

        if not function_exists:
            # Create function
            function_config = {
                "FunctionName": function_name,
                "Runtime": runtime,
                "Role": role_arn,
                "Handler": handler,
                "Timeout": timeout,
                "MemorySize": memory_size,
            }

            # Add code (S3 or ZipFile)
            if code.get("s3_bucket"):
                function_config["Code"] = {
                    "S3Bucket": code["s3_bucket"],
                    "S3Key": code.get("s3_key", "lambda.zip"),
                }
            elif code.get("zip_file"):
                function_config["Code"] = {"ZipFile": code["zip_file"]}
            else:
                # Default minimal code
                function_config["Code"] = {
                    "ZipFile": b"def handler(event, context): return {'statusCode': 200}"
                }

            # Add environment variables
            if environment:
                function_config["Environment"] = {"Variables": environment}

            # Add layers
            if layers:
                function_config["Layers"] = layers

            # Add tags
            if tags:
                function_config["Tags"] = tags

            lambda_client.create_function(**function_config)
            changed = True

        return {
            "status": "changed" if changed else "ok",
            "function": function_name,
            "runtime": runtime,
            "memory_size": memory_size,
            "timeout": timeout,
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "function": function_name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def invoke_function(action: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke Lambda function."""
    start_time = time.time()
    try:
        import json

        import boto3

        function_name = action.get("name")
        payload = action.get("payload", {})

        lambda_client = boto3.client("lambda")
        response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )

        return {
            "status": "changed",
            "function": function_name,
            "status_code": response["StatusCode"],
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


def update_function_code(action: Dict[str, Any]) -> Dict[str, Any]:
    """Update Lambda function code."""
    return {"status": "ok", "message": "Code update requires S3 bucket or ZIP", "changed": False}
