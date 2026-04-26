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

"""Coverage for the V1.5+ event bus (Mediocre-#4 / Missing-#6).

The event bus is forge-cli's pub/sub primitive for run-level
signals (LLM call completions, catalog fetches, validator
findings). Decomposes the previously god-class ``RunCostTracker``
state into per-domain receivers.

Pinned contracts:

1. ``subscribe`` returns an unsubscribe callable.
2. ``emit`` delivers to every subscriber synchronously.
3. A handler that raises is logged and swallowed — one bad
   observer must not break the rest of the pipeline.
4. ``timestamp_ms`` is auto-stamped on emit.
5. ``RunCostTracker.record_call`` emits ``llm.call_completed``
   events alongside its existing state mutation. Other observers
   can subscribe to the same bus for per-agent cost attribution
   or telemetry export without touching the tracker.
"""

from __future__ import annotations

from typing import List
from unittest.mock import MagicMock

import pytest

from fluid_build.copilot.cost import (
    CostBreakdown,
    get_run_tracker,
    reset_run_tracker,
)
from fluid_build.copilot.events import (
    Event,
    EventBus,
    get_event_bus,
    reset_event_bus,
)


@pytest.fixture(autouse=True)
def _hermetic():
    reset_event_bus()
    reset_run_tracker()
    yield
    reset_event_bus()
    reset_run_tracker()


class TestEventBusPrimitive:
    def test_subscribe_then_emit_calls_handler(self):
        bus = EventBus()
        received: List[Event] = []
        bus.subscribe(received.append)

        bus.emit(Event(event_type="test.signal", payload={"x": 1}))

        assert len(received) == 1
        assert received[0].event_type == "test.signal"
        assert received[0].payload == {"x": 1}

    def test_unsubscribe_stops_delivery(self):
        bus = EventBus()
        received: List[Event] = []
        unsub = bus.subscribe(received.append)

        bus.emit(Event(event_type="a"))
        unsub()
        bus.emit(Event(event_type="b"))

        assert [e.event_type for e in received] == ["a"]

    def test_unsubscribe_idempotent(self):
        bus = EventBus()
        received: List[Event] = []
        unsub = bus.subscribe(received.append)
        unsub()
        unsub()  # second call is a no-op, not a crash

    def test_handler_exception_does_not_break_other_handlers(self):
        bus = EventBus()
        seen: List[Event] = []

        def bad(_event):
            raise RuntimeError("simulated handler crash")

        def good(event):
            seen.append(event)

        bus.subscribe(bad)
        bus.subscribe(good)

        bus.emit(Event(event_type="x"))

        # ``good`` must still have received the event.
        assert len(seen) == 1

    def test_timestamp_auto_stamped(self):
        bus = EventBus()
        received: List[Event] = []
        bus.subscribe(received.append)

        bus.emit(Event(event_type="t"))

        assert received[0].timestamp_ms > 0

    def test_reset_drops_subscribers(self):
        bus = EventBus()
        received: List[Event] = []
        bus.subscribe(received.append)
        bus.reset()
        bus.emit(Event(event_type="silenced"))
        assert received == []


class TestCostTrackerEmitsEvents:
    """``RunCostTracker.record_*`` methods now emit events on the
    process-wide bus alongside their existing state mutations.
    Subscribers can react to events without poking at tracker
    internals — the foundation for Missing-#5 (per-agent
    attribution) and Missing-#6 (structured events for telemetry)."""

    def test_record_call_emits_llm_call_completed(self):
        bus = get_event_bus()
        received: List[Event] = []
        bus.subscribe(received.append)

        get_run_tracker().record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=100,
            output_tokens=50,
            stage="modeler",
            agent_class="ModelerAgent",
        )

        assert len(received) == 1
        ev = received[0]
        assert ev.event_type == "llm.call_completed"
        assert ev.payload["provider"] == "openai"
        assert ev.payload["model"] == "gpt-4.1-mini"
        assert ev.payload["input_tokens"] == 100
        assert ev.payload["output_tokens"] == 50
        assert ev.payload["stage"] == "modeler"
        assert ev.payload["agent_class"] == "ModelerAgent"
        assert ev.payload["missing_usage"] is False

    def test_record_call_zero_zero_marks_missing_usage_in_event(self):
        bus = get_event_bus()
        received: List[Event] = []
        bus.subscribe(received.append)

        get_run_tracker().record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=0,
            output_tokens=0,
        )
        assert received[0].payload["missing_usage"] is True

    def test_record_missing_usage_emits_event(self):
        bus = get_event_bus()
        received: List[Event] = []
        bus.subscribe(received.append)

        get_run_tracker().record_missing_usage()

        assert any(e.event_type == "llm.usage_missing" for e in received)

    def test_record_catalog_fetch_emits_event(self):
        bus = get_event_bus()
        received: List[Event] = []
        bus.subscribe(received.append)

        get_run_tracker().record_catalog_fetch("snowflake", 1500)

        assert len(received) == 1
        assert received[0].event_type == "catalog.fetch_completed"
        assert received[0].payload["catalog_name"] == "snowflake"
        assert received[0].payload["duration_ms"] == 1500

    def test_record_variant_lint_emits_event(self):
        bus = get_event_bus()
        received: List[Event] = []
        bus.subscribe(received.append)

        get_run_tracker().record_variant_lint("snowflake", 2)

        assert any(e.event_type == "validator.variant_lint" for e in received)


class TestPerAgentAttribution:
    """Missing-#5 — the cost summary tells operators WHICH agent
    drove the cost, not just which model was billed."""

    def test_record_call_with_attribution_populates_agent_rows(self):
        get_run_tracker().record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=100,
            output_tokens=50,
            stage="modeler",
            agent_class="ModelerAgent",
        )
        get_run_tracker().record_call(
            provider="openai",
            model="gpt-4.1",
            input_tokens=200,
            output_tokens=100,
            stage="builder",
            agent_class="BuilderAgent",
        )

        breakdown = get_run_tracker().breakdown()
        assert len(breakdown.agent_rows) == 2
        # Sorted by (stage, agent_class) — builder before modeler
        # alphabetically.
        assert breakdown.agent_rows[0].stage == "builder"
        assert breakdown.agent_rows[1].stage == "modeler"

    def test_same_agent_two_calls_collapses_to_one_row(self):
        get_run_tracker().record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=100,
            output_tokens=50,
            stage="modeler",
            agent_class="ModelerAgent",
        )
        get_run_tracker().record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=200,
            output_tokens=100,
            stage="modeler",
            agent_class="ModelerAgent",
        )
        breakdown = get_run_tracker().breakdown()
        assert len(breakdown.agent_rows) == 1
        row = breakdown.agent_rows[0]
        assert row.input_tokens == 300
        assert row.output_tokens == 150
        assert row.calls == 2

    def test_unattributed_calls_excluded_from_agent_rows(self):
        """Empty stage + empty agent_class signals 'older code path
        that hasn't been updated to pass attribution'. Filtered
        from agent_rows so the per-agent table doesn't get a noisy
        ``("", "")`` row."""
        get_run_tracker().record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=100,
            output_tokens=50,
            # No stage / agent_class.
        )
        breakdown = get_run_tracker().breakdown()
        assert breakdown.agent_rows == []
        # The per-(provider, model) row IS still populated —
        # backwards compat for older callers.
        assert len(breakdown.rows) == 1

    def test_format_summary_renders_per_agent_table(self):
        from fluid_build.copilot.cost import format_cost_summary

        get_run_tracker().record_call(
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=10_000,
            output_tokens=2_000,
            stage="modeler",
            agent_class="ModelerAgent",
        )
        get_run_tracker().record_call(
            provider="anthropic",
            model="claude-haiku-4-5",
            input_tokens=1_000,
            output_tokens=200,
            stage="readme",
            agent_class="ReadmeAgent",
        )

        text = format_cost_summary(get_run_tracker().breakdown())
        assert "Per-agent attribution" in text
        assert "modeler/ModelerAgent" in text
        assert "readme/ReadmeAgent" in text

    def test_format_summary_no_per_agent_section_when_empty(self):
        """Older callers that didn't pass attribution must not
        suddenly see an empty 'Per-agent attribution' header."""
        from fluid_build.copilot.cost import format_cost_summary

        get_run_tracker().record_call(
            provider="openai",
            model="gpt-4.1-mini",
            input_tokens=100,
            output_tokens=50,
            # No attribution.
        )
        text = format_cost_summary(get_run_tracker().breakdown())
        assert "Per-agent attribution" not in text
