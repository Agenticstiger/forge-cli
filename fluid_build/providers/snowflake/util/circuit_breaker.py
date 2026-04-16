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

# fluid_build/providers/snowflake/util/circuit_breaker.py
"""
Circuit Breaker Pattern for Snowflake Operations.

Prevents cascading failures by stopping repeated failed operations.
Implements three states: CLOSED, OPEN, HALF_OPEN.

Features:
- Automatic failure detection
- Configurable failure threshold
- Timeout-based recovery
- Metrics and monitoring
- Integration with retry logic

Usage:
    from fluid_build.providers.snowflake.util import CircuitBreaker

    breaker = CircuitBreaker(failure_threshold=5, timeout_seconds=60)

    with breaker.protected_call():
        execute_snowflake_query()
"""

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing - reject requests
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreakerMetrics:
    """Circuit breaker metrics."""

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: Optional[datetime] = None
    state_changed_time: datetime = field(default_factory=datetime.now)
    total_calls: int = 0
    rejected_calls: int = 0

    def to_dict(self):
        """Convert to dictionary."""
        return {
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "last_failure_time": (
                self.last_failure_time.isoformat() if self.last_failure_time else None
            ),
            "state_changed_time": self.state_changed_time.isoformat(),
            "total_calls": self.total_calls,
            "rejected_calls": self.rejected_calls,
            "success_rate": self.success_count / self.total_calls if self.total_calls > 0 else 0,
        }


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open."""

    def __init__(self, message: str, retry_after_seconds: float):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class CircuitBreaker:
    """
    Circuit breaker for Snowflake operations.

    Prevents cascading failures by detecting repeated errors and temporarily
    blocking requests until the system recovers.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Too many failures, reject all requests
    - HALF_OPEN: Testing if system recovered, allow limited requests

    Configuration:
    - failure_threshold: Number of failures before opening circuit
    - timeout_seconds: Time to wait before attempting recovery (OPEN -> HALF_OPEN)
    - half_open_max_calls: Max successful calls needed in HALF_OPEN to close circuit
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        timeout_seconds: float = 60.0,
        half_open_max_calls: int = 3,
        name: str = "snowflake",
    ):
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of consecutive failures before opening
            timeout_seconds: Seconds to wait before attempting recovery
            half_open_max_calls: Successful calls needed to close circuit
            name: Circuit breaker name for logging
        """
        self.failure_threshold = failure_threshold
        self.timeout_seconds = timeout_seconds
        self.half_open_max_calls = half_open_max_calls
        self.name = name

        self.metrics = CircuitBreakerMetrics()
        self._lock = threading.Lock()

    @contextmanager
    def protected_call(self):
        """
        Context manager for protected operation.

        Usage:
            with breaker.protected_call():
                execute_operation()

        Raises:
            CircuitBreakerOpenError: If circuit is open
        """
        with self._lock:
            self.metrics.total_calls += 1

            # Check if circuit is open
            if self.metrics.state == CircuitState.OPEN:
                # Check if timeout expired
                if self._should_attempt_reset():
                    logger.info(
                        f"Circuit breaker [{self.name}]: Attempting recovery (OPEN -> HALF_OPEN)"
                    )
                    self._transition_to_half_open()
                else:
                    # Still in timeout period
                    self.metrics.rejected_calls += 1
                    time_remaining = self._get_time_until_reset()

                    raise CircuitBreakerOpenError(
                        f"Circuit breaker [{self.name}] is OPEN. "
                        f"Too many failures ({self.metrics.failure_count}). "
                        f"Retry after {time_remaining:.1f} seconds.",
                        retry_after_seconds=time_remaining,
                    )

        # Execute protected operation
        try:
            yield

            # Operation succeeded
            with self._lock:
                self._record_success()

        except Exception as e:
            # Operation failed
            with self._lock:
                self._record_failure()

            logger.error(f"Circuit breaker [{self.name}]: Operation failed - {e}")
            raise

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection.

        Args:
            func: Function to call
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Function result

        Raises:
            CircuitBreakerOpenError: If circuit is open
        """
        with self.protected_call():
            return func(*args, **kwargs)

    def _record_success(self):
        """Record successful operation."""
        self.metrics.success_count += 1

        if self.metrics.state == CircuitState.HALF_OPEN:
            # Check if we have enough successes to close circuit
            if self.metrics.success_count >= self.half_open_max_calls:
                logger.info(
                    f"Circuit breaker [{self.name}]: Recovery successful "
                    f"({self.metrics.success_count} successes) (HALF_OPEN -> CLOSED)"
                )
                self._transition_to_closed()

        elif self.metrics.state == CircuitState.CLOSED:
            # Reset failure count on success
            self.metrics.failure_count = 0

    def _record_failure(self):
        """Record failed operation."""
        self.metrics.failure_count += 1
        self.metrics.last_failure_time = datetime.now()

        if self.metrics.state == CircuitState.HALF_OPEN:
            # Failure during recovery - back to OPEN
            logger.warning(
                f"Circuit breaker [{self.name}]: Recovery failed, "
                f"reopening circuit (HALF_OPEN -> OPEN)"
            )
            self._transition_to_open()

        elif self.metrics.state == CircuitState.CLOSED:
            # Check if threshold exceeded
            if self.metrics.failure_count >= self.failure_threshold:
                logger.error(
                    f"Circuit breaker [{self.name}]: Failure threshold exceeded "
                    f"({self.metrics.failure_count}/{self.failure_threshold}) (CLOSED -> OPEN)"
                )
                self._transition_to_open()

    def _transition_to_open(self):
        """Transition to OPEN state."""
        self.metrics.state = CircuitState.OPEN
        self.metrics.state_changed_time = datetime.now()
        self.metrics.success_count = 0

    def _transition_to_half_open(self):
        """Transition to HALF_OPEN state."""
        self.metrics.state = CircuitState.HALF_OPEN
        self.metrics.state_changed_time = datetime.now()
        self.metrics.success_count = 0
        self.metrics.failure_count = 0

    def _transition_to_closed(self):
        """Transition to CLOSED state."""
        self.metrics.state = CircuitState.CLOSED
        self.metrics.state_changed_time = datetime.now()
        self.metrics.failure_count = 0
        self.metrics.success_count = 0

    def _should_attempt_reset(self) -> bool:
        """Check if circuit should attempt reset."""
        if self.metrics.state != CircuitState.OPEN:
            return False

        if not self.metrics.state_changed_time:
            return False

        elapsed = (datetime.now() - self.metrics.state_changed_time).total_seconds()
        return elapsed >= self.timeout_seconds

    def _get_time_until_reset(self) -> float:
        """Get seconds until circuit can attempt reset."""
        if not self.metrics.state_changed_time:
            return 0.0

        elapsed = (datetime.now() - self.metrics.state_changed_time).total_seconds()
        remaining = self.timeout_seconds - elapsed
        return max(0.0, remaining)

    def reset(self):
        """Manually reset circuit breaker to CLOSED state."""
        with self._lock:
            logger.info(f"Circuit breaker [{self.name}]: Manual reset")
            self._transition_to_closed()

    def get_metrics(self) -> CircuitBreakerMetrics:
        """Get current metrics."""
        return self.metrics

    def is_open(self) -> bool:
        """Check if circuit is open."""
        return self.metrics.state == CircuitState.OPEN


class SnowflakeCircuitBreaker:
    """
    Circuit breaker specifically for Snowflake operations.

    Provides operation-specific circuit breakers for:
    - DDL operations (CREATE, ALTER, DROP)
    - DML operations (INSERT, UPDATE, DELETE)
    - Query operations (SELECT)
    - COPY operations (data loading)
    """

    def __init__(self):
        """Initialize Snowflake circuit breakers."""
        self.ddl_breaker = CircuitBreaker(
            failure_threshold=3, timeout_seconds=30.0, name="snowflake-ddl"
        )

        self.dml_breaker = CircuitBreaker(
            failure_threshold=5, timeout_seconds=60.0, name="snowflake-dml"
        )

        self.query_breaker = CircuitBreaker(
            failure_threshold=10, timeout_seconds=120.0, name="snowflake-query"
        )

        self.copy_breaker = CircuitBreaker(
            failure_threshold=3, timeout_seconds=180.0, name="snowflake-copy"
        )

    def get_breaker(self, operation_type: str) -> CircuitBreaker:
        """
        Get circuit breaker for operation type.

        Args:
            operation_type: One of 'ddl', 'dml', 'query', 'copy'

        Returns:
            Appropriate CircuitBreaker
        """
        breakers = {
            "ddl": self.ddl_breaker,
            "dml": self.dml_breaker,
            "query": self.query_breaker,
            "copy": self.copy_breaker,
        }

        return breakers.get(operation_type.lower(), self.query_breaker)

    def get_all_metrics(self) -> dict:
        """Get metrics for all circuit breakers."""
        return {
            "ddl": self.ddl_breaker.get_metrics().to_dict(),
            "dml": self.dml_breaker.get_metrics().to_dict(),
            "query": self.query_breaker.get_metrics().to_dict(),
            "copy": self.copy_breaker.get_metrics().to_dict(),
        }

    def reset_all(self):
        """Reset all circuit breakers."""
        self.ddl_breaker.reset()
        self.dml_breaker.reset()
        self.query_breaker.reset()
        self.copy_breaker.reset()
