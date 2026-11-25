# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A date or timestamp column's min and max are rounded to the finest calendar unit enough share.

Where fewer than ``minimumCell`` records hold the exact value, an end is the first (min) or last
(max) value of the finest UTC day, month or year holding at least ``minimumCell`` records,
clipped to the range the contract declares. The real registry reads every document.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from fluid_build.profiler import disclosure
from fluid_build.profiler._stats import Request, RequestColumn, RequestTable, load_backend
from fluid_build.profiler.runner import ExposeSource, profile_exposes

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.profile")
from flux_stats.profile import load_profile_text  # noqa: E402

DAY = 86_400_000
RULE = disclosure.ACRO_DEFAULT


def _ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    moment = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return int(moment.timestamp()) * 1000


def _contract(kind: str, rules: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    column: Dict[str, Any] = {"name": "read_on", "type": kind}
    if rules:
        column["validationRules"] = rules
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "p",
        "exposes": [
            {
                "exposeId": "readings",
                "kind": "table",
                "contract": {"schema": [{"name": "station", "type": "string"}, column]},
            }
        ],
    }


def _spell(kind: str, stamp: Tuple[int, int, int, int]) -> str:
    year, month, day, hour = stamp
    if kind == "date":
        return date(year, month, day).isoformat()
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:00:00Z"


def _profile(
    tmp_path,
    held: Sequence[Tuple[Tuple[int, int, int, int], int]],
    *,
    kind: str = "date",
    rules=None,
    request=None,
):
    """Profile a table whose ``read_on`` holds each ``(year, month, day, hour)`` that many times."""
    rows = [
        {"station": "s", "read_on": _spell(kind, stamp)}
        for stamp, count in held
        for _ in range(count)
    ]
    path = tmp_path / "readings.ndjson"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return profile_exposes(
        _contract(kind, rules),
        [ExposeSource("readings", str(path))],
        backend=load_backend(),
        request=request,
    )


def _metrics(result) -> Dict[str, Any]:
    return result.document["tables"]["readings"]["columns"]["read_on"]["metrics"]


def _end(result, name: str) -> Tuple[str, Optional[int]]:
    entry = _metrics(result)[name]
    return entry["status"], (int(entry["value"]) if "value" in entry else None)


def _rounded(result) -> Tuple[Tuple[str, str, str], ...]:
    return result.tables[0].rounded_extremes


# -- the helper alone ---------------------------------------------------------------------


def _holders(*held: Tuple[int, int]) -> Dict[int, int]:
    return dict(held)


def test_a_date_min_falls_to_the_first_day_of_its_month_when_the_month_holds_enough():
    holders = _holders((_ms(2024, 3, 5), 3), (_ms(2024, 3, 9), 4), (_ms(2024, 3, 20), 5))
    assert disclosure.round_extreme("min", holders, RULE, date=True) == (_ms(2024, 3, 1), "month")


def test_a_date_max_rises_to_the_last_day_of_its_month_not_to_a_later_midnight():
    holders = _holders((_ms(2024, 2, 5), 3), (_ms(2024, 2, 9), 4), (_ms(2024, 2, 20), 5))
    assert disclosure.round_extreme("max", holders, RULE, date=True) == (
        _ms(2024, 2, 29),
        "month",
    )


def test_a_timestamp_end_is_its_day_when_the_day_holds_enough_and_the_last_millisecond():
    day = _ms(2024, 3, 5)
    holders = _holders((day + 1000, 4), (day + 5000, 6), (day + 7 * DAY, 20))
    assert disclosure.round_extreme("min", holders, RULE, date=False) == (day, "day")
    top = _ms(2024, 3, 12)
    holders = _holders((_ms(2024, 3, 1), 20), (top + 1000, 4), (top + 5000, 6))
    assert disclosure.round_extreme("max", holders, RULE, date=False) == (
        top + DAY - 1,
        "day",
    )


def test_a_date_has_no_day_level_and_a_year_is_the_last_resort():
    holders = _holders((_ms(2024, 1, 5), 4), (_ms(2024, 6, 9), 6), (_ms(2024, 12, 31), 1))
    assert disclosure.round_extreme("min", holders, RULE, date=True) == (_ms(2024, 1, 1), "year")
    assert disclosure.round_extreme("max", holders, RULE, date=True) == (_ms(2024, 12, 31), "year")


def test_an_end_stays_none_when_not_even_the_year_holds_enough():
    holders = _holders((_ms(2023, 5, 5), 4), (_ms(2024, 6, 9), 20))
    assert disclosure.round_extreme("min", holders, RULE, date=True) is None


def test_the_last_year_and_the_leap_day_are_whole_buckets():
    holders = _holders((_ms(9999, 12, 30), 12))
    assert disclosure.round_extreme("max", holders, RULE, date=True) == (
        _ms(9999, 12, 31),
        "month",
    )
    holders = _holders((_ms(2024, 2, 29), 12))
    assert disclosure.round_extreme("min", holders, RULE, date=True) == (_ms(2024, 2, 1), "month")


def test_the_clip_keeps_a_rounded_end_inside_the_declared_range():
    holders = _holders((_ms(2024, 3, 5), 12), (_ms(2024, 3, 25), 12))
    low = _ms(2024, 3, 3)
    high = _ms(2024, 3, 28)
    assert disclosure.round_extreme("min", holders, RULE, date=True, floor=low) == (low, "month")
    assert disclosure.round_extreme("max", holders, RULE, date=True, ceiling=high) == (
        high,
        "month",
    )


def test_an_end_is_left_withheld_when_a_record_lies_outside_the_declared_range():
    low, high = _ms(2024, 3, 15), _ms(2024, 3, 28)
    below = _holders((_ms(2024, 3, 2), 1), (_ms(2024, 3, 20), 20))
    assert disclosure.round_extreme("min", below, RULE, date=True, floor=low) is None
    above = _holders((_ms(2024, 3, 20), 20), (_ms(2024, 3, 30), 1))
    assert disclosure.round_extreme("max", above, RULE, date=True, ceiling=high) is None
    # the other end of the same column is unaffected
    assert disclosure.round_extreme("max", below, RULE, date=True, ceiling=high) == (
        high,
        "month",
    )


# -- a profile ----------------------------------------------------------------------------


def test_a_date_end_that_passes_exactly_is_published_exactly(tmp_path):
    result = _profile(
        tmp_path, [((2024, 3, 1, 0), 12), ((2024, 3, 2, 0), 12), ((2024, 3, 9, 0), 12)]
    )
    assert _end(result, "min") == ("F", _ms(2024, 3, 1))
    assert _end(result, "max") == ("F", _ms(2024, 3, 9))
    assert _rounded(result) == ()


def test_a_date_min_goes_to_its_month_and_the_max_stays_when_its_day_holds_enough(tmp_path):
    held = [
        ((2024, 3, 5, 0), 3),
        ((2024, 3, 9, 0), 4),
        ((2024, 3, 20, 0), 5),
        ((2024, 3, 30, 0), 12),
    ]
    result = _profile(tmp_path, held)
    assert _end(result, "min") == ("F", _ms(2024, 3, 1))
    assert _end(result, "max") == ("F", _ms(2024, 3, 30))
    assert _rounded(result) == (("read_on", "min", "month"),)


def test_a_date_max_goes_to_its_year_when_neither_its_day_nor_its_month_holds_enough(tmp_path):
    held = [((2024, 1, 5, 0), 14), ((2024, 6, 9, 0), 6), ((2024, 12, 31, 0), 3)]
    result = _profile(tmp_path, held)
    assert _end(result, "max") == ("F", _ms(2024, 12, 31))
    assert ("read_on", "max", "year") in _rounded(result)


def test_an_end_is_withheld_as_before_when_not_even_its_year_holds_enough(tmp_path):
    held = [((2023, 5, 5, 0), 4), ((2024, 6, 9, 0), 40)]
    result = _profile(tmp_path, held)
    status, value = _end(result, "min")
    assert (status, value) == ("A", None)
    assert _end(result, "max") == ("F", _ms(2024, 6, 9))
    assert _rounded(result) == ()


def test_a_timestamp_rounds_through_the_day_the_month_and_the_year(tmp_path):
    held = [
        ((2024, 3, 5, 8), 4),
        ((2024, 3, 5, 9), 8),  # the smallest value's day holds 12
        ((2024, 3, 20, 10), 12),
        ((2024, 7, 2, 1), 3),
        ((2024, 9, 9, 1), 4),
        ((2024, 9, 9, 2), 3),  # the largest value's day and month hold 7: only the year does
    ]
    result = _profile(tmp_path, held, kind="timestamp")
    assert _end(result, "min") == ("F", _ms(2024, 3, 5))
    assert _end(result, "max") == ("F", _ms(2025, 1, 1) - 1)
    assert _rounded(result) == (("read_on", "min", "day"), ("read_on", "max", "year"))


def test_a_timestamp_max_is_the_last_millisecond_of_its_bucket(tmp_path):
    held = [((2024, 3, 1, 8), 12), ((2024, 5, 20, 9), 6), ((2024, 5, 21, 9), 6)]
    result = _profile(tmp_path, held, kind="timestamp")
    assert _end(result, "max") == ("F", _ms(2024, 6, 1) - 1)
    assert ("read_on", "max", "month") in _rounded(result)


def test_a_declared_range_clips_both_ends(tmp_path):
    held = [
        ((2024, 3, 5, 0), 4),
        ((2024, 3, 9, 0), 6),
        ((2024, 3, 20, 0), 4),
        ((2024, 3, 24, 0), 6),
    ]
    rules = [{"type": "range", "constraint": ">= '2024-03-04' and <= '2024-03-26'"}]
    result = _profile(tmp_path, held, rules=rules)
    assert _end(result, "min") == ("F", _ms(2024, 3, 4))
    assert _end(result, "max") == ("F", _ms(2024, 3, 26))


def test_a_strict_bound_is_one_step_inside(tmp_path):
    held = [((2024, 3, 5, 0), 12), ((2024, 3, 24, 0), 12)]
    rules = [{"type": "range", "constraint": "> '2024-03-01' and < '2024-03-30'"}]
    result = _profile(tmp_path, held, rules=rules)
    assert _end(result, "min") == ("F", _ms(2024, 3, 5))
    held = [
        ((2024, 3, 5, 0), 4),
        ((2024, 3, 8, 0), 8),
        ((2024, 3, 24, 0), 4),
        ((2024, 3, 25, 0), 8),
    ]
    result = _profile(tmp_path, held, rules=rules)
    assert _end(result, "min") == ("F", _ms(2024, 3, 2))
    assert _end(result, "max") == ("F", _ms(2024, 3, 29))


def test_a_range_this_does_not_read_leaves_the_ends_unrounded(tmp_path):
    held = [((2024, 3, 5, 0), 3), ((2024, 3, 9, 0), 4), ((2024, 3, 20, 0), 5)]
    rules = [{"type": "range", "constraint": ">= 'last spring'"}]
    result = _profile(tmp_path, held, rules=rules)
    assert _end(result, "min")[0] == "A"
    assert _rounded(result) == ()


def test_an_end_a_histogram_withholds_is_never_rounded():
    from collections import Counter

    from fluid_build.profiler import runner

    column = runner.ColumnPlan("read_on", "timestamp", date=True, extreme_bounds=(None, None))
    read = [_ms(2024, 3, 5)] * 4 + [_ms(2024, 3, 9)] * 8
    metrics = runner._column_metrics(
        column,
        read,
        len(read),
        load_backend(),
        RULE,
        Counter(),
        frozenset({"min", "max"}),
    )
    assert metrics["min"]["status"] == "D"
    assert metrics["max"]["status"] == "D"


def test_a_request_that_asks_min_and_max_gets_them_rounded_too(tmp_path):
    asked = Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(
            RequestTable(
                "readings",
                False,
                (RequestColumn("read_on", "timestamp", ("min", "max"), ()),),
            ),
        ),
    )
    held = [((2024, 3, 5, 0), 3), ((2024, 3, 9, 0), 4), ((2024, 3, 20, 0), 5)]
    result = _profile(tmp_path, held, request=asked)
    assert _end(result, "min") == ("F", _ms(2024, 3, 1))
    assert _end(result, "max") == ("F", _ms(2024, 3, 31))
    assert _rounded(result) == (("read_on", "min", "month"), ("read_on", "max", "month"))


def test_the_engine_reads_a_document_with_rounded_ends(tmp_path):
    held = [((2024, 3, 5, 0), 3), ((2024, 3, 9, 0), 4), ((2024, 3, 20, 0), 5)]
    result = _profile(tmp_path, held)
    text = json.dumps(result.document)
    assert load_profile_text(text) is not None


def test_a_document_whose_ends_all_pass_exactly_is_unchanged_by_the_rounding(tmp_path):
    held = [((2024, 3, 1, 0), 12), ((2024, 3, 9, 0), 12)]
    first = _profile(tmp_path, held)
    again = _profile(tmp_path, held)
    assert json.dumps(first.document, sort_keys=True) == json.dumps(again.document, sort_keys=True)
    assert _rounded(first) == ()


def test_the_report_names_each_rounded_end_and_its_unit_and_nothing_else(tmp_path):
    from fluid_build.cli import profile_cmd

    held = [((2024, 3, 5, 0), 3), ((2024, 3, 9, 0), 4), ((2024, 3, 20, 0), 5)]
    result = _profile(tmp_path, held)
    lines: List[str] = []
    profile_cmd._report_table(lambda text, **_: lines.append(text), result.tables[0], "", "grid")
    assert "Rounded extremes: read_on min to month, max to month." in lines
    assert not any(any(ch.isdigit() for ch in line) for line in lines if "Rounded" in line)
    assert (
        profile_cmd._named_rounded(
            (("a", "min", "month"), ("a", "max", "day"), ("b", "max", "year"))
        )
        == "a min to month, max to day; b max to year"
    )
