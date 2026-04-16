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

# fluid_build/providers/local/util/retry.py
"""
Retry utilities with exponential backoff for Local provider operations.

Adapted from GCP provider for DuckDB and local file system operations.
Handles transient failures common in I/O operations with intelligent
retry logic and proper error categorization.
"""

import logging
import random
import time
from functools import wraps
from typing import Any, Callable, List, Optional, Type


class RetryableError(Exception):
    """Exception that indicates an operation should be retried."""

    pass


class NonRetryableError(Exception):
    """Exception that indicates an operation should not be retried."""

    pass


def is_retryable_error(exception: Exception) -> bool:
    """
    Determine if an exception should trigger a retry.

    For local operations, retryable errors include:
    - File system lock errors
    - I/O errors (disk full, permissions)
    - DuckDB transient errors
    - Network errors (for remote sources)

    Args:
        exception: Exception to evaluate

    Returns:
        True if the operation should be retried
    """
    # Always retry explicitly retryable errors
    if isinstance(exception, RetryableError):
        return True

    # Never retry explicitly non-retryable errors
    if isinstance(exception, NonRetryableError):
        return False

    exception_str = str(exception).lower()
    exception_type = type(exception).__name__.lower()

    # Patterns that indicate retryable errors
    retryable_patterns = [
        # File system errors
        "file locked",
        "resource temporarily unavailable",
        "permission denied",  # Can be transient in some cases
        "disk full",
        "no space left",
        # I/O errors
        "i/o error",
        "io error",
        "broken pipe",
        "connection reset",
        # DuckDB specific errors
        "database is locked",
        "database locked",
        "cannot start a transaction within a transaction",
        "busy",
        "checkpoint",
        # Network errors (for HTTP/S3 sources)
        "connection error",
        "timeout",
        "network",
        "socket",
        "temporary failure",
        # HTTP errors
        "rate limit",
        "too many requests",
        "429",
        "500",
        "502",
        "503",
        "504",
    ]

    # Check if error message contains retryable patterns
    for pattern in retryable_patterns:
        if pattern in exception_str:
            return True

    # Common Python exceptions that might be retryable
    retryable_exception_types = [
        "oserror",  # File system errors
        "ioerror",  # I/O errors
        "connectionerror",  # Network errors
        "timeout",  # Timeout errors
        "socketerror",  # Socket errors
        "gaierror",  # DNS errors
    ]

    if exception_type in retryable_exception_types:
        return True

    # DuckDB specific exceptions
    try:
        import duckdb

        if isinstance(exception, duckdb.IOException):
            return True
        if isinstance(exception, duckdb.BinderException):
            # Some binder errors are retryable (temp table not ready)
            return "does not exist" not in exception_str
    except ImportError:
        pass

    return False


def with_retry(
    func: Callable[[], Any],
    logger: Optional[logging.Logger] = None,
    max_attempts: int = 3,
    base_delay: float = 0.5,  # Shorter for local operations
    max_delay: float = 10.0,  # Lower max for local operations
    backoff_multiplier: float = 2.0,
    jitter: bool = True,
) -> Any:
    """
    Execute a function with retry logic and exponential backoff.

    Args:
        func: Function to execute
        logger: Optional logger for retry events
        max_attempts: Maximum number of attempts (default: 3)
        base_delay: Initial delay between retries in seconds (default: 0.5)
        max_delay: Maximum delay between retries in seconds (default: 10.0)
        backoff_multiplier: Multiplier for exponential backoff (default: 2.0)
        jitter: Whether to add random jitter to delays (default: True)

    Returns:
        Result of successful function execution

    Raises:
        The last exception if all retries are exhausted
    """
    last_exception = None

    for attempt in range(1, max_attempts + 1):
        try:
            result = func()

            # Log successful retry if not first attempt
            if attempt > 1 and logger:
                logger.info(
                    f"Operation succeeded on attempt {attempt}",
                    extra={"attempt": attempt, "max_attempts": max_attempts, "retry_success": True},
                )

            return result

        except Exception as e:
            last_exception = e

            # Check if we should retry this error
            if not is_retryable_error(e):
                if logger:
                    logger.warning(
                        f"Non-retryable error encountered: {e}",
                        extra={
                            "attempt": attempt,
                            "error_type": type(e).__name__,
                            "non_retryable": True,
                        },
                    )
                raise e

            # If this was the last attempt, raise the exception
            if attempt == max_attempts:
                if logger:
                    logger.error(
                        f"All retry attempts exhausted. Last error: {e}",
                        extra={
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "final_error": True,
                            "error_type": type(e).__name__,
                        },
                    )
                raise e

            # Calculate delay for next attempt
            delay = min(base_delay * (backoff_multiplier ** (attempt - 1)), max_delay)

            # Add jitter to prevent thundering herd
            if jitter:
                delay = delay * (0.5 + random.random() * 0.5)

            if logger:
                logger.warning(
                    f"Attempt {attempt} failed, retrying in {delay:.2f}s: {e}",
                    extra={
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "delay_seconds": delay,
                        "error_type": type(e).__name__,
                        "retryable": True,
                    },
                )

            time.sleep(delay)

    # This should never be reached, but just in case
    if last_exception:
        raise last_exception
    else:
        raise RuntimeError("Retry loop completed without success or exception")


def retry(
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    backoff_multiplier: float = 2.0,
    jitter: bool = True,
    retryable_exceptions: Optional[List[Type[Exception]]] = None,
):
    """
    Decorator for adding retry logic to functions.

    Usage:
        @retry(max_attempts=5, base_delay=1.0)
        def my_function():
            # Your code here
            pass

    Args:
        max_attempts: Maximum number of attempts (default: 3)
        base_delay: Initial delay between retries in seconds (default: 0.5)
        max_delay: Maximum delay between retries in seconds (default: 10.0)
        backoff_multiplier: Multiplier for exponential backoff (default: 2.0)
        jitter: Whether to add random jitter to delays (default: True)
        retryable_exceptions: Specific exception types to retry (optional, unused currently)

    Returns:
        Decorated function with retry logic
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Get logger if available
            logger = None
            if hasattr(func, "__self__") and hasattr(func.__self__, "logger"):
                logger = func.__self__.logger

            def inner_func():
                return func(*args, **kwargs)

            return with_retry(
                inner_func,
                logger=logger,
                max_attempts=max_attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                backoff_multiplier=backoff_multiplier,
                jitter=jitter,
            )

        return wrapper

    return decorator


def exponential_backoff(
    attempt: int,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    multiplier: float = 2.0,
    jitter: bool = True,
) -> float:
    """
    Calculate exponential backoff delay.

    Args:
        attempt: Current attempt number (1-based)
        base_delay: Base delay in seconds (default: 0.5)
        max_delay: Maximum delay in seconds (default: 10.0)
        multiplier: Exponential multiplier (default: 2.0)
        jitter: Whether to add jitter (default: True)

    Returns:
        Delay in seconds
    """
    delay = min(base_delay * (multiplier ** (attempt - 1)), max_delay)

    if jitter:
        # Add ±25% jitter
        jitter_range = delay * 0.25
        delay += random.uniform(-jitter_range, jitter_range)
        delay = max(0, delay)  # Ensure non-negative

    return delay


class CircuitBreaker:
    """
    Circuit breaker for protecting against cascading failures.

    Monitors failure rates and temporarily disables operations
    when failure threshold is exceeded.

    Useful for operations that might repeatedly fail (e.g., corrupted file).
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,  # Shorter for local operations
        expected_exception: Type[Exception] = Exception,
    ):
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Time to wait before attempting recovery
            expected_exception: Exception type that counts as failure
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception

        self.failure_count = 0
        self.last_failure_time = None
        self.state = "closed"  # closed, open, half-open

    def __call__(self, func: Callable) -> Callable:
        """Use circuit breaker as decorator."""

        @wraps(func)
        def wrapper(*args, **kwargs):
            return self.call(func, *args, **kwargs)

        return wrapper

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection.

        Args:
            func: Function to execute
            *args: Function arguments
            **kwargs: Function keyword arguments

        Returns:
            Function result

        Raises:
            CircuitBreakerOpenError: If circuit is open
            Original exception: If function fails
        """
        if self.state == "open":
            if self._should_attempt_reset():
                self.state = "half-open"
            else:
                raise CircuitBreakerOpenError("Circuit breaker is open")

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result

        except self.expected_exception as e:
            self._on_failure()
            raise e

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset."""
        if self.last_failure_time is None:
            return True
        return time.time() - self.last_failure_time >= self.recovery_timeout

    def _on_success(self):
        """Handle successful operation."""
        self.failure_count = 0
        self.state = "closed"

    def _on_failure(self):
        """Handle failed operation."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = "open"


class CircuitBreakerOpenError(Exception):
    """Exception raised when circuit breaker is open."""

    pass
