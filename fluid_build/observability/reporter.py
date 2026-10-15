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
import os
import queue
import threading
import time
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlsplit

try:
    import requests
except ImportError:
    requests = None  # type: ignore

# Reuse the canonical SSRF post-DNS-resolution gate (RFC1918,
# link-local 169.254.0.0/16 — AWS/GCP metadata — loopback, reserved;
# fails closed on DNS errors). Sourced from the tier-0 ``_net`` module
# (PR #139) so this observability surface does NOT need to import
# build_runners at package-init time — the structural cycle fix
# removes the need for the function-local import that previously
# guarded against re-entering ``cli/__init__.py``.
from fluid_build._net import _hostname_is_private

from .config import CommandCenterConfig

logger = logging.getLogger(__name__)

# Opt-in allow-list for a Command Center on a genuinely internal
# address; loopback (the local dev default) is always permitted. The
# SSRF concern is a config-supplied URL bouncing the X-API-Key header
# to a cloud-metadata endpoint.
_CC_HOST_ALLOWLIST_ENV = "FLUID_COMMAND_CENTER_HOST_ALLOWLIST"
_CC_ALWAYS_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def endpoint_origin(url: Optional[str]) -> str:
    """``scheme://host[:port]`` of ``url``: what a log line may say about it.

    The path and query can carry a secret (a token segment, a placeholder that
    was expanded) and userinfo is a credential, so neither is ever logged; the
    origin alone tells the operator where a request went. The port appears
    only when the URL names one.
    """
    parts = urlsplit(url or "")
    host = parts.hostname
    if not host:
        return "<endpoint with no host>"
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    origin = f"{parts.scheme or '?'}://{host}"
    return f"{origin}:{port}" if port else origin


def _command_center_host_refusal(url: Optional[str]) -> Optional[str]:
    """Why the reporter (or a catalog publish) may not send a credential to ``url``.

    The one Command Center host gate: the Command Center client's detection
    probes (``cli/_command_center.py``) use it too. ``None`` when it may.
    Loopback and any host on ``FLUID_COMMAND_CENTER_HOST_ALLOWLIST`` are
    permitted over http or https.
    Any other host needs https (the credential travels as a header) and must
    not resolve to a private/link-local/loopback/cloud-metadata address
    (:func:`_hostname_is_private`, which fails closed on DNS errors).
    """
    if not url:
        return "no endpoint is configured"
    parts = urlparse(url)
    host = parts.hostname
    if not host:
        return "the endpoint has no host"
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return f"the endpoint scheme is {scheme or 'missing'}; use an https:// endpoint"
    if host in _CC_ALWAYS_ALLOWED_HOSTS:
        return None
    suffixes = [
        s.strip() for s in os.environ.get(_CC_HOST_ALLOWLIST_ENV, "").split(",") if s.strip()
    ]
    if any(host == s or host.endswith("." + s) for s in suffixes):
        return None
    if _hostname_is_private(host):
        return (
            "its host resolves to a private or metadata address; allow it with "
            f"{_CC_HOST_ALLOWLIST_ENV}"
        )
    if scheme != "https":
        return (
            "it is plain http, which would send the credential in clear text; use an "
            f"https:// endpoint, or allow-list the host with {_CC_HOST_ALLOWLIST_ENV}"
        )
    return None


def _command_center_host_allowed(url: Optional[str]) -> bool:
    """Return True when the reporter may send its credential to ``url``.

    See :func:`_command_center_host_refusal` for the rule and the reasons.
    """
    return _command_center_host_refusal(url) is None


def project_endpoint_refusal(catalog_name: str, catalog_config: Mapping[str, Any]) -> Optional[str]:
    """Why a catalog endpoint named by the project config may not be published to.

    ``None`` when it may. Every ``fluid publish`` path (``--target`` and a
    contract's ``properties.catalog.register``) applies this to an endpoint
    from a config file in the checkout: the same host gate the Command Center
    report uses (:func:`_command_center_host_refusal`). The message names only
    the origin, never the path, query or userinfo.
    """
    endpoint = catalog_config.get("endpoint") or catalog_config.get("url")
    if not endpoint:
        return None
    refusal = _command_center_host_refusal(str(endpoint))
    if refusal is None:
        return None
    return (
        f"Not publishing to catalog '{catalog_name}' at {endpoint_origin(str(endpoint))}, "
        f"an endpoint from the project config: {refusal}"
    )


_DEFAULT_PORTS = {"http": 80, "https": 443}


class CredentialRedirectRefused(Exception):
    """A credential-bearing request was redirected where its credential may not go.

    ``str()`` names origins only (:func:`endpoint_origin`), never a path or query.
    """


def _origin_of(url: str) -> Optional[Tuple[str, str, int]]:
    """``(scheme, host, port)`` of an http(s) URL, the port filled in; else ``None``."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        return None
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    return scheme, host, _DEFAULT_PORTS[scheme] if port is None else port


def credential_redirect_refusal(source: str, target: str) -> Optional[str]:
    """Why a request carrying a credential to ``source`` may not follow a redirect to ``target``.

    ``None`` when it may: ``target`` (absolute) is on the same origin (scheme,
    host and port), or is the same host moved from http to https on the
    default ports, the one cross-origin hop for which ``requests`` keeps
    ``Authorization`` (``should_strip_auth``). Every other hop is refused
    rather than followed with the credential stripped: the request body is the
    run's report, which is not for another origin either. An https request is
    never followed down to http.
    """
    old, new = _origin_of(source), _origin_of(target)
    if old is None or new is None:
        return f"it redirects to {endpoint_origin(target)}, which is not an http(s) origin"
    if old == new:
        return None
    if old[0] == "https" and new[0] != "https":
        return f"it redirects from https down to {endpoint_origin(target)}"
    if old == ("http", new[1], 80) and new == ("https", old[1], 443):
        return None
    return (
        f"it redirects from {endpoint_origin(source)} to another origin, "
        f"{endpoint_origin(target)}"
    )


def refuse_foreign_redirect(response: Any, *_args: Any, **_kwargs: Any) -> Any:
    """A ``requests`` response hook: raise :class:`CredentialRedirectRefused` for
    a redirect :func:`credential_redirect_refusal` refuses, before it is followed.

    ``requests`` runs a request's response hooks on every hop, so passing
    ``hooks={"response": refuse_foreign_redirect}`` to one call guards that
    call; :func:`guard_credential_redirects` installs it on a whole session.
    """
    if not response.is_redirect:
        return response
    target = urljoin(response.url, response.headers.get("location", ""))
    refusal = credential_redirect_refusal(response.url, target)
    if refusal is None:
        return response
    response.close()
    raise CredentialRedirectRefused(refusal)


def guard_credential_redirects(session: Any) -> Any:
    """Make ``session`` (a ``requests.Session``) refuse a redirect its credentials may not follow.

    ``requests`` follows redirects by default and, when the host changes, drops
    only ``Authorization``: a credential in any other header (``X-API-Key``)
    went on to wherever the endpoint redirected. A response hook
    (:func:`refuse_foreign_redirect`) sees every hop before ``requests``
    follows it, and raises :class:`CredentialRedirectRefused` for one
    :func:`credential_redirect_refusal` refuses. Returns ``session``.
    """
    session.hooks["response"].append(refuse_foreign_redirect)
    return session


def foreign_redirect_trace_config() -> Any:
    """An ``aiohttp.TraceConfig`` that refuses what :func:`refuse_foreign_redirect` refuses.

    The same rule for an ``aiohttp.ClientSession``, which follows redirects by
    default: built with ``trace_configs=[foreign_redirect_trace_config()]``, a
    session raises :class:`CredentialRedirectRefused` for every hop
    :func:`credential_redirect_refusal` refuses, on every request it makes,
    before ``aiohttp`` follows it.
    """
    import aiohttp

    async def _on_redirect(_session: Any, _context: Any, params: Any) -> None:
        response = params.response
        source = str(response.url)
        location = response.headers.get("Location") or response.headers.get("URI") or ""
        refusal = credential_redirect_refusal(source, urljoin(source, location))
        if refusal is not None:
            response.close()
            raise CredentialRedirectRefused(refusal)

    trace = aiohttp.TraceConfig()
    trace.on_request_redirect.append(_on_redirect)
    return trace


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
                # The type only: a requests exception's text repeats the URL.
                logger.warning(
                    "Circuit breaker transitioning HALF_OPEN → OPEN (recovery failed: %s)",
                    type(error).__name__,
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

        #: What the worker managed to deliver, for a caller that has to say
        #: whether the run reached the Command Center (the queue is async).
        self.stats: Dict[str, int] = {"sent": 0, "failed": 0}

        # Session for connection pooling. Every request carries the credential,
        # so a redirect is followed only within the endpoint's own origin.
        self.session: Optional[Any] = None
        if self.enabled and requests:
            self.session = guard_credential_redirects(requests.Session())
            headers = {"Content-Type": "application/json"}
            if config.api_key:
                headers["X-API-Key"] = config.api_key
            headers.update(config.headers or {})
            self.session.headers.update(headers)

    def start(self):
        """Start background worker thread."""
        if not self.enabled:
            logger.debug("Command Center not configured, reporter disabled")
            return

        if not requests:
            logger.warning("requests library not installed, Command Center integration disabled")
            self.enabled = False
            return

        # SSRF gate: ``config.url`` is user/config-supplied and every
        # event POST carries the X-API-Key header. Refuse to start the
        # reporter against a host that resolves to a private/link-local/
        # cloud-metadata address unless it is loopback (local dev) or
        # explicitly allow-listed — otherwise a poisoned config could
        # exfil the API key to e.g. http://169.254.169.254/.
        if not _command_center_host_allowed(self.config.url):
            logger.warning(
                "Command Center reporter disabled for %s: %s.",
                endpoint_origin(self.config.url),
                _command_center_host_refusal(self.config.url),
            )
            self.enabled = False
            return

        if self.running:
            return

        self.running = True
        self.worker_thread = threading.Thread(target=self._worker, daemon=True, name="CC-Reporter")
        self.worker_thread.start()
        logger.info(
            "Command Center reporter started (destination=%s)", endpoint_origin(self.config.url)
        )

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
            self.stats["sent"] += 1

        except Exception as e:
            self.stats["failed"] += 1
            if isinstance(e, CredentialRedirectRefused):
                # Its text names the origins only.
                outcome = f"not followed, {e}"
            else:
                # The type and the status only: a requests exception's text
                # repeats the URL, and nothing here needs the response body.
                status = getattr(getattr(e, "response", None), "status_code", None)
                outcome = str(status or type(e).__name__)
            logger.warning(
                "Failed to send event to Command Center: %s %s -> %s",
                method,
                endpoint,
                outcome,
            )
            raise
