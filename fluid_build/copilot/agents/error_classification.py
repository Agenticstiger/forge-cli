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

"""Maps low-level HTTP / SDK exceptions to the typed
:class:`fluid_build.copilot.agents.errors.ProviderError` taxonomy.

The agent layer used to catch ``Exception`` at the LLM call site and
hand it to a generic exponential-backoff retry. That wastes credits on
rate-limits (which carry a ``Retry-After`` hint) and on
context-overflow (which is non-retryable). :func:`classify_provider_error`
converts the raw exception into the right typed class so the retry
logic can branch correctly.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from fluid_build.copilot.agents.errors import (
    ContextOverflowError,
    ProviderAuthError,
    ProviderError,
    ProviderServerError,
    ProviderTimeoutError,
    RateLimitError,
)

__all__ = ["classify_provider_error", "parse_retry_after"]


_CONTEXT_OVERFLOW_PATTERNS = (
    "context_length_exceeded",
    "context window",
    "maximum context length",
    "prompt is too long",
    "request too large",
    "input is too long",
    "exceeds the maximum",
)


def parse_retry_after(value: Any) -> Optional[float]:
    """Parse an HTTP ``Retry-After`` header value into seconds.

    The header may be either a delta-seconds integer (``"5"``) or an
    HTTP-date — we only support the delta-seconds shape since that's
    what every major provider sends. Returns ``None`` if the value is
    missing or unparseable so callers fall back to their default
    backoff.
    """
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
        if seconds < 0:
            return None
        # Cap at 5 minutes — providers occasionally send absurd values
        # and we never want to block a CLI for longer than that.
        return min(seconds, 300.0)
    except (TypeError, ValueError):
        return None


def _looks_like_context_overflow(message: str) -> bool:
    lower = message.lower()
    return any(pattern in lower for pattern in _CONTEXT_OVERFLOW_PATTERNS)


def _extract_response_body(response: httpx.Response) -> str:
    """Best-effort extraction of the response body for diagnostics.

    Falls back to an empty string if the body has already been consumed
    or can't be decoded — never raises, since the caller is already in
    an error path.
    """
    try:
        return response.text or ""
    except Exception:  # noqa: BLE001 — diagnostic best-effort
        return ""


def classify_provider_error(
    exc: BaseException,
    *,
    provider: str = "",
) -> ProviderError:
    """Convert a low-level provider exception into a typed
    :class:`ProviderError` subclass.

    The mapping rules:

    * :class:`httpx.TimeoutException`           → :class:`ProviderTimeoutError`
    * :class:`httpx.HTTPStatusError` 429        → :class:`RateLimitError`
    * :class:`httpx.HTTPStatusError` 401/403    → :class:`ProviderAuthError`
    * :class:`httpx.HTTPStatusError` 5xx        → :class:`ProviderServerError`
    * :class:`httpx.HTTPStatusError` 400 with
      a ``context_length_exceeded``-style body → :class:`ContextOverflowError`
    * Any other ``HTTPStatusError``             → generic :class:`ProviderError`
    * Anything else                             → generic :class:`ProviderError`

    Always returns a typed exception (never re-raises a different one)
    so call sites can ``raise classify_provider_error(exc) from exc``
    without losing the original traceback.
    """
    if isinstance(exc, ProviderError):
        # Already classified — don't re-wrap.
        return exc

    if isinstance(exc, httpx.TimeoutException):
        return ProviderTimeoutError(
            f"Provider {provider or 'unknown'} request timed out: {exc}",
            provider=provider,
        )

    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        status = response.status_code
        body = _extract_response_body(response)
        retry_after = parse_retry_after(response.headers.get("retry-after"))

        if status == 429:
            return RateLimitError(
                f"Provider {provider or 'unknown'} rate-limited (429): {body[:200]}",
                provider=provider,
                status_code=status,
                retry_after=retry_after,
            )
        if status in (401, 403):
            return ProviderAuthError(
                f"Provider {provider or 'unknown'} auth failed ({status}): {body[:200]}",
                provider=provider,
                status_code=status,
            )
        if 500 <= status < 600:
            return ProviderServerError(
                f"Provider {provider or 'unknown'} server error ({status}): {body[:200]}",
                provider=provider,
                status_code=status,
                retry_after=retry_after,
            )
        if status == 400 and _looks_like_context_overflow(body):
            return ContextOverflowError(
                f"Prompt exceeds context window for provider {provider or 'unknown'}: {body[:200]}",
                provider=provider,
                status_code=status,
            )
        return ProviderError(
            f"Provider {provider or 'unknown'} returned {status}: {body[:200]}",
            provider=provider,
            status_code=status,
            retry_after=retry_after,
        )

    # httpx network errors that aren't timeouts (connection refused,
    # DNS failure, etc) — treat as transient server errors so they
    # retry with backoff.
    if isinstance(exc, httpx.RequestError):
        return ProviderServerError(
            f"Provider {provider or 'unknown'} request failed: {exc}",
            provider=provider,
        )

    # String-match the message for context-overflow signals from
    # providers that surface it via something other than HTTP 400
    # (e.g. SDK-level pre-flight checks).
    msg = str(exc)
    if _looks_like_context_overflow(msg):
        return ContextOverflowError(
            f"Prompt exceeds context window: {msg}",
            provider=provider,
        )

    # Match Anthropic-style overloaded errors that come through as
    # ``APIError`` with status 529 — retry as a transient.
    if re.search(r"\b5\d\d\b", msg) or "overloaded" in msg.lower():
        return ProviderServerError(
            f"Provider {provider or 'unknown'} transient failure: {msg}",
            provider=provider,
        )

    return ProviderError(
        f"Provider {provider or 'unknown'} call failed: {msg}",
        provider=provider,
    )
