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

"""Tests for fluid_build.observability.reporter (CircuitBreaker + CommandCenterReporter)."""

import queue
import time
from unittest.mock import MagicMock, Mock, patch

import pytest

from fluid_build.observability.reporter import (
    CircuitBreaker,
    CircuitState,
    CommandCenterReporter,
)

# ── CircuitState ──────────────────────────────────────────────────────


class TestCircuitState:
    def test_states_exist(self):
        assert CircuitState.CLOSED.value == "closed"
        assert CircuitState.OPEN.value == "open"
        assert CircuitState.HALF_OPEN.value == "half_open"


# ── CircuitBreaker ────────────────────────────────────────────────────


class TestCircuitBreaker:
    def test_initial_state_is_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED

    def test_successful_call_stays_closed(self):
        cb = CircuitBreaker()
        result = cb.call(lambda: "ok")
        assert result == "ok"
        assert cb.state == CircuitState.CLOSED

    def test_transitions_to_open_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.call(self._failing_func)
        assert cb.state == CircuitState.OPEN

    def test_open_circuit_returns_none(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.call(self._failing_func)  # opens circuit
        assert cb.state == CircuitState.OPEN
        result = cb.call(lambda: "should not run")
        assert result is None

    def test_open_to_half_open_after_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, timeout=0)
        cb.call(self._failing_func)  # opens circuit
        assert cb.state == CircuitState.OPEN
        # With timeout=0, next call should transition to HALF_OPEN
        time.sleep(0.01)
        result = cb.call(lambda: "recovered")
        assert result == "recovered"
        assert cb.state == CircuitState.CLOSED

    def test_half_open_to_open_on_failure(self):
        cb = CircuitBreaker(failure_threshold=1, timeout=0)
        cb.call(self._failing_func)  # CLOSED → OPEN
        time.sleep(0.01)
        # Next call transitions OPEN → HALF_OPEN, then fails → OPEN
        cb.call(self._failing_func)
        assert cb.state == CircuitState.OPEN

    def test_half_open_to_closed_on_success(self):
        cb = CircuitBreaker(failure_threshold=1, timeout=0, success_threshold=1)
        cb.call(self._failing_func)  # CLOSED → OPEN
        time.sleep(0.01)
        cb.call(lambda: "ok")  # OPEN → HALF_OPEN → CLOSED
        assert cb.state == CircuitState.CLOSED

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.call(self._failing_func)
        cb.call(self._failing_func)
        assert cb.failure_count == 2
        cb.call(lambda: "ok")
        assert cb.failure_count == 0

    @staticmethod
    def _failing_func():
        raise RuntimeError("fail")


# ── CommandCenterReporter ─────────────────────────────────────────────


class TestCommandCenterReporter:
    def _make_config(self, configured=False):
        config = MagicMock()
        config.is_configured.return_value = configured
        config.url = "http://localhost:8000"
        config.api_key = "test-key"
        config.timeout = 5
        return config

    def test_disabled_when_not_configured(self):
        config = self._make_config(configured=False)
        reporter = CommandCenterReporter(config)
        assert reporter.enabled is False

    def test_start_does_nothing_when_disabled(self):
        config = self._make_config(configured=False)
        reporter = CommandCenterReporter(config)
        reporter.start()
        assert reporter.running is False

    def test_stop_does_nothing_when_not_running(self):
        config = self._make_config(configured=False)
        reporter = CommandCenterReporter(config)
        reporter.stop()  # should not raise

    def test_register_execution_noop_when_disabled(self):
        config = self._make_config(configured=False)
        reporter = CommandCenterReporter(config)
        reporter.register_execution(execution_id="123", command="validate")
        assert reporter.queue.empty()

    def test_update_execution_noop_when_disabled(self):
        config = self._make_config(configured=False)
        reporter = CommandCenterReporter(config)
        reporter.update_execution(execution_id="123", status="success")
        assert reporter.queue.empty()

    def test_send_logs_noop_when_disabled(self):
        config = self._make_config(configured=False)
        reporter = CommandCenterReporter(config)
        reporter.send_logs("123", [{"msg": "test"}])
        assert reporter.queue.empty()

    def test_send_logs_noop_when_empty_logs(self):
        config = self._make_config(configured=True)
        with patch("fluid_build.observability.reporter.requests", MagicMock()):
            reporter = CommandCenterReporter(config)
            reporter.send_logs("123", [])
            assert reporter.queue.empty()

    def test_send_metrics_noop_when_disabled(self):
        config = self._make_config(configured=False)
        reporter = CommandCenterReporter(config)
        reporter.send_metrics("123", [{"name": "duration", "value": 1.0}])
        assert reporter.queue.empty()

    @patch("fluid_build.observability.reporter.requests")
    def test_enqueue_adds_to_queue(self, _mock_requests):
        config = self._make_config(configured=True)
        reporter = CommandCenterReporter(config)
        reporter._enqueue("POST", "/api/v1/test", {"key": "val"})
        assert not reporter.queue.empty()
        event = reporter.queue.get_nowait()
        assert event["method"] == "POST"
        assert event["endpoint"] == "/api/v1/test"

    @patch("fluid_build.observability.reporter.requests")
    def test_enqueue_drops_when_full(self, _mock_requests):
        config = self._make_config(configured=True)
        reporter = CommandCenterReporter(config)
        reporter.queue = queue.Queue(maxsize=1)
        reporter._enqueue("POST", "/test1", {})
        reporter._enqueue("POST", "/test2", {})  # should be dropped
        assert reporter.queue.qsize() == 1

    @patch("fluid_build.observability.reporter.requests")
    def test_register_execution_with_git_info(self, _mock_requests):
        config = self._make_config(configured=True)
        reporter = CommandCenterReporter(config)
        reporter.register_execution(
            execution_id="abc",
            command="plan",
            provider="gcp",
            git_info={"repo": "test", "commit": "abc123", "branch": "main"},
        )
        event = reporter.queue.get_nowait()
        assert event["payload"]["git_repo"] == "test"
        assert event["payload"]["git_commit"] == "abc123"

    @patch("fluid_build.observability.reporter.requests")
    def test_update_execution_payload(self, _mock_requests):
        config = self._make_config(configured=True)
        reporter = CommandCenterReporter(config)
        reporter.update_execution(
            execution_id="abc",
            status="success",
            progress=100.0,
            current_phase="done",
        )
        event = reporter.queue.get_nowait()
        assert event["payload"]["status"] == "success"
        assert event["payload"]["progress"] == 100.0

    @patch("fluid_build.observability.reporter.requests")
    def test_update_execution_empty_payload_not_enqueued(self, _mock_requests):
        config = self._make_config(configured=True)
        reporter = CommandCenterReporter(config)
        reporter.update_execution(execution_id="abc")
        assert reporter.queue.empty()

    @patch("fluid_build.observability.reporter.requests")
    def test_send_event_post(self, _mock_requests):
        config = self._make_config(configured=True)
        reporter = CommandCenterReporter(config)
        mock_response = MagicMock()
        mock_response.status_code = 200
        reporter.session = MagicMock()
        reporter.session.post.return_value = mock_response

        event = {"method": "POST", "endpoint": "/api/v1/test", "payload": {"k": "v"}}
        reporter._send_event(event)
        reporter.session.post.assert_called_once()

    @patch("fluid_build.observability.reporter.requests")
    def test_send_event_patch(self, _mock_requests):
        config = self._make_config(configured=True)
        reporter = CommandCenterReporter(config)
        mock_response = MagicMock()
        reporter.session = MagicMock()
        reporter.session.patch.return_value = mock_response

        event = {"method": "PATCH", "endpoint": "/api/v1/test/123", "payload": {}}
        reporter._send_event(event)
        reporter.session.patch.assert_called_once()

    @patch("fluid_build.observability.reporter.requests")
    def test_send_event_unsupported_method_raises(self, _mock_requests):
        config = self._make_config(configured=True)
        reporter = CommandCenterReporter(config)
        reporter.session = MagicMock()

        event = {"method": "DELETE", "endpoint": "/test", "payload": {}}
        with pytest.raises(ValueError, match="Unsupported method"):
            reporter._send_event(event)
