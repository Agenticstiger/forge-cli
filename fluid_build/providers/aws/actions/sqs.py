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

# fluid_build/providers/aws/actions/sqs.py
"""
AWS SQS (Simple Queue Service) actions.

Implements idempotent SQS operations including:
- Queue creation and configuration
- Dead letter queues
- FIFO queues
- Message sending
"""

import time
from typing import Any, Dict

from ..util.logging import duration_ms


def ensure_queue(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure SQS queue exists with specified configuration.

    Args:
        action: Queue configuration
            - queue_name: Name of the queue (required)
            - fifo: Whether this is a FIFO queue (default: False)
            - visibility_timeout: Message visibility timeout in seconds (default: 30)
            - message_retention_period: Message retention in seconds (default: 345600 = 4 days)
            - receive_wait_time: Long polling wait time (default: 0)
            - max_message_size: Max message size in bytes (default: 262144 = 256KB)
            - dead_letter_target_arn: DLQ target ARN (optional)
            - max_receive_count: Max receives before DLQ (default: 5)
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

    queue_name = action.get("queue_name")
    fifo = action.get("fifo", False)
    visibility_timeout = action.get("visibility_timeout", 30)
    message_retention_period = action.get("message_retention_period", 345600)
    receive_wait_time = action.get("receive_wait_time", 0)
    max_message_size = action.get("max_message_size", 262144)
    dead_letter_target_arn = action.get("dead_letter_target_arn")
    max_receive_count = action.get("max_receive_count", 5)
    region = action.get("region", "us-east-1")
    tags = action.get("tags", {})

    # Input validation
    if not queue_name:
        return {
            "status": "error",
            "error": "'queue_name' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    # FIFO queues must end with .fifo
    if fifo and not queue_name.endswith(".fifo"):
        queue_name = f"{queue_name}.fifo"

    # Validate visibility timeout (0 to 43200 seconds = 12 hours)
    if visibility_timeout < 0 or visibility_timeout > 43200:
        return {
            "status": "error",
            "error": f"'visibility_timeout' must be between 0 and 43200 seconds, got {visibility_timeout}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    # Validate message retention (60 to 1209600 seconds = 14 days)
    if message_retention_period < 60 or message_retention_period > 1209600:
        return {
            "status": "error",
            "error": f"'message_retention_period' must be between 60 and 1209600 seconds, got {message_retention_period}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        sqs = boto3.client("sqs", region_name=region)

        changed = False

        # Check if queue exists
        try:
            response = sqs.get_queue_url(QueueName=queue_name)
            queue_url = response["QueueUrl"]
            queue_exists = True

            # Get current attributes
            attrs_response = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["All"])
            current_attrs = attrs_response["Attributes"]

            # Check if attributes need updating
            update_attrs = {}

            if int(current_attrs.get("VisibilityTimeout", 30)) != visibility_timeout:
                update_attrs["VisibilityTimeout"] = str(visibility_timeout)

            if int(current_attrs.get("MessageRetentionPeriod", 345600)) != message_retention_period:
                update_attrs["MessageRetentionPeriod"] = str(message_retention_period)

            if update_attrs:
                sqs.set_queue_attributes(QueueUrl=queue_url, Attributes=update_attrs)
                changed = True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "AWS.SimpleQueueService.NonExistentQueue":
                queue_exists = False
            else:
                raise

        if not queue_exists:
            # Create queue
            attributes = {
                "VisibilityTimeout": str(visibility_timeout),
                "MessageRetentionPeriod": str(message_retention_period),
                "ReceiveMessageWaitTimeSeconds": str(receive_wait_time),
                "MaximumMessageSize": str(max_message_size),
            }

            # Add FIFO-specific attributes
            if fifo:
                attributes["FifoQueue"] = "true"
                attributes["ContentBasedDeduplication"] = "true"

            # Add dead letter queue configuration
            if dead_letter_target_arn:
                import json

                attributes["RedrivePolicy"] = json.dumps(
                    {
                        "deadLetterTargetArn": dead_letter_target_arn,
                        "maxReceiveCount": max_receive_count,
                    }
                )

            create_params = {
                "QueueName": queue_name,
                "Attributes": attributes,
            }

            if tags:
                create_params["tags"] = tags

            response = sqs.create_queue(**create_params)
            queue_url = response["QueueUrl"]
            changed = True

        # Get queue ARN
        attrs = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])
        queue_arn = attrs["Attributes"]["QueueArn"]

        return {
            "status": "changed" if changed else "ok",
            "queue_name": queue_name,
            "queue_url": queue_url,
            "queue_arn": queue_arn,
            "fifo": fifo,
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "queue_name": queue_name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def send_message(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Send message to SQS queue.

    Args:
        action: Message configuration
            - queue_url: URL of the queue (required)
            - message_body: Message content (required)
            - message_group_id: FIFO group ID (required for FIFO)
            - message_deduplication_id: FIFO deduplication ID (optional)
            - delay_seconds: Delay before message is available (default: 0)
            - region: AWS region

    Returns:
        Action result with status and details
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

    queue_url = action.get("queue_url")
    message_body = action.get("message_body")
    message_group_id = action.get("message_group_id")
    message_deduplication_id = action.get("message_deduplication_id")
    delay_seconds = action.get("delay_seconds", 0)
    region = action.get("region", "us-east-1")

    # Input validation
    if not queue_url:
        return {
            "status": "error",
            "error": "'queue_url' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    if not message_body:
        return {
            "status": "error",
            "error": "'message_body' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        sqs = boto3.client("sqs", region_name=region)

        send_params = {
            "QueueUrl": queue_url,
            "MessageBody": message_body,
        }

        if delay_seconds:
            send_params["DelaySeconds"] = delay_seconds

        if message_group_id:
            send_params["MessageGroupId"] = message_group_id

        if message_deduplication_id:
            send_params["MessageDeduplicationId"] = message_deduplication_id

        response = sqs.send_message(**send_params)

        return {
            "status": "changed",
            "message_id": response["MessageId"],
            "region": region,
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
