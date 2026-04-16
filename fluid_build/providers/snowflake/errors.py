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

# fluid_build/providers/snowflake/errors.py
"""
Snowflake error categorization for intelligent retry logic.

Categorizes Snowflake errors into retryable, non-retryable, and transient
to enable smart retry policies aligned with FLUID 0.7.1 error handling.
"""

from enum import Enum
from typing import Optional


class SnowflakeErrorCategory(Enum):
    """Error categories for retry logic."""

    RETRYABLE = "retryable"  # Temporary issues - retry immediately
    NON_RETRYABLE = "non_retryable"  # Permanent failures - don't retry
    TRANSIENT = "transient"  # May be temporary - retry with caution
    RATE_LIMIT = "rate_limit"  # Rate limited - retry with backoff


class SnowflakeProviderError(Exception):
    """Base exception for Snowflake provider errors."""

    def __init__(
        self,
        message: str,
        category: SnowflakeErrorCategory = None,
        snowflake_errno: Optional[int] = None,
    ):
        super().__init__(message)
        self.category = category or SnowflakeErrorCategory.TRANSIENT
        self.snowflake_errno = snowflake_errno


class PermissionDeniedError(SnowflakeProviderError):
    """Permission denied - non-retryable."""

    def __init__(self, message: str, snowflake_errno: Optional[int] = None):
        super().__init__(message, SnowflakeErrorCategory.NON_RETRYABLE, snowflake_errno)


class ObjectNotFoundError(SnowflakeProviderError):
    """Object does not exist - non-retryable."""

    def __init__(self, message: str, snowflake_errno: Optional[int] = None):
        super().__init__(message, SnowflakeErrorCategory.NON_RETRYABLE, snowflake_errno)


class CompilationError(SnowflakeProviderError):
    """SQL compilation error - non-retryable."""

    def __init__(self, message: str, snowflake_errno: Optional[int] = None):
        super().__init__(message, SnowflakeErrorCategory.NON_RETRYABLE, snowflake_errno)


class NetworkError(SnowflakeProviderError):
    """Network error - retryable."""

    def __init__(self, message: str, snowflake_errno: Optional[int] = None):
        super().__init__(message, SnowflakeErrorCategory.RETRYABLE, snowflake_errno)


class TimeoutError(SnowflakeProviderError):
    """Timeout error - retryable."""

    def __init__(self, message: str, snowflake_errno: Optional[int] = None):
        super().__init__(message, SnowflakeErrorCategory.RETRYABLE, snowflake_errno)


class RateLimitError(SnowflakeProviderError):
    """Rate limit exceeded - retryable with backoff."""

    def __init__(self, message: str, snowflake_errno: Optional[int] = None):
        super().__init__(message, SnowflakeErrorCategory.RATE_LIMIT, snowflake_errno)


def categorize_snowflake_error(exc: Exception) -> SnowflakeErrorCategory:
    """
    Categorize Snowflake exception for intelligent retry logic.

    Snowflake Error Code Reference:
    https://docs.snowflake.com/en/user-guide/odbc-error-codes.html

    Args:
        exc: Exception from snowflake-connector-python

    Returns:
        Error category for retry decision
    """
    # If already categorized, return it
    if isinstance(exc, SnowflakeProviderError):
        return exc.category

    # Extract Snowflake error code
    error_code = getattr(exc, "errno", None)
    error_msg = str(exc).lower()

    # Non-retryable errors (permanent failures)
    non_retryable_codes = {
        1003,  # Compilation error
        2003,  # Object does not exist
        2043,  # Access control error
        3001,  # Invalid identifier
        3003,  # Schema does not exist
        100038,  # Invalid warehouse
        100040,  # Invalid database
        100041,  # Invalid schema
        100168,  # Invalid role
    }

    if error_code in non_retryable_codes:
        return SnowflakeErrorCategory.NON_RETRYABLE

    # Check error message for non-retryable patterns
    non_retryable_patterns = [
        "permission denied",
        "access control",
        "does not exist",
        "invalid identifier",
        "compilation error",
        "sql compilation error",
        "syntax error",
        "object does not exist",
    ]

    if any(pattern in error_msg for pattern in non_retryable_patterns):
        return SnowflakeErrorCategory.NON_RETRYABLE

    # Retryable errors (temporary network/system issues)
    retryable_codes = {
        253012,  # Network error
        390189,  # Connection timeout
        390144,  # Connection closed
        604,  # Rate limit exceeded (also rate_limit category)
    }

    if error_code in retryable_codes:
        if error_code == 604:
            return SnowflakeErrorCategory.RATE_LIMIT
        return SnowflakeErrorCategory.RETRYABLE

    # Check error message for retryable patterns
    retryable_patterns = [
        "network",
        "timeout",
        "connection closed",
        "connection lost",
        "connection reset",
        "temporary failure",
    ]

    if any(pattern in error_msg for pattern in retryable_patterns):
        return SnowflakeErrorCategory.RETRYABLE

    # Rate limit patterns
    if "rate limit" in error_msg or "throttled" in error_msg:
        return SnowflakeErrorCategory.RATE_LIMIT

    # Default to transient (retry with caution)
    return SnowflakeErrorCategory.TRANSIENT


def should_retry(exc: Exception, attempt: int, max_retries: int = 3) -> bool:
    """
    Determine if an error should be retried.

    Args:
        exc: Exception to evaluate
        attempt: Current attempt number (0-based)
        max_retries: Maximum retry attempts

    Returns:
        True if should retry, False otherwise
    """
    if attempt >= max_retries:
        return False

    category = categorize_snowflake_error(exc)

    # Never retry non-retryable errors
    if category == SnowflakeErrorCategory.NON_RETRYABLE:
        return False

    # Always retry retryable errors (up to max)
    if category == SnowflakeErrorCategory.RETRYABLE:
        return True

    # Retry rate limits with exponential backoff
    if category == SnowflakeErrorCategory.RATE_LIMIT:
        return True

    # Retry transient errors cautiously (fewer attempts)
    if category == SnowflakeErrorCategory.TRANSIENT:
        return attempt < max(1, max_retries // 2)

    return False


def get_retry_delay(exc: Exception, attempt: int, base_delay: float = 1.0) -> float:
    """
    Calculate retry delay based on error category and attempt number.

    Args:
        exc: Exception being retried
        attempt: Current attempt number (0-based)
        base_delay: Base delay in seconds

    Returns:
        Delay in seconds before retry
    """
    category = categorize_snowflake_error(exc)

    # Rate limit: aggressive exponential backoff
    if category == SnowflakeErrorCategory.RATE_LIMIT:
        return base_delay * (3**attempt)  # 1s, 3s, 9s, 27s

    # Retryable: standard exponential backoff
    if category == SnowflakeErrorCategory.RETRYABLE:
        return base_delay * (2**attempt)  # 1s, 2s, 4s, 8s

    # Transient: linear backoff
    if category == SnowflakeErrorCategory.TRANSIENT:
        return base_delay * (attempt + 1)  # 1s, 2s, 3s

    # Default
    return base_delay
