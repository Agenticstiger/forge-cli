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

# fluid_build/providers/aws/actions/cloudwatch.py
"""
AWS CloudWatch actions for monitoring and logging.

Implements idempotent CloudWatch operations including:
- Log group creation and retention
- Log stream management
- Metric alarms
- Dashboards
"""

import time
from typing import Any, Dict

from ..util.logging import duration_ms


def ensure_log_group(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure CloudWatch Log Group exists with retention policy.

    Args:
        action: Log group configuration
            - log_group_name: Name of the log group (required)
            - retention_days: Log retention in days (default: 7)
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

    log_group_name = action.get("log_group_name")
    retention_days = action.get("retention_days", 7)
    kms_key_id = action.get("kms_key_id")
    region = action.get("region", "us-east-1")
    tags = action.get("tags", {})

    # Input validation
    if not log_group_name:
        return {
            "status": "error",
            "error": "'log_group_name' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    # Validate retention days (valid values: 1, 3, 5, 7, 14, 30, 60, 90, etc.)
    valid_retention = [1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1827, 3653]
    if retention_days and retention_days not in valid_retention:
        return {
            "status": "error",
            "error": f"'retention_days' must be one of {valid_retention}, got {retention_days}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        logs = boto3.client("logs", region_name=region)

        changed = False

        # Check if log group exists
        try:
            response = logs.describe_log_groups(logGroupNamePrefix=log_group_name, limit=1)

            log_group_exists = False
            for group in response.get("logGroups", []):
                if group["logGroupName"] == log_group_name:
                    log_group_exists = True
                    current_retention = group.get("retentionInDays")

                    # Update retention if different
                    if current_retention != retention_days:
                        logs.put_retention_policy(
                            logGroupName=log_group_name, retentionInDays=retention_days
                        )
                        changed = True
                    break

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "ResourceNotFoundException":
                log_group_exists = False
            else:
                raise

        if not log_group_exists:
            # Create log group
            create_params = {"logGroupName": log_group_name}

            if kms_key_id:
                create_params["kmsKeyId"] = kms_key_id

            if tags:
                create_params["tags"] = tags

            logs.create_log_group(**create_params)
            changed = True

            # Set retention policy
            if retention_days:
                logs.put_retention_policy(
                    logGroupName=log_group_name, retentionInDays=retention_days
                )

        return {
            "status": "changed" if changed else "ok",
            "log_group_name": log_group_name,
            "retention_days": retention_days,
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "log_group_name": log_group_name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_metric_alarm(action: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure CloudWatch Metric Alarm exists.

    Args:
        action: Alarm configuration
            - alarm_name: Name of the alarm (required)
            - metric_name: Metric to monitor (required)
            - namespace: Metric namespace (required)
            - comparison_operator: Comparison operator (required)
            - threshold: Alarm threshold (required)
            - evaluation_periods: Number of periods (default: 1)
            - region: AWS region

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

    alarm_name = action.get("alarm_name")
    metric_name = action.get("metric_name")
    namespace = action.get("namespace")
    comparison_operator = action.get("comparison_operator")
    threshold = action.get("threshold")
    evaluation_periods = action.get("evaluation_periods", 1)
    statistic = action.get("statistic", "Average")
    period = action.get("period", 300)
    region = action.get("region", "us-east-1")
    alarm_actions = action.get("alarm_actions", [])

    # Input validation
    if not all([alarm_name, metric_name, namespace, comparison_operator, threshold is not None]):
        return {
            "status": "error",
            "error": "alarm_name, metric_name, namespace, comparison_operator, and threshold are required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    # Validate comparison operator
    valid_operators = [
        "GreaterThanOrEqualToThreshold",
        "GreaterThanThreshold",
        "LessThanThreshold",
        "LessThanOrEqualToThreshold",
    ]
    if comparison_operator not in valid_operators:
        return {
            "status": "error",
            "error": f"comparison_operator must be one of {valid_operators}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        cloudwatch = boto3.client("cloudwatch", region_name=region)

        # Create or update alarm
        cloudwatch.put_metric_alarm(
            AlarmName=alarm_name,
            MetricName=metric_name,
            Namespace=namespace,
            ComparisonOperator=comparison_operator,
            Threshold=threshold,
            EvaluationPeriods=evaluation_periods,
            Statistic=statistic,
            Period=period,
            AlarmActions=alarm_actions,
        )

        return {
            "status": "changed",
            "alarm_name": alarm_name,
            "metric_name": metric_name,
            "threshold": threshold,
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": True,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "alarm_name": alarm_name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }
