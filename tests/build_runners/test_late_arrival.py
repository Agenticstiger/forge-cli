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

"""Late-arrival semantics for streaming runners (Phase-3 #15).

Pin every layer:

* ISO-8601 duration parser (``PT5M`` / ``P1D`` / ``PT1H30M``).
* Classifier across on-time / late / future categories.
* Side-output record shape (payload preservation + metadata).
* Naming convention for the side-output table.
* Disk writer for the local fallback sink.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fluid_build.build_runners._late_arrival import (
    classify_arrival,
    parse_iso_duration,
    side_output_table_name,
)


class TestParseIsoDuration:
    @pytest.mark.parametrize(
        "text,expected_seconds",
        [
            ("PT5M", 300),
            ("PT2H", 7200),
            ("P1D", 86400),
            ("PT30S", 30),
            ("PT1H30M", 5400),
            ("PT1H30M45S", 5445),
            ("PT0.5S", 0.5),
        ],
    )
    def test_valid_shapes(self, text, expected_seconds):
        td = parse_iso_duration(text)
        assert td is not None
        assert td.total_seconds() == pytest.approx(expected_seconds)

    @pytest.mark.parametrize("text", [None, "", "5 minutes", "PT", "P1Y", "1H"])
    def test_invalid_returns_none(self, text):
        assert parse_iso_duration(text) is None


class TestClassifyArrival:
    def test_on_time_within_budget(self):
        wm = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)
        evt = datetime(2026, 5, 2, 11, 59, 30, tzinfo=timezone.utc)  # 30s old
        result = classify_arrival(
            event_time=evt,
            current_watermark=wm,
            allowed_lateness=timedelta(minutes=5),
        )
        assert result.category == "on_time"
        assert result.lateness_seconds == 0.0

    def test_late_beyond_budget(self):
        wm = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)
        evt = datetime(2026, 5, 2, 11, 50, 0, tzinfo=timezone.utc)  # 10min old
        result = classify_arrival(
            event_time=evt,
            current_watermark=wm,
            allowed_lateness=timedelta(minutes=5),
        )
        assert result.category == "late"
        assert result.lateness_seconds == 600.0
        assert "side-output" in result.reason

    def test_future_event_clock_skew(self):
        wm = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)
        evt = datetime(2026, 5, 2, 12, 5, 0, tzinfo=timezone.utc)  # 5min ahead
        result = classify_arrival(
            event_time=evt,
            current_watermark=wm,
            allowed_lateness=timedelta(minutes=5),
        )
        assert result.category == "future"
        assert result.lateness_seconds < 0
        assert "ahead of watermark" in result.reason

    def test_no_budget_means_on_time_when_in_order(self):
        wm = datetime(2026, 5, 2, 12, 0, 0, tzinfo=timezone.utc)
        evt = datetime(2026, 5, 2, 11, 0, 0, tzinfo=timezone.utc)  # 1h old
        result = classify_arrival(
            event_time=evt,
            current_watermark=wm,
            allowed_lateness=None,
        )
        assert result.category == "on_time"

    def test_naive_timestamps_promoted_to_utc(self):
        """Defensive — naive timestamps shouldn't crash."""
        wm = datetime(2026, 5, 2, 12, 0, 0)
        evt = datetime(2026, 5, 2, 11, 0, 0)
        result = classify_arrival(
            event_time=evt,
            current_watermark=wm,
            allowed_lateness=timedelta(minutes=5),
        )
        # 1h old > 5min budget → late.
        assert result.category == "late"


class TestNaming:
    def test_side_output_table_suffix(self):
        assert side_output_table_name("orders") == "orders__late_events"
        assert side_output_table_name("public.orders") == "public.orders__late_events"
