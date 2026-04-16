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

# fluid_build/providers/snowflake/util/logging.py
"""
Logging utilities for Snowflake provider.

Provides structured logging with comprehensive secret redaction and
consistent event formatting. Enhanced to match GCP provider standards.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

# Patterns for sensitive data that should be redacted
SENSITIVE_PATTERNS = [
    # Connection strings (protocol://user:password@host)
    re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^:]+:)[^@]+(@)", re.IGNORECASE),
    # Private keys and credentials
    re.compile(r'"private_key":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"private_key_id":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"private_key_path":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"private_key_passphrase":\s*"[^"]*"', re.IGNORECASE),
    # OAuth tokens and access tokens
    re.compile(r'"oauth_token":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"access_token":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"refresh_token":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r"Authorization:\s*Bearer\s+[^\s]+", re.IGNORECASE),
    # Snowflake passwords and connection strings
    re.compile(r'"password":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r"password=[^\s;&]+", re.IGNORECASE),
    # Generic secrets and credentials
    re.compile(r'"secret":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"token":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"credentials":\s*"[^"]*"', re.IGNORECASE),
    # AWS keys (for external stages)
    re.compile(r'"aws_key_id":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"aws_secret_key":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r"AWS_SECRET_ACCESS_KEY=[^\s;&]+", re.IGNORECASE),
    # Azure keys (for external stages)
    re.compile(r'"azure_sas_token":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r"AZURE_STORAGE_SAS_TOKEN=[^\s;&]+", re.IGNORECASE),
]

# Keys that should be redacted in dictionaries
SENSITIVE_KEYS = {
    "private_key",
    "private_key_id",
    "private_key_path",
    "private_key_passphrase",
    "oauth_token",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "token",
    "credentials",
    "credential",
    "auth",
    "authorization",
    "aws_key_id",
    "aws_secret_key",
    "azure_sas_token",
    "connection_string",
    "conn_str",
}


def format_event(event: str, **kwargs: Any) -> str:
    """Format log event with key-value pairs."""
    parts = [f"event={event}"]
    for key, value in kwargs.items():
        parts.append(f"{key}={value}")
    return " ".join(parts)


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
        # For connection string pattern (has groups), use substitution with groups
        if pattern.groups > 0:
            redacted = pattern.sub(r"\1[REDACTED]\2", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)

    return redacted


def redact_dict(data: Dict[str, Any], max_depth: int = 10) -> Dict[str, Any]:
    """
    Recursively redact sensitive information from a dictionary.

    Enhanced version with comprehensive pattern matching and
    protection against deeply nested structures.

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


def duration_ms(start_time: float, end_time: float) -> int:
    """Calculate duration in milliseconds."""
    return int((end_time - start_time) * 1000)
