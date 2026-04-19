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
#
# SECURITY_REVIEW S-010: extended to cover common token shapes that were
# previously missed (JWTs, Stripe, GitHub, bare ``key: value`` assignments)
# and key-name variants that slipped past the dict-key redactor
# (``api_key``, ``client_secret`` lowercase, ``aws_access_key_id`` as the
# actual AWS standard name).
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
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}", re.IGNORECASE),
    # Snowflake passwords and connection strings
    re.compile(r'"password":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r"password=[^\s;&]+", re.IGNORECASE),
    # Generic secrets and credentials
    re.compile(r'"secret":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"token":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"credentials":\s*"[^"]*"', re.IGNORECASE),
    # API keys (new in S-010)
    re.compile(r'"api[_-]?key":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"client_secret":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"passphrase":\s*"[^"]*"', re.IGNORECASE),
    # Bare key:value / key=value assignments — both separators
    re.compile(r"(?i)\b(api[_-]?key|password|secret|token|passphrase)\s*[:=]\s*\S+"),
    # AWS keys (for external stages)
    re.compile(r'"aws_key_id":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"aws_access_key_id":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"aws_secret_key":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"aws_secret_access_key":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r"AWS_SECRET_ACCESS_KEY=[^\s;&]+", re.IGNORECASE),
    re.compile(r"AWS_ACCESS_KEY_ID=[^\s;&]+", re.IGNORECASE),
    # Azure keys (for external stages)
    re.compile(r'"azure_sas_token":\s*"[^"]*"', re.IGNORECASE),
    re.compile(r"AZURE_STORAGE_SAS_TOKEN=[^\s;&]+", re.IGNORECASE),
    # Third-party / provider token shapes (S-010)
    # JWT (three base64url parts separated by dots)
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    # Stripe restricted / live / test secret keys
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}"),
    # GitHub personal access tokens, OAuth tokens, app installation tokens
    re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
]

# Keys that should be redacted in dictionaries.
#
# S-010: added ``api_key``/``apikey``/``client_secret``/``client_id``/
# ``passphrase``/``aws_access_key_id``/``connection_url``/``jwt``/``bearer``
# — lowercase variants that the prior set missed.
SENSITIVE_KEYS = {
    "private_key",
    "private_key_id",
    "private_key_path",
    "private_key_passphrase",
    "oauth_token",
    "access_token",
    "refresh_token",
    "password",
    "passphrase",
    "secret",
    "token",
    "credentials",
    "credential",
    "auth",
    "authorization",
    "api_key",
    "apikey",
    "client_secret",
    "client_id",
    "aws_key_id",
    "aws_access_key_id",
    "aws_secret_key",
    "aws_secret_access_key",
    "azure_sas_token",
    "connection_string",
    "connection_url",
    "conn_str",
    "bearer",
    "jwt",
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
        # Only the 2-group shape (prefix + suffix) can use \1[REDACTED]\2;
        # 1-group patterns would trigger `invalid group reference 2` on sub.
        if pattern.groups >= 2:
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
