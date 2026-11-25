# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A private release of children per entity of a versioned table (``--privacy-unit`` with ``--follow``).

A table that declares a validity window holds a row per version of each natural entity, so its
parent key repeats by design. A relationship that reaches an entity of it counts children per
entity. The release counts one parent per entity, and says so only where an entity cannot span two
units: the unit does not reach the table, or follows it by its natural entity or by a column a
``dependency`` makes stable per entity.

``network`` is the unit. ``station_version`` is a history of stations (entity ``station_id``,
window ``valid_from`` to ``valid_to``, ``network_id`` stable per station), and a ``reading`` names
the station it was taken at.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest
import yaml

from fluid_build.cli import main
from fluid_build.profiler import _stats
from fluid_build.profiler.dp import follow_release as fr
from fluid_build.profiler.dp import release
from fluid_build.profiler.dp.follows import keep_rows, parse_follow, plan_follows
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.dp import _follow_data as fd
from tests.profiler.dp._follow_data import REQUEST_URL

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.noise")

TABLES = ("network", "station_version", "reading")
FOLLOW_VERSIONS = "network.network_id=station_version.network_id:4"
FOLLOW_READINGS = "station_version.station_id=reading.station_id:30"
FOLLOW_READINGS_OF_NETWORK = "network.network_id=reading.network_id:5"
LINK = "reading(station_id) to station_version(station_id)"

CONTRACT: Dict[str, Any] = yaml.safe_load("""
fluidVersion: "0.7.7"
kind: DataProduct
id: weather.stations
name: Weather
metadata:
  owner:
    team: weather-team
exposes:
  - exposeId: network
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/network.parquet }
    contract:
      schema:
        - { name: network_id, type: INT, required: true }
        - { name: region, type: "enum('north','south')", required: true }
    semantics:
      entities:
        - { name: network, type: primary, expr: network_id }
  - exposeId: station_version
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/station_version.parquet }
    contract:
      schema:
        - { name: station_id, type: INT, required: true }
        - { name: network_id, type: INT, required: true }
        - { name: kind, type: "enum('fixed','mobile')", required: true }
        - { name: valid_from, type: DATE, required: true }
        - { name: valid_to, type: DATE }
        - { name: status, type: "enum('active','paused')", required: true }
      invariants:
        - id: network_per_station
          dependency: { from: [station_id], to: [network_id] }
        - id: kind_per_station
          dependency: { from: [station_id], to: [kind] }
    semantics:
      entities:
        - { name: station, type: natural, expr: station_id }
      dimensions:
        - name: valid_from
          type: time
          typeParams: { validityParams: { isStart: true } }
        - name: valid_to
          type: time
          typeParams: { validityParams: { isEnd: true } }
      relationships:
        - name: in_network
          to: network
          fromColumns: [network_id]
          toColumns: [network_id]
          cardinality: many_to_one
  - exposeId: reading
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/reading.parquet }
    contract:
      schema:
        - { name: reading_id, type: INT, required: true }
        - { name: station_id, type: INT, required: true }
        - { name: network_id, type: INT, required: true }
        - { name: flag, type: "enum('ok','odd')", required: true }
    semantics:
      entities:
        - { name: reading, type: primary, expr: reading_id }
      relationships:
        - name: taken_at
          to: station_version
          fromColumns: [station_id]
          toColumns: [station_id]
          cardinality: many_to_one
        - name: of_network
          to: network
          fromColumns: [network_id]
          toColumns: [network_id]
          cardinality: many_to_one
""")


def _contract(*, stable: bool = True) -> Dict[str, Any]:
    found = copy.deepcopy(CONTRACT)
    if not stable:
        found["exposes"][1]["contract"]["invariants"] = []
    return found


def _request(
    *, group_by: Sequence[str] = (), linked: Sequence[Sequence[str]] = ()
) -> Dict[str, Any]:
    tables: Dict[str, Any] = {
        "network": {
            "columns": {
                "network_id": {"domain": "number"},
                "region": {"domain": "category", "statistics": ["levelHistogram/1"]},
            }
        },
        "station_version": {
            "columns": {
                "station_id": {"domain": "number"},
                "network_id": {"domain": "number"},
                "kind": {"domain": "category", "statistics": ["levelHistogram/1"]},
                "status": {"domain": "category", "statistics": ["levelHistogram/1"]},
            }
        },
        "reading": {
            "columns": {
                "reading_id": {"domain": "number"},
                "station_id": {"domain": "number"},
                "network_id": {"domain": "number"},
                "flag": {"domain": "category", "statistics": ["levelHistogram/1"]},
            }
        },
    }
    return {
        "_schemaURL": REQUEST_URL,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "tables": tables,
        "relationships": [
            {
                "parent": {"table": "station_version", "columns": ["station_id"]},
                "child": {"table": "reading", "columns": ["station_id"]},
                "statistics": ["marginalHistogram/1"],
                **({"groupBy": list(group_by)} if group_by else {}),
                **(
                    {"linked": [{"column": a, "parentColumn": b} for a, b in linked]}
                    if linked
                    else {}
                ),
            }
        ],
    }


def _rows(*, readings_of_one: int = 0, drift: bool = False) -> Dict[str, List[Dict[str, Any]]]:
    """12 networks of two stations; the odd stations hold two versions, the even one."""
    network, version, reading = [], [], []
    for n in range(1, 13):
        network.append({"network_id": n, "region": ("north", "south")[n % 2]})
    for station in range(1, 25):
        home = (station + 1) // 2
        for number in range(2 if station % 2 else 1):
            version.append(
                {
                    "station_id": station,
                    "network_id": home,
                    "kind": ("fixed", "mobile")[station % 2],
                    "valid_from": f"2024-0{1 + number}-01",
                    "valid_to": "2024-02-01" if number == 0 and station % 2 else "",
                    "status": ("active", "paused")[number],
                }
            )
        for _ in range(readings_of_one if station == 1 else station % 5):
            reading.append(
                {
                    "reading_id": len(reading) + 1,
                    "station_id": station,
                    "network_id": home,
                    "flag": ("ok", "odd")[len(reading) % 2],
                }
            )
    if drift:  # station 1's second version moves to another network
        version[1]["network_id"] = 2
    return {"network": network, "station_version": version, "reading": reading}


def _workspace(tmp_path, monkeypatch, *, stable=True, rows=None, request=None, contract=None):
    monkeypatch.chdir(tmp_path)
    text = yaml.safe_dump(contract or _contract(stable=stable), sort_keys=False)
    (tmp_path / "weather.fluid.yaml").write_text(text, encoding="utf-8")
    digest = FakeBackend().contract_digest(text, name="weather.fluid.yaml")
    document = {**(request or _request()), "contract": {"digest": digest}}
    (tmp_path / "weather.profile-request.yml").write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    fd.write_csvs(tmp_path, rows or _rows())
    return tmp_path


@pytest.fixture(autouse=True)
def _no_clique_choice(monkeypatch):
    """Choose no clique where OpenDP is not installed.

    Choosing cliques runs OpenDP (the ``[privacy]`` extra), which the unit lanes do not install.
    There the choice is fixed to none, as a release with no categorical pair would make it; the
    privacy lane, which installs the extra, runs these releases with the real choice.
    """
    if importlib.util.find_spec("opendp") is not None:
        return
    from fluid_build.profiler.dp import selection

    monkeypatch.setattr(selection, "select_cliques", lambda *args, **kwargs: ())


@pytest.fixture
def registry(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(_stats, "load_backend", lambda: backend)
    return backend


def _argv(*follows, unit="network", record="rec.json", rho="1", more=(), tables=TABLES):
    flags = [flag for text in follows for flag in ("--follow", text)]
    sources = [part for table in tables for part in ("--source", f"{table}={table}.csv")]
    return [
        "profile",
        "weather.fluid.yaml",
        "--request",
        "weather.profile-request.yml",
        *sources,
        "--privacy-unit",
        unit,
        *flags,
        "--rho",
        rho,
        "--release-record",
        record,
        *more,
    ]


def _out(capsys):
    seen = capsys.readouterr()
    return " ".join((seen.out + seen.err).split())


def _read(path):
    from flux_stats.profile import load_profile_text

    profile, body = load_profile_text(path.read_bytes().decode("utf-8"))
    return profile, body


def _entry(body, parent="station_version", child="reading"):
    (found,) = [
        each
        for each in body["relationships"]
        if each["parent"]["table"] == parent and each["child"]["table"] == child
    ]
    return found


def _plan(*follows, unit="network", contract=None, request=None):
    backend = FakeBackend()
    asked = backend.read_request(json.dumps(request or _request()), name="entities.request")
    return fr.plan_follow_release(
        contract or _contract(),
        asked,
        backend,
        unit=unit,
        rho=release.parse_rho("1"),
        follows=tuple(parse_follow(text) for text in follows),
    )


def _relation(plan):
    (found,) = [each for each in plan.relations if each.group is None]
    return found


def _strings(value: Any) -> List[str]:
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    return [value] if isinstance(value, str) else []


# -- followed by the natural entity or by a stable column ---------------------------------------------


def test_children_per_entity_are_released_though_the_parent_key_repeats(
    tmp_path, monkeypatch, registry, capsys
):
    _workspace(tmp_path, monkeypatch)
    assert main(_argv(FOLLOW_VERSIONS)) == 0
    out = _out(capsys)
    assert "profile_release_repeated_key" not in out
    profile, body = _read(tmp_path / "network.profile.json")
    (statistic,) = _entry(body)["statistics"]
    assert statistic["definition"] == "marginalHistogram/1"
    assert statistic["bins"][0] == "0" and statistic["bins"][-1] == str(10**9)  # the public ladder
    assert dict(profile.accounting.reached) == {"network": 1, "station_version": 4}


def test_children_per_entity_are_charged_by_the_reach_of_the_target(tmp_path, monkeypatch):
    # The target is reached and the readings only through this relationship: a unit holds at most
    # m_T rows of the target, so at most m_T entities, each counted in one cell: L1 = L2 = m_T.
    plan = _plan(FOLLOW_VERSIONS)
    relation = _relation(plan)
    assert plan.reach["station_version"] == 4 and plan.reach.get("reading", 0) == 0
    assert (relation.l1, relation.sensitivity) == (4, 16)
    share, tables = fr.describe(plan, {})
    part = next(each for each in tables if each.label == relation.label)
    assert part.sensitivity == 16
    assert part.scale_units == release.scale_units(share, 16)
    assert part.scale == release.decimal_text(Fraction(part.scale_units, 10**6))
    assert not plan.not_released or all(
        "children per parent" not in w for w, _ in plan.not_released
    )


def test_the_fixed_target_and_a_reached_child_are_charged_twice_the_childs_reach():
    plan = _plan(FOLLOW_READINGS_OF_NETWORK)
    relation = _relation(plan)
    assert plan.reach == {"network": 1, "reading": 5}
    assert (relation.l1, relation.sensitivity) == (10, 50)  # 2 m_C and 2 m_C squared


def test_following_the_readings_by_station_caps_each_entity_and_reaches_four_times_thirty():
    plan = _plan(FOLLOW_VERSIONS, FOLLOW_READINGS)
    relation = _relation(plan)
    assert plan.reach == {"network": 1, "station_version": 4, "reading": 120}
    assert relation.cap == 30 and relation.edges == tuple(range(32))
    assert (relation.l1, relation.sensitivity) == (4, 16)  # the readings come through it alone


def test_the_cap_of_a_followed_entity_keeps_at_most_that_many_children_per_entity():
    plan = _plan(FOLLOW_VERSIONS, FOLLOW_READINGS)
    rows = _rows(readings_of_one=35)
    data = fd.source_columns(plan, rows)
    backend = FakeBackend()
    encoded = fr.encode_follow_rows(plan, data, backend)
    kept = fr.truncated(plan, encoded, fd.SEED)
    stations = [rows["reading"][at]["station_id"] for at in kept["reading"]]
    assert stations.count(1) == 30  # two versions of station 1 are kept: still 30 for the entity
    assert all(stations.count(each) <= 30 for each in set(stations))


def test_the_truncation_groups_children_by_the_parent_key_not_by_the_parent_row():
    plan = plan_follows(
        _contract(),
        "network",
        (
            parse_follow(FOLLOW_VERSIONS),
            parse_follow("station_version.station_id=reading.station_id:3"),
        ),
        FakeBackend(),
    )
    # One station held by two version rows, with five readings: at most 3 for the station, not 3
    # for each of its rows.
    station = b"s1"
    kept = keep_rows(
        plan,
        {"network": 1, "station_version": 2, "reading": 5},
        {
            "network": [b"n"],
            "station_version": [b"v0", b"v1"],
            "reading": [bytes([each]) for each in range(5)],
        },
        {"station_version": [b"n"], "reading": [station, station]},
        {"station_version": [b"n", b"n"], "reading": [station] * 5},
        fd.SEED,
    )
    assert kept["station_version"] == [0, 1]
    assert len(kept["reading"]) == 3


def test_a_release_with_the_readings_followed_is_read_by_the_engine_with_the_stated_reach(
    tmp_path, monkeypatch, registry, capsys
):
    _workspace(tmp_path, monkeypatch, rows=_rows(readings_of_one=35))
    assert main(_argv(FOLLOW_VERSIONS, FOLLOW_READINGS)) == 0
    profile, body = _read(tmp_path / "network.profile.json")
    assert dict(profile.accounting.reached) == {"network": 1, "station_version": 4, "reading": 120}
    (statistic,) = _entry(body)["statistics"]
    assert statistic["bins"] == [str(each) for each in range(32)]


def test_a_stable_column_other_than_the_key_may_follow_the_history():
    # network_id is declared stable per station, so following the history by it is by entity.
    assert _relation(_plan(FOLLOW_VERSIONS)).l1 == 4


def test_the_release_never_writes_a_history_member(tmp_path, monkeypatch, registry, capsys):
    _workspace(tmp_path, monkeypatch)
    assert main(_argv(FOLLOW_VERSIONS)) == 0
    _, body = _read(tmp_path / "network.profile.json")

    def keys(value: Any) -> List[str]:
        if isinstance(value, Mapping):
            return [*value, *(name for item in value.values() for name in keys(item))]
        if isinstance(value, list):
            return [name for item in value for name in keys(item)]
        return []

    assert "history" not in keys(body)


def test_the_record_draws_the_release_again(tmp_path, monkeypatch, registry, capsys):
    _workspace(tmp_path, monkeypatch)
    assert main(_argv(FOLLOW_VERSIONS)) == 0
    written = (tmp_path / "network.profile.json").read_bytes()
    (tmp_path / "network.profile.json").unlink()
    again = [
        "profile",
        "weather.fluid.yaml",
        "--request",
        "weather.profile-request.yml",
        *[part for table in TABLES for part in ("--source", f"{table}={table}.csv")],
        "--release-record",
        "rec.json",
        "--rederive",
    ]
    assert main(again) == 0
    assert (tmp_path / "network.profile.json").read_bytes() == written


# -- groupings of children per entity ------------------------------------------------------------------


def _bin_of(count: int) -> int:
    edges = fr.CHILDREN_LADDER
    return max(at for at, edge in enumerate(edges[:-1]) if edge <= count)


def _grouped_cells(plan, rows):
    """The exact cells of the children per entity grouped by ``kind``, by group, before noise."""
    backend = FakeBackend()
    encoded = fr.encode_follow_rows(plan, fd.source_columns(plan, rows), backend)
    kept = fr.truncated(plan, encoded, fd.SEED)
    _, tables = fr.describe(plan, {})
    measured = fr.measure_counts(plan, encoded, kept, tables, backend)
    (grouped,) = [each for each in plan.relations if each.group is not None]
    flat = measured[grouped.label]
    width = grouped.cells
    return {key: flat[at * width : (at + 1) * width] for at, key in enumerate(grouped.group.levels)}


def test_children_per_entity_grouped_by_a_stable_column_place_each_entity_once():
    plan = _plan(FOLLOW_VERSIONS, request=_request(group_by=("kind",)))
    (grouped,) = [each for each in plan.relations if each.group is not None]
    assert grouped.entity and (grouped.l1, grouped.sensitivity) == (4, 16)
    cells = _grouped_cells(plan, _rows())
    for number, key in ((0, "fixed"), (1, "mobile")):
        expected = [0] * grouped.cells
        for station in range(1, 25):
            if station % 2 == number:
                expected[_bin_of(0 if station == 1 else station % 5)] += 1  # station 1: none
        assert cells[key] == expected  # 12 stations each, not their 36 version rows


def test_an_entity_whose_versions_disagree_on_a_stable_column_is_in_no_group():
    rows = _rows()
    rows["station_version"][1]["kind"] = "fixed"  # station 1 is mobile in its first version
    plan = _plan(FOLLOW_VERSIONS, request=_request(group_by=("kind",)))
    cells = _grouped_cells(plan, rows)
    assert sum(sum(each) for each in cells.values()) == 23


def test_a_column_that_is_not_stable_per_entity_is_not_a_grouping_of_the_release():
    plan = _plan(FOLLOW_VERSIONS, request=_request(group_by=("status",)))
    what = f"children per parent of {LINK} grouped by station_version.status"
    assert dict(plan.not_released)[what].startswith("status: an entity's versions may disagree")
    assert all(each.group is None for each in plan.relations)


def test_a_child_column_is_grouped_through_the_link_by_a_stable_column_when_the_unit_follows_it():
    request = _request(linked=(("flag", "kind"),))
    plan = _plan(FOLLOW_VERSIONS, FOLLOW_READINGS, request=request)
    (link,) = plan.links
    assert (link.parent, link.by.name, link.column.name) == ("station_version", "kind", "flag")
    assert link.sensitivity == 120 * 120
    assert plan.entity_units == (
        ("station_version", ("station_id",), "network", ("network_id",), ("network_id",)),
    )
    # followed by nothing, the history is reached without the readings: not released, as before
    plain = _plan(FOLLOW_VERSIONS, request=request)
    assert not plain.links


def test_a_link_by_a_column_that_is_not_stable_per_entity_is_not_released():
    request = _request(linked=(("flag", "status"),))
    plan = _plan(FOLLOW_VERSIONS, FOLLOW_READINGS, request=request)
    what = "flag of reading grouped by station_version.status"
    assert dict(plan.not_released)[what].startswith("status: an entity's versions may disagree")
    assert not plan.links


# -- where an entity could span two units ------------------------------------------------------------


def test_a_column_not_declared_stable_leaves_the_children_per_entity_unreleased(
    tmp_path, monkeypatch, registry, capsys
):
    _workspace(tmp_path, monkeypatch, stable=False)
    assert main(_argv(FOLLOW_VERSIONS, tables=TABLES[:2])) == 0  # the rest is released
    out = _out(capsys)
    _, body = _read(tmp_path / "network.profile.json")
    assert not any(
        each["parent"]["table"] == "station_version" and each["child"]["table"] == "reading"
        for each in body.get("relationships", [])
    )
    assert f"children per parent of {LINK}" in out
    assert "an entity's versions could follow different units" in out
    assert "follow station_version by its natural entity or by a column stable per entity" in out


def test_the_reason_is_decided_by_the_contract_and_the_flags_alone():
    plan = _plan(FOLLOW_VERSIONS, contract=_contract(stable=False))
    ((what, reason),) = [
        each for each in plan.not_released if each[0] == f"children per parent of {LINK}"
    ]
    assert reason == (
        "an entity's versions could follow different units: follow station_version by its "
        "natural entity or by a column stable per entity"
    )
    assert not plan.relations


def test_a_unit_that_is_the_history_itself_releases_no_children_per_entity(
    tmp_path, monkeypatch, registry, capsys
):
    # Each version row is then a unit, so one entity can be in many units.
    plan = _plan(FOLLOW_READINGS, unit="station_version")
    assert not plan.relations
    reasons = dict(plan.not_released)
    assert (
        "an entity's versions could follow different units"
        in reasons[f"children per parent of {LINK}"]
    )
    _workspace(tmp_path, monkeypatch)
    assert main(_argv(FOLLOW_READINGS, unit="station_version", tables=TABLES[1:])) == 0
    _, body = _read(tmp_path / "station_version.profile.json")
    assert _entry(body)["statistics"] == []  # the followed relationship holds no histogram


def test_versions_of_one_station_in_two_units_refuse_the_release_and_print_no_value(
    tmp_path, monkeypatch, registry, capsys
):
    _workspace(tmp_path, monkeypatch, rows=_rows(drift=True))
    assert main(_argv(FOLLOW_VERSIONS)) == 1
    out = _out(capsys)
    assert "profile_release_entity_units" in out
    assert "'station_version'" in out and "'station_id'" in out
    assert "follow the table by its natural entity, or keep the column stable per entity" in out
    assert not (tmp_path / "rec.json").exists()
    assert not (tmp_path / "network.profile.json").exists()
    # nothing printed holds a value or a count of the records
    for value in ("2024-01-01", "paused", "north", "south"):
        assert value not in out
    assert "row " not in out


def test_the_refusal_is_the_one_a_release_that_does_not_reach_the_target_does_not_make(
    tmp_path, monkeypatch, registry, capsys
):
    # The history is fixed when the unit does not reach it, so its versions move nothing.
    _workspace(tmp_path, monkeypatch, rows=_rows(drift=True))
    assert main(_argv(FOLLOW_READINGS_OF_NETWORK)) == 0
    # the report's guarantee sentence names the refusal; the refusal itself says this
    assert "has versions that follow different rows" not in _out(capsys)


# -- a parent key that is not an entity still repeats by mistake -----------------------------------------


def test_a_repeated_key_of_a_relationship_that_reaches_no_entity_is_still_refused(
    tmp_path, monkeypatch, registry, capsys
):
    rows = _rows()
    rows["network"].append(dict(rows["network"][0]))
    _workspace(tmp_path, monkeypatch, rows=rows)
    assert main(_argv(FOLLOW_VERSIONS)) == 1
    assert "profile_release_repeated_key" in _out(capsys)
