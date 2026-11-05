# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""
FLUID Build - Network Utilities

Provides safe HTTP request wrappers with:
- Automatic timeouts
- Retry logic with exponential backoff
- Rate limiting
- Circuit breaker pattern
- Error handling
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    pass

from ..errors import NetworkError, wrap_error

logger = logging.getLogger(__name__)

# Default configuration
DEFAULT_TIMEOUT = 30  # seconds
DEFAULT_CONNECT_TIMEOUT = 10  # seconds
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_FACTOR = 2
DEFAULT_RATE_LIMIT = 50  # requests per minute


class CircuitState(Enum):
    """Circuit breaker states"""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, rejecting requests
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreaker:
    """
    Circuit breaker pattern to prevent cascading failures.

    Tracks failure rates and opens circuit when threshold exceeded.
    """

    failure_threshold: int = 5
    timeout: float = 60.0  # seconds

    def __post_init__(self):
        self.failure_count = 0
        self.last_failure_time: Optional[float] = None
        self.state = CircuitState.CLOSED

    def call(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with circuit breaker protection.

        Args:
            func: Function to execute
            *args, **kwargs: Arguments to pass to function

        Returns:
            Function result

        Raises:
            NetworkError: If circuit is open
        """
        if self.state == CircuitState.OPEN:
            if self._should_attempt_reset():
                self.state = CircuitState.HALF_OPEN
            else:
                raise NetworkError(
                    "Circuit breaker is OPEN - service appears to be down",
                    suggestions=[
                        "Wait a few minutes for service to recover",
                        "Check service status page",
                        "Verify network connectivity",
                    ],
                )

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception:
            self._on_failure()
            raise

    def _on_success(self):
        """Handle successful request"""
        self.failure_count = 0
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED
            logger.info("Circuit breaker closed - service recovered")

    def _on_failure(self):
        """Handle failed request"""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.warning(f"Circuit breaker opened after {self.failure_count} failures")

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset"""
        if self.last_failure_time is None:
            return True
        return (time.time() - self.last_failure_time) >= self.timeout


class RateLimiter:
    """
    Token bucket rate limiter.

    Limits requests per time window to avoid hitting API rate limits.
    """

    def __init__(self, calls: int, period: float = 60.0):
        """
        Initialize rate limiter.

        Args:
            calls: Number of calls allowed per period
            period: Time period in seconds (default: 60s)
        """
        self.calls = calls
        self.period = period
        self.timestamps: list[float] = []

    def __call__(self, func: Callable) -> Callable:
        """Decorator to rate limit a function"""

        @wraps(func)
        def wrapper(*args, **kwargs):
            self.wait_if_needed()
            return func(*args, **kwargs)

        return wrapper

    def wait_if_needed(self):
        """Wait if rate limit would be exceeded"""
        now = time.time()

        # Remove timestamps outside the window
        self.timestamps = [ts for ts in self.timestamps if now - ts < self.period]

        # Check if we need to wait
        if len(self.timestamps) >= self.calls:
            sleep_time = self.period - (now - self.timestamps[0])
            if sleep_time > 0:
                logger.debug(f"Rate limit reached, waiting {sleep_time:.2f}s")
                time.sleep(sleep_time)
                # Remove old timestamp after waiting
                self.timestamps.pop(0)

        # Add current timestamp
        self.timestamps.append(time.time())


def create_session_with_retries(
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    status_forcelist: tuple = (500, 502, 503, 504, 429),
) -> Any:  # returns httpx.Client
    """
    Create an httpx client with automatic retry configuration.

    Redirects are followed and the proxy environment is honoured, as the
    ``requests`` session this replaces did. Retries follow
    ``urllib3.util.retry.Retry`` semantics (see ``util.http_client``).

    Args:
        max_retries: Maximum number of retries
        backoff_factor: Backoff multiplier (delay = backoff_factor * (2 ** (retry_count - 1)),
            no delay before the first retry)
        status_forcelist: HTTP status codes that trigger retry

    Returns:
        Configured httpx.Client
    """
    from .http_client import new_client

    return new_client(
        retry={
            "total": max_retries,
            "backoff_factor": backoff_factor,
            "status_forcelist": status_forcelist,
            "allowed_methods": ["HEAD", "GET", "PUT", "POST", "DELETE", "OPTIONS", "TRACE"],
        }
    )


_REQUESTS_ONLY_KWARGS = {
    "verify": "pass the CA bundle through the client (REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE "
    "or util.http_client.new_client) -- verify= is a client option in httpx",
    "cert": "client certificates are a client option in httpx (new_client(cert=...))",
    "stream": "use session.stream(...) for a streamed response in httpx",
}


def safe_request(
    method: str,
    url: str,
    *,
    timeout: Optional[float] = None,
    connect_timeout: Optional[float] = None,
    session: Optional[Any] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    **kwargs,
) -> Any:  # returns httpx.Response
    """
    Make HTTP request with safety features.

    Args:
        method: HTTP method (GET, POST, etc.)
        url: Request URL
        timeout: Read timeout in seconds
        connect_timeout: Connection timeout in seconds
        session: Optional httpx client (creates one with retries if not provided)
        circuit_breaker: Optional circuit breaker
        **kwargs: Additional arguments passed to httpx

    Returns:
        Response object

    Raises:
        NetworkError: If request fails after retries
    """
    import httpx

    # Set timeouts
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    if connect_timeout is None:
        connect_timeout = DEFAULT_CONNECT_TIMEOUT

    # connect_timeout bounds the connection, timeout the read (and write/pool)
    request_timeout = httpx.Timeout(timeout, connect=connect_timeout)

    # Create session if not provided
    if session is None:
        session = create_session_with_retries()

    # ``requests`` keyword names that ``httpx`` spells differently: translate the
    # one that maps one-to-one, refuse the rest rather than ignore them silently.
    if "allow_redirects" in kwargs:
        kwargs["follow_redirects"] = kwargs.pop("allow_redirects")
    for name, instead in _REQUESTS_ONLY_KWARGS.items():
        if name in kwargs:
            raise TypeError(f"safe_request() no longer takes {name!r}: {instead}")

    # Add timeout to kwargs
    kwargs["timeout"] = request_timeout

    # Execute request
    try:
        if circuit_breaker:
            response = circuit_breaker.call(session.request, method, url, **kwargs)
        else:
            response = session.request(method, url, **kwargs)

        # Raise for 4xx/5xx only, as ``requests`` did: ``httpx``'s
        # ``raise_for_status()`` also raises on a 1xx/3xx (a 304, a 301 with no
        # ``Location``).
        if response.is_error:
            response.raise_for_status()

        return response

    except httpx.TimeoutException as e:
        raise NetworkError(
            f"Request timed out after {timeout}s: {url}",
            context={"url": url, "method": method, "timeout": timeout},
            suggestions=[
                "Check your internet connection",
                "Increase timeout if the service is slow",
                "Verify the URL is correct",
            ],
            original_error=e,
        )

    except (httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError) as e:
        raise NetworkError(
            f"Connection failed: {url}",
            context={"url": url, "method": method},
            suggestions=[
                "Check your internet connection",
                "Verify the service is online",
                "Check firewall/proxy settings",
            ],
            original_error=e,
        )

    except httpx.HTTPStatusError as e:
        status_code = e.response.status_code
        raise NetworkError(
            f"HTTP {status_code} error: {url}",
            context={"url": url, "method": method, "status_code": status_code},
            suggestions=[
                "Check the URL is correct",
                "Verify authentication credentials" if status_code in (401, 403) else "",
                "Check API rate limits" if status_code == 429 else "",
                "Service may be experiencing issues" if status_code >= 500 else "",
            ],
            original_error=e,
        )

    except Exception as e:
        raise wrap_error(
            e, f"Request failed: {url}", NetworkError, context={"url": url, "method": method}
        )


# Convenience functions


def safe_get(url: str, **kwargs) -> Any:  # returns httpx.Response
    """Safe GET request with timeouts and retries"""
    return safe_request("GET", url, **kwargs)


def safe_post(url: str, **kwargs) -> Any:  # returns httpx.Response
    """Safe POST request with timeouts and retries"""
    return safe_request("POST", url, **kwargs)


def safe_put(url: str, **kwargs) -> Any:  # returns httpx.Response
    """Safe PUT request with timeouts and retries"""
    return safe_request("PUT", url, **kwargs)


def safe_patch(url: str, **kwargs) -> Any:  # returns httpx.Response
    """Safe PATCH request with timeouts and retries"""
    return safe_request("PATCH", url, **kwargs)


def safe_delete(url: str, **kwargs) -> Any:  # returns httpx.Response
    """Safe DELETE request with timeouts and retries"""
    return safe_request("DELETE", url, **kwargs)


# Global instances for common use
_default_session: Optional[Any] = None  # httpx.Client
_default_rate_limiter: Optional[RateLimiter] = None
_default_circuit_breaker: Optional[CircuitBreaker] = None


def get_default_session() -> Any:  # returns httpx.Client
    """Get or create default session"""
    global _default_session
    if _default_session is None:
        _default_session = create_session_with_retries()
    return _default_session


def get_default_rate_limiter() -> RateLimiter:
    """Get or create default rate limiter"""
    global _default_rate_limiter
    if _default_rate_limiter is None:
        _default_rate_limiter = RateLimiter(DEFAULT_RATE_LIMIT, 60.0)
    return _default_rate_limiter


def get_default_circuit_breaker() -> CircuitBreaker:
    """Get or create default circuit breaker"""
    global _default_circuit_breaker
    if _default_circuit_breaker is None:
        _default_circuit_breaker = CircuitBreaker()
    return _default_circuit_breaker
