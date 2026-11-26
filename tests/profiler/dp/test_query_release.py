# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The noisy rate of a consumer query in a private release: what is answered, what it is charged,
and the exact cells behind it (no noise here; the document is in ``test_release_queries``).

Every query is planned before any record is read: it is answered with a ``unit`` and a charge, or
named with a reason. Each answered query is one part per statistic with two cells for each group
(not returned, returned), charged as a count table of its unit. The tables are the gym of
``tests/profiler/dp/_gym_data``; the counts of each query are worked out by hand in
``_gym_queries``.
"""

from __future__ import annotations

import copy
import json
from fractions import Fraction
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

from fluid_build.profiler import query_rates
from fluid_build.profiler.dp import follow_release as fr
from fluid_build.profiler.dp import query_release, release, selection
from fluid_build.profiler.dp.follows import parse_follow
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.dp import _gym_data as gym
from tests.profiler.dp import _gym_queries as gq
from tests.profiler.dp._gym_data import TABLE

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.noise")
pytest.importorskip("sqlglot")
pytest.importorskip("duckdb")

SEED = gym.SEED
F = Fraction


def _plan(
    *queries: Mapping[str, Any], history: Optional[Mapping[str, Any]] = None, **more: Any
) -> fr.FollowReleasePlan:
    return gym.follow_plan(gq.asked(*queries, history=history, **more))


def _cells(plan: fr.FollowReleasePlan, held=None) -> Dict[str, List[int]]:
    backend = FakeBackend()
    rows = fr.encode_follow_rows(plan, gym.read_of(plan, held), backend)
    kept = fr.truncated(plan, rows, SEED)
    found = query_release.exact_counts(plan.queries, rows.raw, kept, backend)
    assert found is not None
    return {label: list(cells) for label, cells in found.items()}


def _named(plan: Any) -> Dict[str, str]:
    return dict(plan.queries.named)


def _part(plan: fr.FollowReleasePlan, name: str) -> release.ReleasedTable:
    _, tables = fr.describe(plan, {})
    (found,) = [each for each in tables if each.label == f"query/{name}"]
    return found


# ---- the exact cells, one query of each form ----------------------------------------------------


def test_each_form_of_unit_counts_the_cells_worked_out_by_hand():
    plan = _plan(*gq.EVERY, history={})
    assert _named(plan) == {}
    assert _cells(plan) == {f"query/{name}": list(cells) for name, cells in gq.CELLS.items()}


def test_a_table_unit_a_view_and_a_grouped_query_each_state_their_unit():
    plan = _plan(*gq.EVERY, history={})
    units = {each.plan.name: (each.unit, each.groups) for each in plan.queries.queries}
    assert units == {
        "plus_rows": ({"table": TABLE}, 1),
        "full_plus": ({"table": TABLE}, 1),
        "full_by_tier": ({"table": TABLE}, 2),
        "members_with_two": ({"table": TABLE, "by": ["member_id"]}, 1),
        "cards_with_two": ({"table": TABLE, "by": ["card_no"]}, 1),
    }


def test_a_grouped_query_publishes_every_declared_level_even_one_no_record_holds():
    rows = [dict(row, tier="basic") for row in gym.versions()]
    plan = _plan(gq.FULL_BY_TIER_QUERY)
    cells = _cells(plan, gym.tables(rows))["query/full_by_tier"]
    assert len(cells) == 4
    assert cells[2:] == [0, 0]  # the plus level: no record, both cells present


def test_the_rows_a_unit_follows_are_the_rows_kept_after_the_truncation_per_parent():
    # a cap of one version for each member: the kept rows, not the rows read, are counted
    plan = gym.follow_plan(
        gq.asked(gq.PLUS_ROWS, gq.MEMBERS_WITH_TWO),
        follows=(f"member.member_id={TABLE}.member_id:1",),
    )
    backend = FakeBackend()
    rows = fr.encode_follow_rows(plan, gym.read_of(plan), backend)
    kept = fr.truncated(plan, rows, SEED)
    assert len(kept[TABLE]) == 5
    found = query_release.exact_counts(plan.queries, rows.raw, kept, backend)
    assert found is not None
    assert sum(found["query/plus_rows"]) == 5
    assert found["query/members_with_two"] == [5, 0]  # five members, none with two versions


# ---- what a unit is charged ---------------------------------------------------------------------


def test_a_table_is_charged_as_a_count_table_of_its_table_and_an_entity_by_what_it_follows():
    plan = _plan(*gq.EVERY, history={})
    _, tables = fr.describe(plan, {})
    by_label = {each.label: each for each in tables}
    tier = by_label[f"{TABLE}/noise/tier"]
    branch = by_label["member/noise/branch"]
    for name in ("plus_rows", "full_plus", "full_by_tier", "cards_with_two"):
        found = by_label[f"query/{name}"]
        assert (found.kind, found.table, found.sensitivity) == ("query", TABLE, 16), name
        assert found.scale == tier.scale  # m_T = 4, as any count table of the table
    members = by_label["query/members_with_two"]
    assert members.sensitivity == 1 and members.scale == branch.scale  # m_P = 1: the unit's expose
    assert members.table == TABLE
    assert members.sizes == (2,) and by_label["query/full_by_tier"].sizes == (4,)
    assert by_label["query/plus_rows"].columns == ()


def test_the_queries_take_one_equal_share_each_and_are_not_made_to_agree_with_a_clique():
    base = _plan()
    plan = _plan(*gq.EVERY, history={})
    _, tables = fr.describe(plan, {})
    _, before = fr.describe(base, {})
    assert len(tables) == len(before) + 5 + 10  # the five queries and the history's ten members
    assert len({each.share for each in tables}) == 1
    assert not release.agrees(plan.tables[1].plan, _part(plan, "plus_rows"))


def _scan_gym() -> Dict[str, Any]:
    """The gym with a ``visit_item`` of a ``visit``: a table two steps below the unit's expose."""
    found = copy.deepcopy(gym.CONTRACT)
    found["exposes"].append(
        {
            "exposeId": "visit_item",
            "kind": "table",
            "binding": {
                "platform": "local",
                "format": "parquet",
                "location": {"path": "./out/visit_item.parquet"},
            },
            "contract": {
                "schema": [
                    {"name": "item_id", "type": "INT", "required": True},
                    {"name": "visit_id", "type": "INT", "required": True},
                    {"name": "kind", "type": "enum('towel','drink')", "required": True},
                ]
            },
            "semantics": {
                "entities": [{"name": "item", "type": "primary", "expr": "item_id"}],
                "relationships": [
                    {
                        "name": "taken_on",
                        "to": "visit",
                        "fromColumns": ["visit_id"],
                        "toColumns": ["visit_id"],
                        "cardinality": "many_to_one",
                    }
                ],
            },
        }
    )
    return found


def _scan_plan(*queries: Mapping[str, Any]) -> fr.FollowReleasePlan:
    asked = gq.asked(*queries, with_visits=True)
    asked["tables"]["visit_item"] = {
        "columns": {
            "item_id": {"domain": "number"},
            "visit_id": {"domain": "number"},
            "kind": {"domain": "category", "statistics": ["levelHistogram/1"]},
        }
    }
    return gym.follow_plan(
        asked,
        contract_=_scan_gym(),
        follows=(gym.FOLLOW_VISITS, "visit.visit_id=visit_item.visit_id:3"),
    )


ITEMS_BY_VISIT = "SELECT visit_id, count(*) AS n FROM visit_item GROUP BY visit_id"
ITEMS_OF_KIND = "SELECT item_id FROM visit_item WHERE kind = 'drink'"


def test_an_entity_of_a_followed_relationship_is_charged_by_its_parent_not_its_table():
    plan = _scan_plan(
        gq.query("many_items", gq.over(ITEMS_BY_VISIT, "visit_id"), unit_sql=ITEMS_BY_VISIT),
        gq.query("drinks", ITEMS_OF_KIND, table="visit_item"),
    )
    assert _named(plan) == {}
    assert plan.reach == {"member": 1, "visit": 2, "visit_item": 6}
    assert _part(plan, "many_items").sensitivity == 4  # m_visit = 2, one group per visit row
    assert _part(plan, "drinks").sensitivity == 36  # m_visit_item = 6, one row at a time
    by_name = {each.plan.name: each for each in plan.queries.queries}
    assert by_name["many_items"].unit == {"table": "visit_item", "by": ["visit_id"]}


def test_the_groups_of_a_relationship_out_of_a_versioned_table_are_checked_as_entities():
    contract = copy.deepcopy(gym.CONTRACT)
    contract["exposes"].append(
        {
            "exposeId": "card_scan",
            "kind": "table",
            "binding": {
                "platform": "local",
                "format": "parquet",
                "location": {"path": "./out/card_scan.parquet"},
            },
            "contract": {
                "schema": [
                    {"name": "scan_id", "type": "INT", "required": True},
                    {"name": "card_no", "type": "INT", "required": True},
                    {"name": "door", "type": "enum('gate','pool')", "required": True},
                ]
            },
            "semantics": {
                "entities": [{"name": "scan", "type": "primary", "expr": "scan_id"}],
                "relationships": [
                    {
                        "name": "scanned",
                        "to": TABLE,
                        "fromColumns": ["card_no"],
                        "toColumns": ["card_no"],
                        "cardinality": "many_to_one",
                    }
                ],
            },
        }
    )
    by_card = "SELECT card_no, count(*) AS n FROM card_scan GROUP BY card_no"
    asked = gq.asked(gq.query("busy_cards", gq.over(by_card, "card_no"), unit_sql=by_card))
    asked["tables"]["card_scan"] = {
        "columns": {
            "scan_id": {"domain": "number"},
            "card_no": {"domain": "number"},
            "door": {"domain": "category", "statistics": ["levelHistogram/1"]},
        }
    }
    plan = gym.follow_plan(
        asked, contract_=contract, follows=(gym.FOLLOW, f"{TABLE}.card_no=card_scan.card_no:2")
    )
    assert _named(plan) == {}
    (asked_query,) = plan.queries.queries
    assert asked_query.form == query_release.FORM_FOLLOWED
    assert asked_query.unit == {"table": "card_scan", "by": ["card_no"]}
    assert _part(plan, "busy_cards").sensitivity == 16  # m_P = 4: the versions one unit holds
    assert any(
        (each[0], each[1], each[2]) == (TABLE, ("card_no",), "member") for each in plan.entity_units
    )


# ---- what is named, and why ---------------------------------------------------------------------


def test_a_query_over_a_table_the_unit_does_not_reach_is_named():
    plan = _plan(
        gq.query(
            "passes", "SELECT pass_no FROM guest_pass WHERE tier = 'plus'", table="guest_pass"
        ),
        gq.query("visits", "SELECT visit_id FROM visit WHERE lane = 'pool'", table="visit"),
        gq.PLUS_ROWS,
        pass_history=True,
        with_visits=True,
    )
    named = _named(plan)
    assert named == {
        "query passes": query_release.NOT_REACHED,
        "query visits": query_release.NOT_REACHED,
    }
    assert [each.plan.name for each in plan.queries.queries] == ["plus_rows"]


def test_an_entity_unit_over_the_unit_s_own_expose_is_named():
    backend = FakeBackend()
    asked = gq.asked(gq.MEMBERS_WITH_TWO)
    built = backend.read_request(json.dumps(asked), name="gym.profile-request.yml")
    plan = release.plan_release(
        gym.CONTRACT, built, backend, unit=TABLE, rho=release.parse_rho("1")
    )
    assert dict(plan.queries.named) == {"query members_with_two": query_release.ENTITY_OF_EXPOSE}
    assert plan.queries.queries == ()


def test_a_plain_release_answers_a_table_unit_of_its_expose_only():
    backend = FakeBackend()
    asked = gq.asked(gq.PLUS_ROWS, gq.FULL_PLUS_VIEW, gq.FULL_BY_TIER_QUERY, gq.CARDS_WITH_TWO)
    built = backend.read_request(json.dumps(asked), name="gym.profile-request.yml")
    plan = release.plan_release(
        gym.CONTRACT, built, backend, unit=TABLE, rho=release.parse_rho("1")
    )
    assert [each.plan.name for each in plan.queries.queries] == [
        "plus_rows",
        "full_plus",
        "full_by_tier",
    ]
    assert set(dict(plan.queries.named)) == {"query cards_with_two"}
    parts = [each for each in release.release_parts(plan, ()) if each.kind == release.QUERY]
    assert [(each.label, each.sizes) for each in parts] == [
        ("query/plus_rows", (2,)),
        ("query/full_plus", (2,)),
        ("query/full_by_tier", (4,)),
    ]


def test_a_query_over_another_table_of_a_plain_release_is_named():
    backend = FakeBackend()
    asked = gq.asked(gq.query("members", "SELECT member_id FROM member", table="member"))
    built = backend.read_request(json.dumps(asked), name="gym.profile-request.yml")
    plan = release.plan_release(
        gym.CONTRACT, built, backend, unit=TABLE, rho=release.parse_rho("1")
    )
    assert dict(plan.queries.named) == {"query members": query_release.NOT_REACHED}


def test_a_group_by_on_an_entity_unit_is_named_by_the_planner():
    grouped = gq.query(
        "grouped", gq.over(gq.BY_MEMBER, "member_id"), unit_sql=gq.BY_MEMBER, group_by="member_id"
    )
    plan = _plan(grouped)
    (reason,) = _named(plan).values()
    assert "groups a query only over a table unit's records" in reason
    assert plan.queries.queries == ()


def test_a_query_the_checker_refuses_is_named_with_its_reason_and_no_text():
    joined = gq.query(
        "joined",
        f"SELECT card_no FROM {TABLE} JOIN member ON {TABLE}.member_id = member.member_id",
    )
    plan = _plan(joined, gq.PLUS_ROWS)
    assert set(_named(plan)) == {"query joined"}
    assert "SELECT" not in "".join(_named(plan).values())
    assert [each.plan.name for each in plan.queries.queries] == ["plus_rows"]


def test_an_entity_of_a_table_with_no_history_held_is_named():
    plan = _plan(gq.CARDS_WITH_TWO)  # the request asks no history, so the document states no entity
    assert _named(plan) == {"query cards_with_two": query_release.NO_HISTORY}
    plan = _plan(gq.CARDS_WITH_TWO, history={"columns": ["tier"]})
    assert _named(plan) == {}


def test_a_natural_entity_the_contract_does_not_require_is_named():
    contract = gym.contract()
    for column in contract["exposes"][1]["contract"]["schema"]:
        if column["name"] == "card_no":
            column["required"] = False
    plan = gym.follow_plan(gq.asked(gq.CARDS_WITH_TWO, history={}), contract_=contract)
    assert _named(plan) == {"query cards_with_two": query_release.ENTITY_MAY_BE_NULL}


def test_a_record_with_no_natural_entity_refuses_the_release_naming_a_column_and_no_row():
    plan = _plan(gq.CARDS_WITH_TWO, history={})
    rows = gym.versions()
    rows[3] = dict(rows[3], card_no=None)
    backend = FakeBackend()
    encoded = fr.encode_follow_rows(plan, gym.read_of(plan, gym.tables(rows)), backend)
    with pytest.raises(release.ReleaseRefused) as refused:
        gym.draw(plan, gym.tables(rows))
    assert refused.value.event == "profile_release_null"
    assert "'card_no'" in str(refused.value) and TABLE in str(refused.value)
    assert "row" not in str(refused.value).replace("every row", "")
    assert encoded.raw  # the records were read for the queries


def test_an_entity_column_that_is_neither_followed_nor_the_natural_entity_is_named():
    by_access = f"SELECT access, count(*) AS n FROM {TABLE} GROUP BY access"
    plan = _plan(gq.query("odd", gq.over(by_access, "access"), unit_sql=by_access), history={})
    assert set(_named(plan)) == {"query odd"}
    assert plan.queries.queries == ()


def test_two_relationships_from_reached_parents_on_the_same_columns_are_named():
    edges = [
        SimpleNamespace(parent="a", child="t", spec=SimpleNamespace(child_columns=("k",))),
        SimpleNamespace(parent="b", child="t", spec=SimpleNamespace(child_columns=("k",))),
    ]
    assert query_release.parents_of(edges, "t", ("k",), {"a": 1, "b": 1}) == ["a", "b"]
    assert query_release.parents_of(edges, "t", ("k",), {"a": 1, "b": 0}) == ["a"]
    assert query_release.parents_of(edges, "t", ("j",), {"a": 1, "b": 1}) == []


def test_a_query_over_a_table_the_unit_follows_is_not_named_for_a_table_it_fixes():
    plan = _plan(gq.PLUS_ROWS)
    assert _named(plan) == {} and plan.queries.read == {TABLE: ("card_no", "tier")}


# ---- the exact path's rules are lifted for a noisy plan, and only for it -----------------------


def test_the_exact_planner_still_refuses_an_entity_the_history_publishes_and_two_ways_to_count():
    request = FakeBackend().read_request(
        json.dumps(gq.asked(gq.CARDS_WITH_TWO, gq.MEMBERS_WITH_TWO, history={})),
        name="gym.profile-request.yml",
    )
    exact = query_rates.plan_queries(gym.CONTRACT, request, FakeBackend().enum_levels)
    reasons = {each.name: each.reason for each in exact.plans}
    assert reasons["cards_with_two"] == query_rates.HISTORY_ENTITY
    assert reasons["members_with_two"] is None
    noisy = query_rates.plan_queries(gym.CONTRACT, request, FakeBackend().enum_levels, noisy=True)
    assert noisy.noisy and not exact.noisy
    assert [each.reason for each in noisy.plans] == [None, None]


def test_a_noisy_plan_counts_one_table_both_as_rows_and_as_entities():
    request = FakeBackend().read_request(
        json.dumps(gq.asked(gq.PLUS_ROWS, gq.MEMBERS_WITH_TWO)), name="gym.profile-request.yml"
    )
    exact = query_rates.plan_queries(gym.CONTRACT, request, FakeBackend().enum_levels)
    assert [each.reason for each in exact.plans] == [None, query_rates.COUNTED_ANOTHER_WAY]
    noisy = query_rates.plan_queries(gym.CONTRACT, request, FakeBackend().enum_levels, noisy=True)
    assert [each.reason for each in noisy.plans] == [None, None]


# ---- a failure that depends on the records withholds every query -------------------------------


def _rows_with_bad_card() -> Dict[str, Any]:
    rows = gym.versions()
    rows[3] = dict(rows[3], card_no="not-a-number")
    return gym.tables(rows)


def test_a_value_the_query_process_cannot_load_withholds_every_query_and_names_none_of_them_a_count():
    plan = _plan(gq.PLUS_ROWS, gq.FULL_PLUS_VIEW, gq.MEMBERS_WITH_TWO)
    backend = FakeBackend()
    rows = fr.encode_follow_rows(plan, gym.read_of(plan, _rows_with_bad_card()), backend)
    kept = fr.truncated(plan, rows, SEED)
    assert query_release.exact_counts(plan.queries, rows.raw, kept, backend) is None


def test_the_other_parts_of_a_release_are_the_same_when_the_queries_are_withheld():
    queries = (gq.PLUS_ROWS, gq.FULL_PLUS_VIEW, gq.MEMBERS_WITH_TWO)
    plan = _plan(*queries)
    good, _ = gym.draw(plan)
    bad, _ = gym.draw(plan, _rows_with_bad_card())
    assert "queries" in good.document and "queries" not in bad.document
    assert bad.document["tables"] == good.document["tables"]
    assert dict(bad.unanswered) == {
        f"query {each['name']}": query_rates.RUN_FAILED for each in queries
    }
    # the parts and their shares were fixed before any record was read
    assert [(each.label, each.scale, each.share) for each in bad.tables] == [
        (each.label, each.scale, each.share) for each in good.tables
    ]
    assert good.unanswered == ()


def test_a_release_with_the_same_records_withholds_the_same_way_each_time():
    plan = _plan(gq.PLUS_ROWS)
    one, _ = gym.draw(plan, _rows_with_bad_card())
    two, _ = gym.draw(plan, _rows_with_bad_card())
    assert one.canonical == two.canonical


# ---- an entity of a table lies in one unit ------------------------------------------------------


def test_an_entity_that_spans_two_parent_rows_is_refused_for_the_natural_entity_form():
    plan = _plan(gq.CARDS_WITH_TWO, history={})
    assert any(each[0] == TABLE and each[1] == ("card_no",) for each in plan.entity_units)
    rows = gym.versions()
    rows[1] = dict(rows[1], member_id="2")  # card 101 now follows members 1 and 2
    with pytest.raises(release.ReleaseRefused) as refused:
        fr.encode_follow_rows(plan, gym.read_of(plan, gym.tables(rows)), FakeBackend())
    assert refused.value.event == "profile_release_entity_units"
    assert "101" not in str(refused.value)


def test_an_entity_that_is_the_followed_column_needs_no_check_of_the_records():
    plan = _plan(gq.MEMBERS_WITH_TWO)
    assert plan.entity_units == ()


# ---- the cap on the cells -----------------------------------------------------------------------


def test_the_queries_are_admitted_after_the_history_and_before_the_links_and_named_when_left_out(
    monkeypatch,
):
    plan = _plan(gq.PLUS_ROWS, gq.FULL_BY_TIER_QUERY, history={})
    parts = fr.follow_parts(plan, {})
    cells = {each.part.label: each.part.cells for each in parts}
    total = sum(cells.values())
    monkeypatch.setattr(selection, "MOST_RELEASE_CELLS", total - cells["query/full_by_tier"])
    kept, left = fr.admit(plan, {})
    assert dict(left) == {"query/full_by_tier": release.left_out_reason()}
    assert [each.plan.name for each in kept.queries.queries] == ["plus_rows"]
    assert kept.tables[1].plan.history is not None
    # a history that does not fit takes the queries with it: they come after it
    both = cells["query/plus_rows"] + cells["query/full_by_tier"]
    monkeypatch.setattr(selection, "MOST_RELEASE_CELLS", total - both - 1)
    kept, left = fr.admit(plan, {})
    assert {label for label, _ in left} >= {
        f"{TABLE}/history/reasons",
        "query/plus_rows",
        "query/full_by_tier",
    }
    assert kept.queries.queries == ()


def test_a_release_without_queries_holds_no_query_plan():
    plan = _plan()
    assert plan.queries is None or plan.queries.queries == ()
    parts = fr.follow_parts(plan, {})
    assert not any(each.part.kind == release.QUERY for each in parts)


# ---- the columns a query reads are read for the queries only -------------------------------------


def test_the_columns_only_a_query_reads_are_named_for_the_read_and_never_released():
    plan = _plan(gq.FULL_BY_TIER_QUERY)
    assert plan.queries.read[TABLE] == ("access", "card_no", "tier")
    assert "access" not in {each.name for each in dict(plan.reads)[TABLE]}
    drawn, _ = gym.draw(plan)
    # the request lists the column with its domain, and the query's read adds no statistic to it
    assert drawn.document["tables"][TABLE]["columns"]["access"] == {"domain": "category"}
