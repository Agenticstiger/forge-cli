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

"""
FLUID CLI Error Handling and Resilience Patterns

Comprehensive error handling, timeout management, graceful degradation,
and resilience patterns for production CLI operations.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import signal
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, TypeVar

from .core import FluidCLIError

T = TypeVar("T")


class ErrorSeverity(Enum):
    """Error severity levels for classification"""

    LOW = "low"  # Non-critical, operation can continue
    MEDIUM = "medium"  # Important but recoverable
    HIGH = "high"  # Critical, operation should stop
    CRITICAL = "critical"  # System-level error, immediate attention


class RetryStrategy(Enum):
    """Retry strategies for failed operations"""

    NO_RETRY = "no_retry"
    FIXED_DELAY = "fixed_delay"
    EXPONENTIAL_BACKOFF = "exponential_backoff"
    LINEAR_BACKOFF = "linear_backoff"


@dataclass
class ErrorContext:
    """Context information for error handling"""

    operation: str
    component: str
    user_message: str
    technical_details: Dict[str, Any]
    severity: ErrorSeverity
    retry_strategy: RetryStrategy = RetryStrategy.NO_RETRY
    max_retries: int = 3
    suggestions: List[str] = None
    recovery_actions: List[str] = None

    def __post_init__(self):
        if self.suggestions is None:
            self.suggestions = []
        if self.recovery_actions is None:
            self.recovery_actions = []


class TimeoutManager:
    """Advanced timeout management with graceful handling"""

    def __init__(self, default_timeout: int = 300):
        self.default_timeout = default_timeout
        self.logger = logging.getLogger(__name__)
        self._active_timeouts: Dict[str, float] = {}

    @contextmanager
    def timeout(self, seconds: Optional[int] = None, operation: str = "operation"):
        """Context manager for operation timeouts"""
        timeout_value = seconds or self.default_timeout
        start_time = time.time()

        def timeout_handler(signum, frame):
            elapsed = time.time() - start_time
            raise TimeoutError(
                f"Operation '{operation}' timed out after {elapsed:.1f} seconds "
                f"(limit: {timeout_value}s)"
            )

        # Store timeout info
        self._active_timeouts[operation] = start_time

        # Set up signal handler (Unix only)
        old_handler = None
        if hasattr(signal, "SIGALRM"):
            old_handler = signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(timeout_value)

        try:
            yield
        except TimeoutError as e:
            self.logger.warning(f"Timeout in {operation}: {e}")
            raise FluidCLIError(
                1,
                "operation_timeout",
                f"Operation timed out: {operation}",
                context={
                    "operation": operation,
                    "timeout_seconds": timeout_value,
                    "elapsed_seconds": time.time() - start_time,
                },
                suggestions=[
                    f"Try increasing timeout for {operation}",
                    "Check if the operation is stuck",
                    "Verify network connectivity if applicable",
                    "Break down large operations into smaller parts",
                ],
            )
        finally:
            # Clean up
            if hasattr(signal, "SIGALRM"):
                signal.alarm(0)
                if old_handler:
                    signal.signal(signal.SIGALRM, old_handler)

            self._active_timeouts.pop(operation, None)

    @asynccontextmanager
    async def async_timeout(self, seconds: Optional[int] = None, operation: str = "operation"):
        """Async context manager for operation timeouts"""
        timeout_value = seconds or self.default_timeout

        try:
            async with asyncio.timeout(timeout_value):
                yield
        except asyncio.TimeoutError:
            raise FluidCLIError(
                1,
                "operation_timeout",
                f"Async operation timed out: {operation}",
                context={"operation": operation, "timeout_seconds": timeout_value},
                suggestions=[
                    f"Increase timeout for {operation}",
                    "Check async operation implementation",
                    "Verify concurrent operation limits",
                ],
            )

    def get_active_timeouts(self) -> Dict[str, float]:
        """Get currently active timeouts and their elapsed time"""
        current_time = time.time()
        return {
            operation: current_time - start_time
            for operation, start_time in self._active_timeouts.items()
        }


class RetryManager:
    """Advanced retry management with different strategies"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def retry(
        self,
        strategy: RetryStrategy = RetryStrategy.EXPONENTIAL_BACKOFF,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        backoff_factor: float = 2.0,
        exceptions: tuple = (Exception,),
    ):
        """Decorator for retry functionality with configurable strategies"""

        def decorator(func: Callable[..., T]) -> Callable[..., T]:
            @functools.wraps(func)
            def wrapper(*args, **kwargs) -> T:
                last_exception = None

                for attempt in range(max_attempts):
                    try:
                        return func(*args, **kwargs)
                    except exceptions as e:
                        last_exception = e

                        if attempt == max_attempts - 1:
                            # Final attempt failed
                            break

                        # Calculate delay based on strategy
                        delay = self._calculate_delay(
                            strategy, attempt, base_delay, max_delay, backoff_factor
                        )

                        self.logger.warning(
                            f"Attempt {attempt + 1}/{max_attempts} failed for {func.__name__}: {e}. "
                            f"Retrying in {delay:.1f}s..."
                        )

                        time.sleep(delay)

                # All attempts failed
                raise FluidCLIError(
                    1,
                    "operation_failed_after_retries",
                    f"Operation failed after {max_attempts} attempts",
                    context={
                        "function": func.__name__,
                        "attempts": max_attempts,
                        "strategy": strategy.value,
                        "last_error": str(last_exception),
                    },
                    suggestions=[
                        "Check error logs for root cause",
                        "Verify system resources and connectivity",
                        "Consider increasing retry attempts or delays",
                        "Check if the operation requires different parameters",
                    ],
                )

            return wrapper

        return decorator

    def _calculate_delay(
        self,
        strategy: RetryStrategy,
        attempt: int,
        base_delay: float,
        max_delay: float,
        backoff_factor: float,
    ) -> float:
        """Calculate delay based on retry strategy"""
        if strategy == RetryStrategy.NO_RETRY:
            return 0
        elif strategy == RetryStrategy.FIXED_DELAY:
            return min(base_delay, max_delay)
        elif strategy == RetryStrategy.LINEAR_BACKOFF:
            return min(base_delay * (attempt + 1), max_delay)
        elif strategy == RetryStrategy.EXPONENTIAL_BACKOFF:
            return min(base_delay * (backoff_factor**attempt), max_delay)
        else:
            return base_delay


class GracefulDegradation:
    """Graceful degradation patterns for CLI operations"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self._fallback_registry: Dict[str, List[Callable]] = {}

    def register_fallback(self, operation: str, fallback_func: Callable) -> None:
        """Register a fallback function for an operation"""
        if operation not in self._fallback_registry:
            self._fallback_registry[operation] = []
        self._fallback_registry[operation].append(fallback_func)

    def with_fallback(self, operation: str, primary_value: Any = None):
        """Decorator for operations with fallback support"""

        def decorator(func: Callable[..., T]) -> Callable[..., T]:
            @functools.wraps(func)
            def wrapper(*args, **kwargs) -> T:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    self.logger.warning(f"Primary operation {operation} failed: {e}")
                    return self._execute_fallbacks(operation, e, *args, **kwargs)

            return wrapper

        return decorator

    def _execute_fallbacks(self, operation: str, error: Exception, *args, **kwargs) -> Any:
        """Execute registered fallbacks for an operation"""
        fallbacks = self._fallback_registry.get(operation, [])

        for i, fallback in enumerate(fallbacks):
            try:
                self.logger.info(f"Trying fallback {i + 1} for {operation}")
                result = fallback(*args, **kwargs)
                self.logger.info(f"Fallback {i + 1} succeeded for {operation}")
                return result
            except Exception as fallback_error:
                self.logger.warning(f"Fallback {i + 1} failed for {operation}: {fallback_error}")
                continue

        # All fallbacks failed
        raise FluidCLIError(
            1,
            "operation_and_fallbacks_failed",
            f"Operation {operation} and all fallbacks failed",
            context={
                "operation": operation,
                "primary_error": str(error),
                "fallbacks_attempted": len(fallbacks),
            },
            suggestions=[
                "Check system configuration",
                "Verify required dependencies are installed",
                "Try running with debug mode for more information",
                "Contact support if the issue persists",
            ],
        )


class CircuitBreaker:
    """Circuit breaker pattern for preventing cascading failures"""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        expected_exception: type = Exception,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception

        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.state = "closed"  # closed, open, half-open
        self.logger = logging.getLogger(__name__)

    def __call__(self, func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            if self.state == "open":
                if self._should_attempt_reset():
                    self.state = "half-open"
                    self.logger.info(f"Circuit breaker half-open for {func.__name__}")
                else:
                    raise FluidCLIError(
                        1,
                        "circuit_breaker_open",
                        f"Circuit breaker is open for {func.__name__}",
                        context={
                            "function": func.__name__,
                            "failure_count": self.failure_count,
                            "last_failure": self.last_failure_time,
                        },
                        suggestions=[
                            f"Wait {self.recovery_timeout}s before retrying",
                            "Check if the underlying service has recovered",
                            "Verify system resources and connectivity",
                        ],
                    )

            try:
                result = func(*args, **kwargs)
                self._on_success()
                return result
            except self.expected_exception:
                self._on_failure()
                raise

        return wrapper

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset"""
        if self.last_failure_time is None:
            return True
        return time.time() - self.last_failure_time >= self.recovery_timeout

    def _on_success(self) -> None:
        """Handle successful execution"""
        if self.state == "half-open":
            self.state = "closed"
            self.failure_count = 0
            self.logger.info("Circuit breaker closed after successful execution")

    def _on_failure(self) -> None:
        """Handle failed execution"""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            self.logger.warning(f"Circuit breaker opened after {self.failure_count} failures")


class HealthChecker:
    """Health checking for external dependencies"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self._health_checks: Dict[str, Callable[[], bool]] = {}

    def register_health_check(self, name: str, check_func: Callable[[], bool]) -> None:
        """Register a health check function"""
        self._health_checks[name] = check_func

    def check_health(self, dependency: str) -> bool:
        """Check health of a specific dependency"""
        if dependency not in self._health_checks:
            self.logger.warning(f"No health check registered for {dependency}")
            return True  # Assume healthy if no check

        try:
            return self._health_checks[dependency]()
        except Exception as e:
            self.logger.error(f"Health check failed for {dependency}: {e}")
            return False

    def check_all_health(self) -> Dict[str, bool]:
        """Check health of all registered dependencies"""
        results = {}
        for name, check_func in self._health_checks.items():
            try:
                results[name] = check_func()
            except Exception as e:
                self.logger.error(f"Health check failed for {name}: {e}")
                results[name] = False
        return results

    def require_healthy(self, dependency: str):
        """Decorator to require a dependency to be healthy"""

        def decorator(func: Callable[..., T]) -> Callable[..., T]:
            @functools.wraps(func)
            def wrapper(*args, **kwargs) -> T:
                if not self.check_health(dependency):
                    raise FluidCLIError(
                        1,
                        "dependency_unhealthy",
                        f"Dependency {dependency} is not healthy",
                        context={"dependency": dependency},
                        suggestions=[
                            f"Check {dependency} service status",
                            "Verify network connectivity",
                            "Check service configuration",
                            "Wait for service to recover",
                        ],
                    )
                return func(*args, **kwargs)

            return wrapper

        return decorator


# Global instances
_timeout_manager = TimeoutManager()
_retry_manager = RetryManager()
_graceful_degradation = GracefulDegradation()
_health_checker = HealthChecker()


# Convenience functions and decorators
def timeout(seconds: Optional[int] = None, operation: str = "operation"):
    """Decorator for operation timeouts"""
    return _timeout_manager.timeout(seconds, operation)


def retry(
    strategy: RetryStrategy = RetryStrategy.EXPONENTIAL_BACKOFF,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    exceptions: tuple = (Exception,),
):
    """Decorator for retry functionality"""
    return _retry_manager.retry(strategy, max_attempts, base_delay, exceptions=exceptions)


def with_fallback(operation: str):
    """Decorator for operations with fallback support"""
    return _graceful_degradation.with_fallback(operation)


def circuit_breaker(failure_threshold: int = 5, recovery_timeout: int = 60):
    """Decorator for circuit breaker pattern"""
    return CircuitBreaker(failure_threshold, recovery_timeout)


def require_healthy(dependency: str):
    """Decorator to require a dependency to be healthy"""
    return _health_checker.require_healthy(dependency)


def register_fallback(operation: str, fallback_func: Callable) -> None:
    """Register a fallback function for an operation"""
    _graceful_degradation.register_fallback(operation, fallback_func)


def register_health_check(name: str, check_func: Callable[[], bool]) -> None:
    """Register a health check function"""
    _health_checker.register_health_check(name, check_func)


def check_dependency_health(dependency: str) -> bool:
    """Check health of a specific dependency"""
    return _health_checker.check_health(dependency)


def get_active_timeouts() -> Dict[str, float]:
    """Get currently active timeouts"""
    return _timeout_manager.get_active_timeouts()


# Export public interface
__all__ = [
    "ErrorSeverity",
    "RetryStrategy",
    "ErrorContext",
    "TimeoutManager",
    "RetryManager",
    "GracefulDegradation",
    "CircuitBreaker",
    "HealthChecker",
    "timeout",
    "retry",
    "with_fallback",
    "circuit_breaker",
    "require_healthy",
    "register_fallback",
    "register_health_check",
    "check_dependency_health",
    "get_active_timeouts",
]
