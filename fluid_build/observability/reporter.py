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
Command Center reporter with circuit breaker and async queue.
"""

import logging
import queue
import threading
import time
from enum import Enum
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    requests = None  # type: ignore

from .config import CommandCenterConfig

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, stop trying
    HALF_OPEN = "half_open"  # Testing if service recovered


class CircuitBreaker:
    """
    Circuit breaker to prevent repeated failures from slowing down CLI.

    States:
    - CLOSED: Normal operation, requests allowed
    - OPEN: Too many failures, requests blocked
    - HALF_OPEN: Testing if service recovered

    Transitions:
    - CLOSED → OPEN: After `failure_threshold` consecutive failures
    - OPEN → HALF_OPEN: After `timeout` seconds
    - HALF_OPEN → CLOSED: After successful request
    - HALF_OPEN → OPEN: After any failure
    """

    def __init__(self, failure_threshold: int = 5, timeout: int = 60, success_threshold: int = 1):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.success_threshold = success_threshold

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[float] = None
        self._lock = threading.Lock()

    def call(self, func, *args, **kwargs):
        """
        Execute function with circuit breaker protection.

        Returns:
            Result from function, or None if circuit is OPEN
        """
        with self._lock:
            # Check if we should transition from OPEN to HALF_OPEN
            if self.state == CircuitState.OPEN:
                if (
                    self.last_failure_time
                    and (time.time() - self.last_failure_time) >= self.timeout
                ):
                    logger.info("Circuit breaker transitioning OPEN → HALF_OPEN (testing recovery)")
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                else:
                    # Still in OPEN state, don't try
                    return None

            # If OPEN, don't try (before timeout)
            if self.state == CircuitState.OPEN:
                return None

        # Try to execute
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure(e)
            return None

    def _on_success(self):
        """Handle successful call."""
        with self._lock:
            self.failure_count = 0

            if self.state == CircuitState.HALF_OPEN:
                self.success_count += 1
                if self.success_count >= self.success_threshold:
                    logger.info(
                        "Circuit breaker transitioning HALF_OPEN → CLOSED (recovery confirmed)"
                    )
                    self.state = CircuitState.CLOSED
                    self.success_count = 0

    def _on_failure(self, error: Exception):
        """Handle failed call."""
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()

            if self.state == CircuitState.HALF_OPEN:
                logger.warning(
                    f"Circuit breaker transitioning HALF_OPEN → OPEN (recovery failed: {error})"
                )
                self.state = CircuitState.OPEN
                self.failure_count = 0
            elif self.failure_count >= self.failure_threshold:
                logger.warning(
                    f"Circuit breaker transitioning CLOSED → OPEN "
                    f"(threshold reached: {self.failure_count} failures)"
                )
                self.state = CircuitState.OPEN


class CommandCenterReporter:
    """
    Non-blocking reporter for Command Center integration.

    Features:
    - Async event queue (never blocks CLI execution)
    - Background worker thread
    - Circuit breaker (stops trying if CC is down)
    - Automatic retry with exponential backoff
    - Graceful shutdown

    Usage:
        config = CommandCenterConfig.from_environment()
        reporter = CommandCenterReporter(config)
        reporter.start()

        # Register execution
        reporter.register_execution(
            execution_id="550e8400-...",
            command="validate",
            contract_path="contracts/sales.yaml"
        )

        # Update status
        reporter.update_execution(
            execution_id="550e8400-...",
            status="success",
            progress=100.0
        )

        # Shutdown
        reporter.stop()
    """

    def __init__(self, config: CommandCenterConfig):
        self.config = config
        self.enabled = config.is_configured()

        # Event queue for async processing
        self.queue: queue.Queue = queue.Queue(maxsize=1000)

        # Background worker
        self.worker_thread: Optional[threading.Thread] = None
        self.running = False

        # Circuit breaker
        self.circuit_breaker = CircuitBreaker(failure_threshold=5, timeout=60, success_threshold=1)

        # Session for connection pooling
        self.session: Optional[Any] = None
        if self.enabled and requests:
            self.session = requests.Session()
            self.session.headers.update(
                {"X-API-Key": config.api_key, "Content-Type": "application/json"}
            )

    def start(self):
        """Start background worker thread."""
        if not self.enabled:
            logger.debug("Command Center not configured, reporter disabled")
            return

        if not requests:
            logger.warning("requests library not installed, Command Center integration disabled")
            self.enabled = False
            return

        if self.running:
            return

        self.running = True
        self.worker_thread = threading.Thread(target=self._worker, daemon=True, name="CC-Reporter")
        self.worker_thread.start()
        logger.info(f"Command Center reporter started (url={self.config.url})")

    def stop(self, timeout: float = 5.0):
        """
        Stop background worker and flush remaining events.

        Args:
            timeout: Max time to wait for queue to drain (seconds)
        """
        if not self.running:
            return

        self.running = False

        # Wait for worker to finish
        if self.worker_thread:
            self.worker_thread.join(timeout=timeout)

        # Close session
        if self.session:
            self.session.close()

        logger.info("Command Center reporter stopped")

    def register_execution(
        self,
        execution_id: str,
        command: str,
        contract_path: Optional[str] = None,
        provider: Optional[str] = None,
        environment: Optional[str] = None,
        git_info: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        """
        Register a new execution with Command Center.

        Args:
            execution_id: Unique execution ID (UUID)
            command: Command being executed (validate, plan, apply, etc.)
            contract_path: Path to contract file
            provider: Cloud provider (aws, gcp, azure, snowflake, etc.)
            environment: Target environment (dev, staging, prod)
            git_info: Git context from get_git_info()
            **kwargs: Additional execution context
        """
        if not self.enabled:
            return

        payload = {
            "execution_id": execution_id,
            "command": command,
            "contract_path": contract_path,
            "provider": provider,
            "environment": environment,
            "status": "running",
            **kwargs,
        }

        # Add git context
        if git_info:
            payload.update(
                {
                    "git_repo": git_info.get("repo"),
                    "git_commit": git_info.get("commit"),
                    "git_branch": git_info.get("branch"),
                    "git_tag": git_info.get("tag"),
                    "git_author": git_info.get("author"),
                }
            )

        self._enqueue("POST", "/api/v1/executions", payload)

    def update_execution(
        self,
        execution_id: str,
        status: Optional[str] = None,
        progress: Optional[float] = None,
        current_phase: Optional[str] = None,
        error_message: Optional[str] = None,
        error_traceback: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        """
        Update an existing execution.

        Args:
            execution_id: Execution ID to update
            status: Updated status (running, success, failed, etc.)
            progress: Progress percentage (0-100)
            current_phase: Current execution phase
            error_message: Error message if failed
            error_traceback: Full error traceback
            result: Execution results (JSON)
            **kwargs: Additional metadata
        """
        if not self.enabled:
            return

        payload = {}
        if status:
            payload["status"] = status
        if progress is not None:
            payload["progress"] = progress
        if current_phase:
            payload["current_phase"] = current_phase
        if error_message:
            payload["error_message"] = error_message
        if error_traceback:
            payload["error_traceback"] = error_traceback
        if result:
            payload["result"] = result
        if kwargs:
            payload["metadata"] = kwargs

        if payload:
            self._enqueue("PATCH", f"/api/v1/executions/{execution_id}", payload)

    def send_logs(self, execution_id: str, logs: List[Dict[str, Any]]):
        """
        Send batch of logs to Command Center.

        Args:
            execution_id: Execution ID
            logs: List of log entries (max 1000)
        """
        if not self.enabled or not logs:
            return

        payload = {"execution_id": execution_id, "logs": logs[:1000]}  # Enforce max batch size
        self._enqueue("POST", "/api/v1/logs/batch", payload)

    def send_metrics(self, execution_id: str, metrics: List[Dict[str, Any]]):
        """
        Send batch of metrics to Command Center.

        Args:
            execution_id: Execution ID
            metrics: List of metric entries (max 1000)
        """
        if not self.enabled or not metrics:
            return

        payload = {
            "execution_id": execution_id,
            "metrics": metrics[:1000],  # Enforce max batch size
        }
        self._enqueue("POST", "/api/v1/metrics/batch", payload)

    def _enqueue(self, method: str, endpoint: str, payload: Dict[str, Any]):
        """Add event to queue for async processing."""
        try:
            self.queue.put_nowait(
                {
                    "method": method,
                    "endpoint": endpoint,
                    "payload": payload,
                    "timestamp": time.time(),
                }
            )
        except queue.Full:
            logger.warning("Command Center event queue full, dropping event")

    def _worker(self):
        """Background worker thread that processes events."""
        logger.debug("Command Center reporter worker started")

        while self.running or not self.queue.empty():
            try:
                # Get event from queue (with timeout to check running flag)
                try:
                    event = self.queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                # Send to Command Center (with circuit breaker)
                self.circuit_breaker.call(self._send_event, event)

            except Exception as e:
                logger.error(f"Error in Command Center worker: {e}", exc_info=True)

        logger.debug("Command Center reporter worker stopped")

    def _send_event(self, event: Dict[str, Any]):
        """
        Send event to Command Center.

        Raises:
            Exception: If request fails (circuit breaker will handle)
        """
        if not self.session:
            return

        method = event["method"]
        endpoint = event["endpoint"]
        payload = event["payload"]

        url = f"{self.config.url.rstrip('/')}{endpoint}"

        try:
            if method == "POST":
                response = self.session.post(url, json=payload, timeout=self.config.timeout)
            elif method == "PATCH":
                response = self.session.patch(url, json=payload, timeout=self.config.timeout)
            else:
                raise ValueError(f"Unsupported method: {method}")

            response.raise_for_status()
            logger.debug(f"Command Center: {method} {endpoint} → {response.status_code}")

        except Exception as e:
            logger.warning(f"Failed to send event to Command Center: {e}")
            raise
