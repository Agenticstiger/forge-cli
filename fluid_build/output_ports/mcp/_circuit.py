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

Physically extracted from ``server.py``. ``server.py`` re-imports
:class:`_CircuitBreaker` so existing
``from fluid_build.output_ports.mcp.server import _CircuitBreaker``
imports (e.g. tests/output_ports/test_pii_audit_circuit_token.py)
keep resolving unchanged.

In-process, single-replica. See the :class:`_CircuitBreaker` docstring
for the borrow-before-build rationale.
"""

from __future__ import annotations

import collections
import os
import time
from dataclasses import dataclass, field
from typing import Deque, Optional

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

    In-process (single-replica). A cross-replica, Redis-backed variant
    was removed as speculative — no deployment ran multiple gateway
    replicas, so the fleet-wide coordination it provided had no
    consumer. Re-add a shared backend only when that topology is real.

    Borrow-before-build: surveyed ``pybreaker`` (de-facto Python
    circuit breaker) and ``tenacity``. We keep this hand-rolled breaker
    because it is a dependency-free ~1-deque-plus-timestamp counter;
    adopt ``pybreaker`` if the breaker ever needs richer state.
    """

    threshold: int = DEFAULT_CIRCUIT_THRESHOLD
    window_seconds: float = DEFAULT_CIRCUIT_WINDOW_SECONDS
    cooldown_seconds: float = DEFAULT_CIRCUIT_COOLDOWN_SECONDS
    _failures: Deque[float] = field(default_factory=collections.deque)
    _opened_at: Optional[float] = None

    def is_open(self) -> bool:
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
        tripped = self._opened_at is None and len(self._failures) >= self.threshold
        if tripped:
            self._opened_at = now
        return tripped

    def record_success(self) -> None:
        # Successful call partially heals the breaker — clear the
        # most recent failure so a flaky engine doesn't permanently
        # accumulate towards the threshold.
        if self._failures:
            self._failures.pop()
