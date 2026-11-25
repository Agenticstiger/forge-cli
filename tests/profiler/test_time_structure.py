# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A table's calendar and periods, and a column's statistic counted per entity (format 1-3-0).

The real registry reads every document and runs every output check. The loans below give every
hour of the week at least ten records, so a withheld cell is the check's doing or the test's.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.profiler import calendars, per_entity
from fluid_build.profiler import request as asked_for
from fluid_build.profiler._stats import (
    Request,
    RequestColumn,
    RequestEnd,
    RequestRelationship,
    RequestTable,
    calendar_levels,
    load_backend,
)
from fluid_build.profiler.runner import ExposeSource, ProfileError, StageTimings, profile_exposes

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.profile")
from flux_stats.profile import load_profile_text  # noqa: E402

START = datetime(2023, 1, 2, tzinfo=timezone.utc)  # a Monday
HOURS = 2000
MS = 86_400_000


def _contract(*, time: bool = True, dependency: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    semantics: Dict[str, Any] = {
        "entities": [{"name": "loan", "type": "primary", "expr": "loan_id"}],
        "relationships": [
            {
                "name": "borrower",
                "to": "members",
                "fromColumns": ["member_id"],
                "toColumns": ["member_id"],
                "cardinality": "many_to_one",
            }
        ],
    }
    if time:
        semantics["defaultAggTimeDimension"] = "loan_date"
    rule = dependency or {"from": ["member_id"], "to": ["branch"]}
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "p",
        "exposes": [
            {
                "exposeId": "loans",
                "kind": "table",
                "semantics": semantics,
                "contract": {
                    "schema": [
                        {"name": "loan_id", "type": "integer"},
                        {"name": "member_id", "type": "string"},
                        {"name": "branch", "type": "enum('north','south')"},
                        {"name": "loan_date", "type": "date"},
                        {"name": "returned_at", "type": "timestamp"},
                        {"name": "fine", "type": "integer"},
                    ],
                    "invariants": [{"id": "follow", "dependency": rule}],
                },
            }
        ],
    }


def _loans(*, thin: Optional[int] = None, undated: int = 0) -> List[Dict[str, Any]]:
    """One loan an hour; *thin* moves the loans of hour-of-week 10 after the third an hour on."""
    rows = []
    for number in range(HOURS):
        moment = START + timedelta(hours=number)
        returned = moment + timedelta(
            hours=1 if thin == 10 and number % 168 == 10 and number > 400 else 0
        )
        member = number % 100
        flipped = (number // 100) % 20 == 0
        north = (member % 2 == 0) != flipped
        rows.append(
            {
                "loan_id": number,
                "member_id": f"m{member}",
                "branch": "north" if north else "south",
                "loan_date": moment.date().isoformat() if number >= undated else None,
                "returned_at": returned.isoformat().replace("+00:00", "Z"),
                "fine": number % 7,
            }
        )
    return rows


def _profile(tmp_path, rows, contract=None, **options: Any):
    path = tmp_path / "loans.ndjson"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return profile_exposes(
        contract or _contract(),
        [ExposeSource("loans", str(path))],
        backend=options.pop("backend", load_backend()),
        **options,
    )


def _table(result) -> Dict[str, Any]:
    return result.document["tables"]["loans"]


def _calendar(result) -> Dict[str, Dict[str, Any]]:
    return {each["column"]: each for each in _table(result).get("calendar", ())}


# -- the counts ------------------------------------------------------------------------------


def test_civil_dates_match_the_calendar():
    for moment in (
        datetime(1969, 12, 31, tzinfo=timezone.utc),
        datetime(1970, 1, 1, tzinfo=timezone.utc),
        datetime(2000, 2, 29, tzinfo=timezone.utc),
        datetime(2000, 3, 1, tzinfo=timezone.utc),
        datetime(2024, 12, 31, tzinfo=timezone.utc),
        datetime(1, 1, 1, tzinfo=timezone.utc),
        datetime(9999, 12, 31, tzinfo=timezone.utc),
    ):
        day = (moment - datetime(1970, 1, 1, tzinfo=timezone.utc)).days
        assert calendars.civil(day) == (moment.year, moment.month)


def test_the_counts_are_read_in_utc_with_iso_weekdays():
    # Sunday 1 January 2023, 23:30 at -05:00 is Monday 2 January, 04:30 UTC.
    moment = datetime(2023, 1, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    milliseconds = int(moment.timestamp()) * 1000
    months, weekdays, week = calendars.calendar_counts([milliseconds], True)
    assert months.index(1) == 0  # January
    assert weekdays.index(1) == 0  # Monday is ISO weekday 1
    assert week is not None and week.index(1) == 4  # hour 4 of the week
    sunday = int(datetime(2023, 1, 1, tzinfo=timezone.utc).timestamp()) * 1000
    assert calendars.calendar_counts([sunday], True)[2].index(1) == 6 * 24  # type: ignore[union-attr]
    assert calendars.calendar_counts([sunday], False)[1].index(1) == 6
    assert calendars.calendar_counts([sunday], False)[2] is None
    before_epoch = int(datetime(1969, 12, 31, 5, tzinfo=timezone.utc).timestamp()) * 1000
    months, weekdays, week = calendars.calendar_counts([before_epoch], True)
    assert months.index(1) == 11 and weekdays.index(1) == 2  # a Wednesday in December
    assert week is not None and week.index(1) == 2 * 24 + 5


# -- the calendar ----------------------------------------------------------------------------


def test_a_date_has_a_month_and_a_weekday_and_a_timestamp_an_hour_too(tmp_path):
    result = _profile(tmp_path, _loans())
    calendar = _calendar(result)
    assert list(calendar) == ["loan_date", "returned_at"]  # contract order
    assert set(calendar["loan_date"]) == {"column", "month", "dayOfWeek"}
    assert set(calendar["returned_at"]) == {"column", "month", "dayOfWeek", "hourOfWeek"}
    levels = calendar_levels()
    for entry in calendar.values():
        for key in entry:
            if key != "column":
                assert entry[key]["levels"] == list(levels[key])
                assert entry[key]["definition"] == "levelHistogram/1"
                assert "groupBy" not in entry[key]
    assert "1-3-0" in result.document["_schemaURL"]
    (group,) = calendar["returned_at"]["hourOfWeek"]["groups"]
    assert group["status"] == "F" and len(group["levelCounts"]) == 168
    # Monday is the first level: the first loan is at Monday 00:00.
    assert group["levelCounts"][0] in (10, 15)


def test_a_withheld_hour_withholds_the_day_it_would_give_back(tmp_path):
    seen: List[Any] = []

    class Spy:
        def __init__(self, backend):
            self._backend = backend

        def __getattr__(self, name):
            return getattr(self._backend, name)

        def check_edge_counts(self, statistics, **kwargs):
            seen.append(list(statistics))
            return self._backend.check_edge_counts(statistics, **kwargs)

    result = _profile(tmp_path, _loans(thin=10), backend=Spy(load_backend()))
    joint = [call for call in seen if [len(each.edges) for each in call] == [169, 8]]
    assert len(joint) == 1  # the hours and the days of the week are decided together
    entry = _calendar(result)["returned_at"]
    (hours,) = entry["hourOfWeek"]["groups"]
    (days,) = entry["dayOfWeek"]["groups"]
    assert hours["levelCounts"][10] is None  # the thin hour is withheld
    # The day holding the thin hour is not published beside a lone withheld hour: either a second
    # hour of that day is withheld with it or the day is.
    withheld_hours = [count for count in hours["levelCounts"][:24] if count is None]
    assert len(withheld_hours) >= 2 or days["status"] != "F" or days["levelCounts"][0] is None


def test_the_month_is_withheld_whole_beside_a_span_that_withholds_a_cell(tmp_path):
    rows = _loans()
    published = _calendar(_profile(tmp_path, rows))["loan_date"]["month"]
    assert published["groups"][0]["status"] == "F"
    # Almost no loan in February: the span's February cell is withheld.
    thin = [
        (
            {**each, "loan_date": "2023-01-20"}
            if each["loan_date"].startswith("2023-02-") and each["loan_id"] % 250 != 0
            else each
        )
        for each in rows
    ]
    month = _calendar(_profile(tmp_path, thin))["loan_date"]["month"]
    assert month["groups"] == [{"group": "", "status": "C"}]


def test_a_request_forms_the_columns_it_asks_and_no_others(tmp_path):
    asked = Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(
            RequestTable(
                "loans",
                False,
                (
                    RequestColumn("returned_at", "timestamp"),
                    RequestColumn("loan_date", "timestamp"),
                ),
                calendar=("returned_at",),
            ),
        ),
    )
    result = _profile(tmp_path, _loans(), request=asked)
    assert list(_calendar(result)) == ["returned_at"]
    assert "periods" not in _table(result)
    assert "calendar of loans.returned_at" not in dict(result.not_measured)


def test_a_column_with_no_known_value_is_named(tmp_path):
    rows = [{**each, "loan_date": None} for each in _loans()]
    result = _profile(tmp_path, rows)
    assert "loan_date" not in _calendar(result)
    assert dict(result.not_measured)["calendar of loans.loan_date"] == (
        "no non-null record to form it over"
    )


def test_strict_leaves_a_calendar_on_a_public_profile_and_a_private_release_names_it():
    asked = Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(
            RequestTable(
                "loans",
                False,
                (RequestColumn("returned_at", "timestamp"),),
                calendar=("returned_at",),
            ),
        ),
    )
    asked_for.refuse_unformed(asked)  # no refusal
    assert asked_for.unformed(asked) == []
    assert asked_for.unformed(asked, private=True) == [
        ("calendar of loans.returned_at", "a private release forms no calendar")
    ]
    with pytest.raises(ProfileError):
        asked_for.refuse_unformed(asked, private=True)


# -- periods ---------------------------------------------------------------------------------


def _periods(result) -> List[Dict[str, Any]]:
    return _table(result).get("periods", [])


def test_periods_need_a_default_time_dimension(tmp_path):
    assert _periods(_profile(tmp_path, _loans(), _contract(time=False))) == []
    (entry,) = _periods(_profile(tmp_path, _loans()))
    assert entry["column"] == "loan_date" and entry["period"] == "P1M"
    (statistic,) = entry["statistics"]  # fine is the one number column that is no key
    assert statistic["of"] == "fine" and statistic["definition"] == "mean/1"
    keys = [each["group"] for each in statistic["groups"]]
    assert keys[0] == "2023-01" and keys == sorted(keys)
    assert all(each["status"] == "F" for each in statistic["groups"])


def test_every_month_of_the_span_is_a_group_and_a_gap_withholds_them_all(tmp_path):
    rows = [
        {**each, "loan_date": "2023-01-20"} if each["loan_date"].startswith("2023-02-") else each
        for each in _loans()
    ]
    (entry,) = _periods(_profile(tmp_path, rows))
    groups = entry["statistics"][0]["groups"]
    assert [each["group"] for each in groups][:3] == ["2023-01", "2023-02", "2023-03"]
    assert all(each["status"] != "F" for each in groups)  # an empty month withholds the rest


def test_the_rows_with_no_time_are_a_group_that_can_withhold_the_rest(tmp_path):
    (entry,) = _periods(_profile(tmp_path, _loans(undated=3)))  # three loans have no date
    assert all(each["status"] == "D" for each in entry["statistics"][0]["groups"])


def test_periods_are_withheld_whole_beside_a_span_with_a_withheld_cell(tmp_path):
    rows = [
        (
            {**each, "loan_date": "2023-01-20"}
            if each["loan_date"].startswith("2023-02-") and each["loan_id"] % 250 != 0
            else each
        )
        for each in _loans()
    ]
    (entry,) = _periods(_profile(tmp_path, rows))
    assert {each["status"] for each in entry["statistics"][0]["groups"]} == {"D"}


# -- per entity ------------------------------------------------------------------------------


def _per(result) -> List[Dict[str, Any]]:
    return [each for each in _table(result)["columns"]["branch"]["statistics"] if "per" in each]


def test_a_dependency_from_an_entity_key_gives_a_statistic_per_entity(tmp_path):
    result = _profile(tmp_path, _loans())
    (statistic,) = _per(result)
    assert statistic["per"] == ["member_id"] and statistic["levels"] == ["north", "south"]
    (group,) = statistic["groups"]
    assert group["status"] == "F" and group["k"] == 100  # a hundred members, not 2000 loans
    assert group["levelCounts"] == [50, 50]
    assert "1-3-0" in result.document["_schemaURL"]


def test_a_dependency_from_a_column_that_is_no_key_gives_none(tmp_path):
    contract = _contract(dependency={"from": ["fine"], "to": ["branch"]})
    assert _per(_profile(tmp_path, _loans(), contract)) == []


def test_each_entity_counts_once_at_its_most_frequent_value():
    values = [("b",), ("a",), ("b",), ("a",), ("b",), ("z",), (None,), ("c",)]
    ids = [0, 0, 0, 1, 1, -1, 2, 3]  # entity 2 holds no known value; row 5 has a NULL key part
    assert per_entity._entity_values(ids, 4, values) == ["b", "a", "c"]
    # A tie goes to the first in code point order of the text.
    assert per_entity._entity_values([0, 0], 1, [("south",), ("north",)]) == ["north"]


# -- every document --------------------------------------------------------------------------


def test_timings_leave_the_document_bytes_as_they_are(tmp_path):
    plain = _profile(tmp_path, _loans())
    timed = _profile(tmp_path, _loans(), timings=StageTimings())
    assert plain.canonical == timed.canonical


# -- the calendar beside the column's own edges ----------------------------------------------


def _ranged(column: str, constraint: str) -> Dict[str, Any]:
    """The contract with a declared range on *column*: its quantiles sit on that range's bins."""
    contract = _contract()
    for each in contract["exposes"][0]["contract"]["schema"]:
        if each["name"] == column:
            each["validationRules"] = [{"type": "range", "constraint": constraint}]
    return contract


_WIDE = ">= '2022-12-01' and <= '2025-12-31'"


def _status(entry: Dict[str, Any]) -> str:
    (group,) = entry["groups"]
    return group["status"]


def test_the_month_is_withheld_beside_quantile_edges_that_are_not_month_starts(tmp_path):
    result = _profile(tmp_path, _loans(), _ranged("loan_date", _WIDE))
    entry = _calendar(result)["loan_date"]
    assert _status(entry["month"]) == "C"
    # The declared range's edges are day starts: the days of the week keep their counts.
    assert _status(entry["dayOfWeek"]) == "F"


def test_the_week_keeps_its_counts_beside_range_edges_that_cut_no_record(tmp_path):
    # The declared range's ends are mid-day, but every record lies inside it: its lower edge has
    # no record below it, so a count there says nothing. Its first inner edge is a day start.
    midday = ">= '2022-12-01T05:30:00Z' and <= '2025-12-31T05:30:00Z'"
    entry = _calendar(_profile(tmp_path, _loans(), _ranged("returned_at", midday)))["returned_at"]
    assert _status(entry["month"]) == "C"  # the first inner edge is no month start
    assert {_status(entry[key]) for key in ("dayOfWeek", "hourOfWeek")} == {"F"}


def test_a_ladder_column_keeps_its_calendar_when_its_statistics_cut_only_at_unit_starts(tmp_path):
    # Years apart, so that no span is published and the type's ladder (one bin a year, and the
    # outer edges 0001-01-01 and 9999-12-31) is what the column's quantiles are counted on.
    rows = _loans()
    for row in rows:
        number = row["loan_id"]
        row["loan_date"] = (
            "1500-03-04"
            if number % 20 == 0
            else f"{2000 + number % 24}-0{1 + number % 9}-1{number % 9}"
        )
    result = _profile(tmp_path, rows)
    statistics = _table(result)["columns"]["loan_date"]["statistics"]
    assert not [each for each in statistics if each["definition"] == "marginalHistogram/1"]
    assert {each["groups"][0]["status"] for each in statistics} == {"F"}
    entry = _calendar(result)["loan_date"]
    assert {_status(entry[key]) for key in ("month", "dayOfWeek")} == {"F"}


def test_a_column_with_no_statistic_on_its_edges_keeps_its_calendar(tmp_path):
    asked = Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(
            RequestTable(
                "loans",
                False,
                (RequestColumn("returned_at", "timestamp"),),
                calendar=("returned_at",),
            ),
        ),
    )
    entry = _calendar(_profile(tmp_path, _loans(), request=asked))["returned_at"]
    assert {_status(entry[key]) for key in ("month", "dayOfWeek", "hourOfWeek")} == {"F"}


def _statistic(**fields: Any) -> Dict[str, Any]:
    return {"definition": "marginalHistogram/1", "groups": [{"group": "", "status": "F"}], **fields}


def test_every_edge_statistic_on_the_column_counts_for_the_calendar():
    month = calendars._month_start
    jan, feb = str(1672531200000), str(1675209600000)
    fits = {"columns": {"d": {"statistics": [_statistic(bins=[jan, feb])]}}}
    assert calendars.edge_fit(fits, "d", month, "month") is None
    assert calendars.edge_fit(fits, "other", month, "month") is None  # no edge statistic: it fits
    odd = {"columns": {"d": {"statistics": [_statistic(bins=[jan, str(1675209600001)])]}}}
    assert "d" in (calendars.edge_fit(odd, "d", month, "month") or "")
    # A statistic of another column grouped by bins of this one is an edge statistic of it.
    grouped = {
        "columns": {
            "d": {"statistics": []},
            "x": {
                "statistics": [
                    _statistic(groupByBins={"column": "d", "bins": [jan, "1672617600000"]})
                ]
            },
        }
    }
    assert calendars.edge_fit(grouped, "d", month, "month") is not None
    assert calendars.edge_fit(grouped, "d", calendars._day_start, "day") is None
    elsewhere = {
        "columns": {"x": {"statistics": [_statistic(groupByBins={"column": "y", "bins": ["1"]})]}}
    }
    assert calendars.edge_fit(elsewhere, "d", month, "month") is None
    # A quantile counts, a statistic of the column grouped by another column does not.
    quantile = {"columns": {"d": {"statistics": [_statistic(definition="quantile/2", bins=["5"])]}}}
    assert calendars.edge_fit(quantile, "d", month, "month") is not None
    other = {"columns": {"d": {"statistics": [_statistic(groupBy="x", bins=["5"])]}}}
    assert calendars.edge_fit(other, "d", month, "month") is None


def test_a_withheld_cell_or_group_beside_the_edges_withholds_the_calendar():
    jan, feb = str(1672531200000), str(1675209600000)
    for group in (
        {"group": "", "status": "C"},
        {"group": "", "status": "F", "cellStatus": ["F", "C"]},
    ):
        held = {"columns": {"d": {"statistics": [_statistic(bins=[jan, feb], groups=[group])]}}}
        assert calendars.edge_fit(held, "d", calendars._month_start, "month") is not None


def _quantile(*, below: int, through: int, at: int = 0, k: int = 100, **fields: Any):
    group = {"group": "", "k": k, "quantileBin": at, "binBelow": below, "binThrough": through}
    return _statistic(definition="quantile/2", groups=[{**group, "status": "F"}], **fields)


def test_a_statistic_counts_only_at_the_instants_a_published_count_cuts_at():
    month, day = calendars._month_start, calendars._day_start
    jan, feb, mid = str(1672531200000), str(1675209600000), str(1672617600000)  # mid: a day start
    noon = str(1672574400000)
    outer = ["-62135596800000", jan, feb, "253402300799999"]

    def fit(statistic, unit=month):
        return calendars.edge_fit(
            {"columns": {"d": {"statistics": [statistic]}}}, "d", unit, "unit"
        )

    # quantile/2: the lower edge of its bin where binBelow cuts, the upper where binThrough does
    assert fit(_quantile(below=0, through=100, at=1, bins=outer)) is None
    assert fit(_quantile(below=30, through=100, at=1, bins=outer)) is None  # a month start cuts
    assert (
        fit(_quantile(below=0, through=60, at=1, bins=[outer[0], jan, mid, outer[3]])) is not None
    )
    assert (
        fit(_quantile(below=0, through=60, at=1, bins=[outer[0], jan, mid, outer[3]]), day) is None
    )
    assert fit(_quantile(below=100, through=100, at=2, bins=[outer[0], jan, mid, outer[3]])) is None
    # marginalHistogram/1: the records below an edge are the sum of the bins before it
    histogram = _statistic(
        bins=[jan, mid, feb], groups=[{"group": "", "k": 40, "binCounts": [0, 40], "status": "F"}]
    )
    assert fit(histogram) is None  # nothing below mid, all of it below feb
    histogram["groups"][0]["binCounts"] = [10, 30]
    assert fit(histogram) is not None and fit(histogram, day) is None
    histogram["bins"] = [jan, noon, feb]  # a cut at noon is no day start either
    assert fit(histogram, day) is not None
    # a count that is not published as one cuts at every edge
    histogram["groups"][0]["binCounts"] = [0, 40]
    del histogram["groups"][0]["k"]
    assert fit(histogram) is not None


def test_a_cut_is_judged_against_all_the_rounded_cells_and_not_the_rounded_total():
    jan, feb, mid = str(1672531200000), str(1675209600000), str(1672617600000)  # mid: a day start
    # The cells are rounded each on its own, so their sum and the rounded ``k`` can differ: here
    # the rounded total (25) is below the records the first cell alone holds (30), which hid the
    # cut at ``mid`` from a count of "all of them".
    histogram = _statistic(
        bins=[jan, mid, feb],
        groups=[{"group": "", "k": 25, "binCounts": [30, 10], "status": "F"}],
    )
    doc = {"columns": {"d": {"statistics": [histogram]}}}
    assert calendars.edge_fit(doc, "d", calendars._month_start, "month") is not None
    assert calendars.edge_fit(doc, "d", calendars._day_start, "day") is None
    assert calendars._cuts(histogram, histogram["bins"]) == [mid]


def test_a_statistic_grouped_by_bins_cuts_at_the_edges_its_groups_split():
    jan, feb, mar = str(1672531200000), str(1675209600000), str(1677628800000)

    def grouped(first: int, second: int, edges):
        groups = [
            {"group": edges[1], "k": second, "status": "F"},  # the groups are not in bin order
            {"group": edges[0], "k": first, "status": "F"},
        ]
        return _statistic(groupByBins={"column": "d", "bins": edges}, groups=groups)

    def fit(statistic):
        doc = {"columns": {"x": {"statistics": [statistic]}}}
        return calendars.edge_fit(doc, "d", calendars._month_start, "month")

    assert fit(grouped(30, 20, [jan, feb, mar])) is None  # the one inner edge is a month start
    assert fit(grouped(30, 20, [jan, "1672617600000", mar])) is not None
    assert fit(grouped(0, 20, [jan, "1672617600000", mar])) is None  # nothing below it
    assert fit(grouped(30, 0, [jan, "1672617600000", mar])) is None  # nothing above it


def _beside(statistic: Dict[str, Any]) -> Dict[str, Any]:
    return {"parent": {"table": "t", "columns": ["id"]}, "statistics": [statistic]}


def _calendar_doc() -> Dict[str, Any]:
    def published() -> Dict[str, Any]:
        return {"groups": [{"group": "", "k": 100, "status": "F"}]}

    return {
        "calendar": [
            {
                "column": "d",
                "month": published(),
                "dayOfWeek": published(),
                "hourOfWeek": published(),
            }
        ],
        "periods": [
            {
                "column": "d",
                "statistics": [{"groups": [{"group": "2023-01", "k": 9, "status": "F"}]}],
            }
        ],
    }


def _children(first: int, second: int, edges, **fields: Any) -> Dict[str, Any]:
    return _statistic(
        groupByBins={"column": "d", "bins": edges},
        groups=[
            {"group": edges[0], "k": first, "status": "F"},
            {"group": edges[1], "k": second, "status": "F"},
        ],
        **fields,
    )


def test_a_relationship_grouped_by_a_column_withholds_what_the_calendar_published():
    from collections import Counter

    jan, feb, mar = str(1672531200000), str(1675209600000), str(1677628800000)
    mid = str(1672617600000)  # the second of January: a day start, no month start
    for edges, kept in (([jan, feb, mar], True), ([jan, mid, mar], False)):
        doc = _calendar_doc()
        tallies = (Counter({"F": 4}), Counter())
        calendars.withhold_beside_relationships(
            {"t": doc, "u": _calendar_doc()}, [_beside(_children(30, 20, edges))], tallies
        )
        month = doc["calendar"][0]["month"]["groups"]
        week = [doc["calendar"][0][key]["groups"] for key in ("dayOfWeek", "hourOfWeek")]
        period = doc["periods"][0]["statistics"][0]["groups"]
        if kept:
            assert month[0]["status"] == "F" and period[0]["status"] == "F"
            assert tallies[0] == Counter({"F": 4})
        else:
            assert month == [{"group": "", "status": "C"}]
            assert period == [{"group": "2023-01", "status": "D"}]
            # the days (cut at day starts) stay
            assert all(each[0]["status"] == "F" for each in week)
            assert tallies[0] == Counter({"F": 2, "D": 2})


def test_a_relationship_that_withholds_a_group_withholds_the_calendar_it_groups_by():
    from collections import Counter

    jan, feb, mar = str(1672531200000), str(1675209600000), str(1677628800000)
    held = _children(30, 20, [jan, feb, mar])
    held["groups"][1] = {"group": feb, "status": "C"}
    doc = _calendar_doc()
    calendars.withhold_beside_relationships(
        {"t": doc}, [_beside(held)], (Counter({"F": 4}), Counter())
    )
    assert doc["calendar"][0]["month"]["groups"] == [{"group": "", "status": "C"}]
    assert doc["calendar"][0]["dayOfWeek"]["groups"] == [{"group": "", "status": "C"}]


_JOINED = ">= '2023-01-01' and <= '2023-03-01'"
_COUNTS = (0, 1, 2, 3, 6, 12, 25)


def _members_and_loans(joined: List[str]):
    """Members spread over the bins of *joined*, each with 0 to 25 loans, 20 or more to a cell."""
    members, loans = [], []
    for number in range(1400):
        members.append({"member_id": number, "joined": joined[number % len(joined)]})
        for _ in range(_COUNTS[(number // len(joined)) % 7]):
            loans.append({"loan_id": len(loans), "member_id": number})
    return members, loans


def test_a_request_grouping_children_by_a_date_withholds_the_month_its_groups_cut(tmp_path):
    from fluid_build.profiler import defaults

    low, high = (int(datetime(2023, m, 1, tzinfo=timezone.utc).timestamp()) for m in (1, 3))
    lowers = defaults.timestamp_bins(low, high)[:-1]
    assert all(each.endswith("T00:00:00Z") for each in lowers)  # the inner edges are day starts
    joined = [each[:10] for each in lowers]  # a member on the first day of each bin
    members, loans = _members_and_loans(joined)
    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "p",
        "exposes": [
            {
                "exposeId": "members",
                "kind": "table",
                "contract": {
                    "schema": [
                        {"name": "member_id", "type": "integer"},
                        {
                            "name": "joined",
                            "type": "date",
                            "validationRules": [{"type": "range", "constraint": _JOINED}],
                        },
                    ]
                },
            },
            {
                "exposeId": "loans",
                "kind": "table",
                "semantics": {
                    "relationships": [
                        {
                            "name": "borrower",
                            "to": "members",
                            "fromColumns": ["member_id"],
                            "toColumns": ["member_id"],
                            "cardinality": "many_to_one",
                        }
                    ]
                },
                "contract": {
                    "schema": [
                        {"name": "loan_id", "type": "integer"},
                        {"name": "member_id", "type": "integer"},
                    ]
                },
            },
        ],
    }
    paths = {}
    for name, rows in (("members", members), ("loans", loans)):
        paths[name] = tmp_path / f"{name}.ndjson"
        paths[name].write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    asked = Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(
            RequestTable(
                "members",
                False,
                (RequestColumn("member_id", "number"), RequestColumn("joined", "timestamp")),
                calendar=("joined",),
            ),
            RequestTable(
                "loans",
                False,
                (RequestColumn("member_id", "number"), RequestColumn("loan_id", "number")),
            ),
        ),
        relationships=(
            RequestRelationship(
                RequestEnd("members", ("member_id",)),
                RequestEnd("loans", ("member_id",)),
                ("marginalHistogram/1",),
                group_by=("joined",),
            ),
        ),
    )
    result = profile_exposes(
        contract,
        [ExposeSource(name, str(path)) for name, path in paths.items()],
        backend=load_backend(),
        request=asked,
    )
    grouped = [
        each
        for relationship in result.document["relationships"]
        for each in relationship["statistics"]
        if "groupByBins" in each
    ]
    assert grouped and {g["status"] for g in grouped[0]["groups"]} == {"F"}  # every group published
    (entry,) = result.document["tables"]["members"]["calendar"]
    assert _status(entry["month"]) == "C"  # its inner edges are weeks, not months
    assert _status(entry["dayOfWeek"]) == "F"  # and they are day starts


def test_periods_are_withheld_beside_edges_that_are_not_month_starts(tmp_path):
    (entry,) = _periods(_profile(tmp_path, _loans(), _ranged("loan_date", _WIDE)))
    groups = entry["statistics"][0]["groups"]
    assert groups and {each["status"] for each in groups} == {"D"}


# -- per entity beside the row-level histogram -----------------------------------------------


def _tiered() -> Dict[str, Any]:
    """The contract with a three-level ``tier`` each member follows."""
    contract = _contract(dependency={"from": ["member_id"], "to": ["tier"]})
    contract["exposes"][0]["contract"]["schema"].append(
        {"name": "tier", "type": "enum('a','b','c')"}
    )
    return contract


def _panel(*, a_entities: int, a_rows: int) -> List[Dict[str, Any]]:
    """*a_entities* members of tier a with *a_rows* rows each, and twenty of b and of c with one."""
    rows: List[Dict[str, Any]] = []

    def add(member: str, tier: str) -> None:
        number = len(rows)
        moment = START + timedelta(hours=number)
        rows.append(
            {
                "loan_id": number,
                "member_id": member,
                "branch": "north",
                "tier": tier,
                "loan_date": moment.date().isoformat(),
                "returned_at": moment.isoformat().replace("+00:00", "Z"),
                "fine": number % 7,
            }
        )

    for member in range(a_entities):
        for _ in range(a_rows):
            add(f"a{member}", "a")
    for tier in ("b", "c"):
        for member in range(20):
            add(f"{tier}{member}", tier)
    return rows


def _tier(result) -> Dict[str, Dict[str, Any]]:
    statistics = _table(result)["columns"]["tier"]["statistics"]
    return {"per" if "per" in each else "row": each for each in statistics}


def test_a_per_cell_withheld_beside_a_published_row_cell_withholds_the_statistic(tmp_path):
    # Two entities of tier a with twelve rows each: the row cell (24) is published, the per cell
    # (2) is small. With twelve rows per entity known, the per cell would be the row cell over 12.
    result = _profile(tmp_path, _panel(a_entities=2, a_rows=12), _tiered())
    found = _tier(result)
    assert found["row"]["groups"][0]["levelCounts"][0] is not None
    assert found["per"]["groups"] == [{"group": "", "status": "C"}]
    load_profile_text(result.canonical.decode("utf-8"))


def test_a_per_cell_withheld_beside_a_withheld_row_cell_is_published(tmp_path):
    # Two entities of two rows each: the row cell (4) is small too, so nothing recovers it.
    result = _profile(tmp_path, _panel(a_entities=2, a_rows=2), _tiered())
    found = _tier(result)
    assert found["row"]["groups"][0]["cellStatus"][0] == "C"
    (group,) = found["per"]["groups"]
    assert group["status"] == "F" and group["cellStatus"][0] == "C"
    assert group["levelCounts"][2] is not None


def test_a_column_no_entity_holds_a_value_of_is_written_withheld_whole(tmp_path):
    rows = [{**each, "branch": None} for each in _loans()]
    result = _profile(tmp_path, rows)
    (statistic,) = _per(result)
    assert statistic["groups"] == [{"group": "", "status": "C"}]
    assert "per member_id statistic of loans.branch" not in dict(result.not_measured)
    load_profile_text(result.canonical.decode("utf-8"))


def test_the_report_counts_a_histogram_published_only_when_none_of_its_cells_is_withheld():
    from fluid_build.cli.profile_cmd import _published_groups

    whole = [{"groups": [{"status": "F", "cellStatus": ["F", "F"]}]}]
    partly = [{"groups": [{"status": "F", "cellStatus": ["F", "C"]}]}]
    withheld = [{"groups": [{"status": "C"}]}]
    assert _published_groups(whole + partly + withheld) == (1, 3)


def test_a_per_statistic_is_published_only_beside_the_same_withheld_cells():
    from fluid_build.profiler._stats import EdgeDecision, MeasuredGroup, MeasuredStatistic

    measured = MeasuredStatistic(groups=(MeasuredGroup(key="", document={}, k=0, cells=(0, 0, 0)),))

    def decision(*statuses: str) -> EdgeDecision:
        return EdgeDecision(
            group_status={"": "F"},
            cell_status={"": statuses},
            group_reason={},
            cell_reason={},
            rounded={},
        )

    def row(*statuses: str, group: str = "F") -> Dict[str, Any]:
        return {"groups": [{"group": "", "status": group, "cellStatus": list(statuses)}]}

    beside = per_entity._beside_published
    # The same cells withheld (or none): published.
    assert not beside(row("F", "C", "F"), decision("F", "C", "F"), measured)
    assert not beside(row("F", "F", "F"), decision("F", "F", "F"), measured)
    # The per statistic withholds a cell the row-level histogram publishes: withheld whole.
    assert beside(row("F", "F", "F"), decision("F", "C", "F"), measured)
    # The row-level histogram withholds a cell the per statistic publishes: withheld whole too.
    assert beside(row("F", "C", "F"), decision("F", "F", "F"), measured)
    assert beside(row("F", "C", "F"), decision("C", "F", "F"), measured)  # a subset is not enough
    assert beside(row("F", "C", "F", group="C"), decision("F", "F", "F"), measured)
    # No row-level histogram written: as before, nothing to differ from.
    assert not beside(None, decision("F", "C", "F"), measured)
    # A per statistic withheld whole is already as withheld as it can be.
    whole = EdgeDecision(
        group_status={"": "C"}, cell_status={}, group_reason={}, cell_reason={}, rounded={}
    )
    assert not beside(row("F", "F", "F"), whole, measured)
