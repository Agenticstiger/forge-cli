# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A history follows a foreign key by the keys of the public table it references.

The records are a library's members' cards, each version naming a branch. The branch table is
public, so its keys are the levels of the card's ``branch_id``. The published cells of a key's
parent are also floors on the parent's children, which the guard of the relationship's children
per parent reads; a brute-force reader checks that no withheld bin can be worked out from them.
"""

from __future__ import annotations

import functools
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


LOWER = list(CHILDREN_EDGES[:-1])
NEVER = 10**9
_Range = Tuple[int, int]


@functools.lru_cache(maxsize=None)
def _ranged_tables(
    children: int, ranges: Tuple[_Range, ...]
) -> Dict[Tuple[int, ...], Tuple[int, int]]:
    """Every table of children per parent that parents with these ranges can fill and hold.

    Parent by parent, a table so far keeps the least and the most children its parents hold
    together: a parent in a bin holds at least the larger of its floor and the bin's lower edge,
    and at most the smaller of its ceiling and the bin's top. A table stays when its parents'
    children can add up to *children*. This is a layered search over the tables (and not over the
    ways to give the parents to bins), and it shares nothing with the guard's flow.
    """
    tops = [edge - 1 for edge in CHILDREN_EDGES[1:-1]] + [children]
    states: Dict[Tuple[int, ...], Tuple[int, int]] = {(0,) * len(LOWER): (0, 0)}
    for floor, ceiling in ranges:
        spans = [
            (at, max(floor, edge), min(ceiling, children, tops[at]))
            for at, edge in enumerate(LOWER)
            if max(floor, edge) <= min(ceiling, children, tops[at])
        ]
        following: Dict[Tuple[int, ...], Tuple[int, int]] = {}
        for counts, (least, most) in states.items():
            for at, low, high in spans:
                key = (*counts[:at], counts[at] + 1, *counts[at + 1 :])
                new = (least + low, most + high)
                old = following.get(key)
                following[key] = new if old is None else (min(old[0], new[0]), max(old[1], new[1]))
        states = following
    return {table: found for table, found in states.items() if found[0] <= children <= found[1]}


def _fits(table: Tuple[int, ...], published: Sequence[Optional[int]]) -> bool:
    """A table fits the relationship: each published cell in its band, the rest not small."""
    for value, shown in zip(table, published, strict=True):
        if shown is not None and not _band(shown)[0] <= value <= _band(shown)[1]:
            return False
    if any(shown is not None for shown in published):
        rest = sum(value for value, shown in zip(table, published, strict=True) if shown is None)
        if not disclosure.check_count(rest, RULE).published:
            return False
    return True


def _span(span: _Range, group: Dict[str, Any]) -> _Range:
    """Narrow a count by a published group: its size's band, and its cells' if every one shows."""
    low, high = span
    if group["status"] != "F":
        return span
    if isinstance(group.get("k"), int):
        low, high = max(low, _band(group["k"])[0]), min(high, _band(group["k"])[1])
    cells = group.get("levelCounts") or group.get("binCounts")
    if cells and all(cell is not None for cell in cells):
        low = max(low, sum(_band(cell)[0] for cell in cells if cell))
        high = min(high, sum(_band(cell)[1] for cell in cells if cell))
    return low, high


def _totals(document: Dict[str, Any]) -> Tuple[List[int], List[int]]:
    """The child rows and the entities a reader narrows to, from the document's histograms.

    The rows are those of the table, so every histogram of a column that is never null (the
    start of a version, the entity) counts all of them: the rounded ``rowCount`` and each such
    histogram's size band and, where every cell is shown, its cells' bands, all hold. The
    entities are counted by the first starts, the versions of each, and the first cells.
    """
    card = document["tables"]["card"]
    history = card["history"]
    rows = _band(card["rowCount"])
    for name in (history["start"], *history["entity"]):
        for statistic in card["columns"][name].get("statistics", ()):
            for group in statistic["groups"]:
                rows = _span(rows, group)
    entities = (0, NEVER)
    parts = [history["versionsPerEntity"], history["firstStart"]]
    parts.append(history["columns"]["branch_id"]["initial"])
    for part in parts:
        for group in part["groups"]:
            entities = _span(entities, group)
    return list(range(rows[0], rows[1] + 1)), list(range(entities[0], min(entities[1], NEVER) + 1))


def _scenarios(
    document: Dict[str, Any],
    floors: Sequence[int],
    rows_held: Sequence[int],
    entities_held: Sequence[int],
) -> List[Tuple[int, Tuple[_Range, ...]]]:
    """Each count of child rows and entities that fits, with every key's range of children.

    A key's children are its entities' first versions (its first cell, or what is left of the
    group's entities) and the *rest* rows that are no first version, wherever they fall: at most
    the cell's top plus the rest. A document that shows one version an entity has no rest.
    """
    follow = document["tables"]["card"]["history"]["columns"]["branch_id"]
    (initial,) = follow["initial"]["groups"]
    one = document["tables"]["card"]["history"]["versionsPerEntity"]
    single = one["bins"] == ["1", "2"] and one["groups"][0]["status"] == "F"
    found: List[Tuple[int, Tuple[_Range, ...]]] = []
    for rows in rows_held:
        if initial["status"] != "F":
            found.append((rows, tuple((floor, rows) for floor in floors)))
            continue
        cells = initial["levelCounts"]
        least = sum(_band(cell)[0] for cell in cells if cell is not None)
        for entities in entities_held:
            rest = rows - entities
            if rest < 0 or (single and rest):
                continue
            tops = [
                (_band(cell)[1] if cell is not None else max(entities - least, 0)) + rest
                for cell in cells
            ]
            found.append((rows, tuple(zip(floors, tops, strict=True))))
    return found


def _entities_of(rows: Sequence[Dict[str, Any]]) -> int:
    """The entities whose first version names a branch (the members of the card table)."""
    first: Dict[Any, Dict[str, Any]] = {}
    for row in rows:
        seen = first.get(row["member_id"])
        if seen is None or row["valid_from"] < seen["valid_from"]:
            first[row["member_id"]] = row
    return sum(1 for row in first.values() if row["branch_id"] is not None)


def _derivable(
    result: Any,
    rows: Sequence[Dict[str, Any]],
    keys: Sequence[str],
    *,
    outgoing: bool = True,
    banded: bool = False,
    total: Optional[Sequence[int]] = None,
) -> List[int]:
    """The withheld bins of the relationship's cells a reader can work out.

    The reader knows the children's total and the entities exactly, or with *banded* only as the
    document gives them (:func:`_totals`: any count its histograms leave open). *total* sets the
    child rows it takes outright. The table has no null foreign key.
    """
    document = result.document
    (group,) = document["relationships"][0]["statistics"][0]["groups"]
    published: List[Optional[int]] = (
        list(group["binCounts"]) if group["status"] == "F" else [None] * (len(CHILDREN_EDGES) - 1)
    )
    held = [row["branch_id"] for row in rows if row["branch_id"] is not None]
    per_key = [held.count(key) for key in keys]
    floors = _floors(document, len(keys), outgoing=outgoing)
    rows_held, entities_held = _totals(document) if banded else ([len(held)], [_entities_of(rows)])
    if total is not None:
        rows_held = list(total)
    tables = {
        table
        for children, ranges in _scenarios(document, floors, rows_held, entities_held)
        for table in _ranged_tables(children, ranges)
        if _fits(table, published)
    }
    bare = {
        table
        for children in rows_held
        for table in _ranged_tables(children, ((0, children),) * len(keys))
        if _fits(table, published)
    }
    true = [0] * (len(CHILDREN_EDGES) - 1)
    for count in per_key:
        true[min(max(i for i, edge in enumerate(CHILDREN_EDGES[:-1]) if edge <= count), 6)] += 1
    assert tuple(true) in tables, "the reader's model must hold the true table"

    def fixed(listed: Any) -> List[int]:
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
#: A published first cell bounds the others from above by the rows no first version holds, which
#: the table's own histograms narrow: the rounded ``rowCount`` to 58 or 59 here (the histogram of
#: ``valid_from`` shows all its cells), the entities to 58 or more, and a document that shows one
#: version an entity has none. b01 holds 13 to 17 and b02 28 to 32 rows, the rest 18 or fewer each.
GRID = {
    "grid-15-25-0-10-3": [15, 25, 0, 10, 3],
    "grid-15-25-10-0-3": [15, 25, 10, 0, 3],
    "grid-15-30-0-10-3": [15, 30, 0, 10, 3],
    "grid-15-30-10-0-3": [15, 30, 10, 0, 3],
}
GRID_CASES = sorted(GRID)
#: Twelve branches, one holding 44 or 45 and the others a few each: its first cell floors its
#: parent at 43, and the rest cannot reach 20 between them, so the bin of 20 or more holds one.
MANY = {
    "many-45-then-small": [45, 2, 2, 2, 0, 0, 0, 1, 1, 1, 2, 2],
    "many-44-then-small": [44, 0, 0, 1, 1, 2, 2, 3, 3, 0, 1, 1],
}
MANY_CASES = sorted(MANY)
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
        "twice-45-30-0-30": (
            library.keys(4),
            _twice(library.spread(library.keys(4), [45, 30, 0, 30])),
        ),
        **{
            name: (library.keys(len(per)), library.spread(library.keys(len(per)), per))
            for name, per in {
                **SINGLE_VERSION,
                **GRID,
                **MANY,
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
    result, rows, keys = _profile_of(tmp_path, case)
    assert _derivable(result, rows, keys, banded=banded) == []


@pytest.mark.parametrize("case", sorted({**SINGLE_VERSION, **GRID}))
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


def _single_version_members(rng: random.Random, keys: Sequence[str]) -> List[List[str]]:
    """One version a member, a few of the branches large: the first cells are likely published."""
    sizes = [0, 1, 2, 3, 5, 8, 10, 12, 15, 18, 20, 22, 30, 45]
    return [[key] for key in keys for _ in range(rng.choice(sizes))]


def _search_many_keys(tmp_path: Path, seed: int) -> Tuple[int, List[Tuple[int, int, List[int]]]]:
    """Profile four draws of six to twelve keys; the published first cells and what is derived."""
    rng = random.Random(100 + seed)
    published = 0
    found: List[Tuple[int, int, List[int]]] = []
    for trial in range(4):
        keys = library.keys(rng.randint(6, 12))
        members = _single_version_members(rng, keys) if trial % 2 == 0 else None
        members = members or _random_members(rng, keys)
        if not members:
            continue
        rows = library.cards(members)
        result = library.profile(
            _folder(tmp_path, trial), library.contract(), library.branches(keys), rows
        )
        if "branch_id" not in _tracked(result):
            continue
        published += _initial_of(result)["groups"][0]["status"] == "F"
        if derived := _derivable(result, rows, keys, banded=trial == 0):
            found.append((seed, trial, derived))
    return published, found


@pytest.mark.parametrize("seed", range(6))
def test_no_withheld_bin_is_derivable_over_six_to_twelve_keys(tmp_path, seed):
    """A matching, not an enumeration of the keys' bins, so the reader reaches twelve keys."""
    assert _search_many_keys(tmp_path, seed)[1] == []


def test_mutant_without_the_guard_twelve_keys_give_a_bin_away(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_guarded", lambda *args: False)
    assert _leaks(tmp_path, MANY_CASES)
    assert _leaks(_folder(tmp_path, "band"), MANY_CASES, banded=True)


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
    """Whether the reader works a bin out of any case."""
    cases = cases or PINNED
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


EDGES = list(CHILDREN_EDGES[:-1])
TOPS = [edge - 1 for edge in CHILDREN_EDGES[1:-1]] + [None]


def _in_bin(value: int) -> int:
    return max(at for at, edge in enumerate(EDGES) if edge <= value)


def _every_way(table: Sequence[int], ranges: Sequence[Tuple[int, int]], children: int) -> bool:
    """Whether some way to give the parents to the bins lets their children add up: by brute force.

    Every assignment of parents to bins that fills the table is tried, and every total each can
    hold is added up exactly (a set of sums), so nothing here assumes the totals of two ways meet.
    """
    for assignment in itertools.product(range(len(EDGES)), repeat=len(ranges)):
        if any(assignment.count(at) != table[at] for at in range(len(EDGES))):
            continue
        sums = {0}
        for at, (floor, ceiling) in zip(assignment, ranges, strict=True):
            top = children if (end := TOPS[at]) is None else end
            low, high = max(floor, EDGES[at]), min(ceiling, children, top)
            sums = {have + value for have in sums for value in range(low, high + 1)}
            if not sums:
                break
        if children in sums:
            return True
    return False


def test_the_guard_decides_what_every_way_to_give_the_parents_to_the_bins_decides() -> None:
    """The fast check agrees with trying every assignment and every total, over random cases."""
    rng = random.Random(4)
    both = [0, 0]
    for _ in range(400):
        parents = rng.randint(1, 5)
        children = rng.randint(parents, 90)
        ranges = []
        for _ in range(parents):
            floor = rng.choice([0, 0, 1, 2, 5, 10, 20, 40])
            ceiling = floor + rng.choice([0, 1, 3, 8, 15, 30, 90])
            ranges.append((floor, ceiling))
        table = [0] * len(EDGES)
        for _ in range(parents):
            table[rng.randrange(len(EDGES))] += 1
        want = _every_way(table, ranges, children)
        both[want] += 1
        capped = [(floor, min(ceiling, children)) for floor, ceiling in ranges]
        assert disclosure._ranges_hold(EDGES, TOPS, table, capped, children) == want, (
            table,
            ranges,
            children,
        )
    assert min(both) > 8  # the cases are not all one answer


def test_the_least_a_transport_costs_is_found_by_its_cheapest_way() -> None:
    kinds = {(0, 1, 0, 9): 2, (1, 1, 5, 9): 1}  # two that may lie in bins 0-1, one only in bin 1
    cost = lambda kind, at: at * 10 + kind[2]  # noqa: E731
    table = [1, 2] + [0] * (len(EDGES) - 2)
    assert disclosure._extreme(kinds, table, cost) == (0 + 0) + (10 + 0) + (10 + 5)
    assert disclosure._extreme(kinds, [3] + [0] * (len(EDGES) - 1), cost) is None


def test_a_published_cell_is_read_at_its_tops_band_and_a_withheld_one_by_what_is_left() -> None:
    levels = ["b01", "b02", "b03"]
    initial = _entry(levels, [{"group": "", "k": 60, "levelCounts": [30, 20, None], "status": "F"}])
    # entities 59, 12 rows follow a first version: cells at the top of their bands (32, 22), the
    # withheld one at what 59 leaves after the cells' lower ends (28 and 18), and each + 12
    assert history._published_ceilings(initial, 59, 12, RULE) == [44, 34, 25]
    gone = _entry(levels, [{"group": "", "status": "C"}])
    assert history._published_ceilings(gone, 59, 12, RULE) == [None, None, None]


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


def test_a_ceiling_bounds_the_parent_it_names_from_above() -> None:
    # four parents hold 80 children and three of them at most 18 each: the fourth holds 26 or
    # more, and is the one parent with 20 or more, which no floor said
    table = [0, 0, 0, 0, 1, 2, 1]
    shown = [False] * 7
    base = disclosure.pinned_children_cells(EDGES, table, shown, 80, RULE)
    ceilings = [None, 18, 18, 18]
    got = disclosure.pinned_children_cells(
        EDGES, table, shown, 80, RULE, 0, None, (), (0, 0, 0, 0), ceilings
    )
    assert 6 in got and 6 not in base


def _dropping(*names: str):
    """A guard that is not given what *names* (floors, ceilings, at_least) say."""
    original = disclosure.pinned_children_cells

    def given(lower, cells, published, children, rule, childless=0, upper=None, *rest):
        at_least, floors, ceilings = (*rest, (), (), ())[:3]
        return original(
            lower,
            cells,
            published,
            children,
            rule,
            childless,
            upper,
            () if "at_least" in names else at_least,
            () if "floors" in names else floors,
            () if "ceilings" in names else ceilings,
        )

    return given


def test_mutant_the_guard_knows_no_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(
        disclosure, "pinned_children_cells", _dropping("at_least", "floors", "ceilings")
    )
    assert _leaks(tmp_path)


def test_mutant_the_floors_use_up_none_of_the_childrens_total(tmp_path, monkeypatch):
    # with the floors gone (and the count of parents each lifts) only the ceilings are left
    monkeypatch.setattr(disclosure, "pinned_children_cells", _dropping("floors", "at_least"))
    assert _leaks(tmp_path, ["sink", "moving", "star", "trial-85", *MANY_CASES])
    assert _leaks(_folder(tmp_path, "single"), ["single-30-1-0-10", "single-5-10-12-30"])


def test_mutant_no_parent_has_a_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(disclosure, "pinned_children_cells", _dropping("ceilings"))
    assert _leaks(tmp_path, GRID_CASES, banded=True)


def test_mutant_a_ceiling_leaves_out_the_rows_that_are_no_first_version(tmp_path, monkeypatch):
    original = history._published_ceilings
    monkeypatch.setattr(
        history,
        "_published_ceilings",
        lambda initial, entities, rest, rule: original(initial, entities, 0, rule),
    )
    # the true table is no longer one the guard can hold: it withholds a table it published
    assert _initial_of(_profile_of(tmp_path, "twice-45-30-0-30")[0])["groups"] == [
        {"group": "", "status": "C"}
    ]
    monkeypatch.undo()
    (tmp_path / "real").mkdir()
    assert _initial_of(_profile_of(tmp_path / "real", "twice-45-30-0-30")[0])["groups"] == (
        NO_PIN["initial"]["groups"]
    )


def test_mutant_the_parents_are_given_to_bins_by_the_old_least_total(tmp_path, monkeypatch):
    """Floors paired largest first with the highest bins, and the rest of the ranges ignored."""

    def least_total(lower, tops, table, ranges, children):
        slots: List[int] = []
        for edge, value in reversed(list(zip(lower, table, strict=True))):
            slots += [edge] * value
        floors = sorted((floor for floor, _ in ranges), reverse=True)
        paired = sum(max(slot, floor) for slot, floor in zip(slots, floors, strict=False))
        if paired + sum(slots[len(floors) :]) > children:
            return False
        return (
            tops[-1] is None
            and table[-1] > 0
            or sum((top or 0) * value for top, value in zip(tops, table, strict=True)) >= children
        )

    monkeypatch.setattr(disclosure, "_ranges_hold", least_total)
    assert _leaks(tmp_path, GRID_CASES, banded=True)


def _folder(path: Path, name: object) -> Path:
    folder = path / str(name)
    folder.mkdir(parents=True)
    return folder


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


def test_mutant_the_reader_takes_the_row_counts_lower_end(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_guarded", lambda *args: False)
    result, rows, keys = _profile_of(tmp_path, "3x20")
    low = _band(result.document["tables"]["card"]["rowCount"])[0]
    assert low < len(rows)  # the true count is above the lower end of its band
    assert _derivable(result, rows, keys, banded=True) != []  # the real band holds the true table
    with pytest.raises(AssertionError, match="must hold the true table"):
        _derivable(result, rows, keys, total=[low])


#: One version a member, four branches: the first cells are published where the total the guard
#: knows exactly leaves a reader no room, and the rounded band would. No reader works a bin out of
#: them (the document's histograms leave a wider total), so withholding them is no more than care.
BAND_SENSITIVE = [[20, 20, 0, 3], [20, 30, 3, 0], [45, 3, 12, 3], [30, 3, 3, 12]]


def test_the_guard_reads_exact_counts_and_so_withholds_what_a_band_reading_one_publishes(
    tmp_path, monkeypatch
):
    """The conservatism check for a guard that took the total at the band's top, not exactly."""

    def first_cells(folder: Path) -> List[str]:
        found = []
        for number, per in enumerate(BAND_SENSITIVE):
            keys = library.keys(len(per))
            result = library.profile(
                _folder(folder, number),
                library.contract(),
                library.branches(keys),
                library.cards(library.spread(keys, per)),
            )
            found.append(_initial_of(result)["groups"][0]["status"])
        return found

    assert first_cells(tmp_path / "real") == ["C"] * len(BAND_SENSITIVE)
    monkeypatch.setattr(disclosure, "pinned_children_cells", _total_shifted(0, band=1))
    assert first_cells(tmp_path / "band") == ["F"] * len(BAND_SENSITIVE)  # it would publish more


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
