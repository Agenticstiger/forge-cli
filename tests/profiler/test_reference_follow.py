# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A history follows a foreign key by the keys of the public table it references.

The records are a library's members' cards, each version naming a branch. The branch table is
public, so its keys are the levels of the card's ``branch_id``. The published cells of a key's
parent are also floors on the parent's children, which the guard of the relationship's children
per parent reads; a brute-force reader checks that no withheld bin can be worked out from them.
"""

from __future__ import annotations

import itertools
import json
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from fluid_build.profiler import disclosure, history
from fluid_build.profiler.runner import CHILDREN_EDGES
from tests.profiler import _library as library

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.profile")
from flux_stats.profile import load_profile_text  # noqa: E402

RULE = disclosure.ACRO_DEFAULT
BRANCHES = library.keys(12)


def _history(result: Any) -> Dict[str, Any]:
    found: Dict[str, Any] = result.document["tables"]["card"]["history"]
    return found


def _tracked(result: Any) -> Dict[str, Any]:
    columns: Dict[str, Any] = _history(result).get("columns", {})
    return columns


def _said(result: Any, name: str = "branch_id") -> str:
    return dict(result.not_measured).get(f"history of card.{name}", "")


def _twice(members: Sequence[Sequence[Any]]) -> List[List[Any]]:
    """Each member holds two versions at the branch it starts at: the children are twice the cells."""
    return [[*chain, *chain] for chain in members]


def _spread(keys: Sequence[Any] = BRANCHES, per: int = 20) -> List[List[Any]]:
    return library.spread(keys, [per] * len(keys))


# -- the levels ----------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["string", "uuid"])
def test_the_levels_are_the_published_keys_in_their_order(tmp_path, kind):
    keys = library.keys(12, kind="uuid" if kind == "uuid" else "string")
    result = library.profile(
        tmp_path,
        library.contract(key_type=kind),
        library.branches(list(reversed(keys))),
        library.cards(_spread(keys)),
    )
    published = [row[0] for row in result.document["references"]["branch"]["rows"]]
    follow = _tracked(result)["branch_id"]
    assert published == sorted(keys)
    assert follow["initial"]["levels"] == published
    assert follow["transitions"]["levels"] == published
    assert follow["transitions"]["groupBy"] == "branch_id"
    load_profile_text(result.canonical.decode("utf-8"))  # the registry reads the history back


def test_a_key_that_moves_is_counted_by_its_transitions(tmp_path):
    result, _ = _run(tmp_path, MOVING)
    follow = _tracked(result)["branch_id"]
    groups = {group["group"]: group for group in follow["transitions"]["groups"]}
    assert set(groups) == set(BRANCHES)


def test_the_published_cells_of_a_key_are_its_entities_by_their_first_branch(tmp_path):
    keys = library.keys(12)
    per = [30, 25, 40, 35, 50, 45, 60, 55, 70, 65, 80, 75]
    result = library.profile(
        tmp_path,
        library.contract(),
        library.branches(keys),
        library.cards(library.spread(keys, per)),
    )
    # every key's cell would be a floor on its parent's children: the guard withholds the lot
    (tmp_path / "kept").mkdir()
    assert _tracked(result)["branch_id"]["initial"]["groups"] == [{"group": "", "status": "C"}]
    kept, size = library.keys(4), [45, 30, 0, 30]
    (shown,) = _tracked(
        library.profile(
            tmp_path / "kept",
            library.contract(),
            library.branches(kept),
            library.cards(_twice(library.spread(kept, size))),
        )
    )["branch_id"]["initial"]["groups"]
    for published, true in zip(shown["levelCounts"], size, strict=True):
        assert published is None or published == round(true / 5) * 5


# -- what is not followed -----------------------------------------------------------------------


def test_a_value_that_is_no_key_is_named_and_the_column_is_not_followed(tmp_path):
    members = _spread()
    members.append(["zz-missing"])
    result = library.profile(
        tmp_path, library.contract(), library.branches(BRANCHES), library.cards(members)
    )
    assert "branch_id" not in _tracked(result)
    assert "not a key of the table it references" in _said(result)
    assert "zz-missing" not in " ".join(why for _, why in result.not_measured)


def test_a_null_foreign_key_is_followed_and_counted_in_no_level(tmp_path):
    keys = library.keys(4)
    members = _twice(library.spread(keys, [45, 30, 0, 30])) + [[None] for _ in range(15)]
    result = library.profile(
        tmp_path, library.contract(), library.branches(keys), library.cards(members)
    )
    follow = _tracked(result)["branch_id"]
    (group,) = follow["initial"]["groups"]
    assert group["status"] == "F"
    assert group["k"] == 105  # the entities with a branch: the 15 with none are in no level


def test_a_composite_foreign_key_is_not_followed_and_its_table_is_not_published(tmp_path):
    result = library.profile(
        tmp_path,
        library.contract(composite=True),
        [{**row, "region": "north"} for row in library.branches(BRANCHES)],
        library.cards(_spread(), region="north"),
    )
    assert "references" not in result.document
    assert "branch_id" not in _tracked(result)
    assert "reference table branch" in dict(result.not_measured)


def test_a_numeric_key_is_published_and_its_foreign_key_is_named_not_followed(tmp_path):
    keys = library.keys(12, kind="integer")
    result = library.profile(
        tmp_path,
        library.contract(key_type="integer"),
        library.branches(keys),
        library.cards(_spread(keys)),
    )
    assert "branch" in result.document["references"]
    assert "branch_id" not in _tracked(result)
    assert "categorical" in _said(result)


def test_a_table_that_is_not_public_is_not_followed(tmp_path):
    result = library.profile(
        tmp_path,
        library.contract(classification="Internal"),
        library.branches(BRANCHES),
        library.cards(_spread()),
    )
    assert "references" not in result.document
    assert "branch_id" not in _tracked(result)
    assert _said(result) == "the table it references is not declared public"


def test_a_table_over_the_cap_is_not_followed_and_the_note_holds_no_count(tmp_path):
    keys = library.keys(65)
    result = library.profile(
        tmp_path,
        library.contract(),
        library.branches(keys),
        library.cards(library.spread(keys, [1] * 65)),
    )
    assert "branch_id" not in _tracked(result)
    assert "more rows" in _said(result)
    assert "65" not in _said(result) and "64" not in _said(result)


def test_a_table_with_one_key_is_not_followed(tmp_path):
    result = library.profile(
        tmp_path,
        library.contract(),
        library.branches(BRANCHES[:1]),
        library.cards(_spread(BRANCHES[:1], 40)),
    )
    assert "branch_id" not in _tracked(result)
    assert "fewer than two keys" in _said(result)


# -- the guard and a brute-force reader --------------------------------------------------------
#
# The reader below reads the document and nothing else: no row, no count the profiler formed, and
# none of the code of the guard. It is what a person holding the profile and the contract does.
# From the published ``initial`` and ``transitions`` of the foreign key it forms, for each key, the
# child rows that key's parent holds at least: its first-version cell and the changes into it, the
# size of the group of the changes out of it, and (where one cell of a group is withheld) the
# group's size less the most the published cells can hold. Then it lists every table of children
# per parent that fits what the relationship publishes and those floors, and works out a withheld
# bin where every such table agrees on it.


def _band(published: int) -> Tuple[int, int]:
    found = disclosure.sample_size(published, RULE)
    return found["min"], found["max"]


def _floors(document: Dict[str, Any], size: int, *, outgoing: bool = True) -> List[int]:
    """The child rows each key's parent holds at least, from what the foreign key publishes."""
    follow = document["tables"]["card"]["history"]["columns"]["branch_id"]
    levels = follow["transitions"]["levels"]
    rows, changes = [0] * size, [0] * size

    def lone(group: Dict[str, Any], skip: Optional[int]) -> Optional[Tuple[int, int]]:
        cells = group["levelCounts"]
        hidden = [at for at, count in enumerate(cells) if count is None and at != skip]
        if len(hidden) != 1 or not isinstance(group.get("k"), int):
            return None
        shown = sum(_band(c)[1] for at, c in enumerate(cells) if c is not None and at != skip)
        return hidden[0], max(_band(group["k"])[0] - shown, RULE.minimum_cell)

    (initial,) = follow["initial"]["groups"]
    if initial["status"] == "F":
        for at, count in enumerate(initial["levelCounts"]):
            if count is not None:
                rows[at] += _band(count)[0]
        if (found := lone(initial, None)) is not None:
            rows[found[0]] += found[1]
    for group in follow["transitions"]["groups"]:
        if group["status"] != "F":
            continue
        source = levels.index(group["group"])
        summed = 0
        for at, count in enumerate(group["levelCounts"]):
            if count is not None and at != source:
                rows[at] += _band(count)[0]
                summed += _band(count)[0]
        if (found := lone(group, source)) is not None:
            rows[found[0]] += found[1]
            summed += found[1]
        if outgoing:
            changes[source] = max(summed, _band(group["k"])[0])
    return [max(held, left) for held, left in zip(rows, changes, strict=True)]


def _tables(
    parents: int,
    totals: Sequence[int],
    published: Sequence[Optional[int]],
    floors: Sequence[int],
) -> List[Tuple[int, ...]]:
    """Every table of children per parent a reader can hold true, by brute force.

    Cells are non-negative and sum to the parents; a published cell lies in its band; the parents
    can be matched to bins so that each holds at least its floor; the children are at least what
    the bins' lower edges and the floors give, and at most what their upper edges give; and the
    withheld cells together are not a small count (the check's own rule). The children's total is
    one of *totals*: the exact count, or each count the document's rounded ``rowCount`` allows. A
    floor above its bin's lower edge uses up more of the total, so it bounds the other parents.
    """
    lower = list(CHILDREN_EDGES[:-1])
    tops = [edge - 1 for edge in CHILDREN_EDGES[1:-1]] + [None]
    ranked = sorted(floors, reverse=True)
    found: List[Tuple[int, ...]] = []

    def holds(table: Tuple[int, ...]) -> bool:
        slots: List[int] = []
        for at in range(len(table) - 1, -1, -1):
            slots += [lower[at]] * table[at]
        least = sum(max(slot, floor) for slot, floor in zip(slots, ranked, strict=False)) + sum(
            slots[len(ranked) :]
        )
        most = sum((top or 0) * value for top, value in zip(tops, table, strict=True))
        if not any(least <= total and (table[-1] > 0 or most >= total) for total in totals):
            return False
        for at, edge in enumerate(lower):
            if sum(table[at:]) < sum(1 for floor in floors if floor >= edge):
                return False
        if any(shown is not None for shown in published):
            rest = sum(
                value for value, shown in zip(table, published, strict=True) if shown is None
            )
            if not disclosure.check_count(rest, RULE).published:
                return False
        return True

    def walk(prefix: Tuple[int, ...], left: int) -> None:
        at = len(prefix)
        if at == len(lower):
            if left == 0 and holds(prefix):
                found.append(prefix)
            return
        shown = published[at]
        low, high = (0, left) if shown is None else (_band(shown)[0], min(_band(shown)[1], left))
        for value in range(low, high + 1):
            walk((*prefix, value), left - value)

    walk((), parents)
    return found


def _single_versions(document: Dict[str, Any]) -> bool:
    """Whether the document says each entity holds one version: one published bin, ``[1, 2)``."""
    entry = document["tables"]["card"]["history"]["versionsPerEntity"]
    (group,) = entry["groups"]
    return bool(
        group["status"] == "F" and entry["bins"] == ["1", "2"] and len(group["binCounts"]) == 1
    )


def _first_cells_published(document: Dict[str, Any]) -> bool:
    """Whether the first cells are published, for a table small enough to search key by key."""
    follow = document["tables"]["card"]["history"]["columns"]["branch_id"]
    (initial,) = follow["initial"]["groups"]
    return bool(initial["status"] == "F" and len(follow["initial"]["levels"]) <= 6)


def _single_version_tables(
    document: Dict[str, Any],
    totals: Sequence[int],
    published: Sequence[Optional[int]],
) -> List[Tuple[int, ...]]:
    """Every table of children per parent a reader can hold true over one version an entity.

    A key's children are then its entities, so each published first-version cell bounds its
    parent's children from both sides, and the children's total is the entities' total: one of
    *totals* that the ``initial`` size's band also allows. A table is held true by an assignment
    of the keys to its bins in which each key's range meets its bin and the children can sum to
    the total. (Ported from a brute-force search over the keys' assignments, which is what a
    reader with these upper bounds does.)
    """
    lower = list(CHILDREN_EDGES[:-1])
    tops = [edge - 1 for edge in CHILDREN_EDGES[1:-1]] + [10**9]
    (initial,) = document["tables"]["card"]["history"]["columns"]["branch_id"]["initial"]["groups"]
    cells = initial["levelCounts"]
    ranges = [_band(cell) if cell is not None else (0, 10**9) for cell in cells]
    low, high = _band(initial["k"])
    sums = [total for total in totals if low <= total <= high]
    found = set()
    for assignment in itertools.product(range(len(lower)), repeat=len(ranges)):
        table = tuple(assignment.count(at) for at in range(len(lower)))
        if table in found:
            continue
        spans = [
            (max(floor, lower[at]), min(ceiling, tops[at]))
            for at, (floor, ceiling) in zip(assignment, ranges, strict=True)
        ]
        if any(least > most for least, most in spans):
            continue  # a key that meets no value of its bin
        if not any(sum(l for l, _ in spans) <= total <= sum(m for _, m in spans) for total in sums):
            continue
        if any(
            shown is not None and not _band(shown)[0] <= value <= _band(shown)[1]
            for value, shown in zip(table, published, strict=True)
        ):
            continue
        if any(shown is not None for shown in published):
            rest = sum(v for v, shown in zip(table, published, strict=True) if shown is None)
            if not disclosure.check_count(rest, RULE).published:
                continue
        found.add(table)
    return sorted(found)


def _row_count_totals(document: Dict[str, Any]) -> List[int]:
    """Every count of child rows the document's rounded ``rowCount`` leaves open."""
    low, high = _band(document["tables"]["card"]["rowCount"])
    return list(range(low, high + 1))


def _derivable(
    result: Any,
    rows: Sequence[Dict[str, Any]],
    keys: Sequence[str],
    *,
    outgoing: bool = True,
    banded: bool = False,
    total: Optional[Sequence[int]] = None,
) -> List[int]:
    """The withheld bins of the relationship's cells a reader can work out, by brute force.

    The reader knows the children's total exactly, or with *banded* only as the document's rounded
    ``rowCount`` gives it (any count in the band). *total* sets the counts it takes outright.
    """
    document = result.document
    (group,) = document["relationships"][0]["statistics"][0]["groups"]
    published: List[Optional[int]] = (
        list(group["binCounts"]) if group["status"] == "F" else [None] * (len(CHILDREN_EDGES) - 1)
    )
    held = [row["branch_id"] for row in rows if row["branch_id"] is not None]
    per_key = [held.count(key) for key in keys]
    floors = _floors(document, len(keys), outgoing=outgoing)
    totals = total or (_row_count_totals(document) if banded else [len(held)])
    tables = _tables(len(keys), totals, published, floors)
    if _single_versions(document) and _first_cells_published(document):
        tables = _single_version_tables(document, totals, published)
    bare = _tables(len(keys), totals, published, [0] * len(keys))
    true = [0] * (len(CHILDREN_EDGES) - 1)
    for count in per_key:
        true[min(max(i for i, edge in enumerate(CHILDREN_EDGES[:-1]) if edge <= count), 6)] += 1
    assert tuple(true) in tables, "the reader's model must hold the true table"

    def fixed(listed: Sequence[Tuple[int, ...]]) -> List[int]:
        return [
            at
            for at, shown in enumerate(published)
            if shown is None and len({table[at] for table in listed}) == 1
        ]

    # what the relationship alone gives away is not the guard's to keep: only what a floor adds
    already = fixed(bare)
    return [at for at in fixed(tables) if at not in already]


def _run(tmp_path: Path, members: Sequence[Sequence[Any]]) -> Tuple[Any, List[Dict[str, Any]]]:
    rows = library.cards(members)
    return library.profile(tmp_path, library.contract(), library.branches(BRANCHES), rows), rows


def _random_members(rng: random.Random, keys: Sequence[str]) -> List[List[str]]:
    members: List[List[str]] = []
    for at, key in enumerate(keys):
        for _ in range(rng.choice([0, 0, 3, 12, 20, 30, 45])):
            chain = [key]
            for _ in range(rng.choice([1, 1, 2, 3]) - 1):
                chain.append(keys[rng.randrange(len(keys))] if rng.random() < 0.7 else chain[-1])
            members.append(chain)
    return members


#: Every branch holds 20 members: each key's first-version cell is published, every parent holds 20
#: child rows, and the children per parent have one bin (20 or more).
GUARDED = _spread()
#: Three branches of twenty members each: the first cell is withheld, and the group's size less the
#: two cells it publishes is at least ten, so every parent is known to hold ten rows.
THREE_BY_20 = library.spread(library.keys(3), [20] * 3)
FOUR_BY_25 = library.spread(library.keys(4), [25] * 4)
TWELVE_BY_40 = _spread(per=40)
#: Ten branches of thirty members, each moving on to one of two sinks that start with twelve:
#: the changes out of every branch show twenty-eight rows of its parent.
SINK = (
    [[BRANCHES[n], BRANCHES[10 + (m % 2)]] for n in range(10) for m in range(30)]
    + [[BRANCHES[10]]] * 12
    + [[BRANCHES[11]]] * 12
)
#: Six branches hold 25 members and six hold 3: the small keys' cells are withheld.
UNGUARDED = library.spread(BRANCHES, [25] * 6 + [3] * 6)
#: Ten members move from each branch to the next: the cells of the changes are published too.
MOVING = [
    [BRANCHES[n], BRANCHES[(n + 1 + step) % 12]]
    for n in range(12)
    for step in range(3)
    for _ in range(10)
]
#: Every member starts at the first branch and moves on through another.
STAR = [
    chain
    for step in range(1, 12)
    for chain in (
        *([[BRANCHES[0], BRANCHES[step], BRANCHES[0]]] * 20),
        *([[BRANCHES[0], BRANCHES[step], BRANCHES[step % 11 + 1]]] * 20),
    )
]


#: One member holds one card, so a branch's first-version cell is its parent's children, and the
#: cell that is published bounds every other parent from above through the children's total: with
#: the first branch holding at least 43 of the 59 to 62 rows, no other holds more than 19. Each is
#: a draw of a seeded search (two to five branches, one version a member) that gave a bin back.
SINGLE_VERSION = {
    "single-2-0-12-45": [2, 0, 12, 45],
    "single-45-0-0-12": [45, 0, 0, 12],
    "single-30-1-0-10": [30, 1, 0, 10],
    "single-5-10-12-30": [5, 10, 12, 30],
    "single-45-12-10-0": [45, 12, 10, 0],
}
#: A draw that only a reader told the exact row count (and the number of entities) works a bin out
#: of: the document's rounded ``rowCount`` leaves it nothing, so it is held to the readers below.
SINGLE_VERSION_EXACT_COUNT = {"single-15-30-0-10-3": [15, 30, 0, 10, 3]}
#: Single versions whose first cells floor no parent beyond what the total holds: published.
SINGLE_VERSION_PUBLISHED = {"single-0-0-20-30": [0, 0, 20, 30]}


def _trial_85() -> Tuple[List[str], List[List[str]]]:
    """The 86th draw of a seeded search: its changes out of a key alone floor four parents."""
    rng = random.Random(2)
    for _ in range(86):
        keys = library.keys(rng.randint(2, 6))
        members = _random_members(rng, keys)
    return keys, members


def _pinned_members() -> Dict[str, Tuple[List[str], List[List[Any]]]]:
    keys85, members85 = _trial_85()
    return {
        "guarded": (BRANCHES, GUARDED),
        "3x20": (library.keys(3), THREE_BY_20),
        "4x25": (library.keys(4), FOUR_BY_25),
        "12x40": (BRANCHES, TWELVE_BY_40),
        "sink": (BRANCHES, SINK),
        "moving": (BRANCHES, MOVING),
        "star": (BRANCHES, STAR),
        "trial-85": (keys85, members85),
        **{
            name: (library.keys(len(per)), library.spread(library.keys(len(per)), per))
            for name, per in {
                **SINGLE_VERSION,
                **SINGLE_VERSION_EXACT_COUNT,
                **SINGLE_VERSION_PUBLISHED,
            }.items()
        },
    }


PINNED = sorted(_pinned_members())


def _profile_of(tmp_path: Path, case: str) -> Tuple[Any, List[Dict[str, Any]], List[str]]:
    keys, members = _pinned_members()[case]
    rows = library.cards(members)
    result = library.profile(tmp_path, library.contract(), library.branches(keys), rows)
    return result, rows, keys


def _published_numbers(result: Any) -> List[str]:
    """What the foreign key's ``initial`` and ``transitions`` write, apart from a group's name."""
    follow = _tracked(result)["branch_id"]
    found: List[str] = []
    for part in ("initial", "transitions"):
        entry = follow[part]
        if entry["sampleSize"] != {"min": 0}:
            found.append(f"{part} sampleSize")
        for group in entry["groups"]:
            found.extend(f"{part} {name}" for name in group if name not in ("group", "status"))
            if group["status"] == "F":
                found.append(f"{part} published group")
    return found


@pytest.mark.parametrize("case", PINNED)
@pytest.mark.parametrize("banded", [False, True], ids=["exact-total", "row-count-band"])
def test_a_reader_forming_each_keys_floor_cannot_work_out_a_withheld_bin_of_children(
    tmp_path, case, banded
):
    if case in SINGLE_VERSION_EXACT_COUNT and not banded:
        pytest.skip("a reader told the row count exactly knows more than the document says")
    result, rows, keys = _profile_of(tmp_path, case)
    assert _derivable(result, rows, keys, banded=banded) == []


@pytest.mark.parametrize("case", sorted(SINGLE_VERSION))
def test_a_published_first_cell_does_not_bound_the_other_parents_from_above(tmp_path, case):
    result, _rows, _keys = _profile_of(tmp_path, case)
    follow = _tracked(result)["branch_id"]
    # what the reader would pin from a cell that is published is withheld with it, whole
    assert follow["initial"]["groups"] == [{"group": "", "status": "C"}]
    assert _published_numbers(result) == []


@pytest.mark.parametrize("case", ["3x20", "4x25", "12x40", "sink", "trial-85"])
def test_where_the_floors_pin_a_bin_the_foreign_key_writes_no_number_for_its_shares(tmp_path, case):
    result, _rows, _keys = _profile_of(tmp_path, case)
    assert _published_numbers(result) == []
    follow = _tracked(result)["branch_id"]
    assert follow["initial"]["groups"] == [{"group": "", "status": "C"}]
    assert {group["status"] for group in follow["transitions"]["groups"]} == {"C"}
    # the column is still followed, with its levels, and the table it follows is published whole
    assert follow["initial"]["levels"] == follow["transitions"]["levels"]
    assert "branch" in result.document["references"]


@pytest.mark.parametrize("seed", range(8))
def test_no_withheld_bin_is_derivable_in_a_seeded_search(tmp_path, seed):
    rng = random.Random(seed)
    for trial in range(25):
        keys = library.keys(rng.randint(2, 6))
        members = _random_members(rng, keys)
        if not members:
            continue
        folder = tmp_path / str(trial)
        folder.mkdir()
        rows = library.cards(members)
        result = library.profile(folder, library.contract(), library.branches(keys), rows)
        if "branch_id" in _tracked(result):
            assert _derivable(result, rows, keys) == [], (seed, trial)


def _initial_of(result: Any) -> Dict[str, Any]:
    return _tracked(result)["branch_id"]["initial"]  # type: ignore[no-any-return]


def test_without_the_guard_the_same_reader_works_a_bin_out(tmp_path, monkeypatch):
    result, rows, keys = _profile_of(tmp_path, "3x20")
    assert _derivable(result, rows, keys) == []
    monkeypatch.setattr(history, "_guarded", lambda *args: False)
    (tmp_path / "off").mkdir()
    free, free_rows, _ = _profile_of(tmp_path / "off", "3x20")
    assert _derivable(free, free_rows, keys) != []


#: What ``fluid profile`` wrote for this foreign key before the guard read the changes out of a key
#: and a group's size (commit f6490a863): the guard changes none of it where no bin is pinned. Each
#: member holds two versions at its branch, so the children total leaves room for every parent.
NO_PIN = {
    "initial": {
        "definition": "levelHistogram/1",
        "disclosure": "acro-default",
        "groups": [
            {
                "cellStatus": ["F", "F", "C", "C"],
                "group": "",
                "k": 105,
                "levelCounts": [45, 30, None, None],
                "status": "F",
            }
        ],
        "levels": ["b01", "b02", "b03", "b04"],
        "sampleSize": {"max": 107, "min": 103},
    },
    "transitions": {
        "definition": "levelHistogram/1",
        "disclosure": "acro-default",
        "groupBy": "branch_id",
        "groups": [{"group": key, "status": "C"} for key in ("b01", "b02", "b03", "b04")],
        "levels": ["b01", "b02", "b03", "b04"],
        "sampleSize": {"max": 9, "min": 0},
    },
}


def test_the_guard_leaves_a_foreign_key_alone_where_the_floors_pin_no_bin(tmp_path):
    # Two branches show their cells; the other two (one of them empty) are withheld together, so
    # no parent is known to hold a row and no bin of the children is given back.
    keys = library.keys(4)
    members = _twice(library.spread(keys, [45, 30, 0, 30]))
    rows = library.cards(members)
    result = library.profile(tmp_path, library.contract(), library.branches(keys), rows)
    assert _derivable(result, rows, keys) == []
    assert _derivable(result, rows, keys, banded=True) == []
    assert json.loads(json.dumps(_tracked(result)["branch_id"])) == NO_PIN


def test_a_first_cell_the_total_leaves_room_around_is_still_published(tmp_path):
    result, rows, keys = _profile_of(tmp_path, "single-0-0-20-30")
    (shown,) = _tracked(result)["branch_id"]["initial"]["groups"]
    assert shown["status"] == "F" and shown["levelCounts"][3] == 30
    assert _derivable(result, rows, keys) == []
    assert _derivable(result, rows, keys, banded=True) == []


def test_the_same_cells_over_one_version_each_are_withheld_for_the_total_they_leave(tmp_path):
    """The golden above over one version a member: 105 rows, which four parents cannot all fill.

    The first two cells floor two parents at 43 and 28, and the group's size less those cells
    floors a third at 24; a fourth with 20 or more would need 115 rows of the 103 to 107 the
    document allows, so the guard reads the 20-or-more bin as pinned at three and withholds the
    entry whole. (It places the withheld mass on one key, which errs toward withholding: the
    reader below, which places it nowhere, finds nothing here.) Over two versions a member there
    are twice the rows, room for all four parents, and the cells are published as they were.
    """
    keys = library.keys(4)
    rows = library.cards(library.spread(keys, [45, 30, 0, 30]))
    result = library.profile(tmp_path, library.contract(), library.branches(keys), rows)
    assert _tracked(result)["branch_id"]["initial"]["groups"] == [{"group": "", "status": "C"}]
    assert _published_numbers(result) == []
    assert _derivable(result, rows, keys) == []
    assert _derivable(result, rows, keys, banded=True) == []


def test_the_guard_changes_no_other_table_and_the_document_reads_back(tmp_path):
    result, _ = _run(tmp_path, GUARDED)
    read, _text = load_profile_text(result.canonical.decode("utf-8"))
    assert "branch" in read.references
    assert json.loads(result.canonical)["references"]["branch"]["columns"] == ["branch_id", "city"]


# -- mutants -------------------------------------------------------------------------------------


SINGLE_CASES = sorted(SINGLE_VERSION)


def _leaks(tmp_path: Path, cases: Optional[Sequence[str]] = None, **reads: Any) -> bool:
    """Whether the reader works a bin out of any case: the exact-count case needs a banded one."""
    cases = cases or [
        case for case in PINNED if reads.get("banded") or case not in SINGLE_VERSION_EXACT_COUNT
    ]
    for number, case in enumerate(cases):
        folder = tmp_path / str(number)
        folder.mkdir()
        result, rows, keys = _profile_of(folder, case)
        if _derivable(result, rows, keys, **reads):
            return True
    return False


def test_the_reader_leaks_nothing_of_the_real_guard(tmp_path):
    assert not _leaks(tmp_path)
    (tmp_path / "band").mkdir()
    assert not _leaks(tmp_path / "band", banded=True)


def test_mutant_the_guard_withholds_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_guarded", lambda *args: False)
    assert _leaks(tmp_path)


def test_mutant_the_guard_knows_no_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(disclosure, "parents_at_or_above", lambda lower, floors: [0] * len(lower))
    assert _leaks(tmp_path)


def _entry(levels: Sequence[str], groups: Sequence[Dict[str, Any]], by: bool = False) -> Any:
    entry: Dict[str, Any] = {"levels": list(levels), "groups": list(groups)}
    if by:
        entry["groupBy"] = "branch_id"
    return entry


def _withheld_groups(levels: Sequence[str], skip: Sequence[str] = ()) -> List[Dict[str, Any]]:
    return [{"group": level, "status": "C"} for level in levels if level not in skip]


def test_a_group_that_withholds_a_cell_and_shows_its_size_floors_that_cell() -> None:
    levels = ["b01", "b02", "b03"]
    initial = _entry(
        levels,
        [{"group": "", "k": 60, "levelCounts": [None, 20, 20], "status": "F"}],
    )
    transitions = _entry(levels, _withheld_groups(levels), by=True)
    # the size's band starts at 58 and each shown cell holds at most 22: 14 rows are in the third
    assert history._published_floors(initial, transitions, RULE) == [14, 18, 18]


def test_a_change_out_of_a_key_floors_its_parent_by_the_group_size() -> None:
    levels = ["b01", "b02", "b03"]
    initial = _entry(levels, [{"group": "", "status": "C"}])
    group = {"group": "b01", "k": 30, "levelCounts": [0, 10, None], "status": "F"}
    transitions = _entry(levels, [group, *_withheld_groups(levels, skip=["b01"])], by=True)
    # 28 rows leave b01, 10 arrive at b02, and the size less a shown cell (at most 12) is in b03
    assert history._published_floors(initial, transitions, RULE) == [28, 10, 16]


def test_mutant_the_guard_drops_the_changes_out_of_a_key(monkeypatch):
    monkeypatch.setattr(history, "_changes_out", lambda group, summed, rule: 0)
    with pytest.raises(AssertionError):
        test_a_change_out_of_a_key_floors_its_parent_by_the_group_size()


def test_mutant_the_guard_drops_the_complement_of_a_withheld_cell(monkeypatch):
    monkeypatch.setattr(history, "_hidden_mass", lambda group, skip, rule: ([], 0))
    with pytest.raises(AssertionError):
        test_a_group_that_withholds_a_cell_and_shows_its_size_floors_that_cell()


def test_mutant_a_cell_is_withheld_and_its_group_still_published(tmp_path, monkeypatch):
    """The old guard: withhold the published cell whose key sits highest, publish the rest."""
    original = history._initial

    def by_cells(context, column, stations, *, whole=False):
        counts = history._initial_counts(column, stations)
        highest = max(range(len(counts)), key=lambda at: (counts[at], -at))
        return history._levels_entry(
            context,
            f"{context.expose_id}.{column.name} initial",
            column.levels,
            counts,
            withheld=frozenset({highest}),
        )

    assert original is not by_cells
    monkeypatch.setattr(history, "_guarded", lambda *args: False)
    monkeypatch.setattr(history, "_initial", by_cells)
    assert _leaks(tmp_path, ["3x20", "4x25", "12x40"])


def _no_floors(lower: Sequence[int], table: Sequence[int], ranked: Sequence[int]) -> int:
    return sum(edge * value for edge, value in zip(lower, table, strict=True))


def _smallest_first(lower: Sequence[int], table: Sequence[int], ranked: Sequence[int]) -> int:
    slots: List[int] = []
    for edge, value in reversed(list(zip(lower, table, strict=True))):
        slots += [edge] * value
    paired = sum(max(slot, floor) for slot, floor in zip(slots, reversed(ranked), strict=False))
    return paired + sum(slots[len(ranked) :])


EDGES = list(CHILDREN_EDGES[:-1])


def test_the_least_children_pair_the_largest_floors_with_the_highest_bins() -> None:
    # two parents in the top bin (20+) and one in the bin of 0: floors 43, 28 and 5
    table = [1, 0, 0, 0, 0, 0, 2]
    assert disclosure._least_children(EDGES, table, [43, 28, 5]) == 43 + 28 + 5
    assert disclosure._least_children(EDGES, table, [43]) == 43 + 20 + 0
    assert disclosure._least_children(EDGES, table, []) == 40
    # a floor under its bin's lower edge adds nothing; one above uses up more of the total
    assert disclosure._least_children(EDGES, table, [3, 3, 3]) == 20 + 20 + 3


def test_a_floor_above_its_bins_edge_bounds_the_other_parents_from_above() -> None:
    # four parents, 62 children, one of them known to hold 43: a second with 20 or more would
    # need 63, so the floor gives a withheld cell back that the children alone did not
    table = [1, 0, 1, 0, 0, 0, 2]
    shown = [False] * 7
    floors = [43, 0, 0, 0]
    known = disclosure.parents_at_or_above(EDGES, floors)
    base = disclosure.pinned_children_cells(EDGES, table, shown, 62, RULE)
    assert disclosure.pinned_children_cells(EDGES, table, shown, 62, RULE, 0, None, known) == base
    assert (
        disclosure.pinned_children_cells(EDGES, table, shown, 62, RULE, 0, None, known, floors)
        - base
    )


def test_mutant_the_floors_use_up_none_of_the_childrens_total(tmp_path, monkeypatch):
    monkeypatch.setattr(disclosure, "_least_children", _no_floors)
    assert _leaks(tmp_path, SINGLE_CASES, banded=True)
    with pytest.raises(AssertionError):
        test_the_least_children_pair_the_largest_floors_with_the_highest_bins()


def test_mutant_the_floors_are_paired_with_the_bins_smallest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(disclosure, "_least_children", _smallest_first)
    with pytest.raises(AssertionError):
        test_the_least_children_pair_the_largest_floors_with_the_highest_bins()
    # a pairing that wastes the total withholds what the real guard publishes
    with pytest.raises(AssertionError):
        test_a_first_cell_the_total_leaves_room_around_is_still_published(tmp_path)


def _total_shifted(by: int, *, band: Optional[int] = None):
    """A guard that reads the children's total off by *by*, or at an end of its rounded band."""
    original = disclosure.pinned_children_cells

    def shifted(lower, cells, published, children, rule, *rest):
        total = children + by if band is None else _band(children)[band]
        return original(lower, cells, published, total, rule, *rest)

    return shifted


def test_the_guard_holds_with_the_total_at_the_upper_end_of_its_rounded_band(tmp_path, monkeypatch):
    """A reader holds the total only to the band of the rounded row count: its upper end is enough."""
    monkeypatch.setattr(disclosure, "pinned_children_cells", _total_shifted(0, band=1))
    assert not _leaks(tmp_path, SINGLE_CASES, banded=True)


def test_mutant_the_guard_takes_the_childrens_total_above_the_row_counts_band(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(disclosure, "pinned_children_cells", _total_shifted(5))
    assert _leaks(tmp_path, SINGLE_CASES, banded=True)


def test_mutant_the_reader_takes_the_row_counts_lower_end(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_guarded", lambda *args: False)
    result, rows, keys = _profile_of(tmp_path, "3x20")
    low = _band(result.document["tables"]["card"]["rowCount"])[0]
    assert low < len(rows)  # the true count is above the lower end of its band
    assert _derivable(result, rows, keys, banded=True) != []  # the real band holds the true table
    with pytest.raises(AssertionError, match="must hold the true table"):
        _derivable(result, rows, keys, total=[low])


def test_mutant_a_value_with_no_key_is_placed_in_the_first_level(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_place", lambda index, text: index.get(text, 0))
    members = _spread() + [["zz-missing"]]
    result = library.profile(
        tmp_path, library.contract(), library.branches(BRANCHES), library.cards(members)
    )
    assert "branch_id" in _tracked(result)  # the real code names it and follows nothing
    assert "not a key of the table it references" not in _said(result)


def test_the_orphan_check_holds_without_the_mutant(tmp_path):
    members = _spread() + [["zz-missing"]]
    result = library.profile(
        tmp_path, library.contract(), library.branches(BRANCHES), library.cards(members)
    )
    assert "branch_id" not in _tracked(result)
