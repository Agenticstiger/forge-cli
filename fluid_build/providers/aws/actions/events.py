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

# fluid_build/providers/aws/actions/events.py
"""AWS EventBridge actions."""

import time
from typing import Any, Dict

from ..util.logging import duration_ms


def ensure_rule(action: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure EventBridge rule exists with full configuration."""
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

    rule_name = action.get("name")
    schedule = action.get("schedule")
    event_pattern = action.get("event_pattern")
    description = action.get("description", "")
    state = action.get("state", "ENABLED")
    event_bus_name = action.get("event_bus_name", "default")
    region = action.get("region", "us-east-1")
    tags = action.get("tags", {})

    # Input validation
    if not rule_name:
        return {
            "status": "error",
            "error": "'name' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    if not schedule and not event_pattern:
        return {
            "status": "error",
            "error": "Either 'schedule' or 'event_pattern' is required",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    if schedule and event_pattern:
        return {
            "status": "error",
            "error": "Cannot specify both 'schedule' and 'event_pattern'",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    # Validate state
    if state not in ["ENABLED", "DISABLED"]:
        return {
            "status": "error",
            "error": f"'state' must be ENABLED or DISABLED, got {state}",
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }

    try:
        events = boto3.client("events", region_name=region)

        changed = False

        # Build rule configuration
        rule_config = {
            "Name": rule_name,
            "State": state,
            "EventBusName": event_bus_name,
        }

        if description:
            rule_config["Description"] = description

        if schedule:
            rule_config["ScheduleExpression"] = schedule

        if event_pattern:
            import json

            if isinstance(event_pattern, dict):
                rule_config["EventPattern"] = json.dumps(event_pattern)
            else:
                rule_config["EventPattern"] = event_pattern

        # Create or update rule
        response = events.put_rule(**rule_config)
        changed = True

        # Tag the rule if tags provided
        if tags and response.get("RuleArn"):
            tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
            try:
                events.tag_resource(ResourceARN=response["RuleArn"], Tags=tag_list)
            except Exception:
                pass  # Tagging is optional

        return {
            "status": "changed" if changed else "ok",
            "rule": rule_name,
            "rule_arn": response.get("RuleArn"),
            "state": state,
            "event_bus": event_bus_name,
            "region": region,
            "duration_ms": duration_ms(start_time),
            "changed": changed,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "rule": rule_name,
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def ensure_schedule(action: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure EventBridge schedule exists."""
    return ensure_rule(action)


def put_events(action: Dict[str, Any]) -> Dict[str, Any]:
    """Send a single event onto the EventBridge bus.

    Action keys:
        source (str): Event source label (e.g. ``"fluid"``).
        detail_type (str): Event type label (e.g. ``"FluidEvent"``).
        detail (dict | str): Event payload — JSON-serialisable dict or
            already-serialised string.
        event_bus (str): Target bus name (default ``"default"``).
        region (str): AWS region (default ``"us-east-1"``).

    Returns:
        Standard FLUID action result dict. ``failed_entry_count`` from
        EventBridge surfaces in ``meta`` so callers can detect partial
        send failures even when the API call returned 200.
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

    source = action.get("source") or "fluid"
    detail_type = action.get("detail_type") or "FluidEvent"
    detail = action.get("detail") or action.get("data") or {}
    event_bus = action.get("event_bus") or "default"
    region = action.get("region") or "us-east-1"

    if isinstance(detail, dict):
        import json as _json

        detail_str = _json.dumps(detail)
    else:
        detail_str = str(detail)

    try:
        events = boto3.client("events", region_name=region)
        resp = events.put_events(
            Entries=[
                {
                    "Source": source,
                    "DetailType": detail_type,
                    "Detail": detail_str,
                    "EventBusName": event_bus,
                }
            ]
        )
        failed = resp.get("FailedEntryCount", 0)
        return {
            "status": "changed" if failed == 0 else "partial",
            "event_bus": event_bus,
            "source": source,
            "detail_type": detail_type,
            "duration_ms": duration_ms(start_time),
            "changed": failed == 0,
            "meta": {
                "failed_entry_count": failed,
                "entries": resp.get("Entries", []),
            },
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "duration_ms": duration_ms(start_time),
            "changed": False,
        }


def put_target(action: Dict[str, Any]) -> Dict[str, Any]:
    """Add target to EventBridge rule."""
    start_time = time.time()
    try:
        import boto3

        rule_name = action.get("rule")
        target_arn = action.get("target_arn")

        events = boto3.client("events")
        events.put_targets(Rule=rule_name, Targets=[{"Id": "1", "Arn": target_arn}])

        return {
            "status": "changed",
            "rule": rule_name,
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
