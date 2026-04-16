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

try:
    import requests as requests_module
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
    requests_module = None  # type: ignore
    HTTPAdapter = None  # type: ignore
    Retry = None  # type: ignore

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
) -> Any:  # returns requests.Session
    """
    Create requests session with automatic retry configuration.

    Args:
        max_retries: Maximum number of retries
        backoff_factor: Backoff multiplier (delay = backoff_factor * (2 ** retry_count))
        status_forcelist: HTTP status codes that trigger retry

    Returns:
        Configured requests.Session
    """
    if not HAS_REQUESTS:
        raise ImportError("requests library not installed")

    session = requests_module.Session()

    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=["HEAD", "GET", "PUT", "POST", "DELETE", "OPTIONS", "TRACE"],
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


def safe_request(
    method: str,
    url: str,
    *,
    timeout: Optional[float] = None,
    connect_timeout: Optional[float] = None,
    session: Optional[Any] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    **kwargs,
) -> Any:  # returns requests.Response
    """
    Make HTTP request with safety features.

    Args:
        method: HTTP method (GET, POST, etc.)
        url: Request URL
        timeout: Read timeout in seconds
        connect_timeout: Connection timeout in seconds
        session: Optional requests session (creates one with retries if not provided)
        circuit_breaker: Optional circuit breaker
        **kwargs: Additional arguments passed to requests

    Returns:
        Response object

    Raises:
        NetworkError: If request fails after retries
    """
    if not HAS_REQUESTS:
        raise ImportError("requests library not installed")

    # Set timeouts
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    if connect_timeout is None:
        connect_timeout = DEFAULT_CONNECT_TIMEOUT

    # Use tuple (connect_timeout, read_timeout)
    timeout_tuple = (connect_timeout, timeout)

    # Create session if not provided
    if session is None:
        session = create_session_with_retries()

    # Add timeout to kwargs
    kwargs["timeout"] = timeout_tuple

    # Execute request
    try:
        if circuit_breaker:
            response = circuit_breaker.call(session.request, method, url, **kwargs)
        else:
            response = session.request(method, url, **kwargs)

        # Raise for HTTP errors
        response.raise_for_status()

        return response

    except requests_module.exceptions.Timeout as e:
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

    except requests_module.exceptions.ConnectionError as e:
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

    except requests_module.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response else None
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


def safe_get(url: str, **kwargs) -> Any:  # returns requests.Response
    """Safe GET request with timeouts and retries"""
    return safe_request("GET", url, **kwargs)


def safe_post(url: str, **kwargs) -> Any:  # returns requests.Response
    """Safe POST request with timeouts and retries"""
    return safe_request("POST", url, **kwargs)


def safe_put(url: str, **kwargs) -> Any:  # returns requests.Response
    """Safe PUT request with timeouts and retries"""
    return safe_request("PUT", url, **kwargs)


def safe_patch(url: str, **kwargs) -> Any:  # returns requests.Response
    """Safe PATCH request with timeouts and retries"""
    return safe_request("PATCH", url, **kwargs)


def safe_delete(url: str, **kwargs) -> Any:  # returns requests.Response
    """Safe DELETE request with timeouts and retries"""
    return safe_request("DELETE", url, **kwargs)


# Global instances for common use
_default_session: Optional[Any] = None  # requests.Session
_default_rate_limiter: Optional[RateLimiter] = None
_default_circuit_breaker: Optional[CircuitBreaker] = None


def get_default_session() -> Any:  # returns requests.Session
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
