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

"""Run-level event bus for forge-cli.

The bus is a tiny in-process pub/sub primitive that lets multiple
observers (cost tracker, audit trail, telemetry exporters, custom
operator dashboards) react to forge-pipeline signals without each
observer needing to know about the others.

Why an event bus instead of direct calls?

* **Decomposes "god classes."** Before the bus, ``RunCostTracker``
  accumulated four unrelated state dimensions (tokens / missing
  usage / variant-lint / catalog-fetch ms) on one singleton. The
  bus lets each dimension live in its own subscriber so adding a
  fifth dimension doesn't grow ``RunCostTracker``.
* **Gives MissingItem #6 a real shape.** Structured events
  (``llm.call_completed``, ``catalog.fetch_completed``,
  ``validator.variant_lint``) are first-class objects an external
  observability dashboard can subscribe to. OTEL spans cover
  distributed tracing; events cover business-level signal.
* **Enables per-agent cost attribution (Missing #5) cleanly.** The
  ``llm.call_completed`` event payload carries ``stage`` /
  ``agent_class`` so a subscriber can roll up cost per agent
  without touching the tracker's existing per-(provider, model)
  shape.

Public surface:

* :class:`Event` — the typed event envelope.
* :class:`EventBus` — pub/sub registry; thread-safe.
* :func:`get_event_bus` — process-wide bus accessor.
* :func:`reset_event_bus` — drops every subscriber, used by tests
  and explicit run boundaries.

The bus is **not** a replacement for OTEL traced spans. Spans are
the right tool for "this code path took 4.2s and called these N
HTTP requests"; events are the right tool for "the
ConformanceAgent flagged 2 dialect-drift warnings and the run
total reached $0.32."
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

_log = logging.getLogger(__name__)


# Type alias for event handlers. Handlers are called synchronously
# on the emitter thread; if they need to defer work they should
# enqueue and return quickly.
EventHandler = Callable[["Event"], None]


@dataclass(frozen=True)
class Event:
    """One signal on the bus.

    ``event_type`` is a dotted-namespace string
    (``llm.call_completed``, ``catalog.fetch_completed``,
    ``validator.variant_lint``, ``conformance.standard_run``).
    Subscribers filter on this string.

    ``payload`` is an arbitrary dict of typed values relevant to
    the event. The shape per ``event_type`` is documented in the
    emitter's docstring; subscribers should treat unknown keys as
    forward-compat additions and tolerate them gracefully.

    ``timestamp_ms`` is the emit time in epoch milliseconds. Set
    by :meth:`EventBus.emit` so the caller doesn't have to.
    """

    event_type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp_ms: int = 0


class EventBus:
    """Thread-safe in-process pub/sub registry.

    Cheap to construct; multiple instances are valid (e.g. a test
    might spin up a private bus to avoid noise from a parent
    test's subscribers). The module-level
    :func:`get_event_bus` returns the singleton used by the
    staged pipeline.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: List[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        """Register ``handler`` and return an unsubscribe callable.

        Calling the returned function removes the handler from the
        registry. Idempotent — calling it twice is a no-op.
        """
        with self._lock:
            self._subscribers.append(handler)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(handler)
                except ValueError:
                    pass

        return unsubscribe

    def emit(self, event: Event) -> None:
        """Deliver ``event`` to every subscriber.

        Stamps ``event.timestamp_ms`` if unset, then calls each
        subscriber synchronously. A subscriber that raises is logged
        at DEBUG and otherwise ignored — one bad observer must not
        break the rest of the pipeline.
        """
        if event.timestamp_ms == 0:
            # ``Event`` is frozen, so we replace via dataclasses.replace.
            from dataclasses import replace as _dc_replace

            event = _dc_replace(event, timestamp_ms=int(time.time() * 1000))
        with self._lock:
            subscribers = list(self._subscribers)
        for handler in subscribers:
            try:
                handler(event)
            except Exception:  # noqa: BLE001
                _log.debug(
                    "event handler %r raised on event %s",
                    handler,
                    event.event_type,
                    exc_info=True,
                )

    def reset(self) -> None:
        """Drop every subscriber. Used by tests; rarely useful at runtime.

        ``RunCostTracker`` and other long-lived subscribers re-subscribe
        on next use because each acquires its own bus reference at
        construction.
        """
        with self._lock:
            self._subscribers.clear()


_BUS = EventBus()
"""Process-wide singleton. Use :func:`get_event_bus` rather than
referring to this directly so tests can swap it."""


def get_event_bus() -> EventBus:
    """Return the process-wide event bus."""
    return _BUS


def reset_event_bus() -> None:
    """Clear every subscriber. Hermetic-test helper."""
    _BUS.reset()


__all__ = [
    "Event",
    "EventBus",
    "EventHandler",
    "get_event_bus",
    "reset_event_bus",
]
