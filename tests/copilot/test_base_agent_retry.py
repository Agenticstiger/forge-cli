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

"""Unit tests for :func:`fluid_build.copilot.agents.base.retry_with_backoff`.

The staged-agent pipeline depends on this retry envelope to smooth over
transient 429/5xx/parse failures from every provider. Cover the three
cases that matter:

* first attempt wins — no sleep, returns immediately
* N-1 transient failures, Nth attempt wins — sleeps exactly N-1 times
* all attempts fail — raises the final exception

All sleep calls are captured via a stub so the suite stays fast and
deterministic (no real backoff delays).
"""

from __future__ import annotations

from typing import List

import pytest

from fluid_build.copilot.agents.base import (
    RETRY_ATTEMPTS,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
    retry_with_backoff,
)


def _make_sleep_capture() -> tuple[List[float], callable]:
    captured: List[float] = []

    def sleep(seconds: float) -> None:
        captured.append(seconds)

    return captured, sleep


class TestRetryWithBackoffCopilotBase:
    def test_returns_on_first_success_without_sleeping(self) -> None:
        sleeps, sleep = _make_sleep_capture()
        calls = 0

        def ok() -> str:
            nonlocal calls
            calls += 1
            return "done"

        assert retry_with_backoff(ok, sleep=sleep) == "done"
        assert calls == 1
        assert sleeps == []  # success on first try → no backoff

    def test_retries_until_success(self) -> None:
        sleeps, sleep = _make_sleep_capture()
        calls = 0

        def flaky() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RuntimeError("transient")
            return "ok"

        # jitter=0 keeps delays exactly deterministic for the assertion below.
        result = retry_with_backoff(flaky, jitter=0.0, sleep=sleep)
        assert result == "ok"
        assert calls == 3
        # Two sleeps: between attempt 1→2 and 2→3. First delay = base, second
        # delay = base * 2 (exponential), both capped at max.
        assert sleeps == [RETRY_BASE_DELAY, RETRY_BASE_DELAY * 2]

    def test_raises_last_error_after_exhausting_attempts(self) -> None:
        sleeps, sleep = _make_sleep_capture()
        calls = 0

        def always_fail() -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError(f"boom-{calls}")

        with pytest.raises(RuntimeError, match=r"boom-3"):
            retry_with_backoff(always_fail, jitter=0.0, sleep=sleep)

        assert calls == RETRY_ATTEMPTS
        # Sleeps happen *between* attempts, so attempts-1 of them.
        assert len(sleeps) == RETRY_ATTEMPTS - 1

    def test_retry_predicate_can_fail_fast_without_sleeping(self) -> None:
        sleeps, sleep = _make_sleep_capture()
        calls = 0

        def schema_error() -> None:
            nonlocal calls
            calls += 1
            raise ValueError("invalid schema")

        with pytest.raises(ValueError, match="invalid schema"):
            retry_with_backoff(
                schema_error,
                retry_if=lambda exc: not isinstance(exc, ValueError),
                sleep=sleep,
            )

        assert calls == 1
        assert sleeps == []

    def test_delay_is_clamped_to_max(self) -> None:
        sleeps, sleep = _make_sleep_capture()
        calls = 0

        def always_fail() -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("keep failing")

        with pytest.raises(RuntimeError):
            retry_with_backoff(
                always_fail,
                attempts=6,
                base_delay=4.0,
                max_delay=RETRY_MAX_DELAY,
                jitter=0.0,
                sleep=sleep,
            )

        # base=4, max=8 → sequence would be 4, 8, 16, 32, 64 uncapped; the
        # cap forces the tail to 8, 8, 8, 8.
        assert sleeps == [4.0, RETRY_MAX_DELAY, RETRY_MAX_DELAY, RETRY_MAX_DELAY, RETRY_MAX_DELAY]

    def test_jitter_never_exceeds_configured_fraction(self) -> None:
        sleeps, sleep = _make_sleep_capture()
        calls = 0

        def always_fail() -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("jittery")

        with pytest.raises(RuntimeError):
            retry_with_backoff(
                always_fail,
                attempts=RETRY_ATTEMPTS,
                base_delay=RETRY_BASE_DELAY,
                jitter=0.25,
                sleep=sleep,
            )

        # Each captured sleep must be >= nominal delay and <= nominal * (1 + jitter).
        nominal = [RETRY_BASE_DELAY, RETRY_BASE_DELAY * 2]
        assert len(sleeps) == len(nominal)
        for actual, base in zip(sleeps, nominal, strict=True):
            assert base <= actual <= base * 1.25 + 1e-9
