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

"""Unit tests for the typed provider-error taxonomy + retry interplay.

These cover:

* :func:`classify_provider_error` correctly routes httpx exceptions to
  the right :class:`ProviderError` subclass.
* ``Retry-After`` is parsed and respected by :func:`retry_with_backoff`.
* Non-retryable typed errors (auth, context-overflow, schema) fail fast
  without sleeping.
* The classification of context-overflow falls back to body-string
  matching when an SDK / proxy surfaces it as a generic 400 instead of
  a structured error code.
"""

from __future__ import annotations

from typing import List

import httpx
import pytest

from fluid_build.copilot.agents.base import retry_with_backoff
from fluid_build.copilot.agents.error_classification import (
    classify_provider_error,
    parse_retry_after,
)
from fluid_build.copilot.agents.errors import (
    AgentExecutionError,
    ContextOverflowError,
    FluidError,
    ProviderAuthError,
    ProviderError,
    ProviderServerError,
    ProviderTimeoutError,
    RateLimitError,
    SchemaValidationError,
    ToolValidationError,
)


def _http_status_error(
    status: int,
    *,
    body: str = "",
    headers: dict | None = None,
) -> httpx.HTTPStatusError:
    """Synthesize an httpx.HTTPStatusError with a real ``Response`` so
    classify_provider_error sees the same shape it would in production.
    """
    request = httpx.Request("POST", "https://example.com/v1/chat")
    response = httpx.Response(
        status_code=status,
        headers=headers or {},
        content=body.encode("utf-8"),
        request=request,
    )
    return httpx.HTTPStatusError(
        f"HTTP {status}",
        request=request,
        response=response,
    )


class TestErrorHierarchy:
    def test_typed_errors_inherit_fluid_error(self) -> None:
        """Every new typed error must remain catchable by the
        repo-wide ``except FluidError`` umbrella so legacy callers
        keep working."""
        assert issubclass(ProviderError, AgentExecutionError)
        assert issubclass(ProviderError, FluidError)
        assert issubclass(RateLimitError, ProviderError)
        assert issubclass(ContextOverflowError, ProviderError)
        assert issubclass(ProviderTimeoutError, ProviderError)
        assert issubclass(ProviderAuthError, ProviderError)
        assert issubclass(ProviderServerError, ProviderError)
        assert issubclass(SchemaValidationError, AgentExecutionError)
        assert issubclass(ToolValidationError, AgentExecutionError)

    def test_provider_error_carries_diagnostic_attributes(self) -> None:
        exc = RateLimitError(
            "rate limited", provider="anthropic", status_code=429, retry_after=12.0
        )
        assert exc.provider == "anthropic"
        assert exc.status_code == 429
        assert exc.retry_after == 12.0

    def test_schema_validation_error_carries_raw_output(self) -> None:
        exc = SchemaValidationError(
            "bad shape",
            schema_name="LogicalDraft",
            validation_errors=[{"loc": ("name",), "msg": "missing"}],
            raw_output="<bad json>",
        )
        assert exc.schema_name == "LogicalDraft"
        assert exc.validation_errors == [{"loc": ("name",), "msg": "missing"}]
        assert exc.raw_output == "<bad json>"


class TestParseRetryAfter:
    @pytest.mark.parametrize(
        "header,expected",
        [
            ("5", 5.0),
            ("0.5", 0.5),
            ("  10  ", 10.0),
            (None, None),
            ("", None),
            ("not-a-number", None),
            ("-1", None),  # Negative values are bogus → ignore
            ("9999", 300.0),  # Capped at 5 minutes
        ],
    )
    def test_parse_retry_after_handles_common_shapes(
        self, header: str | None, expected: float | None
    ) -> None:
        assert parse_retry_after(header) == expected


class TestClassifyProviderError:
    def test_429_is_rate_limit_with_retry_after(self) -> None:
        raw = _http_status_error(
            429, body='{"error":"too many requests"}', headers={"retry-after": "7"}
        )
        classified = classify_provider_error(raw, provider="openai")
        assert isinstance(classified, RateLimitError)
        assert classified.provider == "openai"
        assert classified.status_code == 429
        assert classified.retry_after == 7.0

    def test_429_without_retry_after_still_classifies_correctly(self) -> None:
        raw = _http_status_error(429, body="rate limited")
        classified = classify_provider_error(raw, provider="anthropic")
        assert isinstance(classified, RateLimitError)
        assert classified.retry_after is None

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_errors(self, status: int) -> None:
        raw = _http_status_error(status, body="invalid api key")
        classified = classify_provider_error(raw, provider="openai")
        assert isinstance(classified, ProviderAuthError)
        assert classified.status_code == status

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 529])
    def test_server_errors(self, status: int) -> None:
        raw = _http_status_error(status, body="upstream blew up", headers={"retry-after": "3"})
        classified = classify_provider_error(raw, provider="anthropic")
        assert isinstance(classified, ProviderServerError)
        assert classified.status_code == status
        assert classified.retry_after == 3.0

    def test_400_with_context_overflow_body_classifies_as_overflow(self) -> None:
        raw = _http_status_error(
            400,
            body=(
                '{"error":{"code":"context_length_exceeded",'
                '"message":"This model\'s maximum context length is 128k tokens"}}'
            ),
        )
        classified = classify_provider_error(raw, provider="openai")
        assert isinstance(classified, ContextOverflowError)
        assert classified.status_code == 400

    def test_400_without_overflow_signal_is_generic_provider_error(self) -> None:
        raw = _http_status_error(400, body='{"error":"bad request"}')
        classified = classify_provider_error(raw, provider="anthropic")
        # Generic ProviderError (parent of all the typed subclasses) but
        # NOT one of the specific ones.
        assert isinstance(classified, ProviderError)
        assert not isinstance(
            classified,
            (
                RateLimitError,
                ContextOverflowError,
                ProviderAuthError,
                ProviderServerError,
                ProviderTimeoutError,
            ),
        )

    def test_timeout_classifies_as_provider_timeout(self) -> None:
        timeout = httpx.ReadTimeout("read timed out")
        classified = classify_provider_error(timeout, provider="openai")
        assert isinstance(classified, ProviderTimeoutError)
        assert classified.provider == "openai"

    def test_request_error_falls_back_to_server_error(self) -> None:
        request = httpx.Request("POST", "https://example.com")
        connect_err = httpx.ConnectError("DNS failure", request=request)
        classified = classify_provider_error(connect_err, provider="ollama")
        assert isinstance(classified, ProviderServerError)

    def test_already_typed_error_is_returned_unchanged(self) -> None:
        original = RateLimitError("preserved", provider="x", retry_after=1.0)
        classified = classify_provider_error(original, provider="x")
        assert classified is original

    def test_string_match_overflow_for_sdk_layer_errors(self) -> None:
        raw = RuntimeError("Prompt is too long: 200000 tokens > 128000 max")
        classified = classify_provider_error(raw, provider="anthropic")
        assert isinstance(classified, ContextOverflowError)


class TestRetryWithBackoffTypedErrors:
    """Behavioural contract for retry_with_backoff w/ the typed errors."""

    def _capture_sleeps(self) -> tuple[List[float], callable]:
        sleeps: List[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)

        return sleeps, sleep

    def test_context_overflow_fails_fast_without_sleeping(self) -> None:
        sleeps, sleep = self._capture_sleeps()
        calls = 0

        def overflow() -> None:
            nonlocal calls
            calls += 1
            raise ContextOverflowError("prompt too big", provider="openai")

        with pytest.raises(ContextOverflowError):
            retry_with_backoff(overflow, sleep=sleep)
        assert calls == 1  # No retry — wasteful given the same prompt.
        assert sleeps == []

    def test_auth_error_fails_fast_without_sleeping(self) -> None:
        sleeps, sleep = self._capture_sleeps()
        calls = 0

        def bad_key() -> None:
            nonlocal calls
            calls += 1
            raise ProviderAuthError("bad key", provider="openai", status_code=401)

        with pytest.raises(ProviderAuthError):
            retry_with_backoff(bad_key, sleep=sleep)
        assert calls == 1
        assert sleeps == []

    def test_schema_validation_error_fails_fast(self) -> None:
        """Schema errors must surface to the agent loop so it can route
        corrective feedback to the LLM — retrying the same prompt would
        just produce the same broken output."""
        sleeps, sleep = self._capture_sleeps()
        calls = 0

        def bad_shape() -> None:
            nonlocal calls
            calls += 1
            raise SchemaValidationError("bad json")

        with pytest.raises(SchemaValidationError):
            retry_with_backoff(bad_shape, sleep=sleep)
        assert calls == 1
        assert sleeps == []

    def test_rate_limit_honors_retry_after_over_exponential_backoff(self) -> None:
        sleeps, sleep = self._capture_sleeps()
        calls = 0

        def rate_limited() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                # Provider asked us to wait a specific amount — we
                # should sleep for exactly that, not the default
                # exponential delay.
                raise RateLimitError("slow down", provider="anthropic", retry_after=11.5)
            return "ok"

        result = retry_with_backoff(rate_limited, jitter=0.0, sleep=sleep)
        assert result == "ok"
        assert calls == 3
        # Both retries should sleep for exactly the server-supplied
        # value; default exponential backoff (1.0, 2.0) would be wrong.
        assert sleeps == [11.5, 11.5]

    def test_server_error_with_retry_after_uses_provided_delay(self) -> None:
        sleeps, sleep = self._capture_sleeps()
        calls = 0

        def transient() -> str:
            nonlocal calls
            calls += 1
            if calls < 2:
                raise ProviderServerError(
                    "503", provider="openai", status_code=503, retry_after=4.2
                )
            return "ok"

        assert retry_with_backoff(transient, jitter=0.0, sleep=sleep) == "ok"
        assert sleeps == [4.2]

    def test_server_error_without_retry_after_uses_exponential(self) -> None:
        sleeps, sleep = self._capture_sleeps()
        calls = 0

        def transient() -> str:
            nonlocal calls
            calls += 1
            if calls < 2:
                raise ProviderServerError("503", provider="openai", status_code=503)
            return "ok"

        assert retry_with_backoff(transient, jitter=0.0, sleep=sleep) == "ok"
        # Falls back to base_delay (default 1.0).
        assert sleeps == [1.0]

    def test_provider_timeout_retries_with_exponential_backoff(self) -> None:
        sleeps, sleep = self._capture_sleeps()
        calls = 0

        def times_out() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ProviderTimeoutError("read timeout", provider="openai")
            return "ok"

        assert retry_with_backoff(times_out, jitter=0.0, sleep=sleep) == "ok"
        assert sleeps == [1.0, 2.0]
