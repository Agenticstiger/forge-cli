# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A date or timestamp column with no declared range is binned on the months or years it spans.

The span runs from the month (or year) holding the earliest record to the one after the
latest, so ``fluxsim init`` reads the run window from the histogram's outer edges. The real
registry reads every document.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

import pytest

from fluid_build.profiler import defaults, disclosure
from fluid_build.profiler._stats import (
    Request,
    RequestColumn,
    RequestTable,
    load_backend,
)
from fluid_build.profiler.runner import ExposeSource, profile_exposes

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.profile")
from flux_stats.profile import load_profile_text  # noqa: E402

_RANGE = [{"type": "range", "constraint": ">= '2020-01-01' and <= '2021-12-31'"}]


def _contract(rules: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    stamp: Dict[str, Any] = {"name": "day", "type": "date"}
    if rules:
        stamp["validationRules"] = rules
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "p",
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "contract": {
                    "schema": [
                        {"name": "status", "type": "enum('a','b')"},
                        stamp,
                    ]
                },
            }
        ],
    }


def _profile(tmp_path, days: Sequence[date], *, rules=None, request=None, **options: Any):
    path = tmp_path / "orders.ndjson"
    rows = [
        {"status": "a" if n % 2 else "b", "day": None if day is None else day.isoformat()}
        for n, day in enumerate(days)
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return profile_exposes(
        _contract(rules),
        [ExposeSource("orders", str(path))],
        backend=load_backend(),
        request=request,
        **options,
    )


def _statistics(result, column="day"):
    return result.document["tables"]["orders"]["columns"][column].get("statistics", [])


def _histograms(result):
    return [each for each in _statistics(result) if each["definition"] == "marginalHistogram/1"]


def _quantiles(result):
    return [each for each in _statistics(result) if each["definition"] == "quantile/2"]


def _spell(day: date) -> str:
    """An instant as the document spells it: milliseconds since the epoch."""
    return str((day - date(1970, 1, 1)).days * 86_400_000)


def _on_the_first_of_each_month(count: int, per_month: int, year: int = 2024) -> List[date]:
    return [date(year, month, 1) for month in range(1, count + 1) for _ in range(per_month)]


def test_a_monthly_column_is_binned_on_its_months_and_has_no_quantiles(tmp_path):
    result = _profile(tmp_path, _on_the_first_of_each_month(11, 12))
    assert not _quantiles(result)
    (histogram,) = _histograms(result)
    assert "groupBy" not in histogram
    bins = histogram["bins"]
    assert len(bins) == 12
    assert bins[0] == _spell(date(2024, 1, 1))
    assert bins[-1] == _spell(date(2024, 12, 1))
    assert bins == [_spell(date(2024, month, 1)) for month in range(1, 13)]
    assert not result.tables[0].no_span
    load_profile_text(result.canonical.decode("utf-8"))


def test_the_span_ends_at_the_month_after_the_latest_record(tmp_path):
    days = [date(2024, 3, 31)] * 10 + [date(2024, 5, 1)] * 10 + [date(2024, 12, 31)] * 10
    (histogram,) = _histograms(_profile(tmp_path, days))
    assert histogram["bins"][0] == _spell(date(2024, 3, 1))
    assert histogram["bins"][-1] == _spell(date(2025, 1, 1))


def test_daily_dates_over_30_years_are_binned_by_year(tmp_path):
    start = date(1990, 1, 1)
    days = [start + timedelta(days=n) for n in range((date(2019, 12, 31) - start).days + 1)]
    result = _profile(tmp_path, days)
    (histogram,) = _histograms(result)
    bins = histogram["bins"]
    assert bins == [_spell(date(year, 1, 1)) for year in range(1990, 2021)]
    assert not _quantiles(result)
    load_profile_text(result.canonical.decode("utf-8"))


def test_a_sparse_first_month_in_a_full_first_year_is_binned_by_year(tmp_path):
    days = (
        [date(2022, 1, 15)] * 3  # the first month holds fewer than 10
        + [date(2022, 6, 1)] * 20
        + [date(2023, 4, 1)] * 20
    )
    (histogram,) = _histograms(_profile(tmp_path, days))
    assert histogram["bins"] == [
        _spell(date(2022, 1, 1)),
        _spell(date(2023, 1, 1)),
        _spell(date(2024, 1, 1)),
    ]


def test_a_sparse_first_year_keeps_the_ladder_and_the_report_names_why(tmp_path):
    days = [date(2022, 1, 15)] * 3 + [date(2023, 6, 1)] * 20 + [date(2024, 4, 1)] * 20
    result = _profile(tmp_path, days)
    assert not _histograms(result)
    assert len(_quantiles(result)) == len(defaults.QUANTILE_LEVELS)
    (table,) = result.tables
    assert table.no_span == (
        (
            "day",
            "its first or last month and year hold fewer than 10 records"
            "; its quartiles stay on the type's ladder",
        ),
    )


def test_the_minimum_cell_of_the_rule_sets_how_many_records_an_end_month_holds(tmp_path):
    days = [date(year, 1, 1) for year in (2022, 2023, 2024) for _ in range(4)]
    assert not _histograms(_profile(tmp_path, days))
    rule = replace(disclosure.ACRO_DEFAULT, minimum_cell=4)
    (histogram,) = _histograms(_profile(tmp_path, days, rule=rule))
    assert len(histogram["bins"]) == 26  # 25 months from 2022-01 to 2024-01, and the end


def test_a_sentinel_date_leaves_no_span(tmp_path):
    days = [date(2024, 1, 1)] * 20 + [date(9999, 12, 31)] * 20
    result = _profile(tmp_path, days)
    assert not _histograms(result)
    assert _quantiles(result)
    (table,) = result.tables
    assert table.no_span == (
        ("day", "its span is longer than 240 years; its quartiles stay on the type's ladder"),
    )


def test_a_column_with_a_declared_range_keeps_its_contract_bins(tmp_path):
    declared = [
        str(defaults._read_instant(each) * 1000)  # type: ignore[operator]
        for each in defaults.timestamp_bins(
            *defaults.declared_instants({"validationRules": _RANGE})  # type: ignore[misc]
        )
    ]
    # records on 11 months of 12 each: an unranged column would have spanned them
    days = [date(2020, month, 1) for month in range(1, 12) for _ in range(12)]
    result = _profile(tmp_path, days, rules=_RANGE)
    assert not result.tables[0].no_span
    bins = [each["bins"] for each in _statistics(result) if "bins" in each]
    assert bins
    assert all(each == declared for each in bins)


def _asked(*statistics: str, clique: Sequence[str] = ()) -> Request:
    return Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(
            RequestTable(
                "orders",
                False,
                (
                    RequestColumn("status", "category", (), ("levelHistogram/1",)),
                    RequestColumn("day", "timestamp", (), tuple(statistics)),
                ),
                clique_columns=tuple(clique),
            ),
        ),
    )


def test_a_request_gets_the_histogram_and_is_told_why_the_quantiles_are_not_there(tmp_path):
    result = _profile(
        tmp_path,
        _on_the_first_of_each_month(11, 12),
        request=_asked("marginalHistogram/1", "quantile/2"),
    )
    assert len(_histograms(result)) == 1
    assert not _quantiles(result)
    said = dict(result.not_measured)
    assert "histogram or quantiles, never both" in said["quantile/2 of orders.day"]
    assert "marginalHistogram/1 of orders.day" not in said


def test_a_request_for_a_histogram_of_a_column_with_no_span_is_told_it_gets_quantiles(tmp_path):
    days = [date(2022, 1, 15)] * 3 + [date(2023, 6, 1)] * 20 + [date(2024, 4, 1)] * 20
    result = _profile(tmp_path, days, request=_asked("marginalHistogram/1"))
    assert (
        "gets quantile/2 on its type's ladder"
        in dict(result.not_measured)["marginalHistogram/1 of orders.day"]
    )


def test_a_column_a_request_groups_by_keeps_the_ladder_and_is_named(tmp_path):
    result = _profile(
        tmp_path,
        _on_the_first_of_each_month(11, 12),
        request=_asked("marginalHistogram/1", clique=("status", "day")),
    )
    assert not [each for each in _histograms(result) if "groupBy" not in each]
    (table,) = result.tables
    assert [name for name, _ in table.no_span] == ["day"]
    assert "groups by it" in table.no_span[0][1]
    assert "quartiles stay" not in table.no_span[0][1]  # the request did not ask them
    grouped = [each for each in _statistics(result, "status") if "groupByBins" in each]
    assert len(grouped[0]["groupByBins"]["bins"]) == 203  # the ladder: 1900 to 2100, and the ends


def test_a_request_asking_only_the_quantiles_gets_them(tmp_path):
    result = _profile(tmp_path, _on_the_first_of_each_month(11, 12), request=_asked("quantile/2"))
    assert not _histograms(result)
    assert _quantiles(result)
    assert not result.tables[0].no_span
    assert "quantile/2 of orders.day" not in dict(result.not_measured)


def test_a_column_with_no_value_has_its_own_reason(tmp_path):
    result = _profile(tmp_path, [None] * 12)
    (table,) = result.tables
    assert table.no_span == (
        ("day", "no record holds a value; its quartiles stay on the type's ladder"),
    )


# -- the end bins of a span ----------------------------------------------------------------

_MS_PER_DAY = 86_400_000


def _ms(day: date) -> int:
    return (day - date(1970, 1, 1)).days * _MS_PER_DAY


def _span(*parts: tuple) -> tuple:
    """``date_span`` of (day, count) parts at the default least cell of ten."""
    values = [_ms(day) for day, count in parts for _ in range(count)]
    return defaults.date_span(values, 10)


def _month(index: int) -> date:
    """The first day of the month *index* months after January 2000."""
    return date(2000 + index // 12, index % 12 + 1, 1)


def test_a_small_last_month_falls_to_years_and_a_small_last_year_leaves_no_span():
    # A last month under the least cell: the months are refused, the years hold a hundred each.
    edges, reason = _span(
        (date(2020, 3, 1), 100),
        (date(2020, 12, 1), 100),
        (date(2021, 2, 1), 100),
        (date(2021, 6, 1), 3),
    )
    assert reason is None and len(edges) == 3  # the years 2020 and 2021
    assert edges[0].startswith("2020-01-01") and edges[-1].startswith("2022-01-01")
    # A last year as small too: nothing stands on enough records, and the reason says so.
    edges, reason = _span((date(2020, 3, 1), 100), (date(2021, 6, 1), 3))
    assert edges is None
    assert reason is not None and "fewer than 10 records" in reason


def test_a_small_first_month_falls_to_years():
    edges, reason = _span((date(2020, 3, 1), 3), (date(2020, 8, 1), 100), (date(2021, 6, 1), 100))
    assert reason is None and len(edges) == 3


def test_exactly_the_least_cell_in_each_end_month_keeps_months():
    edges, reason = _span((date(2020, 3, 1), 10), (date(2020, 5, 1), 100), (date(2020, 7, 1), 10))
    assert reason is None
    assert len(edges) == 6  # the months March to July, and the edge after
    edges, reason = _span((date(2020, 3, 1), 10), (date(2020, 5, 1), 100), (date(2020, 7, 1), 9))
    assert reason is None and len(edges) == 2  # July falls short: calendar year 2020 alone


def test_a_span_of_the_most_bins_keeps_months_and_one_more_falls_to_years():
    most = defaults.MOST_SPAN_BINS
    assert most == 240
    edges, reason = _span((_month(0), 20), (_month(most - 1), 20))
    assert reason is None and len(edges) == most + 1
    edges, reason = _span((_month(0), 20), (_month(most), 20))
    assert reason is None and len(edges) == 22  # the twenty-one years 2000 to 2020, and one more
