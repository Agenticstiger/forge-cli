# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The truncation of a table whose history or entity query is released keeps entities whole.

Under one parent row a followed table keeps at most the cap of its rows. Where the release states
an entity of the table (its history, or a query over its entities) the cap is spent on whole
entities: an entity is kept with every version it has under that parent row, or dropped with all of
them, by a keyed priority that reads only that parent's own rows. An entity with more versions than
the cap is dropped whole. A table with no such statement is truncated by row, as before.

The tables are the gym of ``_gym_data`` (members and the versions of their cards).
"""

from __future__ import annotations

import copy
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from fluid_build.profiler.dp import bounding, query_release
from fluid_build.profiler.dp import follow_release as fr
from fluid_build.profiler.dp.follows import keep_rows
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.dp import _gym_data as gym
from tests.profiler.dp import _gym_queries as gq
from tests.profiler.dp._gym_data import TABLE

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.noise")

SEED = gym.SEED
OTHER_SEED = bytes(range(60, 92))
Row = Dict[str, Optional[str]]


def _follow(cap: int) -> Tuple[str, ...]:
    return (f"member.member_id={TABLE}.member_id:{cap}",)


def _plan(cap: int, **asks: Any) -> fr.FollowReleasePlan:
    return gym.follow_plan(gym.request(history={}, **asks), follows=_follow(cap))


def _version(card: str, member: str, day: int, tier: str = "basic") -> Row:
    return {
        "card_no": card,
        "member_id": member,
        "tier": tier,
        "access": "day",
        "valid_from": f"2026-{1 + day // 28:02d}-{1 + day % 28:02d}",
        "valid_to": None,
    }


def _kept(plan: fr.FollowReleasePlan, held: Sequence[Row], seed: bytes = SEED) -> List[Row]:
    rows = gym.encode(plan, gym.tables(held))
    return [held[at] for at in fr.truncated(plan, rows, seed)[TABLE]]


def _whole_or_none(held: Sequence[Row], kept: Sequence[Row], cap: int) -> None:
    """Each entity under each member is kept with all its versions, or none, within the cap."""
    mine: Dict[Tuple[Optional[str], Optional[str]], int] = {}
    for row in held:
        key = (row["member_id"], row["card_no"])
        mine[key] = mine.get(key, 0) + 1
    got: Dict[Tuple[Optional[str], Optional[str]], int] = {}
    for row in kept:
        key = (row["member_id"], row["card_no"])
        got[key] = got.get(key, 0) + 1
    for key, total in mine.items():
        assert got.get(key, 0) in (0, total), key
        if total > cap:
            assert key not in got, key
    per_member: Dict[Optional[str], int] = {}
    for row in kept:
        per_member[row["member_id"]] = per_member.get(row["member_id"], 0) + 1
    assert all(count <= cap for count in per_member.values())


def _random_versions(rng: random.Random) -> List[Row]:
    held: List[Row] = []
    for card in range(rng.randint(3, 14)):
        member = str(1 + rng.randrange(5))
        for day in rng.sample(range(0, 200), rng.randint(1, 6)):
            held.append(_version(str(100 + card), member, day, rng.choice(("basic", "plus"))))
    return held


# ---- the rule, on keys and bytes ----------------------------------------------------------------


def test_bound_entities_keeps_an_entity_whole_or_drops_it_whole_within_the_cap():
    units: List[Optional[bytes]] = [b"u"] * 6
    entities: List[Optional[bytes]] = [b"a", b"a", b"a", b"b", b"b", b"c"]
    contents = [bytes([each]) for each in range(6)]
    kept = bounding.bound_entities(units, entities, contents, SEED, 3)
    held = {entities[at] for at in kept}
    for entity in held:
        assert {at for at, key in enumerate(entities) if key == entity} <= set(kept)
    assert 1 <= len(kept) <= 3 and kept == sorted(kept)


def test_an_entity_with_more_rows_than_the_cap_is_dropped_whole_and_the_smaller_ones_still_fit():
    units: List[Optional[bytes]] = [b"u"] * 5
    entities: List[Optional[bytes]] = [b"big"] * 3 + [b"s", b"t"]
    contents = [bytes([each]) for each in range(5)]
    for seed in (SEED, OTHER_SEED, bytes(range(32))):
        assert bounding.bound_entities(units, entities, contents, seed, 2) == [3, 4]


def test_a_row_with_no_entity_is_a_group_of_its_own():
    units: List[Optional[bytes]] = [b"u"] * 4
    entities: List[Optional[bytes]] = [None, None, None, None]
    contents = [bytes([each]) for each in range(4)]
    assert len(bounding.bound_entities(units, entities, contents, SEED, 2)) == 2


def test_the_rows_of_one_unit_are_chosen_without_reading_another_unit_or_the_order():
    rng = random.Random(41)
    for _ in range(60):
        cap = rng.randint(1, 5)
        mine = [
            (b"me", bytes([rng.randrange(4)]), rng.randbytes(3)) for _ in range(rng.randint(1, 9))
        ]
        others = [
            (f"o{rng.randrange(3)}".encode(), bytes([rng.randrange(4)]), rng.randbytes(3))
            for _ in range(12)
        ]
        alone = bounding.bound_entities(
            [row[0] for row in mine], [row[1] for row in mine], [row[2] for row in mine], SEED, cap
        )
        expect = sorted(mine[at] for at in alone)
        mixed = mine + others
        rng.shuffle(mixed)
        kept = bounding.bound_entities(
            [row[0] for row in mixed],
            [row[1] for row in mixed],
            [row[2] for row in mixed],
            SEED,
            cap,
        )
        assert sorted(row for row in (mixed[at] for at in kept) if row[0] == b"me") == expect


def test_bound_entities_refuses_what_bound_rows_refuses():
    with pytest.raises(ValueError):
        bounding.bound_entities([b"a"], [b"e"], [], SEED, 1)
    with pytest.raises(ValueError):
        bounding.bound_entities([b"a"], [], [b"x"], SEED, 1)
    with pytest.raises(ValueError):
        bounding.bound_entities([b"a"], [b"e"], [b"x"], SEED, 0)


# ---- the release: the history's table -----------------------------------------------------------


def test_the_plan_names_the_entity_of_the_history_s_table():
    assert dict(_plan(2).entity_tables) == {TABLE: ("card_no",)}
    assert dict(_plan(2, with_visits=True).entity_tables) == {TABLE: ("card_no",)}


def test_an_entity_with_more_versions_than_the_cap_is_dropped_whole_the_others_are_kept_whole():
    # cap 2: member 1 holds card 101 (2 versions) and card 102 (1), member 2 holds card 103 (3)
    plan = _plan(2)
    for seed in (SEED, OTHER_SEED):
        kept = _kept(plan, gym.versions(), seed)
        cards = [row["card_no"] for row in kept]
        assert "103" not in cards  # three versions, the cap is two
        assert cards.count("104") == 2 and cards.count("105") == 2 and cards.count("106") == 1
        # member 1 can hold card 101 (two versions) or card 102 (one), never both and never part
        assert ("101" in cards) != ("102" in cards)
        assert cards.count("101") in (0, 2) and cards.count("102") in (0, 1)


def test_the_kept_rows_and_the_document_do_not_depend_on_the_order_of_the_rows():
    plan = _plan(2)
    base = gym.versions()
    drawn, _ = gym.draw(plan, gym.tables(base))
    expected = sorted(tuple(sorted(row.items())) for row in _kept(plan, base))
    rng = random.Random(5)
    for _ in range(12):
        shuffled = list(base)
        rng.shuffle(shuffled)
        assert sorted(tuple(sorted(row.items())) for row in _kept(plan, shuffled)) == expected
        again, _ = gym.draw(plan, gym.tables(shuffled))
        assert again.canonical == drawn.canonical


def test_what_is_kept_under_a_parent_row_reads_only_that_parent_s_rows():
    plan = _plan(2)
    rng = random.Random(77)
    for _ in range(25):
        held = _random_versions(rng)
        before = _kept(plan, held)
        # every other member's rows change: their cards, their number, their dates
        other = [_version(str(900 + at), str(2 + at % 4), at) for at in range(rng.randint(1, 9))]
        mine = [row for row in held if row["member_id"] == "1"]
        theirs_changed = mine + other
        after = _kept(plan, theirs_changed)
        key = lambda row: tuple(sorted(row.items()))  # noqa: E731
        assert sorted(map(key, (r for r in before if r["member_id"] == "1"))) == sorted(
            map(key, (r for r in after if r["member_id"] == "1"))
        )


@pytest.mark.parametrize("cap", [1, 2, 3, 4, 7])
def test_no_entity_ever_appears_in_part(cap):
    plan = _plan(cap)
    for case in range(30):
        rng = random.Random(1000 * cap + case)
        held = _random_versions(rng)
        seed = bytes(rng.randrange(256) for _ in range(32))
        _whole_or_none(held, _kept(plan, held, seed), cap)


def test_a_cap_the_member_fits_under_keeps_every_version_as_before():
    plan = _plan(4)
    held = gym.versions()
    assert _kept(plan, held) == held


# ---- a query over an entity ---------------------------------------------------------------------


def _scan_contract() -> Dict[str, Any]:
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
    return contract


def _scans(per_card: Dict[str, int]) -> List[Row]:
    held: List[Row] = []
    for card, count in per_card.items():
        for _ in range(count):
            held.append(
                {
                    "scan_id": str(len(held)),
                    "card_no": card,
                    "door": ("gate", "pool")[len(held) % 2],
                }
            )
    return held


def test_a_query_by_the_followed_key_keeps_the_rows_of_a_parent_by_row_as_before():
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
        asked,
        contract_=_scan_contract(),
        follows=(gym.FOLLOW, f"{TABLE}.card_no=card_scan.card_no:2"),
    )
    (answered,) = plan.queries.queries
    assert answered.form == query_release.FORM_FOLLOWED
    # a group of a followed relationship is the rows of one parent row and leaves with it whole:
    # no entity grouping, so the rows kept under a parent are the cap's worth, as for any table
    assert plan.entity_tables == ()
    # card 101 has three scans (more than the cap of two), 102 has two, 103 one
    held = _scans({"101": 3, "102": 2, "103": 1})
    tables = {**gym.tables(), "card_scan": held}
    rows = gym.encode(plan, tables)
    kept = fr.truncated(plan, rows, SEED)["card_scan"]
    cards = [held[at]["card_no"] for at in kept]
    assert cards.count("101") == 2 and cards.count("102") == 2 and cards.count("103") == 1


def test_a_query_over_the_entities_of_a_history_table_is_planned_with_the_history_entity():
    plan = gym.follow_plan(gq.asked(gq.CARDS_WITH_TWO, history={}), follows=_follow(2))
    (answered,) = plan.queries.queries
    assert answered.form == query_release.FORM_HISTORY
    assert dict(plan.entity_tables) == {TABLE: ("card_no",)}
    held = gym.versions()
    cards = [row["card_no"] for row in _kept(plan, held)]
    assert "103" not in cards and cards.count("104") == 2


# ---- a table with no entity statement is truncated by row, as before ----------------------------


def test_a_table_with_no_history_or_entity_query_is_truncated_by_row_as_before():
    plan = gym.follow_plan(gym.request(history=None, with_visits=True), follows=_follow(2))
    assert plan.entity_tables == ()
    held = gym.versions()
    rows = gym.encode(plan, gym.tables(held))
    kept = fr.truncated(plan, rows, SEED)
    # the row-level reference: the c smallest keyed rows of each member's rows
    keys = rows.keys[(TABLE, ("member_id",))]
    contents = rows.contents[TABLE]
    expected: List[int] = []
    for key in dict.fromkeys(keys):
        at = [index for index, each in enumerate(keys) if each == key]
        chosen = bounding.bound_rows(
            [keys[i] for i in at],
            [contents[i] for i in at],
            SEED,
            2,
            label=plan.follows.edges[0].label,
        )
        expected.extend(at[each] for each in chosen)
    assert kept[TABLE] == sorted(expected)


def test_keep_rows_without_entity_keys_is_the_row_rule_and_with_them_the_entity_rule():
    plan = gym.follow_plan(gym.request(history={}), follows=_follow(2)).follows
    counts = {"member": 1, TABLE: 4}
    contents = {"member": [b"m"], TABLE: [bytes([each]) for each in range(4)]}
    parents = {TABLE: [b"m"]}
    follows: Dict[str, Sequence[Optional[bytes]]] = {TABLE: [b"m"] * 4}
    entities: Dict[str, Sequence[Optional[bytes]]] = {TABLE: [b"x", b"x", b"x", b"y"]}
    by_row = keep_rows(plan, counts, contents, parents, follows, SEED)
    assert len(by_row[TABLE]) == 2
    by_entity = keep_rows(plan, counts, contents, parents, follows, SEED, entity_keys=entities)
    assert by_entity[TABLE] == [3]  # x has three rows, the cap is two


def test_the_document_is_read_by_the_engine_and_names_only_whole_entities():
    plan = _plan(2)
    drawn, _ = gym.draw(plan)
    FakeBackend().check_document(drawn.canonical.decode("utf-8"))
    assert gym.history_of(drawn)["entity"] == ["card_no"]
