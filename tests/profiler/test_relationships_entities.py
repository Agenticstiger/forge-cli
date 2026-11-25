# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Children per entity: a relationship into a table with a validity window, counted per entity.

A table that declares a validity window holds a row per version of each natural entity. A
relationship reaches an entity when its ``toColumns`` are exactly that entity's key
(RFC-versioned-relationships section 2), and its children per parent are counted one parent per
entity, never one per version.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

import pytest

from fluid_build.profiler import request as asked_for
from fluid_build.profiler import runner, sources
from fluid_build.profiler._stats import (
    Request,
    RequestColumn,
    RequestDateLink,
    RequestEnd,
    RequestExposure,
    RequestLinked,
    RequestRelationship,
    RequestTable,
    load_backend,
)
from fluid_build.profiler.runner import CHILDREN_EDGES, ExposeSource, ProfileError, profile_exposes

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.profile")

STATIONS = 90
LINK = "reading(station_id) to station_version"


def _contract(*, stable: Sequence[str] = ("kind",), relationship: Optional[Dict[str, Any]] = None):
    """Stations kept as versions, and the readings each station takes."""
    version = {
        "exposeId": "station_version",
        "kind": "table",
        "contract": {
            "schema": [
                {"name": "station_id", "type": "int"},
                {"name": "valid_from", "type": "date"},
                {"name": "valid_to", "type": "date"},
                {"name": "kind", "type": "enum('fixed','mobile')"},
                {"name": "status", "type": "enum('active','paused')"},
            ],
            "invariants": [
                {
                    "id": f"{name}_per_station",
                    "dependency": {"from": ["station_id"], "to": [name]},
                }
                for name in stable
            ],
        },
        "semantics": {
            "entities": [{"name": "station", "type": "natural", "expr": "station_id"}],
            "dimensions": [
                {
                    "name": "valid_from",
                    "type": "time",
                    "typeParams": {"validityParams": {"isStart": True}},
                },
                {
                    "name": "valid_to",
                    "type": "time",
                    "typeParams": {"validityParams": {"isEnd": True}},
                },
            ],
        },
    }
    reading = {
        "exposeId": "reading",
        "kind": "table",
        "contract": {
            "schema": [
                {"name": "reading_id", "type": "int"},
                {"name": "station_id", "type": "int"},
                {"name": "flag", "type": "enum('ok','odd')"},
                {"name": "taken", "type": "date"},
            ]
        },
        "semantics": {
            "relationships": [
                relationship
                or {
                    "name": "station",
                    "to": "station_version",
                    "fromColumns": ["station_id"],
                    "toColumns": ["station_id"],
                    "cardinality": "many_to_one",
                }
            ]
        },
    }
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "weather",
        "exposes": [version, reading],
    }


def _versions(stations: int = STATIONS) -> List[Dict[str, Any]]:
    """Station n holds two versions when n is odd and one when it is even: 135 rows, 90 stations."""
    rows = []
    for n in range(stations):
        for number in range(2 if n % 2 else 1):
            rows.append(
                {
                    "station_id": n,
                    "valid_from": f"2024-0{1 + number}-01",
                    "valid_to": f"2024-0{2 + number}-01" if number == 0 and n % 2 else None,
                    "kind": ("fixed", "mobile")[n % 2],
                    "status": ("active", "paused")[number],
                }
            )
    return rows


def _readings(per_station: Sequence[int]) -> List[Dict[str, Any]]:
    rows = []
    for station, count in enumerate(per_station):
        for _ in range(count):
            rows.append(
                {
                    "reading_id": len(rows),
                    "station_id": station,
                    "flag": "ok",
                    "taken": f"2024-03-{1 + len(rows) % 28:02d}",
                }
            )
    return rows


#: 20 stations with no reading, 30 with one, 20 with two, 10 with four and 10 with 25.
PER_STATION = [0] * 20 + [1] * 30 + [2] * 20 + [4] * 10 + [25] * 10


def _write(path, rows) -> str:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return str(path)


def _sources(tmp_path, versions=None, readings=None):
    return [
        ExposeSource(
            "station_version",
            _write(tmp_path / "station_version.ndjson", versions or _versions()),
        ),
        ExposeSource(
            "reading", _write(tmp_path / "reading.ndjson", readings or _readings(PER_STATION))
        ),
    ]


def _profile(tmp_path, contract=None, *, request=None, strict=False, versions=None, readings=None):
    return profile_exposes(
        contract or _contract(),
        _sources(tmp_path, versions, readings),
        backend=load_backend(),
        request=request,
        strict=strict,
    )


def _entry(result, child="reading"):
    (found,) = [
        each for each in result.document["relationships"] if each["child"]["table"] == child
    ]
    return found


def _request(*, group_by=(), linked=(), dates=(), exposure=()) -> Request:
    return Request(
        contract_digest=None,
        fractional_bits=16,
        tables=(
            RequestTable(
                "station_version",
                False,
                (
                    RequestColumn("station_id", "number"),
                    RequestColumn("valid_from", "timestamp"),
                    RequestColumn("kind", "category", (), ("levelHistogram/1",)),
                    RequestColumn("status", "category", (), ("levelHistogram/1",)),
                ),
            ),
            RequestTable(
                "reading",
                False,
                (
                    RequestColumn("station_id", "number"),
                    RequestColumn("taken", "timestamp"),
                    RequestColumn("flag", "category", (), ("levelHistogram/1",)),
                ),
            ),
        ),
        relationships=(
            RequestRelationship(
                RequestEnd("station_version", ("station_id",)),
                RequestEnd("reading", ("station_id",)),
                ("marginalHistogram/1",),
                group_by=tuple(group_by),
                linked=tuple(linked),
                dates=tuple(dates),
                exposure=tuple(exposure),
            ),
        ),
    )


def asking_everything() -> Request:
    """A request that asks each kind of thing of the relationship, the unformable ones too."""
    return _request(
        group_by=("kind", "status"),
        linked=(RequestLinked("flag", "kind"), RequestLinked("flag", "status")),
        dates=(RequestDateLink("valid_from", "taken"),),
        exposure=(RequestExposure("valid_from"),),
    )


# -- counted one parent per entity ---------------------------------------------------------------


def test_children_are_counted_per_entity_and_not_per_version(tmp_path):
    result = _profile(tmp_path)
    assert not result.relationships_left_out
    (statistic,) = _entry(result)["statistics"]
    assert statistic["bins"] == [str(edge) for edge in CHILDREN_EDGES]
    (group,) = statistic["groups"]
    # 90 stations, never the 135 versions.
    assert group["k"] == STATIONS
    assert group["binCounts"][:3] == [20, 30, 20]
    (counted,) = result.relationships
    assert counted.relationship.name == "station" and counted.cells == 7


def test_the_audit_is_given_the_entities_and_their_children(tmp_path, monkeypatch):
    seen: List[Any] = []
    audit = runner._audited_children

    def spy(decision, group, children, childless, rule):
        seen.append((group.cells, children, childless))
        return audit(decision, group, children, childless, rule)

    monkeypatch.setattr(runner, "_audited_children", spy)
    _profile(tmp_path)
    assert seen == [((20, 30, 20, 10, 0, 0, 10), 20 * 0 + 30 + 40 + 40 + 250, 0)]


def test_a_station_whose_key_is_null_is_no_parent_and_a_child_of_none_is_no_child(tmp_path):
    versions = _versions() + [
        {"station_id": None, "valid_from": "2024-01-01", "valid_to": None, "kind": "fixed",
         "status": "active"}
    ]  # fmt: skip
    readings = _readings(PER_STATION) + [
        {"reading_id": 900, "station_id": None, "flag": "ok", "taken": "2024-03-01"},
        {"reading_id": 901, "station_id": 999, "flag": "ok", "taken": "2024-03-01"},
    ]
    result = _profile(tmp_path, versions=versions, readings=readings)
    (group,) = _entry(result)["statistics"][0]["groups"]
    assert group["k"] == STATIONS  # the row with no key is no station


def test_the_childless_class_is_read_per_entity(tmp_path):
    # Every station is declared to hold a reading, and one row of the table has no key: it names
    # no entity, so it is no station that lacks a reading.
    contract = _contract()
    rules = contract["exposes"][0]["contract"]["invariants"]
    rules.append(
        {
            "id": "count",
            "derive": "reading_count",
            "aggregate": {"function": "count", "from": "reading", "via": "station"},
        }
    )
    rules.append({"id": "has", "check": "reading_count >= 1"})
    versions = _versions() + [
        {"station_id": None, "valid_from": "2024-01-01", "valid_to": None, "kind": "fixed",
         "status": "active"}
    ]  # fmt: skip
    every = [1] * 30 + [2] * 30 + [4] * 20 + [25] * 10
    result = _profile(tmp_path, contract, versions=versions, readings=_readings(every))
    assert _entry(result)["structural"] == ["childless"]
    # A station that holds none breaks it, whatever the versions.
    result = _profile(tmp_path, contract, readings=_readings([0] + every[1:]))
    assert "structural" not in _entry(result)


# -- a relationship that does not reach an entity stays left out ----------------------------------


def test_a_relationship_into_a_window_by_another_column_is_left_out(tmp_path):
    other = {
        "name": "by_kind",
        "to": "station_version",
        "fromColumns": ["flag"],
        "toColumns": ["kind"],
        "cardinality": "many_to_one",
    }
    result = _profile(tmp_path, _contract(relationship=other))
    assert "relationships" not in result.document
    ((child, name, reason),) = result.relationships_left_out
    assert (child, name) == ("reading", "by_kind")
    assert "its target declares a validity window" in reason
    assert "toColumns are not the target's natural entity" in reason
    assert "a parent has a row per version" in reason


def test_children_per_entity_are_left_out_when_the_entity_is_a_counted_foreign_key(tmp_path):
    # The entities children per entity count are then the distinct keys the profile withholds:
    # beside the counted relationship's parents they would give the parents with no version back.
    contract = _contract()
    version = contract["exposes"][0]
    version["semantics"]["relationships"] = [
        {
            "name": "site",
            "to": "site",
            "fromColumns": ["station_id"],
            "toColumns": ["station_id"],
            "cardinality": "many_to_one",
        }
    ]
    contract["exposes"].append(
        {
            "exposeId": "site",
            "kind": "table",
            "contract": {"schema": [{"name": "station_id", "type": "int"}]},
        }
    )
    sites = [{"station_id": n} for n in range(STATIONS + 3)]
    result = profile_exposes(
        contract,
        [*_sources(tmp_path), ExposeSource("site", _write(tmp_path / "site.ndjson", sites))],
        backend=load_backend(),
    )
    counted = {each["child"]["table"] for each in result.document["relationships"]}
    assert counted == {"station_version"}
    said = {(child, name): reason for child, name, reason in result.relationships_left_out}
    assert "foreign key of a relationship this profile counts" in said[("reading", "station")]


def test_a_counted_key_holding_the_entity_among_more_columns_leaves_them_out(tmp_path):
    # A composite key that holds the entity counts at least as many tuples as there are entities,
    # so beside its parents the entities still give the parents with no version back.
    contract = _contract()
    version = contract["exposes"][0]
    version["semantics"]["relationships"] = [
        {
            "name": "site",
            "to": "site",
            "fromColumns": ["station_id", "kind"],
            "toColumns": ["station_id", "kind"],
            "cardinality": "many_to_one",
        }
    ]
    contract["exposes"].append(
        {
            "exposeId": "site",
            "kind": "table",
            "contract": {
                "schema": [
                    {"name": "station_id", "type": "int"},
                    {"name": "kind", "type": "enum('fixed','mobile')"},
                ]
            },
        }
    )
    sites = [
        {"station_id": n, "kind": row_kind}
        for n in range(STATIONS + 3)
        for row_kind in ("fixed", "mobile")
    ]
    result = profile_exposes(
        contract,
        [*_sources(tmp_path), ExposeSource("site", _write(tmp_path / "site.ndjson", sites))],
        backend=load_backend(),
    )
    said = {(child, name): reason for child, name, reason in result.relationships_left_out}
    assert "foreign key of a relationship this profile counts" in said[("reading", "station")]


def test_a_window_without_a_natural_entity_leaves_the_relationship_out(tmp_path):
    contract = _contract()
    contract["exposes"][0]["semantics"]["entities"] = []
    result = _profile(tmp_path, contract)
    assert "relationships" not in result.document
    assert "natural entity" in result.relationships_left_out[0][2]


# -- groupings through an entity -------------------------------------------------------------------


def _groups(result):
    (statistic,) = _entry(result)["statistics"]
    return statistic


def test_children_per_station_are_grouped_by_a_column_stable_per_station(tmp_path):
    many = [0] * 40 + [1] * 40 + [2] * 40 + [3] * 40 + [5] * 20 + [12] * 20 + [30] * 20
    result = _profile(
        tmp_path,
        request=_request(group_by=("kind",)),
        versions=_versions(len(many)),
        readings=_readings(many),
    )
    grouped = [each for each in _entry(result)["statistics"] if each.get("groupBy") == "kind"]
    (statistic,) = grouped
    by_key = {each["group"]: each for each in statistic["groups"]}
    # The even stations are fixed and the odd ones mobile: 110 of each, never their versions.
    assert set(by_key) == {"fixed", "mobile"}
    assert by_key["fixed"]["k"] == 110 and by_key["mobile"]["k"] == 110
    assert by_key["fixed"]["binCounts"] == [20, 20, 20, 20, 10, 10, 10]
    assert all("kind" not in what for what, _ in result.not_measured)


def test_a_column_the_versions_may_disagree_on_is_not_a_grouping(tmp_path):
    result = _profile(tmp_path, request=_request(group_by=("status",)))
    (entry,) = [each for each in result.document["relationships"]]
    assert all(each.get("groupBy") != "status" for each in entry["statistics"])
    named = dict(result.not_measured)
    what = f"children per parent of {LINK} grouped by station_version.status"
    assert "an entity's versions may disagree" in named[what]


def test_a_stable_column_is_one_the_contract_says_is_stable_per_entity(tmp_path):
    contract = _contract(stable=())
    result = _profile(tmp_path, contract, request=_request(group_by=("kind",)))
    what = f"children per parent of {LINK} grouped by station_version.kind"
    assert "an entity's versions may disagree" in dict(result.not_measured)[what]


def test_a_child_column_is_grouped_through_the_link_by_a_stable_parent_column(tmp_path):
    request = _request(linked=(RequestLinked("flag", "kind"),))
    result = _profile(tmp_path, request=request)
    linked = [
        each
        for table in result.document["tables"].values()
        for column in table["columns"].values()
        for each in column.get("statistics", ())
        if "groupByLink" in each
    ]
    assert linked and linked[0]["groupByLink"]["column"] == "kind"


def test_a_child_column_is_not_grouped_by_an_unstable_parent_column(tmp_path):
    request = _request(linked=(RequestLinked("flag", "status"),))
    result = _profile(tmp_path, request=request)
    what = "flag of reading grouped by station_version.status"
    assert "an entity's versions may disagree" in dict(result.not_measured)[what]


# -- dates and exposure are not formed through an entity ------------------------------------------


def test_dates_and_exposure_through_an_entity_are_named_not_formed(tmp_path):
    result = _profile(tmp_path)
    entry = _entry(result)
    assert "dates" not in entry and "exposure" not in entry
    assert any(
        what.startswith("dates and exposure of ") and "entity" in reason
        for what, reason in result.not_measured
    )


# -- --strict refuses what the contract rules out ---------------------------------------------------


def _no_read(monkeypatch):
    def refuse(*args: Any, **kwargs: Any):
        raise AssertionError("a record was read")

    monkeypatch.setattr(sources, "read_columns", refuse)


def test_strict_refuses_an_ask_of_a_relationship_that_stays_left_out(tmp_path, monkeypatch):
    other = {
        "name": "by_kind",
        "to": "station_version",
        "fromColumns": ["flag"],
        "toColumns": ["kind"],
        "cardinality": "many_to_one",
    }
    base = _request()
    request = Request(
        contract_digest=None,
        fractional_bits=16,
        tables=base.tables,
        relationships=(
            RequestRelationship(
                RequestEnd("station_version", ("kind",)),
                RequestEnd("reading", ("flag",)),
                ("marginalHistogram/1",),
            ),
        ),
    )
    _no_read(monkeypatch)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, _contract(relationship=other), request=request, strict=True)
    assert "natural entity" in json.dumps(refused.value.context)


def test_strict_refuses_a_grouping_on_a_column_the_versions_may_disagree_on(tmp_path, monkeypatch):
    request = _request(group_by=("status",), linked=(RequestLinked("flag", "status"),))
    _no_read(monkeypatch)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, request=request, strict=True)
    text = json.dumps(refused.value.context)
    assert "an entity's versions may disagree" in text
    assert "grouped by station_version.status" in text


def test_strict_refuses_dates_and_exposure_asked_through_an_entity(tmp_path, monkeypatch):
    request = _request(
        dates=(RequestDateLink("valid_from", "taken"),), exposure=(RequestExposure("valid_from"),)
    )
    _no_read(monkeypatch)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, request=request, strict=True)
    text = json.dumps(refused.value.context)
    assert f"dates station_version.valid_from to reading.taken of {LINK}" in text
    assert f"exposure station_version.valid_from of {LINK}" in text


def test_the_reasons_of_a_strict_refusal_are_the_ones_the_profile_names(tmp_path, monkeypatch):
    request = _request(group_by=("status",), linked=(RequestLinked("flag", "status"),))
    named = _profile(tmp_path, request=request).not_measured
    seen: List[Any] = []
    original = asked_for.refuse_asks

    def spy(found):
        seen.append(list(found))
        return original(found)

    monkeypatch.setattr(asked_for, "refuse_asks", spy)
    with pytest.raises(ProfileError):
        _profile(tmp_path, request=request, strict=True)
    assert seen and all(item in named for item in seen[0])


# -- the engine's reader ----------------------------------------------------------------------------


def test_the_engines_reader_accepts_the_document(tmp_path):
    from flux_stats.profile import load_profile_text

    result = _profile(tmp_path, request=_request(group_by=("kind",)))
    load_profile_text(result.canonical.decode("utf-8"))
