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

"""Unit tests for the dbt source-freshness emitter."""

from __future__ import annotations

import pytest

from fluid_build.copilot.tools.freshness_emitter import propose_freshness


def _envelope(out: dict, *, has_warn: bool = True, has_error: bool = True) -> None:
    assert "filter" in out
    assert out["filter"] is None
    if has_warn:
        assert set(out["warn_after"].keys()) == {"count", "period"}
        assert out["warn_after"]["period"] in {"minute", "hour", "day"}
        assert isinstance(out["warn_after"]["count"], int)
        assert out["warn_after"]["count"] >= 1
    if has_error:
        assert set(out["error_after"].keys()) == {"count", "period"}
        assert out["error_after"]["period"] in {"minute", "hour", "day"}
        assert isinstance(out["error_after"]["count"], int)
        assert out["error_after"]["count"] >= 1


@pytest.mark.parametrize(
    "alias,expected_warn,expected_error",
    [
        ("streaming", {"count": 5, "period": "minute"}, {"count": 15, "period": "minute"}),
        ("realtime", {"count": 5, "period": "minute"}, {"count": 15, "period": "minute"}),
        ("real-time", {"count": 5, "period": "minute"}, {"count": 15, "period": "minute"}),
        ("real_time", {"count": 5, "period": "minute"}, {"count": 15, "period": "minute"}),
        ("Streaming", {"count": 5, "period": "minute"}, {"count": 15, "period": "minute"}),
    ],
)
def test_streaming_thresholds(alias, expected_warn, expected_error):
    out = propose_freshness(alias)
    _envelope(out)
    assert out["warn_after"] == expected_warn
    assert out["error_after"] == expected_error


@pytest.mark.parametrize(
    "alias",
    ["hourly", "every_hour", "every-hour", "HOURLY"],
)
def test_hourly_thresholds(alias):
    out = propose_freshness(alias)
    _envelope(out)
    # 90m and 180m — 90 doesn't divide cleanly into hours, 180 does (3h).
    assert out["warn_after"] == {"count": 90, "period": "minute"}
    assert out["error_after"] == {"count": 3, "period": "hour"}


@pytest.mark.parametrize(
    "alias",
    ["daily", "every_day", "every-day", "nightly", "DAILY"],
)
def test_daily_thresholds(alias):
    out = propose_freshness(alias)
    _envelope(out)
    # 30h and 48h — 48 is 2 days (clean), 30 stays as hours.
    assert out["warn_after"] == {"count": 30, "period": "hour"}
    assert out["error_after"] == {"count": 2, "period": "day"}


def test_weekly_thresholds():
    out = propose_freshness("weekly")
    _envelope(out)
    assert out["warn_after"] == {"count": 8, "period": "day"}
    assert out["error_after"] == {"count": 14, "period": "day"}


def test_monthly_thresholds():
    out = propose_freshness("monthly")
    _envelope(out)
    assert out["warn_after"] == {"count": 32, "period": "day"}
    assert out["error_after"] == {"count": 45, "period": "day"}


def test_cdc_halves_streaming_thresholds():
    out = propose_freshness("streaming", source_type="cdc")
    _envelope(out)
    # 5 // 2 == 2 minute; 15 // 2 == 7 minute.
    assert out["warn_after"] == {"count": 2, "period": "minute"}
    assert out["error_after"] == {"count": 7, "period": "minute"}


def test_cdc_halves_hourly_thresholds():
    out = propose_freshness("hourly", source_type="cdc")
    _envelope(out)
    # 90 // 2 == 45 minute; 180 // 2 == 90 minute.
    assert out["warn_after"] == {"count": 45, "period": "minute"}
    assert out["error_after"] == {"count": 90, "period": "minute"}


def test_cdc_halves_daily_thresholds():
    out = propose_freshness("daily", source_type="cdc")
    _envelope(out)
    # 1800 // 2 == 900 min == 15h; 2880 // 2 == 1440 min == 1 day.
    assert out["warn_after"] == {"count": 15, "period": "hour"}
    assert out["error_after"] == {"count": 1, "period": "day"}


def test_cdc_case_insensitive():
    a = propose_freshness("daily", source_type="CDC")
    b = propose_freshness("daily", source_type="cdc")
    assert a == b


def test_non_cdc_source_type_is_ignored():
    a = propose_freshness("hourly")
    b = propose_freshness("hourly", source_type="batch")
    c = propose_freshness("hourly", source_type=None)
    assert a == b == c


def test_unknown_cadence_returns_empty_dict_with_warning(caplog):
    with caplog.at_level("WARNING"):
        out = propose_freshness("biweekly-on-thursdays")
    assert out == {}
    assert any("unrecognized cadence" in r.message for r in caplog.records)


def test_empty_string_cadence_returns_empty(caplog):
    with caplog.at_level("WARNING"):
        out = propose_freshness("")
    assert out == {}
    assert any("unrecognized cadence" in r.message for r in caplog.records)


def test_non_string_cadence_returns_empty(caplog):
    with caplog.at_level("WARNING"):
        out = propose_freshness(None)  # type: ignore[arg-type]
    assert out == {}


def test_filter_slot_is_present_and_none():
    # Caller-owns-filter is part of the contract — assert it's present
    # rather than just missing so callers know they can drop in a value
    # without checking key existence.
    out = propose_freshness("hourly")
    assert "filter" in out
    assert out["filter"] is None
