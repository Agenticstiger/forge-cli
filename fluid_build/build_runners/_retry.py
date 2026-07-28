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

"""Retry policy executor with retryable-error classification.

Conforms to ``execution.retry`` block: ``count``, ``backoff`` ∈
{linear, exponential}, ``jitter``, ``maxDelay``. Transient errors
(network, 5xx) are retryable; auth/schema errors are not.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, TypeVar

T = TypeVar("T")


# Retryable error classification — extend as new transient signals are catalogued.
_RETRYABLE_KEYWORDS = (
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "connection aborted",
    "socket",
    "temporarily",
    "service unavailable",
    "rate limit",
    "rate-limit",
    "throttle",
    "503",
    "504",
    "502",
    "500",
    "ServerError",
    "EOF",
)

_NON_RETRYABLE_KEYWORDS = (
    "authentication",
    "unauthorized",
    "forbidden",
    "permission denied",
    "401",
    "403",
    "404",
    "schema",
    "syntax",
    "invalid argument",
    "ValueError",
)


def is_retryable(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if any(k.lower() in msg for k in _NON_RETRYABLE_KEYWORDS):
        return False
    return any(k.lower() in msg for k in _RETRYABLE_KEYWORDS)


@dataclass
class RetryPolicy:
    count: int = 3
    backoff: str = "exponential"  # "linear" | "exponential"
    jitter: bool = True
    initial_delay: float = 1.0
    max_delay: float = 60.0

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "RetryPolicy":
        d = d or {}
        return cls(
            count=int(d.get("count", 3)),
            backoff=d.get("backoff", "exponential"),
            jitter=bool(d.get("jitter", True)),
            initial_delay=float(d.get("initialDelay", 1.0)),
            max_delay=float(d.get("maxDelay", 60.0)),
        )

    def delay_for(self, attempt: int) -> float:
        """Delay before attempt N (0-indexed; 0 = first retry, not initial call)."""
        if self.backoff == "linear":
            d = self.initial_delay * (attempt + 1)
        else:
            d = self.initial_delay * (2**attempt)
        d = min(d, self.max_delay)
        if self.jitter:
            d = d * (0.5 + random.random() * 0.5)
        return d


def with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    on_attempt: Optional[Callable[[int, BaseException], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Execute ``fn``, retrying retryable errors per ``policy``.

    Non-retryable errors are re-raised immediately. ``on_attempt`` is called
    before each sleep for observability (logs / OTel spans).
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(policy.count + 1):
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001
            last_exc = exc
            if not is_retryable(exc) or attempt == policy.count:
                raise
            if on_attempt is not None:
                on_attempt(attempt, exc)
            sleep(policy.delay_for(attempt))
    # Unreachable; for typing.
    assert last_exc is not None
    raise last_exc
