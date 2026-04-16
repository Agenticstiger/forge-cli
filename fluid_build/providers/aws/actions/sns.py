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

# fluid_build/providers/aws/actions/sns.py
"""
AWS SNS (Simple Notification Service) actions.

Implements idempotent SNS operations including:
- Topic creation and configuration
- Subscriptions (email, SMS, SQS, Lambda)
- Publishing messages
"""

import time
from typing import Any, Dict

from ..util.logging import duration_ms


def ensure_topic(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure SNS topic exists.

    Args:
        action: Topic configuration
            - topic_name: Name of the topic (required)
            - display_name: Human-readable name (optional)
            - fifo: Whether this is a FIFO topic (default: False)
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

    topic_name = action.get("topic_name")
    display_name = action.get("display_name", "")
    fifo = action.get("fifo", False)
    region = action.get("region", "us-east-1")
    tags = action.get("tags", {})

    # Input validation
    if not topic_name:
        return {
            "status": "error",
            "error": "'topic_name' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    # FIFO topics must end with .fifo
    if fifo and not topic_name.endswith(".fifo"):
        topic_name = f"{topic_name}.fifo"

    try:
        sns = boto3.client("sns", region_name=region)

        changed = False

        # Create or get topic (CreateTopic is idempotent)
        create_params = {"Name": topic_name}

        # Add attributes
        attributes = {}
        if display_name:
            attributes["DisplayName"] = display_name
        if fifo:
            attributes["FifoTopic"] = "true"

        if attributes:
            create_params["Attributes"] = attributes

        if tags:
            tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
            create_params["Tags"] = tag_list

        response = sns.create_topic(**create_params)
        topic_arn = response["TopicArn"]
        changed = True  # Always consider changed since we can't easily detect existing

        return {
            "status": "changed" if changed else "ok",
            "topic_name": topic_name,
            "topic_arn": topic_arn,
            "fifo": fifo,
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "topic_name": topic_name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_subscription(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure SNS subscription exists.

    Args:
        action: Subscription configuration
            - topic_arn: ARN of the topic (required)
            - protocol: Protocol (email, sms, sqs, lambda, etc.) (required)
            - endpoint: Endpoint (email address, phone, queue ARN, etc.) (required)
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

    topic_arn = action.get("topic_arn")
    protocol = action.get("protocol")
    endpoint = action.get("endpoint")
    region = action.get("region", "us-east-1")

    # Input validation
    if not all([topic_arn, protocol, endpoint]):
        return {
            "status": "error",
            "error": "'topic_arn', 'protocol', and 'endpoint' are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    # Validate protocol
    valid_protocols = [
        "http",
        "https",
        "email",
        "email-json",
        "sms",
        "sqs",
        "application",
        "lambda",
    ]
    if protocol not in valid_protocols:
        return {
            "status": "error",
            "error": f"'protocol' must be one of {valid_protocols}, got {protocol}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        sns = boto3.client("sns", region_name=region)

        # Subscribe (idempotent if subscription already exists)
        response = sns.subscribe(
            TopicArn=topic_arn, Protocol=protocol, Endpoint=endpoint, ReturnSubscriptionArn=True
        )

        subscription_arn = response.get("SubscriptionArn")

        return {
            "status": "changed",
            "topic_arn": topic_arn,
            "subscription_arn": subscription_arn,
            "protocol": protocol,
            "endpoint": endpoint,
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": True,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "topic_arn": topic_arn,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def publish_message(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Publish message to SNS topic.

    Args:
        action: Message configuration
            - topic_arn: ARN of the topic (required)
            - message: Message content (required)
            - subject: Message subject (optional)
            - message_group_id: FIFO group ID (required for FIFO topics)
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

    topic_arn = action.get("topic_arn")
    message = action.get("message")
    subject = action.get("subject")
    message_group_id = action.get("message_group_id")
    region = action.get("region", "us-east-1")

    # Input validation
    if not topic_arn:
        return {
            "status": "error",
            "error": "'topic_arn' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    if not message:
        return {
            "status": "error",
            "error": "'message' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        sns = boto3.client("sns", region_name=region)

        publish_params = {
            "TopicArn": topic_arn,
            "Message": message,
        }

        if subject:
            publish_params["Subject"] = subject

        if message_group_id:
            publish_params["MessageGroupId"] = message_group_id

        response = sns.publish(**publish_params)

        return {
            "status": "changed",
            "message_id": response["MessageId"],
            "topic_arn": topic_arn,
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": True,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "topic_arn": topic_arn,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }
