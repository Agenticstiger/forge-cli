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

# fluid_build/providers/local/util/logging.py
"""
Logging utilities for Local Provider.

Provides structured logging with secret redaction and
consistent event formatting. Adapted from GCP provider.
"""

import re
from typing import Any, Dict, List

# Patterns for sensitive data that should be redacted
SENSITIVE_PATTERNS = [
    # Service account keys and credentials
    re.compile(r'"private_key":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"private_key_id":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"client_secret":\s*"[^"]*"', re.IGNORECASE),
    # Authorization headers and tokens
    re.compile(r"Authorization:\s*Bearer\s+[^\s]+", re.IGNORECASE),
    re.compile(r'"access_token":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"refresh_token":\s*"[^"]*"', re.IGNORECASE),
    # API keys
    re.compile(r'"api_key":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r"key=AIza[A-Za-z0-9_-]+"),  # Google API keys
    re.compile(r"key=AKIA[A-Za-z0-9]+"),  # AWS access keys
    # Database passwords and connection strings
    re.compile(r'"password":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r"password=[^\s;&]+", re.IGNORECASE),
    re.compile(r"postgresql://[^:]+:[^@]+@", re.IGNORECASE),  # postgres://user:pass@host
    re.compile(r"mysql://[^:]+:[^@]+@", re.IGNORECASE),  # mysql://user:pass@host
    # Generic secrets
    re.compile(r'"secret":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"token":\s*"[^"]*"', re.IGNORECASE),
    # File paths containing sensitive data
    re.compile(r"(\.pem|\.key|\.p12|\.pfx)\b", re.IGNORECASE),
]

# Keys that should be redacted in dictionaries
SENSITIVE_KEYS = {
    "private_key",
    "private_key_id",
    "client_secret",
    "access_token",
    "refresh_token",
    "api_key",
    "password",
    "secret",
    "token",
    "credentials",
    "auth",
    "authorization",
    "aws_secret_access_key",
    "aws_session_token",
    "gcp_credentials",
    "service_account_key",
}


def redact_string(text: str) -> str:
    """
    Redact sensitive information from a string.

    Args:
        text: Input string that may contain sensitive data

    Returns:
        String with sensitive data replaced with [REDACTED]
    """
    if not isinstance(text, str):
        return text

    redacted = text

    for pattern in SENSITIVE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)

    return redacted


def redact_dict(data: Dict[str, Any], max_depth: int = 10) -> Dict[str, Any]:
    """
    Recursively redact sensitive information from a dictionary.

    Args:
        data: Dictionary that may contain sensitive data
        max_depth: Maximum recursion depth to prevent infinite loops

    Returns:
        Dictionary with sensitive values redacted
    """
    if max_depth <= 0:
        return {"error": "max_redaction_depth_exceeded"}

    if not isinstance(data, dict):
        return data

    redacted = {}

    for key, value in data.items():
        # Check if key indicates sensitive data
        if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
            redacted[key] = "[REDACTED]"
        elif isinstance(value, dict):
            redacted[key] = redact_dict(value, max_depth - 1)
        elif isinstance(value, list):
            redacted[key] = redact_list(value, max_depth - 1)
        elif isinstance(value, str):
            redacted[key] = redact_string(value)
        else:
            redacted[key] = value

    return redacted


def redact_list(data: List[Any], max_depth: int = 10) -> List[Any]:
    """
    Recursively redact sensitive information from a list.

    Args:
        data: List that may contain sensitive data
        max_depth: Maximum recursion depth

    Returns:
        List with sensitive values redacted
    """
    if max_depth <= 0:
        return ["max_redaction_depth_exceeded"]

    if not isinstance(data, list):
        return data

    redacted = []

    for item in data:
        if isinstance(item, dict):
            redacted.append(redact_dict(item, max_depth - 1))
        elif isinstance(item, list):
            redacted.append(redact_list(item, max_depth - 1))
        elif isinstance(item, str):
            redacted.append(redact_string(item))
        else:
            redacted.append(item)

    return redacted


def format_event(event: str, level: str = "INFO", **kwargs: Any) -> Dict[str, Any]:
    """
    Format a structured log event.

    Args:
        event: Event name/type
        level: Log level (INFO, DEBUG, ERROR, etc.)
        **kwargs: Additional event data

    Returns:
        Structured log event dictionary
    """
    from datetime import datetime, timezone

    log_event = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "level": level.upper(),
        "event": event,
        "provider": "local",
    }

    # Add additional fields, redacting sensitive data
    if kwargs:
        log_event.update(redact_dict(kwargs))

    return log_event


def safe_json_dumps(data: Any, indent: int = None) -> str:
    """
    Safely serialize data to JSON with redaction.

    Args:
        data: Data to serialize
        indent: JSON indentation

    Returns:
        JSON string with sensitive data redacted
    """
    import json

    try:
        if isinstance(data, dict):
            redacted_data = redact_dict(data)
        elif isinstance(data, list):
            redacted_data = redact_list(data)
        else:
            redacted_data = data

        return json.dumps(redacted_data, indent=indent, default=str)

    except (TypeError, ValueError) as e:
        return json.dumps(
            {
                "error": "json_serialization_failed",
                "reason": str(e),
                "data_type": type(data).__name__,
            },
            indent=indent,
        )


def create_operation_logger(operation: str, **context: Any):
    """
    Create a logger for a specific operation with context.

    Args:
        operation: Operation name (e.g., "local.load_csv")
        **context: Additional context to include in all log messages

    Returns:
        Logger function that includes operation context
    """

    def log(event: str, level: str = "INFO", **kwargs: Any):
        """Log an event with operation context."""
        log_data = {"operation": operation, **context, **kwargs}
        return format_event(event, level, **log_data)

    return log


def duration_ms(start_time: float) -> int:
    """
    Calculate duration in milliseconds from start time.

    Args:
        start_time: Start time from time.time()

    Returns:
        Duration in milliseconds
    """
    import time

    return int((time.time() - start_time) * 1000)


def truncate_large_data(data: Any, max_size: int = 1000) -> Any:
    """
    Truncate large data structures for logging.

    Prevents log bloat when dealing with large datasets.

    Args:
        data: Data to potentially truncate
        max_size: Maximum size threshold

    Returns:
        Original data or truncated representation
    """
    if isinstance(data, (str, bytes)):
        if len(data) > max_size:
            return f"{data[:max_size]}... [truncated, original length: {len(data)}]"
    elif isinstance(data, (list, tuple)):
        if len(data) > max_size:
            return [*data[:max_size], f"... [truncated, original length: {len(data)}]"]
    elif isinstance(data, dict):
        if len(str(data)) > max_size * 10:  # Rough estimate for dict size
            # Return a summary instead
            return {
                "_truncated": True,
                "_original_key_count": len(data),
                "_sample_keys": list(data.keys())[:10],
            }

    return data


def redact_sql(sql: str) -> str:
    """
    Redact sensitive values from SQL statements.

    Useful for logging SQL without exposing credentials or PII.

    Args:
        sql: SQL statement that may contain sensitive data

    Returns:
        SQL with sensitive values redacted
    """
    # Redact string literals that might contain secrets
    # This is a simple heuristic - for production, use more sophisticated parsing
    redacted = re.sub(
        r"'[^']*(?:password|secret|token|key)[^']*'", "'[REDACTED]'", sql, flags=re.IGNORECASE
    )

    return redact_string(redacted)
