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

"""Tests for fluid_build/cli/resilience.py — retry, circuit breaker, health, degradation."""

import pytest

from fluid_build.cli.resilience import (
    CircuitBreaker,
    ErrorContext,
    ErrorSeverity,
    GracefulDegradation,
    HealthChecker,
    RetryManager,
    RetryStrategy,
    TimeoutManager,
)

# ── Enums & Dataclasses ─────────────────────────────────────────────────


class TestEnums:
    def test_error_severity_values(self):
        assert ErrorSeverity.LOW.value == "low"
        assert ErrorSeverity.CRITICAL.value == "critical"

    def test_retry_strategy_values(self):
        assert RetryStrategy.NO_RETRY.value == "no_retry"
        assert RetryStrategy.EXPONENTIAL_BACKOFF.value == "exponential_backoff"


class TestErrorContext:
    def test_basic(self):
        ctx = ErrorContext(
            operation="validate",
            component="schema",
            user_message="Validation failed",
            technical_details={"field": "id"},
            severity=ErrorSeverity.HIGH,
        )
        assert ctx.operation == "validate"
        assert ctx.suggestions == []
        assert ctx.recovery_actions == []

    def test_with_suggestions(self):
        ctx = ErrorContext(
            operation="deploy",
            component="gcp",
            user_message="Deploy failed",
            technical_details={},
            severity=ErrorSeverity.MEDIUM,
            suggestions=["Check permissions"],
            recovery_actions=["Rollback"],
        )
        assert ctx.suggestions == ["Check permissions"]
        assert ctx.recovery_actions == ["Rollback"]


# ── RetryManager ────────────────────────────────────────────────────────


class TestRetryManager:
    def test_calculate_delay_no_retry(self):
        rm = RetryManager()
        assert rm._calculate_delay(RetryStrategy.NO_RETRY, 0, 1.0, 60.0, 2.0) == 0

    def test_calculate_delay_fixed(self):
        rm = RetryManager()
        # Fixed delay should always be base_delay, capped by max_delay
        assert rm._calculate_delay(RetryStrategy.FIXED_DELAY, 0, 5.0, 60.0, 2.0) == 5.0
        assert rm._calculate_delay(RetryStrategy.FIXED_DELAY, 5, 5.0, 3.0, 2.0) == 3.0

    def test_calculate_delay_linear(self):
        rm = RetryManager()
        assert rm._calculate_delay(RetryStrategy.LINEAR_BACKOFF, 0, 1.0, 60.0, 2.0) == 1.0
        assert rm._calculate_delay(RetryStrategy.LINEAR_BACKOFF, 2, 1.0, 60.0, 2.0) == 3.0
        # Capped at max_delay
        assert rm._calculate_delay(RetryStrategy.LINEAR_BACKOFF, 100, 1.0, 10.0, 2.0) == 10.0

    def test_calculate_delay_exponential(self):
        rm = RetryManager()
        assert rm._calculate_delay(RetryStrategy.EXPONENTIAL_BACKOFF, 0, 1.0, 60.0, 2.0) == 1.0
        assert rm._calculate_delay(RetryStrategy.EXPONENTIAL_BACKOFF, 1, 1.0, 60.0, 2.0) == 2.0
        assert rm._calculate_delay(RetryStrategy.EXPONENTIAL_BACKOFF, 2, 1.0, 60.0, 2.0) == 4.0
        # Capped at max_delay
        assert rm._calculate_delay(RetryStrategy.EXPONENTIAL_BACKOFF, 10, 1.0, 10.0, 2.0) == 10.0

    def test_retry_succeeds_first_try(self):
        rm = RetryManager()

        @rm.retry(strategy=RetryStrategy.NO_RETRY, max_attempts=3)
        def good():
            return "ok"

        assert good() == "ok"

    def test_retry_succeeds_after_failures(self):
        rm = RetryManager()
        call_count = 0

        @rm.retry(strategy=RetryStrategy.FIXED_DELAY, max_attempts=3, base_delay=0.01)
        def flaky():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("not yet")
            return "done"

        assert flaky() == "done"
        assert call_count == 3

    def test_retry_all_fail(self):
        rm = RetryManager()

        @rm.retry(strategy=RetryStrategy.FIXED_DELAY, max_attempts=2, base_delay=0.01)
        def always_fails():
            raise RuntimeError("always")

        with pytest.raises(Exception, match="operation_failed_after_retries"):
            always_fails()


# ── CircuitBreaker ──────────────────────────────────────────────────────


class TestCircuitBreaker:
    def test_closed_state(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=10)
        assert cb.state == "closed"

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60)

        @cb
        def fail_func():
            raise ValueError("boom")

        for _ in range(2):
            with pytest.raises(ValueError):
                fail_func()

        assert cb.state == "open"

    def test_open_rejects_calls(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60)

        @cb
        def fail_func():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            fail_func()

        assert cb.state == "open"
        with pytest.raises(Exception, match="circuit_breaker_open"):
            fail_func()

    def test_half_open_on_recovery_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0)

        @cb
        def fail_then_succeed():
            if cb.failure_count == 1 and cb.state in ("half-open",):
                return "recovered"
            raise ValueError("fail")

        with pytest.raises(ValueError):
            fail_then_succeed()

        assert cb.state == "open"
        # recovery_timeout=0 means immediate half-open
        result = fail_then_succeed()
        assert result == "recovered"
        assert cb.state == "closed"

    def test_success_resets_on_half_open(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0)

        call_count = 0

        @cb
        def func():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("first fail")
            return "ok"

        with pytest.raises(ValueError):
            func()

        assert cb.state == "open"
        assert func() == "ok"
        assert cb.state == "closed"
        assert cb.failure_count == 0


# ── GracefulDegradation ────────────────────────────────────────────────


class TestGracefulDegradation:
    def test_primary_succeeds(self):
        gd = GracefulDegradation()

        @gd.with_fallback("op1")
        def primary():
            return "primary_result"

        assert primary() == "primary_result"

    def test_fallback_used(self):
        gd = GracefulDegradation()
        gd.register_fallback("op2", lambda: "fallback_result")

        @gd.with_fallback("op2")
        def primary():
            raise ValueError("oops")

        assert primary() == "fallback_result"

    def test_multiple_fallbacks(self):
        gd = GracefulDegradation()
        gd.register_fallback("op3", lambda: (_ for _ in ()).throw(ValueError("fb1 fails")))
        gd.register_fallback("op3", lambda: "fb2_result")

        @gd.with_fallback("op3")
        def primary():
            raise RuntimeError("primary fails")

        assert primary() == "fb2_result"

    def test_all_fallbacks_fail(self):
        gd = GracefulDegradation()
        gd.register_fallback("op4", lambda: (_ for _ in ()).throw(ValueError("fb fails")))

        @gd.with_fallback("op4")
        def primary():
            raise RuntimeError("primary fails")

        with pytest.raises(Exception, match="operation_and_fallbacks_failed"):
            primary()


# ── HealthChecker ───────────────────────────────────────────────────────


class TestHealthChecker:
    def test_register_and_check(self):
        hc = HealthChecker()
        hc.register_health_check("db", lambda: True)
        assert hc.check_health("db") is True

    def test_unhealthy(self):
        hc = HealthChecker()
        hc.register_health_check("api", lambda: False)
        assert hc.check_health("api") is False

    def test_check_throws(self):
        hc = HealthChecker()
        hc.register_health_check("bad", lambda: (_ for _ in ()).throw(RuntimeError("err")))
        assert hc.check_health("bad") is False

    def test_unregistered_assumed_healthy(self):
        hc = HealthChecker()
        assert hc.check_health("unknown") is True

    def test_check_all(self):
        hc = HealthChecker()
        hc.register_health_check("a", lambda: True)
        hc.register_health_check("b", lambda: False)
        results = hc.check_all_health()
        assert results == {"a": True, "b": False}

    def test_require_healthy_pass(self):
        hc = HealthChecker()
        hc.register_health_check("svc", lambda: True)

        @hc.require_healthy("svc")
        def do_work():
            return "done"

        assert do_work() == "done"

    def test_require_healthy_fail(self):
        hc = HealthChecker()
        hc.register_health_check("svc", lambda: False)

        @hc.require_healthy("svc")
        def do_work():
            return "done"

        with pytest.raises(Exception, match="dependency_unhealthy"):
            do_work()


# ── TimeoutManager ──────────────────────────────────────────────────────


class TestTimeoutManager:
    def test_no_timeout(self):
        tm = TimeoutManager(default_timeout=10)
        with tm.timeout(seconds=10, operation="quick"):
            pass  # no timeout expected

    def test_get_active_timeouts_empty(self):
        tm = TimeoutManager()
        assert tm.get_active_timeouts() == {}
