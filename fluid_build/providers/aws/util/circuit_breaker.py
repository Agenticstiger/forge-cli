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

# fluid_build/providers/aws/util/circuit_breaker.py
"""
Circuit Breaker pattern for AWS provider.

Prevents cascade failures by detecting repeated failures and temporarily
blocking requests to give AWS services time to recover.

Inspired by Snowflake provider's implementation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import Any, Callable, Optional


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing - reject requests
    HALF_OPEN = "half_open"  # Testing recovery


class CircuitBreakerOpenError(Exception):
    """Raised when circuit breaker is open."""

    def __init__(self, service: str, timeout_remaining: float):
        self.service = service
        self.timeout_remaining = timeout_remaining
        super().__init__(
            f"Circuit breaker is OPEN for {service}. " f"Retry in {timeout_remaining:.1f}s"
        )


@dataclass
class CircuitBreakerMetrics:
    """Metrics for circuit breaker monitoring."""

    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    circuit_opened_count: int = 0
    last_failure_time: Optional[float] = None
    last_success_time: Optional[float] = None

    @property
    def failure_rate(self) -> float:
        """Calculate failure rate (0.0 - 1.0)."""
        if self.total_requests == 0:
            return 0.0
        return self.failed_requests / self.total_requests

    @property
    def success_rate(self) -> float:
        """Calculate success rate (0.0 - 1.0)."""
        return 1.0 - self.failure_rate


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""

    failure_threshold: int = 5  # Open after this many failures
    timeout_seconds: int = 60  # Stay open for this long
    half_open_max_requests: int = 3  # Max requests in half-open state
    min_requests: int = 10  # Min requests before calculating failure rate
    failure_rate_threshold: float = 0.5  # Open if failure rate > this


class CircuitBreaker:
    """
    Circuit breaker for AWS service calls.

    States:
    - CLOSED: Normal operation, all requests allowed
    - OPEN: Failing, all requests rejected
    - HALF_OPEN: Testing recovery, limited requests allowed

    Example:
        breaker = CircuitBreaker(service="s3", config=config)

        try:
            result = breaker.call(s3.create_bucket, Bucket="my-bucket")
        except CircuitBreakerOpenError:
            # Circuit is open, service is down
            pass
    """

    def __init__(self, service: str, config: Optional[CircuitBreakerConfig] = None):
        """
        Initialize circuit breaker.

        Args:
            service: Service name (e.g., "s3", "glue", "athena")
            config: Configuration (uses defaults if None)
        """
        self.service = service
        self.config = config or CircuitBreakerConfig()
        self.state = CircuitState.CLOSED
        self.metrics = CircuitBreakerMetrics()
        self._lock = Lock()
        self._state_change_time = time.time()
        self._half_open_requests = 0

    def call(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        """
        Execute function through circuit breaker.

        Args:
            func: Function to call
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Function result

        Raises:
            CircuitBreakerOpenError: If circuit is open
            Exception: If function fails
        """
        with self._lock:
            # Check if we should transition states
            self._check_state_transition()

            # If circuit is open, reject request
            if self.state == CircuitState.OPEN:
                timeout_remaining = self._get_timeout_remaining()
                raise CircuitBreakerOpenError(self.service, timeout_remaining)

            # If half-open, limit concurrent requests
            if self.state == CircuitState.HALF_OPEN:
                if self._half_open_requests >= self.config.half_open_max_requests:
                    timeout_remaining = self._get_timeout_remaining()
                    raise CircuitBreakerOpenError(self.service, timeout_remaining)
                self._half_open_requests += 1

        # Execute function (outside lock to avoid blocking)
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure(e)
            raise

    def _check_state_transition(self) -> None:
        """Check if circuit should transition states."""
        current_time = time.time()

        if self.state == CircuitState.OPEN:
            # Check if timeout has elapsed
            elapsed = current_time - self._state_change_time
            if elapsed >= self.config.timeout_seconds:
                self._transition_to(CircuitState.HALF_OPEN)

        elif self.state == CircuitState.CLOSED:
            # Check if we should open based on failures
            if self.metrics.total_requests >= self.config.min_requests:
                if self.metrics.failure_rate >= self.config.failure_rate_threshold:
                    self._transition_to(CircuitState.OPEN)

            # Also check consecutive failures
            if self.metrics.failed_requests >= self.config.failure_threshold:
                # Check if these are recent failures
                if self.metrics.last_failure_time:
                    time_since_last_failure = current_time - self.metrics.last_failure_time
                    if time_since_last_failure < 10:  # Within 10 seconds
                        self._transition_to(CircuitState.OPEN)

    def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to new state."""
        old_state = self.state
        self.state = new_state
        self._state_change_time = time.time()

        if new_state == CircuitState.OPEN:
            self.metrics.circuit_opened_count += 1
        elif new_state == CircuitState.HALF_OPEN:
            self._half_open_requests = 0

        # Log state transition
        import logging

        logger = logging.getLogger(__name__)
        logger.warning(f"Circuit breaker for {self.service}: {old_state.value} → {new_state.value}")

    def _on_success(self) -> None:
        """Record successful call."""
        with self._lock:
            self.metrics.total_requests += 1
            self.metrics.successful_requests += 1
            self.metrics.last_success_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                # Successful call in half-open, close circuit
                self._transition_to(CircuitState.CLOSED)
                self._reset_metrics()

            # Decrement half-open counter
            if self._half_open_requests > 0:
                self._half_open_requests -= 1

    def _on_failure(self, error: Exception) -> None:
        """Record failed call."""
        with self._lock:
            self.metrics.total_requests += 1
            self.metrics.failed_requests += 1
            self.metrics.last_failure_time = time.time()

            # If half-open and failed, immediately re-open
            if self.state == CircuitState.HALF_OPEN:
                self._transition_to(CircuitState.OPEN)

            # Decrement half-open counter
            if self._half_open_requests > 0:
                self._half_open_requests -= 1

    def _get_timeout_remaining(self) -> float:
        """Get remaining timeout in seconds."""
        elapsed = time.time() - self._state_change_time
        remaining = max(0, self.config.timeout_seconds - elapsed)
        return remaining

    def _reset_metrics(self) -> None:
        """Reset metrics (called when circuit closes)."""
        self.metrics.total_requests = 0
        self.metrics.successful_requests = 0
        self.metrics.failed_requests = 0

    def get_state(self) -> CircuitState:
        """Get current circuit state."""
        with self._lock:
            return self.state

    def get_metrics(self) -> CircuitBreakerMetrics:
        """Get circuit metrics (copy)."""
        with self._lock:
            return CircuitBreakerMetrics(
                total_requests=self.metrics.total_requests,
                successful_requests=self.metrics.successful_requests,
                failed_requests=self.metrics.failed_requests,
                circuit_opened_count=self.metrics.circuit_opened_count,
                last_failure_time=self.metrics.last_failure_time,
                last_success_time=self.metrics.last_success_time,
            )

    def reset(self) -> None:
        """Manually reset circuit to closed state."""
        with self._lock:
            self.state = CircuitState.CLOSED
            self._state_change_time = time.time()
            self._reset_metrics()


class CircuitBreakerRegistry:
    """
    Global registry of circuit breakers per AWS service.

    Ensures one circuit breaker per service for consistent state.
    """

    _instance: Optional[CircuitBreakerRegistry] = None
    _lock = Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._breakers = {}
                    cls._instance._config = CircuitBreakerConfig()
        return cls._instance

    def get_breaker(self, service: str) -> CircuitBreaker:
        """
        Get or create circuit breaker for service.

        Args:
            service: Service name (e.g., "s3", "glue")

        Returns:
            CircuitBreaker instance
        """
        if service not in self._breakers:
            self._breakers[service] = CircuitBreaker(service, self._config)
        return self._breakers[service]

    def set_config(self, config: CircuitBreakerConfig) -> None:
        """Set global configuration."""
        self._config = config

    def get_all_states(self) -> dict[str, CircuitState]:
        """Get states of all circuit breakers."""
        return {service: breaker.get_state() for service, breaker in self._breakers.items()}

    def get_all_metrics(self) -> dict[str, CircuitBreakerMetrics]:
        """Get metrics of all circuit breakers."""
        return {service: breaker.get_metrics() for service, breaker in self._breakers.items()}

    def reset_all(self) -> None:
        """Reset all circuit breakers."""
        for breaker in self._breakers.values():
            breaker.reset()


# Global registry instance
_registry = CircuitBreakerRegistry()


def get_circuit_breaker(service: str) -> CircuitBreaker:
    """
    Get circuit breaker for AWS service.

    Args:
        service: Service name (e.g., "s3", "glue", "athena")

    Returns:
        CircuitBreaker instance
    """
    return _registry.get_breaker(service)


def configure_circuit_breakers(config: CircuitBreakerConfig) -> None:
    """
    Configure circuit breakers globally.

    Args:
        config: Circuit breaker configuration
    """
    _registry.set_config(config)


def reset_all_circuit_breakers() -> None:
    """Reset all circuit breakers to closed state."""
    _registry.reset_all()
