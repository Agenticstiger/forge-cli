# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The noisy history of a table the unit follows into (format 1-5-0, ``--follow``).

A request of format 1-3-0 may ask a table's history. When the release's unit follows into that table
(and it is not the unit's own expose) the release writes the history with noisy members, each a count
table of its own: ``versionsPerEntity``, ``firstStart``, ``versionLength``, ``chain``,
``lastVersion``, a tracked column's ``initial`` and ``transitions`` and the table's ``reasons``.

The gym of ``_gym_data`` is hand-built so that every member has cells that can be worked out by hand.
Six cards (entities) of eleven versions in all, in the order of the rows:

* card 101: basic/day 2026-01-01 to 2026-02-01, then plus/full from 2026-02-01, open;
* card 102: plus/full 2026-03-10 to 2026-04-09;
* card 103: basic/day 2025-12-01 to 2025-12-31, basic/full 2026-01-02 to 2026-02-01, plus/full from
  2026-02-01, open;
* card 104: plus/full 2026-01-01 to 2026-01-10, then basic/day from 2026-01-11, open;
* card 105: basic/day from 2026-01-01, open, then basic/full from 2026-01-15, open;
* card 106: plus/day 2025-06-01 to 2025-07-01.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence

import pytest

from fluid_build.profiler import dates, history
from fluid_build.profiler.dp import follow_release as fr
from fluid_build.profiler.dp import history_release, release, selection
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.dp import _gym_data as gym
from tests.profiler.dp._gym_data import TABLE

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.noise")

F = Fraction
SEED = gym.SEED
CORE = ("versionsPerEntity", "firstStart", "versionLength", "chain", "lastVersion")


def _ms(day: str) -> int:
    parsed = date.fromisoformat(day)
    return (
        int(datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc).timestamp()) * 1000
    )


def _year_bin(year: int) -> int:
    return 1 + (year - 1900)


def _plan(**asks: Any) -> fr.FollowReleasePlan:
    return gym.follow_plan(gym.request(history={}, **asks))


def _history_plan(plan: Optional[fr.FollowReleasePlan] = None) -> history_release.HistoryPlan:
    found = (plan or _plan()).table(TABLE).plan.history
    assert found is not None
    return found


def _counts(plan: Optional[fr.FollowReleasePlan] = None, held: Any = None) -> Dict[str, List[int]]:
    plan = plan or _plan()
    rows = gym.encode(plan, held)
    kept = fr.truncated(plan, rows, SEED)
    return history_release.history_counts(_history_plan(plan), rows.histories[TABLE], kept[TABLE])


def _event(call: Any) -> str:
    with pytest.raises(release.ReleaseRefused) as info:
        call()
    return info.value.event


# ---- the plan and the parts ---------------------------------------------------------------------


def test_a_history_ask_of_a_followed_table_plans_the_history_of_its_window_and_columns():
    plan = _plan()
    found = _history_plan(plan)
    assert (found.table, found.entity, found.start, found.end) == (
        TABLE,
        "card_no",
        "valid_from",
        "valid_to",
    )
    assert found.date and found.open_end is None
    # the contract's categorical columns that are no entity, start or end, with their levels
    assert [(each.name, each.levels) for each in found.columns] == [
        ("tier", ("basic", "plus")),
        ("access", ("day", "full")),
    ]
    assert found.with_reasons
    # the columns it reads are read, and stated by name and domain in the document
    read = dict(plan.reads)[TABLE]
    assert {each.name for each in read} >= {"card_no", "valid_from", "valid_to", "tier", "access"}
    assert plan.table("member").plan.history is None


def test_the_members_are_parts_of_the_table_with_the_cells_of_their_public_ladders():
    parts = fr.follow_parts(_plan(), {})
    mine = [each for each in parts if each.part.kind == release.HISTORY]
    assert [(each.part.label, each.part.cells) for each in mine] == [
        (f"{TABLE}/history/versionsPerEntity", 6),
        (f"{TABLE}/history/firstStart", 202),
        (f"{TABLE}/history/versionLength", 12),
        (f"{TABLE}/history/chain", 4),
        (f"{TABLE}/history/lastVersion", 2),
        (f"{TABLE}/history/initial/tier", 2),
        (f"{TABLE}/history/transitions/tier", 4),
        (f"{TABLE}/history/initial/access", 2),
        (f"{TABLE}/history/transitions/access", 4),
        (f"{TABLE}/history/reasons", 3),
    ]
    assert {each.table for each in mine} == {TABLE}
    # after the parts the table already held, before the relationships' and the links'
    labels = [each.part.label for each in parts]
    assert labels.index(f"{TABLE}/noise/tier") < labels.index(f"{TABLE}/history/versionsPerEntity")


def test_the_public_ladders_are_the_ones_the_exact_history_uses():
    assert history_release.ladder_of("versionsPerEntity") == tuple(history._version_edges())
    assert history_release.ladder_of("firstStart") == tuple(history._year_ladder())
    assert history_release.ladder_of("versionLength") == tuple(
        dates.DAY_LADDER[dates.DAY_LADDER.index(0) :]
    )
    assert history_release.CHAIN_LEVELS == history._CHAIN_LEVELS
    assert history_release.LAST_LEVELS == history._LAST_LEVELS


def test_a_request_that_names_the_candidate_columns_tracks_those_only():
    plan = gym.follow_plan(gym.request(history={"columns": ["access"]}))
    found = _history_plan(plan)
    assert [each.name for each in found.columns] == ["access"]
    assert not found.with_reasons  # one tracked column has no reasons
    labels = [each.part.label for each in fr.follow_parts(plan, {})]
    assert f"{TABLE}/history/reasons" not in labels
    assert f"{TABLE}/history/initial/tier" not in labels


def test_a_candidate_that_cannot_be_followed_is_named_and_not_tracked():
    from fluid_build.profiler import runner
    from fluid_build.profiler._stats import RequestHistory

    expose = gym.CONTRACT["exposes"][1]
    ask = RequestHistory(columns=("tier", "valid_from", "card_no", "missing"))
    backend = FakeBackend()
    structure = fr._structure_of(gym.CONTRACT, backend, TABLE, 16, frozenset({"member_id"}))
    made, notes = history_release.plan_history(
        TABLE, expose, runner.plan_expose(gym.CONTRACT, TABLE), ask, structure, backend
    )
    assert made is not None and [each.name for each in made.columns] == ["tier"]
    said = dict(notes)
    assert "window" in said[f"history of {TABLE}.valid_from"]
    assert "window" in said[f"history of {TABLE}.card_no"]
    assert "reads" in said[f"history of {TABLE}.missing"]


def test_a_candidate_with_no_declared_levels_is_named_with_why():
    contract = gym.contract()
    contract["exposes"][1]["contract"]["schema"].append({"name": "note", "type": "string"})
    asked = gym.request(history={})
    asked["tables"][TABLE]["columns"]["note"] = {"domain": "category"}
    plan = gym.follow_plan(asked, contract_=contract)
    assert "note" not in {each.name for each in _history_plan(plan).columns}
    assert f"history of {TABLE}.note" in dict(plan.not_released)


# ---- where a noisy history is not released ------------------------------------------------------


def test_the_unit_s_own_expose_is_named_as_not_released_with_why():
    plan = gym.follow_plan(gym.request(history={}, member_history=True))
    reason = dict(plan.not_released)["history of member"]
    assert "one row" in reason and "entity" in reason
    assert plan.table("member").plan.history is None


def test_a_followed_table_with_no_window_is_named_as_not_released():
    plan = gym.follow_plan(
        gym.request(history={}, visits_history=True), follows=(gym.FOLLOW, gym.FOLLOW_VISITS)
    )
    assert "validity window" in dict(plan.not_released)["history of visit"]
    assert plan.table("visit").plan.history is None
    assert plan.table(TABLE).plan.history is not None


def test_a_table_the_unit_does_not_reach_is_named_as_not_released():
    plan = gym.follow_plan(gym.request(history={}, pass_history=True))
    assert "does not reach" in dict(plan.not_released)["history of guest_pass"]


def test_an_own_rows_release_names_the_ask_and_writes_no_history():
    backend = FakeBackend()
    built = gym.request(history={})
    built["tables"]["member"]["history"] = {}
    asked = backend.read_request(json.dumps(built), name="gym.profile-request.yml")
    plan = release.plan_release(
        gym.CONTRACT, asked, backend, unit="member", rho=release.parse_rho("1")
    )
    said = dict(plan.not_released)
    assert "follows" in said["history of member"]
    assert "one table" in said[f"history of {TABLE}"]


# ---- the counts, member by member ---------------------------------------------------------------


def test_versions_per_entity_counts_the_entities_on_the_public_ladder_of_children():
    counts = _counts()["history/versionsPerEntity"]
    # ladder 1, 2, 3, 5, 10, 20, ...: cards 102 and 106 hold one version, 101, 104 and 105 two, 103 three
    assert counts == [2, 3, 1, 0, 0, 0]


def test_first_start_counts_each_entity_s_first_start_by_year():
    counts = _counts()["history/firstStart"]
    expected = [0] * 202
    expected[_year_bin(2025)] = 2  # cards 103 and 106
    expected[_year_bin(2026)] = 4  # cards 101, 102, 104 and 105
    assert counts == expected


def test_version_length_counts_the_days_from_a_start_to_the_next_start():
    counts = _counts()["history/versionLength"]
    # 31 and 32 days (101, 103), 30, 10 and 14 days (103, 104, 105): ladder 0, 1, 2, 8, 31, 91, ...
    assert counts == [0, 0, 0, 3, 2, 0, 0, 0, 0, 0, 0, 0]


def test_chain_counts_how_a_version_ends_where_the_next_begins():
    # dayBefore: 104; gap: 103's first pair; nextStart: 101 and 103's second pair; overlap: 105
    assert _counts()["history/chain"] == [1, 1, 2, 1]


def test_last_version_counts_the_entities_by_how_their_last_version_ends():
    assert _counts()["history/lastVersion"] == [2, 4]  # closed: 102, 106; open: the rest


def test_a_tracked_column_s_initial_counts_its_level_on_each_entity_s_first_version():
    counts = _counts()
    assert counts["history/initial/tier"] == [3, 3]  # basic: 101, 103, 105
    assert counts["history/initial/access"] == [4, 2]  # day: 101, 103, 105, 106


def test_a_tracked_column_s_transitions_count_each_change_by_its_current_value():
    counts = _counts()
    # grouped by the current level, one cell per next level (the diagonal is a cell too, always 0)
    assert counts["history/transitions/tier"] == [
        0,
        2,
        1,
        0,
    ]  # basic to plus: 101, 103; plus to basic: 104
    assert counts["history/transitions/access"] == [0, 3, 1, 0]  # day to full: 101, 103, 105


def test_reasons_count_the_changes_by_the_one_tracked_column_that_moves():
    # access, several, tier: 103 and 105 move access, 101 and 104 move both, 103 moves tier alone
    assert _counts()["history/reasons"] == [2, 2, 1]


def test_the_members_count_the_same_entities_and_the_same_pairs_of_versions():
    counts = _counts()
    versions = counts["history/versionsPerEntity"]
    assert sum(versions) == sum(counts["history/firstStart"]) == sum(counts["history/lastVersion"])
    assert sum(counts["history/initial/tier"]) == sum(versions)
    pairs = sum(counts["history/chain"])
    assert pairs == sum(counts["history/versionLength"]) == 5
    assert sum(counts["history/transitions/tier"]) <= pairs
    assert sum(counts["history/reasons"]) <= pairs


def _hand(
    entities: Sequence[Optional[bytes]],
    starts: Sequence[Optional[int]],
    ends: Sequence[Optional[int]],
    *,
    tracked: Optional[Dict[str, Sequence[int]]] = None,
    open_end: Optional[int] = None,
    columns: Sequence[release.ReleasedColumn] = (),
):
    plan = history_release.HistoryPlan(
        table="t",
        entity="e",
        start="s",
        end="f",
        date=True,
        open_end=open_end,
        columns=tuple(columns),
        with_reasons=len(columns) >= 2,
    )
    rows = history_release.HistoryRows(
        entities=list(entities),
        starts=list(starts),
        ends=list(ends),
        open_end=open_end,
        tracked=dict(tracked or {}),
    )
    return plan, rows


def _level(name: str, *levels: str) -> release.ReleasedColumn:
    return release.ReleasedColumn(name, "category", levels=levels, nullable=True)


def test_a_value_outside_a_public_ladder_is_coded_into_the_end_bin():
    # a first start before 1900 is in the first bin, and one after 2100 in the last
    plan, rows = _hand(
        [b"a", b"b", b"c"],
        [_ms("1850-03-01"), _ms("2150-03-01"), _ms("0100-01-01")],
        [None, None, None],
    )
    counts = history_release.history_counts(plan, rows, [0, 1, 2])
    expected = [0] * 202
    expected[0] = 2
    expected[201] = 1
    assert counts["history/firstStart"] == expected
    # twenty-five versions of one entity are in the last bin of the ladder
    many = [b"x"] * 25
    plan, rows = _hand(many, [_ms("2026-01-01") + n * 86_400_000 for n in range(25)], [None] * 25)
    assert history_release.history_counts(plan, rows, list(range(25)))[
        "history/versionsPerEntity"
    ] == [0, 0, 0, 0, 0, 1]
    # and a length beyond the ladder's last edge is in the last bin
    plan, rows = _hand([b"x", b"x"], [_ms("0100-01-01"), _ms("9000-01-01")], [None, None])
    assert history_release.history_counts(plan, rows, [0, 1])["history/versionLength"][-1] == 1


def test_versions_are_ordered_by_start_then_end_with_an_open_end_last_then_by_position():
    day = 86_400_000
    start = _ms("2026-01-01")
    # one entity; rows in a scrambled order, two with the same start (a closed one, an open one)
    plan, rows = _hand(
        [b"a", b"a", b"a", b"a"],
        [start + 10 * day, start, start, start + 10 * day],
        [None, start + 5 * day, None, start + 20 * day],
    )
    counts = history_release.history_counts(plan, rows, [0, 1, 2, 3])
    # order: (start, closed), (start, open), (start+10, closed), (start+10, open)
    # every pair overlaps: an end at day 5 after a start at day 0, an open end, an end at day 20
    # after a start at day 10
    assert counts["history/chain"] == [0, 0, 0, 3]
    # the starts lie 0, 10 and 0 days apart
    assert counts["history/versionLength"][0] == 2 and counts["history/versionLength"][3] == 1
    assert counts["history/lastVersion"] == [0, 1]  # the last version is the open one


def test_an_end_equal_to_the_open_end_literal_is_open():
    day = 86_400_000
    literal = _ms("9999-12-31")
    start = _ms("2026-01-01")
    plan, rows = _hand(
        [b"a", b"a"], [start, start + 30 * day], [start + 30 * day, literal], open_end=literal
    )
    counts = history_release.history_counts(plan, rows, [0, 1])
    assert counts["history/chain"] == [0, 0, 1, 0]  # the first ends where the second begins
    assert counts["history/lastVersion"] == [0, 1]  # the last ends at the literal: open


def test_a_row_with_no_entity_or_no_start_is_no_version_and_a_null_level_lands_in_no_cell():
    day = 86_400_000
    start = _ms("2026-01-01")
    tier = _level("tier", "basic", "plus")
    plan, rows = _hand(
        [b"a", b"a", None, b"b", b"a"],
        [start, start + 10 * day, start, None, start + 40 * day],
        [start + 10 * day, None, None, None, None],
        tracked={"tier": [0, -1, 0, 0, 1]},
        columns=[tier],
    )
    counts = history_release.history_counts(plan, rows, [0, 1, 2, 3, 4])
    assert counts["history/versionsPerEntity"] == [0, 0, 1, 0, 0, 0]  # entity a: three versions
    assert counts["history/chain"] == [0, 0, 1, 1]  # an end at the next start; an open end
    assert counts["history/versionLength"][3] == 2  # 10 and 30 days
    assert counts["history/lastVersion"] == [0, 1]
    # the second version holds no tier: no change into it, and a change out of it is from a NULL
    assert counts["history/transitions/tier"] == [0, 0, 0, 0]
    assert counts["history/initial/tier"] == [1, 0]
    assert "history/reasons" not in counts  # one tracked column has no reasons


def test_only_the_kept_rows_are_counted():
    plan = _plan()
    rows = gym.encode(plan)
    kept = fr.truncated(plan, rows, SEED)[TABLE]
    some = kept[:3]  # the three versions of cards 101 and 102
    counts = history_release.history_counts(_history_plan(plan), rows.histories[TABLE], some)
    assert counts["history/versionsPerEntity"] == [1, 1, 0, 0, 0, 0]
    assert counts["history/chain"] == [0, 0, 1, 0]


# ---- what a member is charged and how it draws -------------------------------------------------


def _parts(drawn: fr.FollowRelease) -> List[release.ReleasedTable]:
    return [each for each in drawn.tables if each.kind == release.HISTORY]


def test_each_member_is_charged_as_a_count_table_of_its_table_with_the_reach_of_the_table():
    plan = _plan()
    drawn, _ = gym.draw(plan)
    members = _parts(drawn)
    assert len(members) == 10
    reach = plan.reach[TABLE]
    assert reach == 4
    count_table = next(each for each in drawn.tables if each.label == f"{TABLE}/noise/tier")
    for each in members:
        assert each.table == TABLE and each.sensitivity == reach * reach
        assert F(each.scale) * 10**6 == release.scale_units(F(each.share), reach * reach)
        # the pair members are charged the same, the slack of one
        assert each.scale == count_table.scale and each.share == count_table.share


def test_the_charge_follows_the_cap_of_the_followed_relationship():
    small = gym.follow_plan(
        gym.request(history={}), follows=(f"member.member_id={TABLE}.member_id:2",)
    )
    drawn, _ = gym.draw(small)
    assert small.reach[TABLE] == 2
    for each in _parts(drawn):
        assert each.sensitivity == 4
        assert F(each.scale) * 10**6 == release.scale_units(F(each.share), 4)
    big, _ = gym.draw(_plan())
    assert F(_parts(big)[0].scale) > F(_parts(drawn)[0].scale)


def test_each_member_draws_from_the_stream_of_its_own_label_and_joins_no_consistency_step():
    plan = _plan()
    drawn, rows = gym.draw(plan)
    labels = [each.label for each in _parts(drawn)]
    assert len(set(labels)) == len(labels)
    assert all(not release.agrees(plan.table(TABLE).plan, each) for each in _parts(drawn))
    from fluid_build.profiler.dp.discrete_gaussian import sample_discrete_gaussian
    from fluid_build.profiler.dp.randomness import SeededSource

    kept = fr.truncated(plan, rows, SEED)
    _, tables = fr.describe(plan, {})
    exact = fr.measure_counts(plan, rows, kept, tables, FakeBackend())
    for each in _parts(drawn):
        sigma2 = F(each.scale) ** 2
        source = SeededSource(SEED, each.label)
        noise = [sample_discrete_gaussian(sigma2, source) for _ in range(each.cells)]
        member = each.label.split("/", 1)[1]
        written = _written(drawn, member)
        assert [F(text) for text in written] == [
            F(count + draw) for count, draw in zip(exact[each.label], noise, strict=True)
        ], each.label


def _written(drawn: fr.FollowRelease, member: str) -> List[str]:
    """The noisy counts the document holds for *member* (``history/...``), flat, group by group."""
    held = gym.history_of(drawn)
    steps = member.split("/")[1:]
    if steps[0] in ("initial", "transitions"):
        entry = held["columns"][steps[1]][steps[0]]
    else:
        entry = held[steps[0]]
    return [text for group in entry["groups"] for text in group["noisyCounts"]]


def test_the_counts_the_release_measures_are_the_ones_counted_over_the_kept_rows():
    plan = _plan()
    drawn, rows = gym.draw(plan)
    kept = fr.truncated(plan, rows, SEED)
    _, tables = fr.describe(plan, {})
    exact = fr.measure_counts(plan, rows, kept, tables, FakeBackend())
    counted = history_release.history_counts(
        _history_plan(plan), rows.histories[TABLE], kept[TABLE]
    )
    for member, cells in counted.items():
        assert exact[f"{TABLE}/{member}"] == cells


# ---- admission under the cap --------------------------------------------------------------------


def _cells(plan: fr.FollowReleasePlan) -> Dict[str, int]:
    return {each.part.label: each.part.cells for each in fr.follow_parts(plan, {})}


def test_the_history_is_added_after_the_null_counts_and_before_the_links(monkeypatch):
    asked = gym.request(history={}, null_counts=("valid_to",))
    plan = gym.follow_plan(asked)
    cells = _cells(plan)
    total = sum(cells.values())
    history_cells = sum(c for label, c in cells.items() if "/history/" in label)
    monkeypatch.setattr(selection, "MOST_RELEASE_CELLS", total - history_cells)
    kept, left = fr.admit(plan, {})
    assert kept.table(TABLE).plan.history is None
    assert kept.table(TABLE).plan.null_counts == ("valid_to",)
    assert [label for label, _ in left] == [
        f"{TABLE}/history/{name}"
        for name in (
            *CORE,
            "initial/tier",
            "transitions/tier",
            "initial/access",
            "transitions/access",
            "reasons",
        )
    ]
    assert {reason for _, reason in left} == {release.left_out_reason()}
    # one cell fewer and the null count goes first of all, with everything after it
    monkeypatch.setattr(selection, "MOST_RELEASE_CELLS", total - history_cells - 1)
    kept, left = fr.admit(plan, {})
    assert kept.table(TABLE).plan.null_counts == ()
    assert left[0][0] == f"{TABLE}/nullCount/valid_to"


def test_a_tracked_column_that_does_not_fit_leaves_with_the_columns_and_the_reasons_after_it(
    monkeypatch,
):
    plan = _plan()
    cells = _cells(plan)
    total = sum(cells.values())
    monkeypatch.setattr(
        selection, "MOST_RELEASE_CELLS", total - 6 - 3
    )  # the last column and reasons
    kept, left = fr.admit(plan, {})
    assert [label for label, _ in left] == [
        f"{TABLE}/history/initial/access",
        f"{TABLE}/history/transitions/access",
        f"{TABLE}/history/reasons",
    ]
    held = kept.table(TABLE).plan.history
    assert held is not None
    assert [each.name for each in held.columns] == ["tier"] and not held.with_reasons
    drawn, _ = gym.draw(plan)
    assert [label for label, _ in drawn.left_out] == [label for label, _ in left]
    assert list(gym.history_of(drawn)["columns"]) == ["tier"]
    assert "reasons" not in gym.history_of(drawn)
    # only the reasons of the table leave when they alone do not fit
    monkeypatch.setattr(selection, "MOST_RELEASE_CELLS", total - 1)
    kept, left = fr.admit(plan, {})
    assert [label for label, _ in left] == [f"{TABLE}/history/reasons"]
    held = kept.table(TABLE).plan.history
    assert held is not None and len(held.columns) == 2 and not held.with_reasons


def test_the_core_members_leave_together_with_every_column_when_they_do_not_fit(monkeypatch):
    plan = _plan()
    cells = _cells(plan)
    history_cells = sum(c for label, c in cells.items() if "/history/" in label)
    monkeypatch.setattr(
        selection, "MOST_RELEASE_CELLS", sum(cells.values()) - history_cells + 225
    )  # the core members hold 226 cells
    kept, left = fr.admit(plan, {})
    assert kept.table(TABLE).plan.history is None
    assert len(left) == 10
    drawn, _ = gym.draw(plan)
    assert "history" not in drawn.document["tables"][TABLE]
    record = fr.build_follow_record(plan, drawn, SEED, request_bytes=b"r", contract_digest=None)
    assert fr.check_follow_record(plan, record) == {}


# ---- one unit holds a whole entity --------------------------------------------------------------


def test_an_entity_whose_versions_follow_two_parent_rows_refuses_the_release_before_anything_is_written():
    plan = _plan()
    rows = gym.versions()
    rows[1] = dict(rows[1], member_id="2")  # card 101's second version moves to member 2
    assert _event(lambda: gym.encode(plan, gym.tables(rows))) == "profile_release_entity_units"
    with pytest.raises(release.ReleaseRefused) as info:
        gym.draw(plan, gym.tables(rows))
    message = str(info.value)
    assert TABLE in message and "card_no" in message
    assert not any(ch.isdigit() for ch in message)


def test_the_check_covers_the_history_s_entity_and_names_the_table_in_the_plan():
    plan = _plan()
    assert (TABLE, ("card_no",), "member", ("member_id",), ("member_id",)) in plan.entity_units


def test_an_entity_that_is_the_followed_column_holds_by_construction_and_adds_no_check():
    contract = gym.contract(entity="member_id")
    plan = gym.follow_plan(gym.request(history={}), contract_=contract)
    assert _history_plan(plan).entity == "member_id"
    assert not any(item[0] == TABLE for item in plan.entity_units)
    drawn, _ = gym.draw(plan)
    assert gym.history_of(drawn)["entity"] == ["member_id"]


def test_the_truncation_keeps_an_entity_s_versions_together_by_a_rule_over_its_parent_alone():
    # a cap of two keeps at most two rows of a member, and which two reads that member's rows only
    plan = gym.follow_plan(
        gym.request(history={}), follows=(f"member.member_id={TABLE}.member_id:2",)
    )
    rows = gym.versions()
    more = rows + [
        {
            "card_no": "107",
            "member_id": "6",
            "tier": "basic",
            "access": "day",
            "valid_from": "2026-05-01",
            "valid_to": None,
        },
        {
            "card_no": "107",
            "member_id": "6",
            "tier": "plus",
            "access": "day",
            "valid_from": "2026-06-01",
            "valid_to": None,
        },
        {
            "card_no": "108",
            "member_id": "6",
            "tier": "plus",
            "access": "full",
            "valid_from": "2026-07-01",
            "valid_to": None,
        },
    ]

    def kept_of(
        held: List[Dict[str, Optional[str]]], member: str
    ) -> List[Dict[str, Optional[str]]]:
        encoded = gym.encode(plan, gym.tables(held))
        kept = fr.truncated(plan, encoded, SEED)[TABLE]
        return [held[i] for i in kept if held[i]["member_id"] == member]

    for member in ("1", "2", "3", "4", "5"):
        assert kept_of(more, member) == kept_of(rows, member)
    # member 1 holds three rows and keeps two; every kept row is one of its own
    assert len(kept_of(rows, "1")) == 2
    # under the cap every version of every entity is kept, so the history describes whole entities
    roomy = _plan()
    encoded = gym.encode(roomy)
    assert fr.truncated(roomy, encoded, SEED)[TABLE] == list(range(len(rows)))
