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

# fluid_build/providers/snowflake/util/retry.py
"""
Retry logic for Snowflake operations with intelligent error categorization.

Enhanced with error categorization from errors.py for smarter retry decisions.
Integrated with circuit breaker pattern for cascading failure prevention.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

# Try to import enhanced error categorization
try:
    from ..errors import (
        SnowflakeErrorCategory,
        categorize_snowflake_error,
    )
    from ..errors import (
        get_retry_delay as get_categorized_delay,
    )
    from ..errors import (
        should_retry as should_retry_error,
    )

    HAS_ERROR_CATEGORIZATION = True
except ImportError:
    HAS_ERROR_CATEGORIZATION = False

# Try to import circuit breaker
try:
    from .circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

    HAS_CIRCUIT_BREAKER = True
except ImportError:
    HAS_CIRCUIT_BREAKER = False


def with_retry(
    func: Callable[[], Any],
    logger=None,
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    circuit_breaker: Optional[Any] = None,
    use_circuit_breaker: bool = True,
) -> Any:
    """
    Execute function with intelligent retry based on error categorization.

    Features (if error categorization available):
    - Non-retryable errors fail immediately (syntax, permissions)
    - Retryable errors use exponential backoff (network, timeout)
    - Rate limit errors use aggressive backoff
    - Transient errors retry conservatively

    Features (if circuit breaker available):
    - Prevents cascading failures
    - Automatic failure detection
    - Timeout-based recovery

    Fallback (without enhancements):
    - Pattern-based permanent error detection
    - Simple exponential backoff

    Args:
        func: Function to execute
        logger: Optional logger for diagnostics
        max_attempts: Maximum retry attempts
        initial_delay: Initial delay in seconds
        max_delay: Maximum delay in seconds
        backoff_factor: Exponential backoff multiplier
        circuit_breaker: Optional CircuitBreaker instance
        use_circuit_breaker: Whether to use circuit breaker if available

    Returns:
        Result of successful function execution

    Raises:
        Last exception if all retries exhausted
        CircuitBreakerOpenError: If circuit breaker is open
    """
    _attempt = 0  # noqa: F841
    _delay = initial_delay  # noqa: F841
    _last_exception = None  # noqa: F841

    # Use circuit breaker if available and requested
    if HAS_CIRCUIT_BREAKER and use_circuit_breaker and circuit_breaker:
        try:
            with circuit_breaker.protected_call():
                return _retry_loop(
                    func, logger, max_attempts, initial_delay, max_delay, backoff_factor
                )
        except CircuitBreakerOpenError as e:
            if logger:
                logger.err_kv(
                    event="circuit_breaker_open", retry_after=e.retry_after_seconds, error=str(e)
                )
            raise
    else:
        return _retry_loop(func, logger, max_attempts, initial_delay, max_delay, backoff_factor)


def _retry_loop(
    func: Callable[[], Any],
    logger=None,
    max_attempts: int = 3,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
) -> Any:
    """Internal retry loop implementation."""
    attempt = 0
    delay = initial_delay
    last_exception = None

    while attempt < max_attempts:
        attempt += 1

        try:
            return func()

        except Exception as e:
            last_exception = e

            # Use enhanced error categorization if available
            if HAS_ERROR_CATEGORIZATION:
                category = categorize_snowflake_error(e)

                # Non-retryable errors fail immediately
                if category == SnowflakeErrorCategory.NON_RETRYABLE:
                    if logger:
                        logger.debug_kv(
                            event="non_retryable_error",
                            category=category.value,
                            attempt=attempt,
                            error=str(e),
                        )
                    raise

                # Check if should retry based on category
                if not should_retry_error(e, attempt - 1, max_attempts):
                    if logger:
                        logger.err_kv(
                            event="retry_exhausted",
                            category=category.value,
                            attempts=attempt,
                            error=str(e),
                        )
                    raise

                # Get category-based delay
                delay = min(get_categorized_delay(e, attempt - 1, initial_delay), max_delay)

                if logger:
                    logger.warn_kv(
                        event="retry_attempt",
                        category=category.value,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        delay_sec=delay,
                        error=str(e),
                    )

            else:
                # Fallback to simple pattern matching
                error_message = str(e).lower()

                # Check if error is retryable
                if _is_permanent_error(error_message):
                    if logger:
                        logger.debug_kv(
                            event="permanent_error_no_retry", attempt=attempt, error=str(e)
                        )
                    raise

                # Check if we should retry
                if attempt >= max_attempts:
                    if logger:
                        logger.err_kv(event="retry_exhausted", attempts=attempt, error=str(e))
                    raise

                if logger:
                    logger.warn_kv(
                        event="retry_attempt",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        delay_sec=delay,
                        error=str(e),
                    )

            # Wait before retry
            time.sleep(delay)

            # Increase delay with exponential backoff (if not using categorization)
            if not HAS_ERROR_CATEGORIZATION:
                delay = min(delay * backoff_factor, max_delay)

    # Should never reach here, but raise last exception if we do
    if last_exception:
        raise last_exception


def _is_permanent_error(error_message: str) -> bool:
    """
    Determine if error is permanent (non-retryable).

    Permanent errors include:
    - Syntax errors
    - Permission errors
    - Object already exists
    - Object not found
    - Invalid object name
    - Invalid data type
    """
    permanent_patterns = [
        "syntax error",
        "parse error",
        "invalid identifier",
        "does not exist",
        "already exists",
        "permission denied",
        "access denied",
        "insufficient privileges",
        "invalid object name",
        "invalid data type",
        "invalid column name",
        "duplicate column",
        "authentication failed",
        "invalid user",
        "invalid role",
        "invalid warehouse",
        "invalid database",
        "invalid schema",
    ]

    return any(pattern in error_message for pattern in permanent_patterns)
