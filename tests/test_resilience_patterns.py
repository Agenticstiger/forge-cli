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

"""Tests for cli/resilience.py — enums, dataclasses, retry logic, circuit breaker, health checker."""

import time

from fluid_build.cli.resilience import (
    CircuitBreaker,
    ErrorContext,
    ErrorSeverity,
    GracefulDegradation,
    HealthChecker,
    RetryManager,
    RetryStrategy,
)


# ── Enums ────────────────────────────────────────────────────────────
class TestErrorSeverity:
    def test_values(self):
        assert ErrorSeverity.LOW.value == "low"
        assert ErrorSeverity.MEDIUM.value == "medium"
        assert ErrorSeverity.HIGH.value == "high"
        assert ErrorSeverity.CRITICAL.value == "critical"

    def test_count(self):
        assert len(ErrorSeverity) == 4


class TestRetryStrategy:
    def test_values(self):
        assert RetryStrategy.NO_RETRY.value == "no_retry"
        assert RetryStrategy.FIXED_DELAY.value == "fixed_delay"
        assert RetryStrategy.EXPONENTIAL_BACKOFF.value == "exponential_backoff"
        assert RetryStrategy.LINEAR_BACKOFF.value == "linear_backoff"

    def test_count(self):
        assert len(RetryStrategy) == 4


# ── ErrorContext ─────────────────────────────────────────────────────
class TestErrorContext:
    def test_defaults(self):
        ctx = ErrorContext(
            operation="op",
            component="comp",
            user_message="msg",
            technical_details={},
            severity=ErrorSeverity.LOW,
        )
        assert ctx.retry_strategy is RetryStrategy.NO_RETRY
        assert ctx.max_retries == 3
        assert ctx.suggestions == []
        assert ctx.recovery_actions == []

    def test_none_suggestions_defaulted(self):
        ctx = ErrorContext(
            operation="op",
            component="c",
            user_message="m",
            technical_details={},
            severity=ErrorSeverity.HIGH,
            suggestions=None,
            recovery_actions=None,
        )
        assert ctx.suggestions == []
        assert ctx.recovery_actions == []

    def test_explicit_suggestions_kept(self):
        ctx = ErrorContext(
            operation="op",
            component="c",
            user_message="m",
            technical_details={},
            severity=ErrorSeverity.LOW,
            suggestions=["try X"],
        )
        assert ctx.suggestions == ["try X"]


# ── RetryManager._calculate_delay ────────────────────────────────────
class TestCalculateDelay:
    def setup_method(self):
        self.rm = RetryManager()

    def test_no_retry(self):
        assert self.rm._calculate_delay(RetryStrategy.NO_RETRY, 0, 1.0, 60.0, 2.0) == 0

    def test_fixed_delay(self):
        assert self.rm._calculate_delay(RetryStrategy.FIXED_DELAY, 0, 5.0, 60.0, 2.0) == 5.0
        assert self.rm._calculate_delay(RetryStrategy.FIXED_DELAY, 5, 5.0, 60.0, 2.0) == 5.0

    def test_fixed_delay_capped(self):
        assert self.rm._calculate_delay(RetryStrategy.FIXED_DELAY, 0, 100.0, 60.0, 2.0) == 60.0

    def test_linear_backoff(self):
        assert self.rm._calculate_delay(RetryStrategy.LINEAR_BACKOFF, 0, 2.0, 60.0, 2.0) == 2.0
        assert self.rm._calculate_delay(RetryStrategy.LINEAR_BACKOFF, 1, 2.0, 60.0, 2.0) == 4.0
        assert self.rm._calculate_delay(RetryStrategy.LINEAR_BACKOFF, 2, 2.0, 60.0, 2.0) == 6.0

    def test_linear_backoff_capped(self):
        assert self.rm._calculate_delay(RetryStrategy.LINEAR_BACKOFF, 100, 2.0, 60.0, 2.0) == 60.0

    def test_exponential_backoff(self):
        assert self.rm._calculate_delay(RetryStrategy.EXPONENTIAL_BACKOFF, 0, 1.0, 60.0, 2.0) == 1.0
        assert self.rm._calculate_delay(RetryStrategy.EXPONENTIAL_BACKOFF, 1, 1.0, 60.0, 2.0) == 2.0
        assert self.rm._calculate_delay(RetryStrategy.EXPONENTIAL_BACKOFF, 2, 1.0, 60.0, 2.0) == 4.0
        assert self.rm._calculate_delay(RetryStrategy.EXPONENTIAL_BACKOFF, 3, 1.0, 60.0, 2.0) == 8.0

    def test_exponential_capped(self):
        assert (
            self.rm._calculate_delay(RetryStrategy.EXPONENTIAL_BACKOFF, 10, 1.0, 60.0, 2.0) == 60.0
        )


# ── CircuitBreaker ───────────────────────────────────────────────────
class TestCircuitBreaker:
    def test_initial_state(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10)
        assert cb.state == "closed"
        assert cb.failure_count == 0

    def test_on_failure_increments(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10)
        cb._on_failure()
        assert cb.failure_count == 1
        assert cb.state == "closed"

    def test_on_failure_opens_at_threshold(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10)
        cb._on_failure()
        cb._on_failure()
        assert cb.state == "open"

    def test_on_success_resets_from_half_open(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10)
        cb.state = "half-open"
        cb.failure_count = 2
        cb._on_success()
        assert cb.state == "closed"
        assert cb.failure_count == 0

    def test_on_success_no_change_when_closed(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10)
        cb._on_success()
        assert cb.state == "closed"

    def test_should_attempt_reset_no_failure(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10)
        assert cb._should_attempt_reset() is True

    def test_should_attempt_reset_too_soon(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10)
        cb.last_failure_time = time.time()
        assert cb._should_attempt_reset() is False

    def test_should_attempt_reset_after_timeout(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=1)
        cb.last_failure_time = time.time() - 2
        assert cb._should_attempt_reset() is True


# ── GracefulDegradation ─────────────────────────────────────────────
class TestGracefulDegradation:
    def test_register_fallback(self):
        gd = GracefulDegradation()
        gd.register_fallback("op1", lambda: "fallback")
        assert len(gd._fallback_registry["op1"]) == 1

    def test_register_multiple_fallbacks(self):
        gd = GracefulDegradation()
        gd.register_fallback("op1", lambda: "a")
        gd.register_fallback("op1", lambda: "b")
        assert len(gd._fallback_registry["op1"]) == 2


# ── HealthChecker ────────────────────────────────────────────────────
class TestHealthChecker:
    def test_register_and_check(self):
        hc = HealthChecker()
        hc.register_health_check("db", lambda: True)
        assert hc.check_health("db") is True

    def test_unknown_dependency_returns_true(self):
        hc = HealthChecker()
        assert hc.check_health("unknown") is True

    def test_check_failure_returns_false(self):
        hc = HealthChecker()
        hc.register_health_check("bad", lambda: (_ for _ in ()).throw(RuntimeError("down")))
        assert hc.check_health("bad") is False

    def test_check_all_health(self):
        hc = HealthChecker()
        hc.register_health_check("ok", lambda: True)
        hc.register_health_check("bad", lambda: False)
        results = hc.check_all_health()
        assert results == {"ok": True, "bad": False}

    def test_check_all_health_exception(self):
        hc = HealthChecker()

        def explode():
            raise ValueError("oops")

        hc.register_health_check("x", explode)
        results = hc.check_all_health()
        assert results["x"] is False
