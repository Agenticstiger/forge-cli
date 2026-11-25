# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A profile request's consumer queries: each answered with its rate, as a ``queries`` entry.

A query ``{name, sql, unit, groupBy}`` becomes one ``levelHistogram/1`` over ``false`` and
``true`` (PROFILE-SPEC section 5): ``k`` the records of the unit, the ``true`` count the rows the
query returns, per level of the ``groupBy`` column when it has one. What is answered is decided
before any record is read (``--strict`` refuses the rest), the text runs only as a checked tree
that was built again and checked again, and the queries of one population are checked as a set
with the facts of the table, because two queries that differ by one literal reveal one record by
differencing. The tables are stations and their readings.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from fluid_build.profiler import _stats, consumer_queries, disclosure, query_rates
from fluid_build.profiler import request as asked_for
from fluid_build.profiler.runner import ExposeSource, ProfileError, profile_exposes
from tests.profiler._fake_backend import FakeBackend

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.request")
pytest.importorskip("sqlglot")
pytest.importorskip("duckdb")

CONTRACT: Dict[str, Any] = {
    "exposes": [
        {
            "exposeId": "station",
            "contract": {
                "schema": [
                    {"name": "station_id", "type": "string"},
                    {"name": "region", "type": "enum('NORTH','SOUTH','WEST')"},
                    {"name": "active", "type": "boolean"},
                    {"name": "altitude", "type": "long"},
                ],
                "invariants": [{"id": "north", "check": "region = 'NORTH'"}],
            },
        },
        {
            "exposeId": "reading",
            "contract": {
                "schema": [
                    {"name": "reading_id", "type": "string"},
                    {"name": "station_id", "type": "string"},
                    {"name": "value", "type": "decimal(10,2)"},
                ]
            },
        },
    ]
}

NORTH = "SELECT station_id FROM station WHERE region = 'NORTH'"
HEAVY_VIEW = "SELECT station_id, count(*) AS n FROM reading GROUP BY station_id"
HEAVY_QUERY = (
    f'WITH "v" AS ({HEAVY_VIEW}) SELECT "v"."station_id" AS "station_id" FROM "v" AS "v" '
    'WHERE "v"."n" >= 20'
)
NORTH_VIEW = "SELECT * FROM station WHERE region = 'NORTH'"
NORTH_VIEW_QUERY = (
    f'WITH "v" AS ({NORTH_VIEW}) SELECT "v"."station_id" AS "station_id" FROM "v" AS "v" '
    'WHERE "v"."altitude" < 150'
)


def _stations() -> List[Dict[str, Any]]:
    return [
        {
            "station_id": f"s{n:03d}",
            "region": ("NORTH", "SOUTH", "WEST")[n % 3],
            "active": n % 2 == 0,
            "altitude": n,
        }
        for n in range(300)
    ]


def _readings() -> List[Dict[str, Any]]:
    rows = []
    for station in range(100):
        for each in range(25 if station % 2 == 0 else 10):
            rows.append(
                {
                    "reading_id": f"r{station:03d}-{each:02d}",
                    "station_id": f"s{station:03d}",
                    "value": "1.00",
                }
            )
    return rows


def _write(path: Path, rows: Sequence[Dict[str, Any]]) -> str:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return str(path)


def _uses_readings(queries) -> bool:
    return any(
        each.table == "reading" or "reading" in each.sql or "reading" in (each.unit_sql or "")
        for each in queries
    )


def _sources(tmp_path: Path, queries=(), stations=None, readings=None) -> List[ExposeSource]:
    found = [ExposeSource("station", _write(tmp_path / "station.ndjson", stations or _stations()))]
    if _uses_readings(queries):
        found.append(
            ExposeSource("reading", _write(tmp_path / "reading.ndjson", readings or _readings()))
        )
    return found


def _on_table(name: str, sql: str, *, group_by: Optional[str] = None, table: str = "station"):
    return _stats.RequestQuery(name, sql, table=table, group_by=group_by)


def _on_view(name: str, sql: str, unit_sql: str):
    return _stats.RequestQuery(name, sql, unit_sql=unit_sql)


def _request(*queries, facts=(), columns=("region",)) -> _stats.Request:
    tables = [
        _stats.RequestTable(
            "station", False, tuple(_stats.RequestColumn(c, "category") for c in columns)
        )
    ]
    if _uses_readings(queries):
        tables.append(_stats.RequestTable("reading", False, ()))
    return _stats.Request(None, 16, tuple(tables), (), tuple(facts), tuple(queries))


def _profile(
    tmp_path: Path, *queries, facts=(), columns=("region",), strict=False, sources=None, **kw
):
    return profile_exposes(
        CONTRACT,
        sources if sources is not None else _sources(tmp_path, queries, **kw),
        backend=FakeBackend(),
        request=_request(*queries, facts=facts, columns=columns),
        strict=strict,
    )


def _gone(tmp_path: Path, *queries) -> List[ExposeSource]:
    """Sources that do not exist: a read would be a different refusal than the one tested."""
    found = [ExposeSource("station", str(tmp_path / "none.ndjson"))]
    if _uses_readings(queries):
        found.append(ExposeSource("reading", str(tmp_path / "no.ndjson")))
    return found


def _entry(result, name: str) -> Dict[str, Any]:
    (found,) = [each for each in result.document["queries"] if each["name"] == name]
    (statistic,) = found["statistics"]
    return statistic


def _groups(statistic) -> Dict[str, Dict[str, Any]]:
    return {each["group"]: each for each in statistic["groups"]}


def _said(result) -> Dict[str, str]:
    return dict(result.not_measured)


# ---- a query over a table, a grouped one, and a view ---------------------------------------


def test_a_query_over_a_table_is_answered_with_its_rate(tmp_path):
    result = _profile(tmp_path, _on_table("north", NORTH))
    statistic = _entry(result, "north")
    assert statistic["definition"] == "levelHistogram/1"
    assert statistic["levels"] == ["false", "true"]
    assert "groupBy" not in statistic
    (group,) = statistic["groups"]
    assert group == {"group": "", "status": "F", "k": 300, "levelCounts": [200, 100]}
    assert not _said(result)
    (outcome,) = result.queries
    assert (outcome.name, outcome.published, outcome.groups_published) == ("north", True, None)


def test_a_request_that_lists_no_column_still_has_the_columns_its_queries_read_measured(tmp_path):
    result = _profile(tmp_path, _on_table("north", NORTH), columns=())
    assert _groups(_entry(result, "north"))[""]["levelCounts"] == [200, 100]
    # reading a column for a query publishes nothing else about it
    assert "region" not in result.document["tables"]["station"].get("columns", {})


def test_a_query_grouped_by_a_categorical_column_has_a_group_per_level(tmp_path):
    result = _profile(
        tmp_path,
        _on_table(
            "active", "SELECT station_id, region FROM station WHERE active", group_by="region"
        ),
    )
    statistic = _entry(result, "active")
    assert statistic["groupBy"] == "region"
    assert [each["group"] for each in statistic["groups"]] == ["NORTH", "SOUTH", "WEST"]
    for group in statistic["groups"]:
        assert group == {**group, "status": "F", "k": 100, "levelCounts": [50, 50]}
    assert result.document["_schemaURL"].endswith("/1-3-0/FluxProfile.json")


def test_a_view_unit_that_filters_counts_the_views_rows(tmp_path):
    result = _profile(tmp_path, _on_view("low", NORTH_VIEW_QUERY, NORTH_VIEW))
    (group,) = _entry(result, "low")["groups"]
    # 100 stations of the north; altitude below 150 holds half of them
    assert (group["k"], group["levelCounts"]) == (100, [50, 50])


NOT_A_FILTER = (
    "a unit that aggregates, joins or reshapes rows is not one fluid profile can check with "
    "its table: name the table, or a view that keeps some of one table's rows"
)
JOIN_VIEW = "SELECT a.station_id FROM station AS a JOIN station AS b ON b.altitude = 7777"
UNFILTERED_VIEWS = [
    ("join", JOIN_VIEW, "station_id"),
    ("distinct", "SELECT DISTINCT region FROM station", "region"),
    ("computed", "SELECT altitude + 1 AS up FROM station", "up"),
    (
        "limit",
        "SELECT * FROM station ORDER BY station_id, region, active, altitude LIMIT 5",
        "station_id",
    ),
    (
        "subquery",
        "SELECT * FROM station WHERE station_id IN (SELECT station_id FROM station)",
        "station_id",
    ),
]


def _over(view: str, where: str = "TRUE", column: str = "station_id") -> str:
    return (
        f'WITH "v" AS ({view}) SELECT "v"."{column}" AS "{column}" FROM "v" AS "v" '
        f"WHERE {where}"
    )


@pytest.mark.parametrize(
    ("label", "view", "column"), UNFILTERED_VIEWS, ids=[each[0] for each in UNFILTERED_VIEWS]
)
def test_a_view_unit_that_is_not_a_filter_of_one_table_is_refused_before_any_read(
    tmp_path, label, view, column
):
    query = _on_view("odd", _over(view, f'"v"."{column}" IS NOT NULL', column), view)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, query, strict=True, sources=_gone(tmp_path, query))
    assert refused.value.event == "profile_request_not_formed"
    assert NOT_A_FILTER in str(refused.value.context)
    assert view not in str(refused.value.context)


def test_a_view_unit_that_aggregates_names_what_an_aggregating_unit_is_answered_by(tmp_path):
    query = _on_view("odd", _over(HEAVY_VIEW, '"v"."n" IS NOT NULL', "n"), HEAVY_VIEW)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, query, strict=True, sources=_gone(tmp_path, query))
    assert query_rates.ENTITY_ONLY in str(refused.value.context)
    assert HEAVY_VIEW not in str(refused.value.context)


def _padded_views(count: int):
    found = []
    for pad in range(count):
        ids = ", ".join(f"'s{299 - each:03d}'" for each in range(pad)) or "'none'"
        view = f"SELECT * FROM station WHERE altitude <> 7777 AND station_id NOT IN ({ids})"
        found.append(_on_view(f"pad{pad}", _over(view, '"v"."station_id" < \'s150\''), view))
    return found


@pytest.mark.parametrize("small", [1, 2, 3, 4])
def test_filter_views_that_exclude_a_few_known_records_give_no_small_cell_back(tmp_path, small):
    stations = _stations()
    for each in range(small):
        stations[each]["altitude"] = 7777
    result = _profile(tmp_path, *_padded_views(5), stations=stations)
    # the table's own queries of the same shape are withheld by the table's set check, and
    # a view unit is one more factor of that set: no size of a view is published either
    assert "queries" not in result.document or all(
        group["status"] != "F"
        for entry in result.document["queries"]
        for group in entry["statistics"][0]["groups"]
    )


def test_a_filter_view_and_a_table_query_that_differ_by_one_record_are_both_withheld(tmp_path):
    view = "SELECT * FROM station WHERE altitude <> 7777"
    stations = _stations()
    stations[0]["altitude"] = 7777
    result = _profile(
        tmp_path,
        _on_view("unit", _over(view, '"v"."station_id" < \'s150\''), view),
        _on_table(
            "plain",
            "SELECT station_id FROM station WHERE altitude <> 7777 AND station_id <> 's299'",
        ),
        stations=stations,
    )
    for name in ("unit", "plain"):
        (group,) = _entry(result, name)["groups"]
        assert group["status"] != "F"


def test_a_view_unit_that_joins_cannot_read_a_records_attribute_through_its_size(tmp_path):
    query = _on_view("size", _over(JOIN_VIEW), JOIN_VIEW)
    with pytest.raises(ProfileError):
        _profile(tmp_path, query, strict=True, sources=_gone(tmp_path, query))
    result = _profile(tmp_path, query)
    assert _said(result)["query size"] == NOT_A_FILTER
    assert "queries" not in result.document


def test_the_engines_reader_accepts_the_document_and_the_rate_is_the_true_rate(tmp_path):
    from flux_stats.profile import load_profile_text

    def rates(*queries) -> Dict[str, List[float]]:
        result = _profile(tmp_path, *queries)
        profile, _ = load_profile_text(result.canonical.decode("utf-8"))
        found: Dict[str, List[float]] = {}
        for entry in profile.queries:
            for group in entry.statistics[0].groups:
                counts = group.statistic.level_counts
                found.setdefault(entry.name, []).append(counts[1] / sum(counts))
        return found

    assert rates(_on_table("north", NORTH)) == {"north": [100 / 300]}
    grouped = _on_table(
        "grouped", "SELECT station_id, region FROM station WHERE active", group_by="region"
    )
    assert rates(grouped) == {"grouped": [0.5, 0.5, 0.5]}
    assert rates(_on_view("low", NORTH_VIEW_QUERY, NORTH_VIEW)) == {"low": [0.5]}


def test_the_set_of_queries_is_kept_in_the_order_of_the_request(tmp_path):
    result = _profile(
        tmp_path,
        _on_table("b_second", NORTH),
        _on_table("a_first", "SELECT station_id FROM station WHERE active"),
    )
    assert [each["name"] for each in result.document["queries"]] == ["b_second", "a_first"]


# ---- what is answered is decided before any record is read ----------------------------------

SHAPES = [
    ("join", "SELECT s.station_id FROM station s JOIN reading r ON s.station_id = r.station_id"),
    ("distinct", "SELECT DISTINCT region FROM station"),
    ("group", "SELECT region FROM station GROUP BY region"),
    ("having", "SELECT region FROM station GROUP BY region HAVING count(*) > 3"),
    ("order", "SELECT station_id FROM station ORDER BY station_id"),
    ("limit", "SELECT station_id FROM station ORDER BY station_id LIMIT 5"),
    ("aggregate", "SELECT count(*) AS n FROM station"),
    ("window", "SELECT row_number() OVER (ORDER BY station_id) AS r FROM station"),
    ("union", "SELECT station_id FROM station UNION SELECT station_id FROM reading"),
    (
        "subquery",
        "SELECT station_id FROM station WHERE station_id IN (SELECT station_id FROM reading)",
    ),
    ("computed", "SELECT altitude + 1 AS up FROM station"),
    ("constant", "SELECT 1 AS one"),
    ("other table", "SELECT station_id FROM reading"),
    ("with", "WITH x AS (SELECT * FROM station) SELECT station_id FROM x"),
]


@pytest.mark.parametrize(("label", "sql"), SHAPES, ids=[each[0] for each in SHAPES])
def test_a_query_that_is_not_a_subset_of_its_units_records_is_refused_before_any_read(
    tmp_path, label, sql
):
    each = _on_table("odd", sql)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, each, strict=True, sources=_gone(tmp_path, each))
    assert refused.value.event == "profile_request_not_formed"
    text = str(refused.value.context)
    assert "query odd" in text
    assert sql not in text


def test_without_strict_each_query_not_answered_is_named_with_its_reason(tmp_path):
    result = _profile(
        tmp_path,
        _on_table("north", NORTH),
        _on_table("joined", SHAPES[0][1]),
        _on_table("counted", SHAPES[6][1]),
    )
    said = _said(result)
    assert "query north" not in said
    assert said["query joined"].startswith(
        "fluid profile answers a query that keeps some of its unit's records: "
    )
    assert said["query counted"].startswith(
        "fluid profile answers a query that keeps some of its unit's records: "
    )
    assert [each["name"] for each in result.document["queries"]] == ["north"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/hosts')",
        "COPY station TO '/tmp/out.csv'",
        "ATTACH '/tmp/other.db' AS other",
        "SELECT station_id FROM station; SELECT station_id FROM reading",
        "INSTALL httpfs",
        "SELECT station_id FROM station WHERE current_setting('home_directory') = 'x'",
    ],
)
def test_a_text_that_reads_a_file_or_changes_a_setting_is_refused_and_nothing_runs(
    tmp_path, sql, monkeypatch
):
    from fluid_build.profiler import consumer_queries

    def must_not_run(*args, **kwargs):  # pragma: no cover - the failure itself
        raise AssertionError("a refused text reached the query process")

    monkeypatch.setattr(consumer_queries, "run_in_child", must_not_run)
    result = _profile(tmp_path, _on_table("bad", sql))
    assert "queries" not in result.document
    assert "query bad" in _said(result)
    assert sql not in _said(result)["query bad"]


def test_a_group_by_that_the_contract_does_not_allow_is_refused_before_any_read(tmp_path):
    cases = [
        _on_table("number", "SELECT station_id, altitude FROM station", group_by="altitude"),
        _on_table("text", "SELECT station_id FROM station", group_by="station_id"),
        _on_table("absent", "SELECT station_id FROM station", group_by="region"),
        _on_table(
            "renamed", "SELECT station_id, station_id AS region FROM station", group_by="region"
        ),
        _on_view("view", HEAVY_QUERY, HEAVY_VIEW),
    ]
    view_grouped = _stats.RequestQuery(
        "viewed", HEAVY_QUERY, unit_sql=HEAVY_VIEW, group_by="station_id"
    )
    for each in [*cases[:-1], view_grouped]:
        with pytest.raises(ProfileError) as refused:
            _profile(tmp_path, each, strict=True, sources=_gone(tmp_path, each))
        assert refused.value.event == "profile_request_not_formed"
        assert f"query {each.name}" in str(refused.value.context)


def test_the_queries_past_the_bound_one_unit_holds_are_named_and_not_answered():
    queries = tuple(
        _on_table(f"q{n:03d}", f"SELECT station_id FROM station WHERE altitude > {n}")
        for n in range(query_rates.MOST_PER_UNIT + 2)
    )
    planned = query_rates.plan_queries(CONTRACT, _request(*queries), FakeBackend().enum_levels)
    refused = [each for each in planned.plans if each.reason is not None]
    assert [each.name for each in refused] == ["q064", "q065"]
    assert all(str(query_rates.MOST_PER_UNIT) in each.reason for each in refused)


def test_the_texts_past_the_public_budget_are_named_in_the_order_of_the_request(monkeypatch):
    monkeypatch.setattr(query_rates, "MOST_TEXT_TOKENS", 16)
    queries = (
        _on_table("first", NORTH),
        _on_table("big", "SELECT station_id FROM station WHERE altitude > 1 AND altitude < 99"),
        _on_table("last", "SELECT station_id FROM station WHERE active"),
    )
    planned = query_rates.plan_queries(CONTRACT, _request(*queries), FakeBackend().enum_levels)
    by_name = {each.name: each.reason for each in planned.plans}
    assert by_name["first"] is None
    assert "not checked" in str(by_name["big"])
    # past the budget every later query is named too, in the order the request lists them
    assert by_name["last"] == by_name["big"]


# ---- disclosure: the queries of one population are one set ----------------------------------


def _withheld(statistic) -> bool:
    return all(each["status"] != "F" for each in statistic["groups"])


def test_a_query_and_the_same_query_with_one_more_row_are_both_withheld(tmp_path):
    near = "SELECT station_id FROM station WHERE region = 'NORTH' OR station_id = 's001'"
    result = _profile(tmp_path, _on_table("north", NORTH), _on_table("near", near))
    assert _withheld(_entry(result, "north")) and _withheld(_entry(result, "near"))
    assert {each["status"] for each in _entry(result, "near")["groups"]} == {"C"}
    assert result.queries[0].published is False and result.queries[0].code == disclosure.SECONDARY


def test_a_query_beside_a_larger_one_that_differs_by_a_small_overlap_is_withheld(tmp_path):
    a = _on_table("a", "SELECT station_id FROM station WHERE altitude >= 100")
    b = _on_table("b", "SELECT station_id FROM station WHERE altitude <= 102")
    either = _on_table(
        "either", "SELECT station_id FROM station WHERE altitude >= 100 OR altitude <= 102"
    )
    result = _profile(tmp_path, a, b, either)
    # |A and B| = 3 records: no pair of the three shows it, the three together do
    assert _withheld(_entry(result, "a")) and _withheld(_entry(result, "b"))
    published = [each for each in result.queries if each.published]
    assert all(each.name == "either" for each in published)


def test_a_query_and_a_fact_that_differ_by_one_row_are_both_withheld(tmp_path):
    fact = _stats.RequestFact("station.north", "station", "north")
    near = "SELECT station_id FROM station WHERE region = 'NORTH' OR station_id = 's001'"
    result = _profile(tmp_path, _on_table("near", near), facts=(fact,), columns=("region",))
    assert _withheld(_entry(result, "near"))
    (invariant,) = result.document["invariants"]
    assert {each["status"] for each in invariant["statistics"][0]["groups"]} == {"C"}


def test_a_query_that_returns_every_record_has_a_zero_cell_and_is_withheld(tmp_path):
    result = _profile(tmp_path, _on_table("all", "SELECT station_id FROM station"))
    assert _withheld(_entry(result, "all"))


def test_a_small_group_is_withheld_with_its_siblings_when_the_rest_would_give_it_back(tmp_path):
    stations = _stations()
    for row in stations:
        if row["region"] == "WEST" and row["altitude"] >= 12:
            row["region"] = "NORTH"  # the west keeps four stations
    result = _profile(
        tmp_path,
        _on_table(
            "active", "SELECT station_id, region FROM station WHERE active", group_by="region"
        ),
        stations=stations,
    )
    statistic = _entry(result, "active")
    # the west is small, and every other group is the rest of the table: all are withheld
    assert {each["status"] for each in statistic["groups"]} == {"C"}
    assert result.queries[0].published is False


def test_records_with_no_value_of_the_grouping_column_are_one_group_never_written(tmp_path):
    stations = _stations()
    for row in stations[:5]:
        row["region"] = None
    result = _profile(
        tmp_path,
        _on_table(
            "active", "SELECT station_id, region FROM station WHERE active", group_by="region"
        ),
        stations=stations,
    )
    # five records with no region would be a small group of their own: nothing is published
    assert {each["status"] for each in _entry(result, "active")["groups"]} == {"C"}


def test_two_grouped_queries_that_differ_in_one_group_by_one_row_withhold_that_group(tmp_path):
    first = "SELECT station_id, region FROM station WHERE altitude < 150"
    second = "SELECT station_id, region FROM station WHERE altitude < 150 OR station_id = 's151'"
    result = _profile(
        tmp_path,
        _on_table("first", first, group_by="region"),
        _on_table("second", second, group_by="region"),
    )
    for name in ("first", "second"):
        groups = _groups(_entry(result, name))
        assert groups["SOUTH"]["status"] == "C"  # s151 is a station of the south
    outcome = {each.name: each for each in result.queries}
    assert all(each.groups_published is not None for each in outcome.values())


# ---- what the records decide is named, and nothing is guessed --------------------------------


def _run_with(monkeypatch, change):
    from fluid_build.profiler import consumer_queries

    real = consumer_queries.run_in_child

    def wrapped(tables, statements, **kwargs):
        run = real(tables, statements, **kwargs)
        return change(run)

    monkeypatch.setattr(consumer_queries, "run_in_child", wrapped)


def test_a_query_whose_rows_are_not_its_units_records_is_not_published(tmp_path, monkeypatch):
    from dataclasses import replace

    def change(run):
        results = tuple(
            replace(each, count=(each.count or 0) + 1) if each.id.startswith("c") else each
            for each in run.results
        )
        return replace(run, results=results)

    _run_with(monkeypatch, change)
    result = _profile(tmp_path, _on_table("north", NORTH))
    assert "queries" not in result.document
    assert _said(result)["query north"] == query_rates.RUN_FAILED
    assert asked_for.decided_by_records(_said(result)["query north"])


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("timedOut", query_rates.RUN_FAILED),
        ("outOfMemory", query_rates.RUN_FAILED),
        ("error", query_rates.RUN_FAILED),
        ("crashed", query_rates.RUN_FAILED),
    ],
)
def test_a_statement_that_does_not_finish_names_its_query_and_is_never_a_strict_refusal(
    tmp_path, monkeypatch, status, reason
):
    from dataclasses import replace

    def change(run):
        results = tuple(
            (
                replace(each, status=status, reason="x", bits=None, count=None, groups=None)
                if each.id.startswith("b")
                else each
            )
            for each in run.results
        )
        return replace(run, results=results)

    _run_with(monkeypatch, change)
    result = _profile(tmp_path, _on_table("north", NORTH))
    assert _said(result)["query north"] == reason
    assert asked_for.decided_by_records(reason)
    assert "queries" not in result.document


def test_a_value_the_process_cannot_load_names_the_query_and_not_the_value(tmp_path):
    stations = _stations()
    stations[7]["altitude"] = "not a number"
    result = _profile(tmp_path, _on_table("north", NORTH + " AND altitude > 3"), stations=stations)
    reason = _said(result)["query north"]
    assert reason == query_rates.RUN_FAILED
    assert "not a number" not in reason
    assert asked_for.decided_by_records(reason)


# ---- the report, and what is never printed ---------------------------------------------------


def test_the_report_counts_the_queries_and_names_no_record_count(tmp_path):
    result = _profile(
        tmp_path,
        _on_table("north", NORTH),
        _on_table("everyone", "SELECT station_id FROM station"),
        _on_table("joined", SHAPES[0][1]),
    )
    lines = query_rates.report_lines(result.queries, 3)
    assert lines[0] == "Queries: 1 of 3 published whole."
    assert all(not any(char.isdigit() for char in line.replace("1 of 3", "")) for line in lines)


def test_a_grouped_query_with_some_groups_published_is_counted_in_part():
    from fluid_build.profiler.runner import QueryResult

    results = (
        QueryResult("a", True, None, None),
        QueryResult("b", True, None, (2, 3)),
        QueryResult("c", False, "C", None),
    )
    assert query_rates.report_lines(results, 4)[0] == "Queries: 1 of 4 published whole, 1 in part."


# ---- a private release names each query it does not release ---------------------------------


def test_a_private_release_publishes_no_query_rate():
    request = _request(_on_table("north", NORTH), _on_view("heavy", HEAVY_QUERY, HEAVY_VIEW))
    found = asked_for.unformed(request, private=True)
    assert found == [
        ("query north", asked_for.NO_QUERIES_PRIVATE),
        ("query heavy", asked_for.NO_QUERIES_PRIVATE),
    ]
    assert "noise on a query statistic" in asked_for.NO_QUERIES_PRIVATE
    assert asked_for.unformed(request) == []


def test_the_reasons_a_profile_gives_before_reading_are_not_the_ones_the_records_give():
    assert asked_for.decided_by_records(query_rates.RUN_FAILED)
    assert not asked_for.decided_by_records(query_rates.CAN_FAIL)
    planned = query_rates.plan_queries(
        CONTRACT, _request(_on_table("joined", SHAPES[0][1])), FakeBackend().enum_levels
    )
    assert not asked_for.decided_by_records(planned.plans[0].reason)


# ---- a group the rest of the table hides is withheld alone; what a column loads as --------------


def test_a_group_with_a_small_cell_is_withheld_and_the_others_are_published(tmp_path):
    stations = _stations()
    for row in stations:
        # the west keeps 100 stations, three of which the query returns: a small cell of its own
        if row["region"] == "WEST":
            row["active"] = row["altitude"] in (2, 5, 8)
        else:
            row["active"] = row["altitude"] % 2 == 0
    result = _profile(
        tmp_path,
        _on_table(
            "active", "SELECT station_id, region FROM station WHERE active", group_by="region"
        ),
        stations=stations,
    )
    groups = _groups(_entry(result, "active"))
    assert groups["WEST"]["status"] == "C"
    assert groups["NORTH"]["status"] == "F" and groups["SOUTH"]["status"] == "F"
    (outcome,) = result.queries
    assert outcome.published and outcome.groups_published == (2, 3) and not outcome.whole
    assert query_rates.report_lines(result.queries, 1) == [
        "Queries: 0 of 1 published whole, 1 in part."
    ]


def test_a_value_that_is_no_level_of_the_grouping_column_names_the_query_and_not_the_value(
    tmp_path,
):
    stations = _stations()
    stations[10]["region"] = "EAST"
    result = _profile(
        tmp_path,
        _on_table(
            "active", "SELECT station_id, region FROM station WHERE active", group_by="region"
        ),
        stations=stations,
    )
    assert _said(result)["query active"] == query_rates.RUN_FAILED
    assert "EAST" not in repr(result.not_measured)


def test_a_view_that_is_not_the_text_of_the_unit_is_refused_before_any_read(tmp_path):
    other = "SELECT * FROM station WHERE region = 'SOUTH'"
    query = _on_view("low", NORTH_VIEW_QUERY, other)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, query, strict=True, sources=_gone(tmp_path, query))
    assert "query low" in str(refused.value.context)
    assert "is not the unit's view" in str(refused.value.context)


@pytest.mark.parametrize(
    ("kind", "values", "good"),
    [
        ("BOOLEAN", [True, "false", None], [True, False, None]),
        ("INTEGER", [1, "2", None], [1, 2, None]),
        ("TINYINT", [127], [127]),
        ("DECIMAL(10,2)", ["1.50", 2, None], None),
        ("DATE", ["2026-07-01", "2026-07-02T00:00:00Z"], [1782864000000, 1782950400000]),
        ("TIMESTAMP", ["2026-07-01T10:00:00.250Z"], [1782900000250]),
        ("VARCHAR", ["a", None], ["a", None]),
    ],
)
def test_a_column_loads_as_exactly_the_type_the_contract_gives(kind, values, good):
    loaded = query_rates._loaded(kind, values)
    assert loaded is not None
    if good is not None:
        assert loaded == good


@pytest.mark.parametrize(
    ("kind", "values"),
    [
        ("BOOLEAN", ["yes"]),
        ("INTEGER", ["1.5"]),
        ("INTEGER", [2**40]),
        ("TINYINT", [128]),
        ("DECIMAL(10,2)", ["1.505"]),
        ("DECIMAL(5,2)", ["1000.00"]),
        ("DATE", ["2026-07-01T12:00:00Z"]),
        ("TIMESTAMP", ["2026-07-01T10:00:00"]),
        ("TIMESTAMP", ["2026-07-01T10:00:00.0005Z"]),
        ("VARCHAR", [5]),
        ("DOUBLE", [object()]),
    ],
)
def test_a_value_that_does_not_fit_its_type_exactly_is_not_loaded(kind, values):
    assert query_rates._loaded(kind, values) is None


def test_a_query_grouped_by_a_boolean_column_has_the_two_levels(tmp_path):
    result = _profile(
        tmp_path,
        _on_table(
            "low", "SELECT station_id, active FROM station WHERE altitude < 150", group_by="active"
        ),
    )
    statistic = _entry(result, "low")
    assert [each["group"] for each in statistic["groups"]] == ["false", "true"]
    assert [each["levelCounts"] for each in statistic["groups"]] == [[75, 75], [75, 75]]


# ---- a condition that can fail on a row's value is refused before anything runs --------------

ORACLE = (
    "SELECT station_id FROM station WHERE CASE WHEN station_id = 's007' AND active "
    "THEN CAST(region AS BIGINT) = 1 ELSE altitude < 150 END"
)
CAN_FAIL_CONDITIONS = [
    ("case", ORACLE),
    ("cast", "SELECT station_id FROM station WHERE CAST(region AS BIGINT) = 1"),
    ("arithmetic", "SELECT station_id FROM station WHERE altitude + 1 > 3"),
    ("division", "SELECT station_id FROM station WHERE 100 / altitude > 3"),
]


@pytest.mark.parametrize(
    ("label", "sql"), CAN_FAIL_CONDITIONS, ids=[each[0] for each in CAN_FAIL_CONDITIONS]
)
def test_a_condition_that_can_fail_on_a_value_is_refused_before_any_read(tmp_path, label, sql):
    each = _on_table("oracle", sql)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, each, strict=True, sources=_gone(tmp_path, each))
    assert refused.value.event == "profile_request_not_formed"
    assert "query oracle" in str(refused.value.context)
    assert query_rates.CAN_FAIL in str(refused.value.context)
    assert sql not in str(refused.value.context)
    assert not asked_for.decided_by_records(query_rates.CAN_FAIL)


def test_a_condition_in_a_units_view_that_can_fail_is_refused_before_any_read(tmp_path):
    view = "SELECT * FROM station WHERE altitude + 1 > 3"
    each = _on_view("oracle", _over(view), view)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, each, strict=True, sources=_gone(tmp_path, each))
    assert query_rates.CAN_FAIL in str(refused.value.context)


def test_the_answers_do_not_depend_on_what_one_chosen_record_holds(tmp_path):
    said = []
    for active in (True, False):
        stations = _stations()
        stations[7]["active"] = active
        result = _profile(tmp_path, _on_table("oracle", ORACLE), stations=stations)
        said.append(([each["name"] for each in result.document.get("queries", [])], _said(result)))
    assert said[0] == said[1]


def _failing_one(monkeypatch, status):
    from dataclasses import replace

    real = consumer_queries.run_in_child

    def wrapped(tables, statements, **kwargs):
        run = real(tables, statements, **kwargs)
        results = tuple(
            (
                replace(each, status=status, reason="x", bits=None, count=None, groups=None)
                if each.id == "b0"
                else each
            )
            for each in run.results
        )
        return replace(run, results=results)

    monkeypatch.setattr(consumer_queries, "run_in_child", wrapped)


@pytest.mark.parametrize("status", ["error", "timedOut", "outOfMemory", "crashed"])
def test_a_statement_that_fails_on_the_records_withholds_every_query_of_the_run(
    tmp_path, monkeypatch, status
):
    _failing_one(monkeypatch, status)
    result = _profile(
        tmp_path,
        _on_table("north", NORTH),
        _on_table("active", "SELECT station_id FROM station WHERE active"),
    )
    assert _said(result) == {
        "query north": query_rates.RUN_FAILED,
        "query active": query_rates.RUN_FAILED,
    }
    assert "queries" not in result.document


def test_a_value_that_does_not_load_withholds_the_queries_that_read_other_columns_too(tmp_path):
    stations = _stations()
    stations[7]["altitude"] = "not a number"
    result = _profile(
        tmp_path,
        _on_table("north", NORTH),
        _on_table("high", "SELECT station_id FROM station WHERE altitude > 3"),
        stations=stations,
    )
    assert set(_said(result).values()) == {query_rates.RUN_FAILED}
    assert len(_said(result)) == 2


# ---- a query beside a column's published cells ------------------------------------------------


def _three_west() -> List[Dict[str, Any]]:
    """Three WEST stations; the others alternate NORTH and SOUTH, so WEST's cell is withheld."""
    stations = _stations()
    for n, station in enumerate(stations):
        station["region"] = "WEST" if n < 3 else ("NORTH" if n % 2 else "SOUTH")
    return stations


def _with_histogram(tmp_path: Path, *queries, stations):
    region = _stats.RequestColumn("region", "category", (), ("levelHistogram/1",))
    request = _stats.Request(
        None, 16, (_stats.RequestTable("station", False, (region,)),), (), (), tuple(queries)
    )
    result = profile_exposes(
        CONTRACT,
        _sources(tmp_path, queries, stations=stations),
        backend=FakeBackend(),
        request=request,
    )
    (histogram,) = result.document["tables"]["station"]["columns"]["region"]["statistics"]
    (group,) = histogram["groups"]
    assert group["cellStatus"] == ["F", "C", "C"]  # NORTH published; SOUTH, WEST withheld
    return result


def test_a_query_that_a_published_cell_would_difference_to_a_withheld_one_is_withheld(tmp_path):
    # NORTH or WEST, less the published NORTH cell, is the withheld WEST cell
    either = _on_table("either", "SELECT station_id FROM station WHERE region IN ('NORTH','WEST')")
    result = _with_histogram(tmp_path, either, stations=_three_west())
    assert _withheld(_entry(result, "either"))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT station_id FROM station WHERE region IN ('SOUTH','WEST')",
        NORTH,
    ],
)
def test_a_query_the_published_cells_cannot_split_stays_answered(tmp_path, sql):
    result = _with_histogram(tmp_path, _on_table("whole", sql), stations=_three_west())
    assert not _withheld(_entry(result, "whole"))


def test_a_query_that_names_no_counted_column_is_not_checked_against_its_cells(tmp_path):
    # its rows split the NORTH cell into a small part, but nothing published names that part
    low = _on_table("low", "SELECT station_id FROM station WHERE altitude <= 20")
    result = _with_histogram(tmp_path, low, stations=_three_west())
    assert not _withheld(_entry(result, "low"))


def _altitudes(tmp_path: Path, *queries, column: _stats.RequestColumn, stations):
    contract = json.loads(json.dumps(CONTRACT))
    (altitude,) = [
        each for each in contract["exposes"][0]["contract"]["schema"] if each["name"] == "altitude"
    ]
    altitude["validationRules"] = [{"type": "range", "constraint": ">= 0 and <= 300"}]
    request = _stats.Request(
        None, 16, (_stats.RequestTable("station", False, (column,)),), (), (), tuple(queries)
    )
    return profile_exposes(
        contract,
        _sources(tmp_path, queries, stations=stations),
        backend=FakeBackend(),
        request=request,
    )


def _three_high() -> List[Dict[str, Any]]:
    stations = _stations()
    for n, station in enumerate(stations):
        station["altitude"] = 280 if n < 3 else n % 270
    return stations


@pytest.mark.parametrize(("floor", "withheld"), [(245, True), (250, False)])
def test_a_query_that_cuts_a_small_piece_out_of_a_published_bin_is_withheld(
    tmp_path, floor, withheld
):
    # at 245 the query less the published top bin is the few stations from 245 to 249
    high = _on_table("high", f"SELECT station_id FROM station WHERE altitude >= {floor}")
    histogram = _stats.RequestColumn("altitude", "number", (), ("marginalHistogram/1",))
    result = _altitudes(tmp_path, high, column=histogram, stations=_three_high())
    (statistic,) = result.document["tables"]["station"]["columns"]["altitude"]["statistics"]
    assert statistic["bins"][-2:] == ["250", "300"] and "cellStatus" not in statistic["groups"][0]
    assert _withheld(_entry(result, "high")) is withheld


def test_a_query_that_differs_from_a_published_count_of_values_by_a_few_rows_is_withheld(tmp_path):
    stations = _stations()
    for station in stations[100:120]:
        station["altitude"] = None
    either = _on_table(
        "either", "SELECT station_id FROM station WHERE altitude IS NULL OR station_id = 's050'"
    )
    counted = _stats.RequestColumn("altitude", "number", ("count",), ())
    result = _altitudes(tmp_path, either, column=counted, stations=stations)
    assert (
        result.document["tables"]["station"]["columns"]["altitude"]["metrics"]["count"]["status"]
        == disclosure.FREE
    )
    assert _withheld(_entry(result, "either"))


def test_a_fact_that_a_published_cell_would_difference_to_a_withheld_one_is_withheld(tmp_path):
    contract = json.loads(json.dumps(CONTRACT))
    contract["exposes"][0]["contract"]["invariants"].append(
        {"id": "north_or_west", "check": "region = 'NORTH' OR region = 'WEST'"}
    )
    fact = _stats.RequestFact("station.north_or_west", "station", "north_or_west")
    low = _on_table("low", "SELECT station_id FROM station WHERE altitude <= 150")
    region = _stats.RequestColumn("region", "category", (), ("levelHistogram/1",))
    request = _stats.Request(
        None, 16, (_stats.RequestTable("station", False, (region,)),), (), (fact,), (low,)
    )
    result = profile_exposes(
        contract,
        _sources(tmp_path, (low,), stations=_three_west()),
        backend=FakeBackend(),
        request=request,
    )
    (invariant,) = result.document["invariants"]
    assert {each["status"] for each in invariant["statistics"][0]["groups"]} == {"C"}


def test_a_view_unit_that_a_published_cell_would_difference_to_a_withheld_one_is_withheld(
    tmp_path,
):
    # the view keeps NORTH and WEST, so its published k less the NORTH cell is WEST's
    view = "SELECT * FROM station WHERE region = 'NORTH' OR region = 'WEST'"
    query = (
        f'WITH "v" AS ({view}) SELECT "v"."station_id" AS "station_id" FROM "v" AS "v" '
        'WHERE "v"."altitude" >= 100'
    )
    result = _with_histogram(tmp_path, _on_view("kept", query, view), stations=_three_west())
    assert _withheld(_entry(result, "kept"))
