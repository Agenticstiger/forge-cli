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

"""Tests for fluid_build/util/network.py — HTTP wrappers, circuit breaker, rate limiter."""

import time

import httpx
import pytest
import respx

from fluid_build.errors import NetworkError
from fluid_build.util.network import (
    DEFAULT_BACKOFF_FACTOR,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RATE_LIMIT,
    DEFAULT_TIMEOUT,
    CircuitBreaker,
    CircuitState,
    RateLimiter,
    create_session_with_retries,
    safe_request,
)


class TestCircuitState:
    def test_values(self):
        assert CircuitState.CLOSED.value == "closed"
        assert CircuitState.OPEN.value == "open"
        assert CircuitState.HALF_OPEN.value == "half_open"


class TestCircuitBreaker:
    def test_initial_state(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_success_keeps_closed(self):
        cb = CircuitBreaker(failure_threshold=3)
        result = cb.call(lambda: "ok")
        assert result == "ok"
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=2, timeout=60.0)

        for _ in range(2):
            with pytest.raises(ValueError):
                cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))

        assert cb.state == CircuitState.OPEN
        assert cb.failure_count == 2

    def test_open_rejects(self):
        cb = CircuitBreaker(failure_threshold=1, timeout=60.0)

        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("boom")))

        assert cb.state == CircuitState.OPEN
        with pytest.raises(NetworkError, match="OPEN"):
            cb.call(lambda: "should not run")

    def test_half_open_recovery(self):
        cb = CircuitBreaker(failure_threshold=1, timeout=0)

        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))

        assert cb.state == CircuitState.OPEN
        # timeout=0 → immediate reset attempt
        result = cb.call(lambda: "recovered")
        assert result == "recovered"
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_should_attempt_reset_no_failure(self):
        cb = CircuitBreaker()
        assert cb._should_attempt_reset() is True

    def test_failure_resets_on_success(self):
        cb = CircuitBreaker(failure_threshold=5)
        # One failure
        with pytest.raises(RuntimeError):
            cb.call(lambda: (_ for _ in ()).throw(RuntimeError("err")))
        assert cb.failure_count == 1
        # Success resets
        cb.call(lambda: "ok")
        assert cb.failure_count == 0


class TestRateLimiter:
    def test_under_limit(self):
        rl = RateLimiter(calls=10, period=1.0)

        @rl
        def func():
            return "ok"

        # Should not block for small number of calls
        for _ in range(5):
            assert func() == "ok"

    def test_wait_if_needed_no_wait(self):
        rl = RateLimiter(calls=100, period=60.0)
        rl.wait_if_needed()
        assert len(rl.timestamps) == 1

    def test_timestamp_cleanup(self):
        rl = RateLimiter(calls=100, period=0.01)
        rl.timestamps = [time.time() - 1.0]  # Old timestamp
        rl.wait_if_needed()
        # Old timestamp should be cleaned up
        assert all(time.time() - ts < 1.0 for ts in rl.timestamps)


class TestDefaults:
    def test_default_values(self):
        assert DEFAULT_TIMEOUT == 30
        assert DEFAULT_CONNECT_TIMEOUT == 10
        assert DEFAULT_MAX_RETRIES == 3
        assert DEFAULT_BACKOFF_FACTOR == 2
        assert DEFAULT_RATE_LIMIT == 50


class TestSafeRequest:
    """``safe_request`` over httpx: success, HTTP status, timeout and connection errors."""

    URL = "https://api.example.test/thing"

    def test_success_returns_response(self):
        with respx.mock() as router:
            router.get(self.URL).respond(200, json={"ok": True})
            with httpx.Client() as session:
                response = safe_request("GET", self.URL, session=session)
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_http_error_status_raises_network_error_with_status(self):
        with respx.mock() as router:
            router.get(self.URL).respond(404)
            with httpx.Client() as session:
                with pytest.raises(NetworkError, match="HTTP 404") as raised:
                    safe_request("GET", self.URL, session=session)
        assert raised.value.context["status_code"] == 404

    def test_timeout_raises_network_error(self):
        with respx.mock() as router:
            router.get(self.URL).mock(side_effect=httpx.ReadTimeout("slow"))
            with httpx.Client() as session:
                with pytest.raises(NetworkError, match="timed out"):
                    safe_request("GET", self.URL, session=session)

    def test_connection_error_raises_network_error(self):
        with respx.mock() as router:
            router.get(self.URL).mock(side_effect=httpx.ConnectError("refused"))
            with httpx.Client() as session:
                with pytest.raises(NetworkError, match="Connection failed"):
                    safe_request("GET", self.URL, session=session)

    def test_retrying_session_retries_a_transient_status(self):
        with respx.mock() as router:
            route = router.get(self.URL).mock(
                side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
            )
            session = create_session_with_retries(max_retries=2, backoff_factor=0)
            try:
                response = safe_request("GET", self.URL, session=session)
            finally:
                session.close()
        assert response.status_code == 200
        assert route.call_count == 2
