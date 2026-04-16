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

# fluid_build/providers/aws/util/logging.py
"""
AWS-specific logging utilities.
"""

from typing import Any, Dict


def format_event(event: str, **kwargs: Any) -> str:
    """
    Format a structured log event.

    Args:
        event: Event name
        **kwargs: Event attributes

    Returns:
        Formatted event string
    """
    parts = [f"{event}"]
    for key, value in kwargs.items():
        parts.append(f"{key}={value}")
    return " | ".join(parts)


def redact_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Redact sensitive information from dictionary.

    Args:
        data: Dictionary potentially containing secrets

    Returns:
        Dictionary with sensitive values redacted
    """
    sensitive_keys = {
        "password",
        "secret",
        "key",
        "token",
        "credential",
        "api_key",
        "access_key",
        "secret_key",
        "private_key",
    }

    redacted = {}
    for key, value in data.items():
        key_lower = key.lower()
        if any(sensitive in key_lower for sensitive in sensitive_keys):
            redacted[key] = "***REDACTED***"
        elif isinstance(value, dict):
            redacted[key] = redact_dict(value)
        else:
            redacted[key] = value

    return redacted


def duration_ms(start_time: float) -> int:
    """
    Calculate duration in milliseconds from start time.

    Args:
        start_time: Start timestamp from time.time()

    Returns:
        Duration in milliseconds
    """
    import time

    return int((time.time() - start_time) * 1000)
