# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Synchronous retry-with-backoff for catalog-registrar HTTP calls.

Catalog registrars are *metadata sinks* — a transient GMS/REST blip
(a rolling restart, a 503 while the search index catches up, a 429
rate-limit) must self-heal rather than turn a whole publish into a
failure. This helper wraps a single HTTP operation (open client →
POST/PUT → ``raise_for_status``) in a bounded exponential-backoff
retry so those transient failures recover on their own, while a real
outage still terminates quickly with the original exception (which the
registrar's own ``try/except`` converts into a clean
``succeeded=False`` result — it never crashes the pipeline).

Borrow-before-build receipts:
  * The retryable-status-code set (``429, 500, 502, 503, 504``), the
    "POST is retryable" stance, the ``backoff_factor=2`` and the
    default of four total attempts mirror DataHub's own
    ``DatahubRestEmitter`` (``metadata-ingestion/src/datahub/emitter/
    rest_emitter.py`` — ``_DEFAULT_RETRY_STATUS_CODES`` /
    ``_DEFAULT_RETRY_METHODS`` / ``_DEFAULT_RETRY_MAX_TIMES``). Mirroring
    keeps our behaviour unsurprising to DataHub operators.
  * The exponential-backoff-with-jitter shape follows the repo's
    existing ``providers/local/util/retry.py``. We diverge from
    depending on ``acryl-datahub`` (heavy dep tree, and it speaks
    ``requests``/``urllib3`` — incompatible with our SSRF-guarded
    ``httpx`` chokepoint) and from ``tenacity`` (a thin hand-rolled
    loop is the established house style).
  * We honour a ``Retry-After`` header (RFC 9110 §10.2.3 — int seconds
    or HTTP-date) on ``429``/``503`` responses, capped to ``max_delay``
    so a hostile/huge value can't stall the pipeline.
"""

from __future__ import annotations

import email.utils
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, FrozenSet, Optional, TypeVar

# Retryable HTTP status codes — mirrors DataHub's
# ``_DEFAULT_RETRY_STATUS_CODES``. 429 (rate-limited) + the transient
# 5xx family. A 4xx other than 429 is a client error (bad payload, auth)
# that a retry can't fix, so we do NOT retry it.
DEFAULT_RETRY_STATUS_CODES: FrozenSet[int] = frozenset({429, 500, 502, 503, 504})

# Four total attempts = DataHub's ``retry_max_times=4`` default.
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 0.5  # seconds
DEFAULT_MAX_DELAY = 30.0  # seconds — also the ceiling for Retry-After
DEFAULT_BACKOFF_FACTOR = 2.0

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential-backoff retry policy for one HTTP operation."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay: float = DEFAULT_BASE_DELAY
    max_delay: float = DEFAULT_MAX_DELAY
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR
    jitter: bool = True
    retry_status_codes: FrozenSet[int] = field(default_factory=lambda: DEFAULT_RETRY_STATUS_CODES)


def is_transient_http_error(exc: BaseException, policy: RetryPolicy) -> bool:
    """True iff ``exc`` is a transient error worth retrying.

    Transient means:
      * an ``httpx.HTTPStatusError`` whose status is in
        ``policy.retry_status_codes`` (429 + 5xx), OR
      * any ``httpx.TransportError`` — connect errors, connect/read/
        write/pool timeouts, protocol errors, DNS failures. These are
        network-layer blips that a retry can plausibly recover from.

    Everything else (4xx other than 429, programming errors) is
    non-retryable — a retry would just fail the same way.
    """
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in policy.retry_status_codes
    if isinstance(exc, httpx.TransportError):
        return True
    return False


def retry_after_seconds(exc: BaseException) -> Optional[float]:
    """Parse a ``Retry-After`` header off an ``httpx.HTTPStatusError``.

    Supports both RFC 9110 forms: an integer delta-seconds
    (``Retry-After: 5``) and an HTTP-date
    (``Retry-After: Wed, 21 Oct 2026 07:28:00 GMT``). Returns ``None``
    when the header is absent or unparseable so the caller falls back
    to computed exponential backoff.
    """
    import httpx

    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    raw = exc.response.headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    try:
        secs = float(int(raw))
        return secs if secs >= 0 else None
    except ValueError:
        pass
    # HTTP-date form → seconds from now. ``parsedate_to_datetime``
    # returns ``None`` on some Pythons and raises ``ValueError`` /
    # ``TypeError`` on others for a malformed value — handle both.
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (ValueError, TypeError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = (parsed - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, delta)


def compute_delay(attempt: int, policy: RetryPolicy, retry_after: Optional[float] = None) -> float:
    """Delay before the next attempt.

    ``attempt`` is the 1-based number of the attempt that just failed.
    A server-supplied ``Retry-After`` wins over computed backoff (but is
    capped to ``max_delay`` so a hostile value can't stall the pipeline)
    and is used verbatim — no jitter, the server already told us when to
    come back.
    """
    if retry_after is not None:
        return min(max(0.0, retry_after), policy.max_delay)
    delay = min(policy.base_delay * (policy.backoff_factor ** (attempt - 1)), policy.max_delay)
    if policy.jitter:
        # Half-to-full jitter (AWS "equal jitter"-style) — spreads a
        # thundering herd after a shared outage without ever waiting 0.
        delay = delay * (0.5 + random.random() * 0.5)
    return delay


def run_with_retry(
    operation: Callable[[], T],
    *,
    policy: RetryPolicy,
    logger: Optional[logging.Logger] = None,
    description: str = "http call",
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``operation`` with bounded exponential-backoff retry.

    Retries only transient failures (see :func:`is_transient_http_error`).
    A non-transient error re-raises immediately; a transient one that
    survives ``policy.max_attempts`` re-raises the *last* exception. The
    original exception (and traceback) is always what propagates, so the
    caller's ``except`` sees the real failure.

    Log lines carry only the attempt counter, status code / exception
    class, and the sleep — never response bodies — so a retry can't leak
    secrets into logs (the global redaction filter is a second layer).
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 — classify then re-raise
            last_exc = exc
            if attempt >= policy.max_attempts or not is_transient_http_error(exc, policy):
                raise
            retry_after = retry_after_seconds(exc)
            delay = compute_delay(attempt, policy, retry_after)
            if logger is not None:
                logger.warning(
                    "catalog_http_retry",
                    extra={
                        "description": description,
                        "attempt": attempt,
                        "max_attempts": policy.max_attempts,
                        "reason": _reason(exc),
                        "retry_after_honored": retry_after is not None,
                        "delay_seconds": round(delay, 3),
                    },
                )
            sleep(delay)
    # Unreachable: the loop either returns or re-raises. Guard anyway.
    assert last_exc is not None  # pragma: no cover
    raise last_exc


def _reason(exc: BaseException) -> str:
    """A short, body-free label for a failure (status code or exc class)."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return f"status={exc.response.status_code}"
    return type(exc).__name__


__all__ = [
    "DEFAULT_BACKOFF_FACTOR",
    "DEFAULT_BASE_DELAY",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_DELAY",
    "DEFAULT_RETRY_STATUS_CODES",
    "RetryPolicy",
    "compute_delay",
    "is_transient_http_error",
    "retry_after_seconds",
    "run_with_retry",
]
