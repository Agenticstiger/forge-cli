# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The history of a table with a validity window (PROFILE-SPEC format 1-3-0, ``history``).

The real registry reads every document and runs every output check. The stations below give
every cell the tests read at least ten entities, versions or changes, so a withheld cell is the
check's doing (or the test's) and not the data's. Each expected count comes from a plain
re-count of the rows the file holds, never from the profiler.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from fluid_build.profiler import _stats, disclosure, document, history
from fluid_build.profiler._stats import (
    Request,
    RequestColumn,
    RequestDatePair,
    RequestTable,
    load_backend,
)
from fluid_build.profiler.runner import ExposeSource, profile_exposes

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.profile")
from flux_stats.profile import load_profile_text  # noqa: E402

DAY = 86_400_000
RULE = disclosure.ACRO_DEFAULT
STATUS = ("active", "maintenance", "retired")
MODELS = ("m1", "m2")
#: Entities with this many versions, each number of versions on its own cell of the histogram.
ENTITIES = {1: 80, 2: 70, 3: 60, 5: 50, 10: 40, 20: 30}
#: Days between the starts of one entity's versions, by entity: each lands in its own bin of the
#: ladder of days.
GAPS = (5, 20, 60, 120, 250, 400, 40)
TABLE = "station_version"


def _rounded(count: int) -> int:
    return disclosure.round_count(count, RULE)


# -- the stations -------------------------------------------------------------------------------


def _contract(
    *,
    chain: Optional[str] = None,
    open_end: Optional[str] = None,
    stamped: bool = False,
    window: bool = True,
    types: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    kind = "timestamp" if stamped else "date"
    end_params: Dict[str, Any] = {"isEnd": True}
    if chain is not None:
        end_params["chain"] = chain
    if open_end is not None:
        end_params["openEnd"] = open_end
    semantics: Dict[str, Any] = {
        "entities": [{"name": "station", "type": "natural", "expr": "station_id"}]
    }
    if window:
        semantics["dimensions"] = [
            {
                "name": "valid_from",
                "type": "time",
                "typeParams": {"timeGranularity": "day", "validityParams": {"isStart": True}},
            },
            {
                "name": "valid_to",
                "type": "time",
                "typeParams": {"timeGranularity": "day", "validityParams": end_params},
            },
        ]
    schema = [
        {"name": "station_id", "type": "integer"},
        {"name": "valid_from", "type": (types or {}).get("valid_from", kind)},
        {"name": "valid_to", "type": (types or {}).get("valid_to", kind)},
        {"name": "status", "type": "enum('active','maintenance','retired')"},
        {"name": "sensor_model", "type": "enum('m1','m2')"},
    ]
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "p",
        "exposes": [
            {
                "exposeId": TABLE,
                "kind": "table",
                "semantics": semantics,
                "contract": {"schema": schema},
            }
        ],
    }


def _text(day: date, stamped: bool) -> str:
    return day.isoformat() + ("T06:30:00Z" if stamped else "")


def _stations(
    chain: str = "nextStart",
    *,
    stamped: bool = False,
    open_end: Optional[str] = None,
    start: date = date(2022, 1, 1),
    entities_by_versions: Optional[Dict[int, int]] = None,
) -> List[Dict[str, Any]]:
    """Each station's versions: one row per version, a status and a sensor model on each."""
    rows: List[Dict[str, Any]] = []
    number = 0
    for versions, entities in (entities_by_versions or ENTITIES).items():
        for _ in range(entities):
            gap = GAPS[number % len(GAPS)]
            first = start + timedelta(days=number)
            step = 1 if number % 2 == 0 else 2
            for k in range(versions):
                begins = first + timedelta(days=k * gap)
                if chain == "nextStart":
                    ends = begins + timedelta(days=gap)
                elif chain == "dayBefore":
                    ends = begins + timedelta(days=gap - 1)
                else:
                    ends = begins + timedelta(days=gap - (1 if number % 5 == 0 else 3))
                last = k == versions - 1
                closed = not last or number % 3 == 0
                moves = k - k // 3
                rows.append(
                    {
                        "station_id": number,
                        "valid_from": _text(begins, stamped),
                        "valid_to": (
                            _text(ends, stamped)
                            if closed
                            else (open_end if open_end is not None else None)
                        ),
                        "status": STATUS[(number + step * moves) % 3],
                        "sensor_model": MODELS[(number // 3 + k // 2) % 2],
                    }
                )
            number += 1
    return rows


def _write(path: Path, rows: Sequence[Dict[str, Any]]) -> str:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return str(path)


def _profile(
    tmp_path: Path, contract: Dict[str, Any], rows: Sequence[Dict[str, Any]], **options: Any
):
    return profile_exposes(
        contract,
        [ExposeSource(TABLE, _write(tmp_path / "stations.ndjson", rows))],
        backend=options.pop("backend", load_backend()),
        **options,
    )


def _history_of(result: Any) -> Dict[str, Any]:
    found: Dict[str, Any] = result.document["tables"][TABLE]["history"]
    return found


# -- a plain re-count of the rows ----------------------------------------------------------------


def _ms(text: str) -> int:
    stamp = datetime.fromisoformat(
        text.replace("Z", "+00:00") if "T" in text else text + "T00:00:00+00:00"
    )
    return int(stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).timestamp()) * 1000


def _versions(
    rows: Sequence[Dict[str, Any]], literal: Optional[str] = None
) -> Dict[int, List[Dict[str, Any]]]:
    """Each station's versions in order: by start, then end with an open one last."""
    by: Dict[int, List[Tuple[Tuple[int, int, int, int], Dict[str, Any]]]] = {}
    for position, row in enumerate(rows):
        open_ = row["valid_to"] is None or row["valid_to"] == literal
        end = None if open_ else _ms(row["valid_to"])
        key = (_ms(row["valid_from"]), 1 if open_ else 0, end or 0, position)
        by.setdefault(row["station_id"], []).append(
            (key, {**row, "start": key[0], "end": end, "open": open_})
        )
    return {
        station: [each for _, each in sorted(held, key=lambda i: i[0])]
        for station, held in by.items()
    }


def _expected(rows: Sequence[Dict[str, Any]], literal: Optional[str] = None) -> Dict[str, Any]:
    stations = _versions(rows, literal)
    pairs = [(a, b) for held in stations.values() for a, b in zip(held, held[1:], strict=False)]
    chain: Counter[str] = Counter()
    for a, b in pairs:
        if a["open"] or a["end"] > b["start"]:
            chain["overlap"] += 1
        elif a["end"] == b["start"]:
            chain["nextStart"] += 1
        elif b["start"] - a["end"] == DAY:
            chain["dayBefore"] += 1
        else:
            chain["gap"] += 1
    initial: Dict[str, Counter[str]] = {c: Counter() for c in ("status", "sensor_model")}
    moves: Dict[str, Dict[str, Counter[str]]] = {c: {} for c in initial}
    reasons: Counter[str] = Counter()
    for held in stations.values():
        for column in initial:
            if held[0][column] is not None:
                initial[column][held[0][column]] += 1
    for a, b in pairs:
        moved = [c for c in initial if b[c] is not None and b[c] != a[c]]
        for column in moved:
            moves[column].setdefault(a[column], Counter())[b[column]] += 1
        if moved:
            reasons[moved[0] if len(moved) == 1 else "several"] += 1
    return {
        "entities": len(stations),
        "versions": sum(len(held) for held in stations.values()),
        "per_entity": Counter(len(held) for held in stations.values()),
        "first": [held[0]["start"] for held in stations.values()],
        "lengths": Counter((b["start"] - a["start"]) // DAY for a, b in pairs),
        "chain": chain,
        "last": Counter("open" if held[-1]["open"] else "closed" for held in stations.values()),
        "initial": initial,
        "moves": moves,
        "reasons": reasons,
    }


# -- reading the document ------------------------------------------------------------------------


def _group(entry: Dict[str, Any]) -> Dict[str, Any]:
    (group,) = entry["groups"]
    assert group["status"] == "F"
    found: Dict[str, Any] = group
    return found


def _cells(group: Dict[str, Any], field: str) -> List[Optional[int]]:
    status = group.get("cellStatus", ["F"] * len(group[field]))
    return [c if s == "F" else None for c, s in zip(group[field], status, strict=True)]


def _levels(entry: Dict[str, Any]) -> Dict[str, Optional[int]]:
    return dict(zip(entry["levels"], _cells(_group(entry), "levelCounts"), strict=True))


def _bins(entry: Dict[str, Any]) -> List[Optional[int]]:
    return _cells(_group(entry), "binCounts")


def _moves(entry: Dict[str, Any]) -> Dict[str, Dict[str, Optional[int]]]:
    found: Dict[str, Dict[str, Optional[int]]] = {}
    for group in entry["groups"]:
        assert group["status"] == "F"
        found[group["group"]] = dict(
            zip(entry["levels"], _cells(group, "levelCounts"), strict=True)
        )
    return found


@pytest.fixture(scope="module")
def stations(tmp_path_factory: pytest.TempPathFactory) -> Tuple[Any, List[Dict[str, Any]]]:
    rows = _stations()
    return _profile(tmp_path_factory.mktemp("stations"), _contract(), rows), rows


# -- the shape of a history ----------------------------------------------------------------------


def test_a_table_with_a_validity_window_publishes_a_history(stations):
    result, _ = stations
    published = _history_of(result)
    assert published["entity"] == ["station_id"]
    assert published["start"] == "valid_from" and published["end"] == "valid_to"
    assert set(published) == {
        "entity",
        "start",
        "end",
        "versionsPerEntity",
        "firstStart",
        "versionLength",
        "chain",
        "lastVersion",
        "columns",
        "reasons",
    }
    assert list(published["columns"]) == ["status", "sensor_model"]
    for member in ("versionsPerEntity", "firstStart", "versionLength", "chain", "lastVersion"):
        assert published[member]["disclosure"] == RULE.name
        assert "noise" not in published[member] and "per" not in published[member]
        assert "groupBy" not in published[member] and "groupByLink" not in published[member]


def test_the_document_names_format_1_3_0_and_the_engine_reads_it(stations):
    result, _ = stations
    assert result.document["_schemaURL"] == document.SCHEMA_URL_1_3_0
    loaded, _ = load_profile_text(result.canonical.decode("utf-8"))
    history_entry = loaded.tables[TABLE].history
    assert history_entry is not None
    assert history_entry.entity == ("station_id",)
    assert set(history_entry.columns) == {"status", "sensor_model"}
    assert history_entry.reasons is not None


def test_a_table_without_a_window_has_no_history(tmp_path):
    rows = _stations()
    result = _profile(tmp_path, _contract(window=False), rows)
    assert "history" not in result.document["tables"][TABLE]
    assert not any("history" in what for what, _ in result.not_measured)


def test_versions_per_entity_counts_the_entities_by_their_versions(stations):
    result, rows = stations
    want = _expected(rows)
    entry = _history_of(result)["versionsPerEntity"]
    assert entry["definition"] == "marginalHistogram/1"
    assert entry["bins"] == ["1", "2", "3", "5", "10", "20", str(disclosure.MAX_COUNT)]
    cells = [
        sum(n for v, n in want["per_entity"].items() if lo <= v < hi)
        for lo, hi in zip((1, 2, 3, 5, 10, 20), (2, 3, 5, 10, 20, 10**9), strict=True)
    ]
    assert _bins(entry) == [_rounded(c) for c in cells]
    assert _group(entry)["k"] == _rounded(want["entities"])
    assert entry["sampleSize"] == disclosure.sample_size(want["entities"], RULE)


def test_first_starts_are_withheld_whole_on_the_year_ladder(stations):
    # Where each end is the next start, the end column's counts are the start column's less the
    # first starts, so published first starts would give a withheld cell of either column back.
    result, _ = stations
    entry = _history_of(result)["firstStart"]
    assert [g["status"] for g in entry["groups"]] == ["C"]
    assert entry["sampleSize"] == {"min": 0}
    edges = [int(each) for each in entry["bins"]]
    assert edges[0] < _ms("1901-01-01") and edges[-1] > _ms("2100-12-31")


def test_version_length_counts_the_days_to_the_next_start_on_the_ladder_of_days(stations):
    from fluid_build.profiler import dates

    result, rows = stations
    want = _expected(rows)
    entry = _history_of(result)["versionLength"]
    edges = [int(each) for each in entry["bins"]]
    assert edges[0] == 2 and edges[-1] == 731
    assert set(edges) <= set(dates.DAY_LADDER)
    cells = Counter()
    for days, n in want["lengths"].items():
        cells[max(i for i, e in enumerate(edges[:-1]) if e <= days)] += n
    assert _bins(entry) == [_rounded(cells[i]) for i in range(len(edges) - 1)]
    assert _group(entry)["k"] == _rounded(want["versions"] - want["entities"])


def test_a_chain_of_next_starts_publishes_the_declared_zeros_as_zero(stations):
    result, rows = stations
    want = _expected(rows)
    entry = _history_of(result)["chain"]
    assert entry["levels"] == ["dayBefore", "gap", "nextStart", "overlap"]
    assert _levels(entry) == {
        "dayBefore": 0,
        "gap": 0,
        "nextStart": _rounded(want["chain"]["nextStart"]),
        "overlap": 0,
    }
    group = _group(entry)
    assert "cellStatus" not in group or set(group["cellStatus"]) == {"F"}
    assert group["k"] == _rounded(want["versions"] - want["entities"])


def test_the_last_version_counts_the_entities_by_how_their_last_end_reads(stations):
    result, rows = stations
    want = _expected(rows)
    entry = _history_of(result)["lastVersion"]
    assert entry["levels"] == ["closed", "open"]
    assert _levels(entry) == {
        "closed": _rounded(want["last"]["closed"]),
        "open": _rounded(want["last"]["open"]),
    }


# -- the chain ----------------------------------------------------------------------------------


def test_a_chain_of_none_checks_the_gaps_and_empties_next_start_and_overlap(tmp_path):
    rows = _stations("none")
    result = _profile(tmp_path, _contract(chain="none"), rows)
    want = _expected(rows)
    assert want["chain"]["gap"] > 10 and want["chain"]["dayBefore"] > 10
    levels = _levels(_history_of(result)["chain"])
    assert levels == {
        "dayBefore": _rounded(want["chain"]["dayBefore"]),
        "gap": _rounded(want["chain"]["gap"]),
        "nextStart": 0,
        "overlap": 0,
    }
    group = _group(_history_of(result)["chain"])
    assert set(group.get("cellStatus", "F")) == {"F"}


def test_a_chain_of_day_before_empties_the_other_levels(tmp_path):
    rows = _stations("dayBefore")
    result = _profile(tmp_path, _contract(chain="dayBefore"), rows)
    want = _expected(rows)
    assert want["chain"]["dayBefore"] == want["versions"] - want["entities"]
    assert _levels(_history_of(result)["chain"]) == {
        "dayBefore": _rounded(want["chain"]["dayBefore"]),
        "gap": 0,
        "nextStart": 0,
        "overlap": 0,
    }


def test_an_open_version_that_is_not_the_last_is_an_overlap(tmp_path):
    rows = _stations()
    opened = 0
    for row in rows:
        if row["station_id"] >= 210 and row["station_id"] % 2 == 0 and opened < 14:
            if row["valid_to"] is not None and row["valid_from"] == _first_start(
                rows, row["station_id"]
            ):
                row["valid_to"] = None
                opened += 1
    assert opened == 14
    result = _profile(tmp_path, _contract(), rows)
    want = _expected(rows)
    assert want["chain"]["overlap"] == 14
    # A level the contract empties that holds records is checked like any cell.
    assert _levels(_history_of(result)["chain"]) == {
        "dayBefore": 0,
        "gap": 0,
        "nextStart": _rounded(want["chain"]["nextStart"]),
        "overlap": _rounded(14),
    }


def _first_start(rows: Sequence[Dict[str, Any]], station: int) -> str:
    return min(row["valid_from"] for row in rows if row["station_id"] == station)


def test_a_declared_empty_level_that_holds_a_few_records_is_checked_like_any_cell(tmp_path):
    rows = _stations()
    changed = 0
    for row in rows:
        if row["station_id"] >= 210 and row["valid_from"] == _first_start(rows, row["station_id"]):
            if row["valid_to"] is not None and changed < 4:
                row["valid_to"] = None
                changed += 1
    result = _profile(tmp_path, _contract(), rows)
    # Four overlaps are small; the cells the contract empties are out of the check, so the one
    # cell it publishes would give the small one back, and the group is withheld whole.
    assert [g["status"] for g in _history_of(result)["chain"]["groups"]] == ["C"]


def test_an_open_end_literal_reads_as_open(tmp_path):
    rows = _stations(open_end="9999-12-31")
    result = _profile(tmp_path, _contract(open_end="9999-12-31"), rows)
    want = _expected(rows, "9999-12-31")
    assert want["last"]["open"] > 10
    assert _levels(_history_of(result)["lastVersion"]) == {
        "closed": _rounded(want["last"]["closed"]),
        "open": _rounded(want["last"]["open"]),
    }
    assert _levels(_history_of(result)["chain"])["overlap"] == 0


def test_a_timestamp_window_is_a_history_too(tmp_path):
    rows = _stations(stamped=True)
    result = _profile(tmp_path, _contract(stamped=True), rows)
    want = _expected(rows)
    published = _history_of(result)
    assert _levels(published["chain"])["nextStart"] == _rounded(want["chain"]["nextStart"])
    assert _levels(published["lastVersion"])["closed"] == _rounded(want["last"]["closed"])
    assert [g["status"] for g in published["firstStart"]["groups"]] == ["C"]
    load_profile_text(result.canonical.decode("utf-8"))


# -- the tracked columns -------------------------------------------------------------------------


def test_initial_counts_the_value_of_each_first_version(stations):
    result, rows = stations
    want = _expected(rows)
    for column in ("status", "sensor_model"):
        entry = _history_of(result)["columns"][column]["initial"]
        counts = want["initial"][column]
        assert _levels(entry) == {level: _rounded(n) for level, n in sorted(counts.items())}


def test_transitions_count_each_change_by_its_current_value_with_a_zero_diagonal(stations):
    result, rows = stations
    want = _expected(rows)
    for column, levels in (("status", STATUS), ("sensor_model", MODELS)):
        entry = _history_of(result)["columns"][column]["transitions"]
        assert entry["groupBy"] == column
        assert entry["levels"] == sorted(levels)
        found = _moves(entry)
        assert sorted(found) == sorted(levels)
        for current in levels:
            for following in levels:
                expected = (
                    0
                    if current == following
                    else _rounded(want["moves"][column][current][following])
                )
                assert found[current][following] == expected, (column, current, following)
        for group in entry["groups"]:
            assert "cellStatus" not in group or set(group["cellStatus"]) == {"F"}


def test_reasons_count_the_changes_by_the_column_that_moves(stations):
    result, rows = stations
    want = _expected(rows)
    entry = _history_of(result)["reasons"]
    assert entry["levels"] == ["sensor_model", "several", "status"]
    assert _levels(entry) == {name: _rounded(want["reasons"][name]) for name in entry["levels"]}
    assert min(want["reasons"].values()) >= 10


def test_reasons_are_withheld_whole_when_a_transitions_group_is_withheld(tmp_path):
    rows = _stations()
    for row in rows:
        if row["status"] == "retired":
            row["status"] = "active"
    result = _profile(tmp_path, _contract(), rows)
    published = _history_of(result)
    assert {g["status"] for g in published["columns"]["status"]["transitions"]["groups"]} == {"C"}
    assert [g["status"] for g in published["reasons"]["groups"]] == ["C"]
    assert published["reasons"]["sampleSize"] == {"min": 0}
    assert result.withheld.get("D", 0) >= 1
    load_profile_text(result.canonical.decode("utf-8"))


def test_reasons_are_withheld_whole_when_a_tracked_column_holds_a_null(tmp_path):
    rows = _stations()
    rows[len(rows) // 2]["sensor_model"] = None
    result = _profile(tmp_path, _contract(), rows)
    published = _history_of(result)
    assert [g["status"] for g in published["reasons"]["groups"]] == ["C"]
    # The other members keep what they publish.
    assert _group(published["chain"])
    assert _group(published["columns"]["status"]["initial"])


def test_changes_from_a_null_are_a_group_that_is_never_written(tmp_path):
    rows = _stations()
    for station in range(260, 274):  # one version of each of fourteen stations with ten
        held = [row for row in rows if row["station_id"] == station]
        held[4]["sensor_model"] = None
    result = _profile(tmp_path, _contract(), rows)
    entry = _history_of(result)["columns"]["sensor_model"]["transitions"]
    assert sorted(g["group"] for g in entry["groups"]) == ["m1", "m2"]
    assert all(g["status"] == "F" for g in entry["groups"])


def test_a_few_changes_from_a_null_withhold_every_group_beside_them(tmp_path):
    rows = _stations()
    rows[len(rows) // 2]["sensor_model"] = None
    result = _profile(tmp_path, _contract(), rows)
    entry = _history_of(result)["columns"]["sensor_model"]["transitions"]
    assert [g["status"] for g in entry["groups"]] == ["C", "C"]


def test_a_column_without_declared_levels_is_named_and_not_tracked(tmp_path):
    contract = _contract()
    contract["exposes"][0]["contract"]["schema"][3]["type"] = "string"
    result = _profile(tmp_path, contract, _stations())
    published = _history_of(result)
    assert list(published["columns"]) == ["sensor_model"]
    assert "reasons" not in published  # one tracked column states no reasons
    said = dict(result.not_measured)
    assert f"history of {TABLE}.status" in said


# -- what is not written -------------------------------------------------------------------------


def test_a_table_whose_entity_is_a_counted_foreign_key_forms_no_history(tmp_path):
    # Its relationship's children per parent are the entities by their versions with one more bin
    # (the parents with none): the history's entities beside those parents would give it back.
    rows = _stations()
    contract = _contract()
    (expose,) = contract["exposes"]
    expose["semantics"]["relationships"] = [
        {
            "name": "station",
            "to": "station",
            "fromColumns": ["station_id"],
            "toColumns": ["station_id"],
            "cardinality": "many_to_one",
        }
    ]
    contract["exposes"].append(
        {
            "exposeId": "station",
            "kind": "table",
            "contract": {"schema": [{"name": "station_id", "type": "integer"}]},
        }
    )
    ids = sorted({row["station_id"] for row in rows}) + [9001, 9002, 9003]
    result = profile_exposes(
        contract,
        [
            ExposeSource(TABLE, _write(tmp_path / "stations.ndjson", rows)),
            ExposeSource(
                "station", _write(tmp_path / "s.ndjson", [{"station_id": i} for i in ids])
            ),
        ],
        backend=load_backend(),
    )
    assert "history" not in result.document["tables"][TABLE]
    said = dict(result.not_measured)
    assert "foreign key of a relationship" in said[f"history of {TABLE}"]


def test_a_request_profile_writes_no_history(tmp_path):
    asked = Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(RequestTable(TABLE, True, (RequestColumn("status", "category"),)),),
    )
    result = _profile(tmp_path, _contract(), _stations(), request=asked)
    assert "history" not in result.document["tables"][TABLE]
    assert not any("history" in what for what, _ in result.not_measured)


def test_a_default_profile_leaves_the_window_pair_to_the_history(stations):
    result, _ = stations
    pairs = result.document["tables"][TABLE].get("datePairs", [])
    assert not any({pair["start"], pair["end"]} == {"valid_from", "valid_to"} for pair in pairs)
    said = dict(result.not_measured)
    what = f"date pair valid_from to valid_to of {TABLE}"
    assert what in said
    assert "validity window" in said[what] and "history" in said[what]


def test_a_request_that_asks_the_window_pair_gets_it(tmp_path):
    asked = Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(
            RequestTable(
                TABLE,
                True,
                (RequestColumn("valid_from", "timestamp"), RequestColumn("valid_to", "timestamp")),
                date_pairs=(RequestDatePair("valid_from", "valid_to"),),
            ),
        ),
    )
    result = _profile(tmp_path, _contract(), _stations(), request=asked)
    pairs = result.document["tables"][TABLE].get("datePairs", [])
    assert [(pair["start"], pair["end"]) for pair in pairs] == [("valid_from", "valid_to")]


def test_a_window_that_is_not_one_of_dates_names_why_and_writes_none(tmp_path):
    rows = _stations()
    for row in rows:
        if row["valid_to"] is not None:
            row["valid_to"] += "T00:00:00Z"
    result = _profile(
        tmp_path, _contract(types={"valid_from": "date", "valid_to": "timestamp"}), rows
    )
    assert "history" not in result.document["tables"][TABLE]
    said = dict(result.not_measured)
    assert f"history of {TABLE}" in said
    assert "one type" in said[f"history of {TABLE}"]


def test_a_value_that_is_not_a_whole_day_names_why_and_writes_none(tmp_path):
    rows = _stations()
    rows[3]["valid_from"] = "2022-01-04T10:30:00Z"
    result = _profile(tmp_path, _contract(), rows)
    assert "history" not in result.document["tables"][TABLE]
    assert "not a whole day" in dict(result.not_measured)[f"history of {TABLE}"]


def test_no_message_holds_a_count(tmp_path):
    rows = _stations()
    rows[3]["valid_from"] = "2022-01-04T10:30:00Z"
    result = _profile(tmp_path, _contract(), rows)
    for what, reason in result.not_measured:
        assert not any(ch.isdigit() for ch in what + reason), (what, reason)


# -- first starts outside a span -----------------------------------------------------------------


def test_first_starts_with_no_span_are_written_on_the_year_ladder_withheld_whole(tmp_path):
    rows = _stations()
    stray = [row for row in rows if row["station_id"] >= 325]
    for row in stray:
        shift = (date(2025, 6, 1) - date(2022, 1, 1)).days
        for key in ("valid_from", "valid_to"):
            if row[key] is not None:
                row[key] = (date.fromisoformat(row[key]) + timedelta(days=shift)).isoformat()
    result = _profile(tmp_path, _contract(), rows)
    entry = _history_of(result)["firstStart"]
    assert [g["status"] for g in entry["groups"]] == ["C"]
    assert entry["sampleSize"] == {"min": 0}
    edges = [int(each) for each in entry["bins"]]
    assert edges[0] < _ms("1901-01-01") and edges[-1] > _ms("2100-12-31")
    assert _ms("2022-01-01") in edges
    # The rest of the history is not touched by it.
    assert _group(_history_of(result)["chain"])
    load_profile_text(result.canonical.decode("utf-8"))


def test_version_lengths_with_no_span_are_written_on_the_ladder_withheld_whole(tmp_path):
    from fluid_build.profiler import dates

    # Eighty stations with one version and five with two: too few versions have a next one for
    # the first or last bin of their lengths to hold the least cell.
    rows = [row for row in _stations() if row["station_id"] < 85]
    result = _profile(tmp_path, _contract(), rows)
    entry = _history_of(result)["versionLength"]
    assert [g["status"] for g in entry["groups"]] == ["C"]
    assert entry["sampleSize"] == {"min": 0}
    assert [int(each) for each in entry["bins"]] == list(dates.DAY_LADDER[8:])
    assert result.withheld.get("D", 0) >= 1
    load_profile_text(result.canonical.decode("utf-8"))


def test_a_table_a_bundle_answers_is_left_alone(tmp_path):
    from fluid_build.profiler import runner, sources

    contract = _contract()
    path = _write(tmp_path / "stations.ndjson", _stations())
    plan = runner.plan_expose(contract, TABLE)
    read = sources.read_columns(path, [each.name for each in plan.columns], table=None, cwd=None)
    tables: Dict[str, Dict[str, Any]] = {TABLE: {"columns": {}}}
    stage = history.HistoryStage(
        {TABLE: contract["exposes"][0]},
        [plan],
        [read],
        [None],
        {TABLE: object()},
        tables,
        load_backend(),
        RULE,
        (Counter(), Counter()),
        16,
        None,
    )
    assert history.form_histories(stage) == []
    assert "history" not in tables[TABLE]


# -- the audit -----------------------------------------------------------------------------------


def test_the_audit_withholds_a_cell_the_entities_and_the_versions_would_give_back(
    tmp_path, monkeypatch
):
    # No station has ten versions: the two empty top cells are withheld, and the versions held
    # beside the entities show that no entity can hold as many as the cell of 3 to 4 might.
    counts = {1: 80, 2: 70, 3: 60, 5: 50}
    rows = _stations(entities_by_versions=counts)
    audited = _group(_history_of(_profile(tmp_path, _contract(), rows))["versionsPerEntity"])
    assert audited["cellStatus"] == ["F", "F", "C", "C", "C", "C"]
    assert audited["binCounts"] == [80, 70, None, None, None, None]
    monkeypatch.setattr(history, "_audited", lambda decision, group, versions, rule: decision)
    plain = _group(_history_of(_profile(tmp_path, _contract(), rows))["versionsPerEntity"])
    assert plain["cellStatus"] == ["F", "F", "F", "C", "C", "C"]


def test_a_versions_audit_that_pins_nothing_withholds_nothing_more(tmp_path):
    result = _profile(tmp_path, _contract(), _stations())
    group = _group(_history_of(result)["versionsPerEntity"])
    assert "cellStatus" not in group or set(group["cellStatus"]) == {"F"}


def test_the_children_audit_reads_a_table_whose_first_bin_starts_at_one():
    # Every entity holds a version, so the first lower edge is 1 and none is childless. With
    # too few versions for any entity to hold twenty, the withheld top cell is pinned at zero;
    # with enough, the cell can hold entities and nothing is pinned.
    lower = (1, 2, 3, 5, 10, 20)
    cells = [20, 50, 12, 15, 10, 0]
    shown = [True, False, True, True, True, False]
    assert disclosure.pinned_children_cells(lower, cells, shown, 332, RULE, 0) == {5}
    assert disclosure.pinned_children_cells(lower, cells, shown, 360, RULE, 0) == frozenset()


# -- the format, the report, a private release ---------------------------------------------------


def test_a_history_is_a_form_of_format_1_3_0_and_a_table_without_one_keeps_its_format():
    plain = {"t": {"columns": {}}}
    assert document.schema_url_for(plain) == document.SCHEMA_URL
    assert document.schema_url_for({"t": {"columns": {}, "history": {}}}) == (
        document.SCHEMA_URL_1_3_0
    )


def test_the_report_counts_the_members_published_and_names_no_count_of_records(stations):
    from fluid_build.cli import profile_cmd

    result, rows = stations
    lines: List[str] = []
    profile_cmd._report_structure(lambda text, **_: lines.append(text), result)
    history_lines = [line for line in lines if "History:" in line]
    assert history_lines == [f"exposes[{TABLE}]: History: 9 of 10 members published."]
    assert not any(str(len(rows)) in line for line in lines)


def test_the_report_names_a_member_published_in_part():
    from fluid_build.cli import profile_cmd

    def member(*groups: Dict[str, Any]) -> Dict[str, Any]:
        return {"groups": list(groups)}

    whole = member({"status": "F", "cellStatus": ["F", "F"]})
    part = member({"status": "F", "cellStatus": ["F", "C"]})
    withheld = member({"status": "C"})
    published = {
        "versionsPerEntity": whole,
        "firstStart": whole,
        "versionLength": part,
        "chain": whole,
        "lastVersion": withheld,
        "columns": {
            "status": {
                "initial": whole,
                "transitions": member(whole["groups"][0], part["groups"][0]),
            }
        },
    }
    assert profile_cmd._history_line(published) == "4 of 7 members published whole, 2 in part."
    assert profile_cmd._history_line({**published, "versionLength": whole, "columns": {}}) == (
        "4 of 5 members published."
    )


def test_a_private_release_writes_no_history(tmp_path, monkeypatch, capsys):
    pytest.importorskip("flux_stats.noise")
    from fluid_build.cli import main
    from tests.profiler._fake_backend import FakeBackend
    from tests.profiler.dp import _release_data as data
    from tests.profiler.dp import _release_run as run

    monkeypatch.chdir(tmp_path)
    for name in ("orders.fluid.yaml", "orders.profile-request.yml"):
        shutil.copy(data.FIXTURES / name, tmp_path / name)
    data.write_orders(tmp_path / "orders.csv", data.orders())
    backend = FakeBackend()
    monkeypatch.setattr(_stats, "load_backend", lambda: backend)
    run.stub_selection(monkeypatch)

    def refuse(*_: Any, **__: Any) -> None:
        raise AssertionError("a private release forms no history")

    monkeypatch.setattr(history, "form_histories", refuse)
    argv = [
        "profile",
        "orders.fluid.yaml",
        "--request",
        "orders.profile-request.yml",
        "--source",
        "orders=orders.csv",
        "--privacy-unit",
        "orders",
        "--unit-key",
        "customer_id",
        "--max-rows-per-unit",
        "3",
        "--rho",
        "1",
        "--release-record",
        "rec.json",
    ]
    assert main(argv) == 0, capsys.readouterr()
    released = json.loads((tmp_path / "orders.profile.json").read_text(encoding="utf-8"))
    assert all("history" not in table for table in released["tables"].values())
