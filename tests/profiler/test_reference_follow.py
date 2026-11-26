# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A history follows a foreign key by the keys of the public table it references.

The records are a library's members' cards, each version naming a branch. The branch table is
public, so its keys are the levels of the card's ``branch_id``. The published cells of a key's
parent are also floors on the parent's children, which the guard of the relationship's children
per parent reads; a brute-force reader checks that no withheld bin can be worked out from them.
"""

from __future__ import annotations

import json
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
    cells = _tracked(result)["branch_id"]["initial"]["groups"][0]["levelCounts"]
    for published, true in zip(cells, per, strict=True):
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
    members = _spread() + [[None] for _ in range(15)]
    result = library.profile(
        tmp_path, library.contract(), library.branches(BRANCHES), library.cards(members)
    )
    follow = _tracked(result)["branch_id"]
    (group,) = follow["initial"]["groups"]
    assert group["status"] == "F"
    assert group["k"] == 240  # the entities with a branch: the 15 with none are in no level


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


def _band(published: int) -> Tuple[int, int]:
    found = disclosure.sample_size(published, RULE)
    return found["min"], found["max"]


def _floors(document: Dict[str, Any], size: int) -> List[int]:
    """The child rows a reader knows each key's parent holds, from the published key cells."""
    follow = document["tables"]["card"]["history"]["columns"]["branch_id"]
    floors = [0] * size
    (initial,) = follow["initial"]["groups"]
    if initial["status"] == "F":
        for at, count in enumerate(initial["levelCounts"]):
            if count is not None:
                floors[at] += _band(count)[0]
    for group in follow["transitions"]["groups"]:
        if group["status"] != "F":
            continue
        source = follow["transitions"]["levels"].index(group["group"])
        for at, count in enumerate(group["levelCounts"]):
            if count is not None and at != source:
                floors[at] += _band(count)[0]
    return floors


def _tables(
    parents: int,
    children: int,
    published: Sequence[Optional[int]],
    known: Sequence[int],
) -> List[Tuple[int, ...]]:
    """Every table of children per parent a reader can hold true, by brute force.

    Cells are non-negative and sum to the parents; a published cell lies in its band; a bin holds
    at least as many parents, with those above it, as the floors show; the children bound the
    cells; and the withheld cells together are not a small count (the check's own rule).
    """
    lower = list(CHILDREN_EDGES[:-1])
    tops = [edge - 1 for edge in CHILDREN_EDGES[1:-1]] + [None]
    found: List[Tuple[int, ...]] = []

    def walk(prefix: Tuple[int, ...], left: int) -> None:
        at = len(prefix)
        if at == len(lower):
            if left == 0 and _holds(prefix):
                found.append(prefix)
            return
        shown = published[at]
        low, high = (0, left) if shown is None else (_band(shown)[0], min(_band(shown)[1], left))
        for value in range(low, high + 1):
            walk((*prefix, value), left - value)

    def _holds(table: Tuple[int, ...]) -> bool:
        if sum(edge * value for edge, value in zip(lower, table, strict=True)) > children:
            return False
        if table[-1] == 0 and (
            sum((top or 0) * value for top, value in zip(tops, table, strict=True)) < children
        ):
            return False
        for at, need in enumerate(known):
            if sum(table[at:]) < need:
                return False
        if any(shown is not None for shown in published):
            rest = sum(
                value for value, shown in zip(table, published, strict=True) if shown is None
            )
            if not disclosure.check_count(rest, RULE).published:
                return False
        return True

    walk((), parents)
    return found


def _derivable(result: Any, rows: Sequence[Dict[str, Any]], keys: Sequence[str]) -> List[int]:
    """The withheld bins of the relationship's cells a reader can work out, by brute force."""
    document = result.document
    (group,) = document["relationships"][0]["statistics"][0]["groups"]
    published: List[Optional[int]] = (
        list(group["binCounts"]) if group["status"] == "F" else [None] * (len(CHILDREN_EDGES) - 1)
    )
    held = [row["branch_id"] for row in rows if row["branch_id"] is not None]
    per_key = [held.count(key) for key in keys]
    floors = _floors(document, len(keys))
    known = [sum(1 for floor in floors if floor >= edge) for edge in CHILDREN_EDGES[:-1]]
    tables = _tables(len(keys), len(held), published, known)
    true = [0] * (len(CHILDREN_EDGES) - 1)
    for count in per_key:
        true[min(max(i for i, edge in enumerate(CHILDREN_EDGES[:-1]) if edge <= count), 6)] += 1
    assert tuple(true) in tables, "the reader's model must hold the true table"
    return [
        at
        for at, shown in enumerate(published)
        if shown is None and len({table[at] for table in tables}) == 1
    ]


def _run(tmp_path: Path, members: Sequence[Sequence[Any]]) -> Tuple[Any, List[Dict[str, Any]]]:
    rows = library.cards(members)
    return library.profile(tmp_path, library.contract(), library.branches(BRANCHES), rows), rows


def _unguarded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(disclosure, "parents_at_or_above", lambda lower, floors: [0] * len(lower))


#: Every branch holds 20 members: each key's first-version cell is published, every parent holds 20
#: child rows, and the children per parent have one bin (20 or more).
GUARDED = _spread()
#: Six branches hold 25 members and six hold 3: the small keys' cells are withheld, so no floor
#: reaches the bins below 20 for them.
UNGUARDED = library.spread(BRANCHES, [25] * 6 + [3] * 6)
#: Ten members move from each branch to the next: the cells of the changes are published too.
#: Every member starts at the first branch and moves on through another: only the first branch's
#: first-version cell can be published, while the changes into every branch are.
STAR = [
    chain
    for step in range(1, 12)
    for chain in (
        *([[BRANCHES[0], BRANCHES[step], BRANCHES[0]]] * 20),
        *([[BRANCHES[0], BRANCHES[step], BRANCHES[step % 11 + 1]]] * 20),
    )
]
MOVING = [
    [BRANCHES[n], BRANCHES[(n + 1 + step) % 12]]
    for n in range(12)
    for step in range(3)
    for _ in range(10)
]


def test_a_reader_forming_each_keys_floor_cannot_work_out_a_withheld_bin_of_children(tmp_path):
    result, rows = _run(tmp_path, GUARDED)
    assert _derivable(result, rows, BRANCHES) == []


def test_without_the_guard_the_same_reader_works_a_withheld_bin_out(tmp_path, monkeypatch):
    _unguarded(monkeypatch)
    result, rows = _run(tmp_path, GUARDED)
    assert _derivable(result, rows, BRANCHES) != []


def test_the_guard_withholds_a_published_cell_of_the_key_where_the_floors_pin_a_bin(
    tmp_path, monkeypatch
):
    guarded, _ = _run(tmp_path, GUARDED)
    (tmp_path / "off").mkdir()
    with monkeypatch.context() as patch:
        _unguarded(patch)
        free, _ = _run(tmp_path / "off", GUARDED)

    def withheld(result: Any) -> int:
        return (
            _tracked(result)["branch_id"]["initial"]["groups"][0].get("cellStatus", []).count("C")
        )

    assert withheld(guarded) > withheld(free)


def test_the_guard_leaves_the_cells_alone_where_the_floors_pin_no_bin(tmp_path, monkeypatch):
    guarded, rows = _run(tmp_path, UNGUARDED)
    assert _derivable(guarded, rows, BRANCHES) == []
    (tmp_path / "off").mkdir()
    with monkeypatch.context() as patch:
        _unguarded(patch)
        free, _ = _run(tmp_path / "off", UNGUARDED)
    assert _tracked(guarded) == _tracked(free)


def _withheld_keys(result: Any) -> int:
    (group,) = _tracked(result)["branch_id"]["initial"]["groups"]
    return group.get("cellStatus", []).count("C") if group["status"] == "F" else 12


def test_a_published_change_into_a_key_is_a_floor_too(tmp_path, monkeypatch):
    result, rows = _run(tmp_path, MOVING)
    assert _derivable(result, rows, BRANCHES) == []
    (tmp_path / "off").mkdir()
    with monkeypatch.context() as patch:
        patch.setattr(history, "_incoming_floors", lambda *args: [0] * len(BRANCHES))
        blind, _ = _run(tmp_path / "off", MOVING)
    # beside the cells of the keys, the changes the check publishes pin a bin: more keys go
    assert _withheld_keys(result) > _withheld_keys(blind)


def test_changes_into_every_key_that_still_pin_a_bin_are_withheld_whole(tmp_path, monkeypatch):
    result, rows = _run(tmp_path, STAR)
    assert _derivable(result, rows, BRANCHES) == []
    groups = _tracked(result)["branch_id"]["transitions"]["groups"]
    assert all(group["status"] != "F" for group in groups)
    (tmp_path / "off").mkdir()
    with monkeypatch.context() as patch:
        patch.setattr(history, "_incoming_floors", lambda *args: [0] * len(BRANCHES))
        blind, blind_rows = _run(tmp_path / "off", STAR)
    assert _derivable(blind, blind_rows, BRANCHES) != []


def test_the_guard_changes_no_other_table_and_the_document_reads_back(tmp_path):
    result, _ = _run(tmp_path, GUARDED)
    read, _text = load_profile_text(result.canonical.decode("utf-8"))
    assert "branch" in read.references
    assert json.loads(result.canonical)["references"]["branch"]["columns"] == ["branch_id", "city"]


# -- mutants -------------------------------------------------------------------------------------


def _leaks(tmp_path: Path) -> bool:
    for number, members in enumerate((GUARDED, MOVING, STAR)):
        folder = tmp_path / str(number)
        folder.mkdir()
        result, rows = _run(folder, members)
        if _derivable(result, rows, BRANCHES):
            return True
    return False


def test_mutant_the_guard_knows_no_floor(tmp_path, monkeypatch):
    _unguarded(monkeypatch)
    assert _leaks(tmp_path)


def test_mutant_the_guard_ignores_the_changes_into_a_key(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_incoming_floors", lambda *args: [0] * len(BRANCHES))
    assert _leaks(tmp_path)


def test_mutant_the_guard_withholds_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "_guarded", lambda *args: (frozenset(), False))
    assert _leaks(tmp_path)


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
