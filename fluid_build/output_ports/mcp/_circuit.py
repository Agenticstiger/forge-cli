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

"""Circuit breaker for the MCP output-port server.

Physically extracted from ``server.py`` (which crossed the project's
>1500-LOC "extract to natural seams" threshold). ``server.py``
re-imports :class:`_CircuitBreaker` so existing
``from fluid_build.output_ports.mcp.server import _CircuitBreaker``
imports (e.g. tests/output_ports/test_pii_audit_circuit_token.py)
keep resolving unchanged.

See the :class:`_CircuitBreaker` docstring for the borrow-before-build
rationale (intentional divergence from pybreaker/tenacity — they are
in-process only; this carries cross-replica state via Redis).
"""

from __future__ import annotations

import collections
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Deque, Optional

_log = logging.getLogger("fluid.output_port.mcp.circuit")

# One-shot guard (process-global, not per-instance) so the
# "fleet-wide coordination degraded" warning fires exactly once even
# across many breaker instances and many failed Redis calls.
_WARNED_REDIS_DEGRADED = False

# One-shot guard so the "redis circuit backend is experimental" notice
# fires exactly once per process, the first time the Redis backend is
# actually selected (FLUID_MCP_CIRCUIT_BACKEND=redis).
_WARNED_CIRCUIT_REDIS_EXPERIMENTAL = False

# Circuit breaker: when a driver fails ``threshold`` times within
# ``window_seconds``, subsequent calls fast-fail with a
# ``CircuitOpen`` envelope for ``cooldown_seconds`` instead of
# hitting the engine. Prevents a downstream outage from cascading
# into per-call timeouts that pin event-loop slots.
DEFAULT_CIRCUIT_THRESHOLD = int(os.environ.get("FLUID_MCP_CIRCUIT_THRESHOLD", "5"))
DEFAULT_CIRCUIT_WINDOW_SECONDS = float(os.environ.get("FLUID_MCP_CIRCUIT_WINDOW_SECONDS", "60"))
DEFAULT_CIRCUIT_COOLDOWN_SECONDS = float(os.environ.get("FLUID_MCP_CIRCUIT_COOLDOWN_SECONDS", "30"))


@dataclass
class _CircuitBreaker:
    """Sliding-window failure counter that trips into ``open`` for
    ``cooldown_seconds`` once ``threshold`` failures land within
    ``window_seconds``. Half-open behaviour is implicit: the first
    call after cooldown is allowed; if it fails, the circuit
    re-opens.

    Single-replica deployments use the in-process counter (default).
    Multi-replica deployments set
    ``FLUID_MCP_CIRCUIT_BACKEND=redis`` so a downstream warehouse
    outage trips the breaker for EVERY gateway replica
    simultaneously — preventing the thundering-herd retry storm
    that would otherwise hit the recovering warehouse from N
    independently-healing breakers. Falls back to in-process on
    Redis outage (fail-open is documented loud — same posture as
    the rate-limit Redis backend).

    Borrow-before-build — intentional divergence (per
    /borrow-before-build Step 3):
      Surveyed ``pybreaker`` (the de-facto Python circuit breaker)
      and ``tenacity`` (retry/backoff with a circuit option). Both
      are **in-process only** — neither carries shared breaker state
      across replicas. Our governing requirement is the cross-replica
      coordinated trip above (one warehouse outage → all N gateway
      replicas open together), which no lightweight library provides;
      bolting a Redis layer onto ``pybreaker`` would mean a new
      dependency PLUS the same custom Redis sync we'd write anyway,
      for a net increase in moving parts. So we hand-roll a minimal
      sliding-window breaker (~1 deque + 1 timestamp) with a pluggable
      Redis backend. If the multi-replica scenario is ever dropped,
      collapse this to ``pybreaker`` for the in-process case.
    """

    threshold: int = DEFAULT_CIRCUIT_THRESHOLD
    window_seconds: float = DEFAULT_CIRCUIT_WINDOW_SECONDS
    cooldown_seconds: float = DEFAULT_CIRCUIT_COOLDOWN_SECONDS
    breaker_key: str = "fluid:mcp:circuit:default"
    _failures: Deque[float] = field(default_factory=collections.deque)
    _opened_at: Optional[float] = None
    _redis_client: Any = None
    _redis_unavailable: bool = False

    def is_open(self) -> bool:
        # Try Redis first when configured; fall through to local.
        if self._is_redis_enabled():
            redis_state = self._is_open_redis()
            if redis_state is not None:
                return redis_state
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at < self.cooldown_seconds:
            return True
        # Cooldown elapsed → reset to closed (half-open semantics).
        self._opened_at = None
        self._failures.clear()
        return False

    def record_failure(self) -> bool:
        """Append a failure timestamp; returns True if the circuit
        just tripped from this failure."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()
        self._failures.append(now)
        tripped_local = self._opened_at is None and len(self._failures) >= self.threshold
        if tripped_local:
            self._opened_at = now
        # Mirror to Redis for fleet-wide visibility.
        if self._is_redis_enabled():
            self._record_failure_redis(now=now, tripped=tripped_local)
        return tripped_local

    def record_success(self) -> None:
        # Successful call partially heals the breaker — clear the
        # most recent failure so a flaky engine doesn't permanently
        # accumulate towards the threshold.
        if self._failures:
            self._failures.pop()

    # ------------------------------------------------------------------
    # Redis backend (fleet-wide circuit state)
    # ------------------------------------------------------------------

    def _is_redis_enabled(self) -> bool:
        if self._redis_unavailable:
            return False
        enabled = os.environ.get("FLUID_MCP_CIRCUIT_BACKEND", "memory").lower() == "redis"
        if enabled:
            global _WARNED_CIRCUIT_REDIS_EXPERIMENTAL
            if not _WARNED_CIRCUIT_REDIS_EXPERIMENTAL:
                _WARNED_CIRCUIT_REDIS_EXPERIMENTAL = True
                _log.warning(
                    "mcp_circuit_redis_experimental: FLUID_MCP_CIRCUIT_BACKEND=redis "
                    "(fleet-wide cross-replica breaker coordination) is EXPERIMENTAL "
                    "and not yet under support guarantees. The default in-process "
                    "breaker covers single-replica deployments — which is every "
                    "deployment until you actually run multiple gateway replicas. "
                    "Enable Redis only when you have that topology."
                )
        return enabled

    def _mark_redis_unavailable(self) -> None:
        """Degrade to the in-process breaker and warn ONCE per process.

        Reached only after the operator opted into the Redis backend
        (``FLUID_MCP_CIRCUIT_BACKEND=redis``) — so a silent fallback
        would hide that the cross-replica coordination they asked for
        is no longer active (each gateway replica trips independently,
        re-exposing the thundering-herd this backend exists to
        prevent). The warning is actionable: it names the env var to
        set. Parity with the rate-limiter's
        ``redis-unavailable-fallback-open`` signal.
        """
        self._redis_unavailable = True
        global _WARNED_REDIS_DEGRADED
        if not _WARNED_REDIS_DEGRADED:
            _WARNED_REDIS_DEGRADED = True
            _log.warning(
                "MCP circuit breaker: Redis backend unreachable — "
                "fleet-wide breaker coordination is OFF (each gateway "
                "replica now trips independently). Point "
                "FLUID_MCP_CIRCUIT_REDIS_URL at a reachable Redis to "
                "restore cross-replica coordination."
            )

    def _redis(self):
        if self._redis_client is not None:
            return self._redis_client
        try:
            import redis  # type: ignore[import-not-found]

            url = os.environ.get(
                "FLUID_MCP_CIRCUIT_REDIS_URL",
                os.environ.get("FLUID_MCP_RATE_LIMIT_REDIS_URL", "redis://127.0.0.1:6379/0"),
            )
            client = redis.Redis.from_url(url, socket_timeout=2.0)
            client.ping()
            self._redis_client = client
            return client
        except Exception:  # noqa: BLE001
            self._mark_redis_unavailable()
            return None

    def _is_open_redis(self) -> Optional[bool]:
        """Return True if Redis says the breaker is open, False if
        closed, None if Redis is unreachable (caller falls back to
        in-process state)."""
        client = self._redis()
        if client is None:
            return None
        try:
            opened_at_raw = client.get(f"{self.breaker_key}:opened_at")
        except Exception:  # noqa: BLE001
            self._mark_redis_unavailable()
            return None
        if opened_at_raw is None:
            return False
        try:
            opened_at = float(opened_at_raw)
        except ValueError:
            return False
        if time.time() - opened_at < self.cooldown_seconds:
            return True
        # Cooldown elapsed — best-effort clean up so the next
        # success doesn't re-read stale state.
        try:
            client.delete(f"{self.breaker_key}:opened_at", f"{self.breaker_key}:failures")
        except Exception:  # noqa: BLE001
            pass
        return False

    def _record_failure_redis(self, *, now: float, tripped: bool) -> None:
        client = self._redis()
        if client is None:
            return
        try:
            now_ms = int(time.time() * 1000)
            cutoff_ms = now_ms - int(self.window_seconds * 1000)
            failures_key = f"{self.breaker_key}:failures"
            opened_key = f"{self.breaker_key}:opened_at"
            pipe = client.pipeline()
            pipe.zremrangebyscore(failures_key, 0, cutoff_ms)
            # Member MUST be unique per failure — ``{now_ms}-{pid}`` alone
            # collides on sub-millisecond same-PID bursts (the exact
            # pattern a real outage produces), so ``zadd`` would OVERWRITE
            # instead of append and the fleet breaker would never trip.
            # The uuid suffix guarantees a distinct member per call.
            member = f"{now_ms}-{os.getpid()}-{uuid.uuid4().hex}"
            pipe.zadd(failures_key, {member: now_ms})
            pipe.zcard(failures_key)
            pipe.expire(failures_key, int(self.window_seconds) + 60)
            _, _, count, _ = pipe.execute()
            if count >= self.threshold:
                # Use NX so a peer replica's open marker isn't
                # overwritten — we want first-tripper-wins semantics.
                client.set(opened_key, str(time.time()), nx=True, ex=int(self.cooldown_seconds) + 5)
        except Exception:  # noqa: BLE001
            self._mark_redis_unavailable()
