# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A history follows a foreign key by the keys of the public table it references.

The records are a library's members' cards, each version naming a branch. The branch table is
public, so its keys are the levels of the card's ``branch_id``. The published cells of a key's
parent are also floors on the parent's children, so the relationship that keys the column
publishes no children per parent: the plan decides it, and no count is read to decide it.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from fluid_build.profiler import disclosure, history, references
from fluid_build.profiler._stats import (
    DeclaredBlock,
    DeclaredStatistic,
    Request,
    RequestColumn,
    RequestHistory,
    RequestTable,
    load_backend,
)
from fluid_build.profiler.runner import ExposeSource, profile_exposes
from tests.profiler import _library as library
from tests.profiler.dp import _gym_data as gym

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


#: Ten members move from each branch to the next: the cells of the changes are published too.
MOVING = [
    [BRANCHES[n], BRANCHES[(n + 1 + step) % 12]]
    for n in range(12)
    for step in range(3)
    for _ in range(10)
]


def test_a_key_that_moves_is_counted_by_its_transitions(tmp_path):
    result = library.profile(
        tmp_path, library.contract(), library.branches(BRANCHES), library.cards(MOVING)
    )
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
    # every key's cell is a floor on its parent's children, and is published as any cell is
    (tmp_path / "kept").mkdir()
    (cells,) = _tracked(result)["branch_id"]["initial"]["groups"]
    assert cells["status"] == "F"
    for published, true in zip(cells["levelCounts"], per, strict=True):
        assert published is None or published == round(true / 5) * 5
    # and the relationship that keys it publishes no children per parent, for these same floors
    (relationship,) = result.document["relationships"]
    assert [group["status"] for each in relationship["statistics"] for group in each["groups"]] == [
        "C"
    ]
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


def _column_of(result: Any, name: str = "branch_id") -> Dict[str, Any]:
    entry: Dict[str, Any] = result.document["tables"]["card"]["columns"][name]
    return entry


#: The metrics only a number is read in: a followed key is not read in them.
NUMBER_METRICS = {"sum", "min", "max", "quantiles"}


@pytest.mark.parametrize("kind", ["integer", "bigint", "decimal(10,0)", "decimal(10)"])
def test_a_whole_number_key_is_followed_by_the_keys_its_table_publishes(tmp_path, kind):
    keys = library.keys(12, kind="integer")
    result = library.profile(
        tmp_path,
        library.contract(key_type=kind),
        library.branches(list(reversed(keys))),
        library.cards(_spread(keys)),
    )
    published = [row[0] for row in result.document["references"]["branch"]["rows"]]
    follow = _tracked(result)["branch_id"]
    assert published == sorted(str(key) for key in keys)
    assert follow["initial"]["levels"] == published
    assert follow["transitions"]["levels"] == published
    assert follow["transitions"]["groupBy"] == "branch_id"
    assert _said(result) == ""
    load_profile_text(result.canonical.decode("utf-8"))  # the registry reads the history back


@pytest.mark.parametrize(
    ("kind", "key_kind"),
    [
        ("decimal(10,2)", "integer"),
        ("decimal", "integer"),
        ("numeric(8,1)", "integer"),
        ("money", "integer"),
        ("date", "date"),
    ],
)
def test_a_key_that_is_not_a_whole_number_is_named_and_not_followed(tmp_path, kind, key_kind):
    keys = library.keys(12, kind=key_kind)
    result = library.profile(
        tmp_path,
        library.contract(key_type=kind),
        library.branches(keys),
        library.cards(_spread(keys)),
    )
    assert "branch_id" not in _tracked(result)
    assert _said(result) == "its key is not text or a whole number"
    assert _column_of(result)["domain"] == ("timestamp" if key_kind == "date" else "number")
    load_profile_text(result.canonical.decode("utf-8"))


def _scaled_child(tmp_path: Path, fractional: bool) -> Any:
    """Cards whose key is ``decimal(10,2)`` into a whole-number branch key, whole in value or not."""
    keys = library.keys(12, kind="integer")
    contract = library.contract(key_type="integer")
    for column in contract["exposes"][1]["contract"]["schema"]:
        if column["name"] == "branch_id":
            column["type"] = "decimal(10,2)"
    members = [[f"{key}.00" for key in each] for each in _spread(keys)]
    if fractional:
        members.append(["2.50"])
    return library.profile(tmp_path, contract, library.branches(keys), library.cards(members))


def test_a_scaled_column_whose_values_are_all_whole_follows_a_whole_number_key(tmp_path):
    result = _scaled_child(tmp_path, fractional=False)
    published = [row[0] for row in result.document["references"]["branch"]["rows"]]
    assert _tracked(result)["branch_id"]["initial"]["levels"] == published
    assert _column_of(result)["domain"] == "category"


def test_a_scaled_column_with_one_fractional_value_is_an_orphan_and_is_written_as_a_category(
    tmp_path,
):
    # the plan decides the domain, so a value that is no key leaves the history unwritten only
    result = _scaled_child(tmp_path, fractional=True)
    assert "branch_id" not in _tracked(result)
    assert _said(result) != ""
    assert _column_of(result)["domain"] == "category"


def test_the_column_of_a_followed_whole_number_key_is_written_as_a_text_key_is(tmp_path):
    keys = library.keys(12, kind="integer")
    whole = library.profile(
        tmp_path, library.contract(key_type="integer"), library.branches(keys),
        library.cards(_spread(keys)),
    )  # fmt: skip
    (tmp_path / "text").mkdir()
    text_keys = library.keys(12)
    text = library.profile(
        tmp_path / "text", library.contract(), library.branches(text_keys),
        library.cards(_spread(text_keys)),
    )  # fmt: skip
    assert _column_of(whole) == _column_of(text)
    assert _column_of(whole)["domain"] == "category"
    assert not NUMBER_METRICS & set(_column_of(whole)["metrics"])
    assert "statistics" not in _column_of(whole)


def _not_followed(tmp_path: Path, why: str) -> Any:
    """A profile in which the card's whole-number key is left untracked for the reason *why*."""
    keys = library.keys(12, kind="integer")
    members, contract, branch_keys = _spread(keys), library.contract(key_type="integer"), keys
    if why == "orphan":
        members = [*members, [9999]]
    elif why == "not public":
        contract = library.contract(key_type="integer", classification="Internal")
    elif why == "one key":
        branch_keys, members = keys[:1], _spread(keys[:1], 40)
    elif why == "two keys":
        branch_keys, members = keys[:2], _spread(keys[:2], 40)
    elif why == "over the cap":
        branch_keys = library.keys(65, kind="integer")
        members = library.spread(branch_keys, [1] * 65)
    elif why == "two relationships":
        card = contract["exposes"][1]
        card["semantics"]["relationships"].append(
            {**card["semantics"]["relationships"][0], "name": "also_branch"}
        )
    else:
        raise AssertionError(why)
    return library.profile(
        tmp_path, contract, library.branches(branch_keys), library.cards(members)
    )


@pytest.mark.parametrize(
    "why", ["not public", "one key", "two keys", "over the cap", "two relationships"]
)
def test_a_whole_number_key_that_is_not_followed_keeps_its_number_domain_and_metrics(tmp_path, why):
    result = _not_followed(tmp_path, why)
    assert "branch_id" not in _tracked(result)
    assert _said(result) != ""
    entry = _column_of(result)
    assert entry["domain"] == "number"
    assert NUMBER_METRICS <= set(entry["metrics"])
    load_profile_text(result.canonical.decode("utf-8"))


def test_a_whole_number_key_into_a_table_that_is_not_public_is_not_followed(tmp_path):
    result = _not_followed(tmp_path, "not public")
    assert "references" not in result.document
    assert _said(result) == "the table it references is not declared public"


def test_a_whole_number_key_with_a_value_that_is_no_key_is_named_written_as_a_category(tmp_path):
    result = _not_followed(tmp_path, "orphan")
    assert "branch_id" not in _tracked(result)
    assert "not a key of the table it references" in _said(result)
    assert "9999" not in " ".join(why for _, why in result.not_measured)
    entry = _column_of(result)
    assert entry["domain"] == "category"  # no decision about a followed key reads a value
    assert not NUMBER_METRICS & set(entry["metrics"])
    load_profile_text(result.canonical.decode("utf-8"))


def test_the_disclosure_of_a_key_is_not_widened_by_following_it(tmp_path):
    # the metrics a followed key still publishes are the text key's, each as the text key's is
    keys = library.keys(12, kind="integer")
    result = library.profile(
        tmp_path, library.contract(key_type="integer"), library.branches(keys),
        library.cards(_spread(keys)),
    )  # fmt: skip
    metrics = _column_of(result)["metrics"]
    assert set(metrics) == {"count", "nullCount", "distinctCount"}
    # a foreign key's distinct values are the parents that have a child: withheld, as for text
    assert metrics["distinctCount"]["status"] == "D"
    assert "value" not in metrics["distinctCount"]


def test_a_followed_whole_number_key_that_a_declared_statistic_reads_is_not_followed(
    tmp_path, monkeypatch
):
    keys = library.keys(12, kind="integer")
    monkeypatch.setattr(history, "_reads", lambda block, name: name == "branch_id")
    result = library.profile(
        tmp_path, library.contract(key_type="integer"), library.branches(keys),
        library.cards(_spread(keys)),
    )  # fmt: skip
    assert "branch_id" not in _tracked(result)
    assert "a statistic the contract declares reads it" in _said(result)
    assert _column_of(result)["domain"] == "number"
    assert NUMBER_METRICS <= set(_column_of(result)["metrics"])
    # a text key is read as a category, whatever else reads it: it is followed as before
    (tmp_path / "text").mkdir()
    text = library.profile(
        tmp_path / "text", library.contract(), library.branches(BRANCHES),
        library.cards(_spread()),
    )  # fmt: skip
    assert "branch_id" in _tracked(text)


def test_a_statistic_reads_a_column_by_its_column_its_group_or_its_structure():
    def statistic(column: str, group_by: Optional[str] = None) -> DeclaredStatistic:
        return DeclaredStatistic(
            "levelHistogram/1",
            "levelHistogram",
            column,
            group_by,
            {},
            (column, group_by) if group_by else (column,),
            {},
        )

    held = DeclaredBlock(16, (statistic("kind"), statistic("tier", "branch_id")))
    assert history._reads(held, "kind") and history._reads(held, "branch_id")
    assert not history._reads(held, "city") and not history._reads(None, "kind")
    structured = DeclaredBlock(16, (), structure=(statistic("branch_id"),))
    assert history._reads(structured, "branch_id")


def _stage_of(tmp_path: Path, members: Sequence[Sequence[Any]]) -> Tuple[Any, Any]:
    """The history stage a profile of whole-number cards forms, and the profile itself."""
    from fluid_build.profiler import runner

    stages: List[Any] = []
    real = history.followed_columns

    def seen(stage: Any) -> Any:
        stages.append(stage)
        return real(stage)

    keys = library.keys(12, kind="integer")
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(runner.history, "followed_columns", seen)
        result = library.profile(
            tmp_path, library.contract(key_type="integer"), library.branches(keys),
            library.cards(members),
        )  # fmt: skip
    (stage,) = stages
    return stage, result


class _Unreadable:
    """A backend that refuses to read a value: the plan alone must still answer."""

    def read_value(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a value was read")


def test_the_foreign_keys_a_history_follows_are_decided_by_the_plan_alone(tmp_path):
    stage, result = _stage_of(tmp_path, _spread(library.keys(12, kind="integer")))
    assert history.followed_columns(stage) == frozenset({("card", "branch_id")})
    assert history.followed_columns(replace(stage, backend=_Unreadable())) == frozenset(
        {("card", "branch_id")}
    )
    assert "branch_id" in _tracked(result)


def test_a_key_that_a_value_leaves_untracked_is_still_a_followed_key(tmp_path):
    keys = library.keys(12, kind="integer")
    stage, result = _stage_of(tmp_path, [*_spread(keys), [9999]])
    assert history.followed_columns(stage) == frozenset({("card", "branch_id")})
    assert "branch_id" not in _tracked(result)
    statistics = result.document["relationships"][0]["statistics"]
    assert all(group["status"] == "C" for each in statistics for group in each["groups"])


def test_a_history_follows_at_most_the_most_columns_counted_over_its_candidates(tmp_path):
    """A candidate keeps its place: the key behind the most columns is not followed, whatever."""
    keys = library.keys(12)
    built = library.contract()
    schema = built["exposes"][1]["contract"]["schema"]
    extra = [f"kind{n:02d}" for n in range(history.MOST_COLUMNS)]
    at = next(place for place, each in enumerate(schema) if each["name"] == "branch_id")
    schema[at:at] = [{"name": name, "type": "enum('x', 'y')"} for name in extra]
    rows = [{**row, **dict.fromkeys(extra, "x")} for row in library.cards(_spread(keys))]
    result = library.profile(tmp_path, built, library.branches(keys), rows)
    assert "branch_id" not in _tracked(result)
    assert f"at most {history.MOST_COLUMNS} columns" in _said(result)
    assert set(extra) <= set(_tracked(result))


@pytest.mark.parametrize("path", ["request", "private release"])
def test_a_request_and_a_private_release_follow_nothing(tmp_path, path):
    if path == "request":
        keys = library.keys(12, kind="integer")
        asked = Request(
            contract_digest=None,
            fractional_bits=16,
            tables=(
                RequestTable("branch", True, (RequestColumn("branch_id", "number"),)),
                RequestTable(
                    "card", True, (RequestColumn("branch_id", "number"),), history=RequestHistory()
                ),
            ),
        )
        result = profile_exposes(
            library.contract(key_type="integer"),
            [
                ExposeSource(library.BRANCH, library.write(tmp_path / "b.ndjson", library.branches(keys))),
                ExposeSource(library.CARD, library.write(tmp_path / "c.ndjson", library.cards(_spread(keys)))),
            ],
            backend=load_backend(),
            request=asked,
        )  # fmt: skip
        document = result.document
        assert document["tables"]["card"]["columns"]["branch_id"]["domain"] == "number"
        assert "branch_id" not in document["tables"]["card"]["history"].get("columns", {})
    else:
        drawn, _ = gym.draw(gym.follow_plan(gym.request(history={})))
        document = drawn.document
    assert "references" not in document
    for table in document["tables"].values():
        for name in table.get("history", {}).get("columns", {}):
            assert table["columns"][name]["domain"] == "category"


def test_a_small_cell_of_a_whole_number_key_is_withheld_as_a_text_keys_is(tmp_path):
    size = [45, 30, 3, 30]

    def follow(kind: str) -> Dict[str, Any]:
        (tmp_path / kind).mkdir()
        keys = library.keys(4, kind="integer" if kind == "whole" else "string")
        result = library.profile(
            tmp_path / kind,
            library.contract(key_type="integer" if kind == "whole" else "string"),
            library.branches(keys),
            library.cards(_twice(library.spread(keys, size))),
        )
        found: Dict[str, Any] = _tracked(result)["branch_id"]
        return found

    whole, text = follow("whole"), follow("text")
    for part in ("initial", "transitions"):
        assert [(each["status"], each.get("levelCounts")) for each in whole[part]["groups"]] == [
            (each["status"], each.get("levelCounts")) for each in text[part]["groups"]
        ]
    (initial,) = whole["initial"]["groups"]
    assert initial["levelCounts"][2] is None  # the cell of three entities is not written


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


@pytest.mark.parametrize("size", [1, 2])
def test_a_table_with_fewer_than_three_keys_is_not_followed(tmp_path, size, monkeypatch):
    def formed(folder: Path) -> Any:
        folder.mkdir()
        return library.profile(
            folder,
            library.contract(),
            library.branches(BRANCHES[:size]),
            library.cards(_spread(BRANCHES[:size], 40)),
        )

    result = formed(tmp_path / "formed")
    assert "branch_id" not in _tracked(result)
    assert "fewer than three keys" in _said(result)
    # the relationship is formed as it is where no key is followed at all
    monkeypatch.setattr(references.References, "follow", lambda self, child, column: None)
    assert formed(tmp_path / "unfollowed").canonical == result.canonical


def test_a_table_with_three_keys_is_followed(tmp_path):
    result = library.profile(
        tmp_path,
        library.contract(),
        library.branches(BRANCHES[:3]),
        library.cards(_spread(BRANCHES[:3], 40)),
    )
    assert "branch_id" in _tracked(result)


# -- the relationship that keys a followed column ---------------------------------------------
#
# A published cell of a followed key is a floor on one parent's children, so the relationship that
# keys the column publishes no children per parent whatever the counts: every group of every
# statistic is written withheld, a decision that reads no count. The key's own cells go through the
# ordinary output check.


def _statuses(result: Any) -> List[str]:
    (relationship,) = result.document["relationships"]
    return [group["status"] for each in relationship["statistics"] for group in each["groups"]]


def _zipf(size: int, exponent: float) -> List[float]:
    weights = [1 / (place + 1) ** exponent for place in range(size)]
    return [each / sum(weights) for each in weights]


def _drawn(tmp_path: Path, size: int, entities: int, skew: str) -> Tuple[Any, List[List[str]]]:
    """Members with 1 to 6 versions, a fifth of the later ones at another branch; a seeded draw."""
    rng = random.Random(1)
    keys = library.keys(size)
    weights = [1 / size] * size if skew == "uniform" else _zipf(size, 1.1)
    members: List[List[str]] = []
    for _ in range(entities):
        versions = rng.choices([1, 2, 3, 4, 5, 6], weights=[3, 3, 2, 1, 1, 0.5])[0]
        chain = [rng.choices(keys, weights=weights)[0]]
        for _ in range(versions - 1):
            chain.append(rng.choices(keys, weights=weights)[0] if rng.random() < 0.2 else chain[-1])
        members.append(chain)
    built = library.contract()
    built["metadata"] = {"layer": "Silver", "owner": {"team": "library"}}
    for expose in built["exposes"]:
        for column in expose["contract"]["schema"]:
            if column["name"] in ("member_id", "valid_from", "branch_id"):
                column["required"] = True
    return library.profile(tmp_path, built, library.branches(keys), library.cards(members)), members


def _sha(entry: Any) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode("utf-8")).hexdigest()


#: The sha256 of the key's history entries, over ten branches of ten thousand members.
HISTORY_DIGESTS = {
    "uniform": "0316d535b869950120020425db869f5a7c48ea0470162fa10d5ee0187efb8702",  # pragma: allowlist secret
    "zipf": "d56951e291ec14c496f56023c33938cf45cbdf1cd92ae0f0938ec48e96ba09b5",  # pragma: allowlist secret
}


@pytest.mark.parametrize("skew", ["uniform", "zipf"])
def test_ten_branches_of_ten_thousand_members_publish_the_key_and_withhold_the_relationship(
    tmp_path, skew
):
    result, _ = _drawn(tmp_path, 10, 10_000, skew)
    follow = _tracked(result)["branch_id"]
    assert set(_statuses(result)) == {"C"}
    assert result.withheld.get("D", 0) >= 1  # tallied as a group withheld beside the key's cells
    assert [group["status"] for group in follow["initial"]["groups"]] == ["F"]
    assert [group["status"] for group in follow["transitions"]["groups"]].count("F") >= 9
    assert _sha(follow) == HISTORY_DIGESTS[skew]
    assert any(what.startswith("children per parent") for what, _ in result.not_measured)
    (counted,) = result.relationships
    assert (counted.published, counted.shown) == (0, (False,) * counted.cells)
    load_profile_text(result.canonical.decode("utf-8"))
    load_backend().check_document(result.canonical.decode("utf-8"))


def test_thirty_branches_of_a_thousand_members_still_publish_the_first_cells(tmp_path):
    result, _ = _drawn(tmp_path, 30, 1_000, "zipf")
    assert set(_statuses(result)) == {"C"}
    (initial,) = _tracked(result)["branch_id"]["initial"]["groups"]
    assert initial["status"] == "F"


def test_the_note_of_a_withheld_relationship_holds_no_count_and_no_key(tmp_path):
    result, _ = _drawn(tmp_path, 10, 2_000, "uniform")
    notes = [why for what, why in result.not_measured if what.startswith("children per parent")]
    assert len(notes) == 1
    assert not any(character.isdigit() for character in notes[0])
    assert "b01" not in notes[0]


def test_a_relationship_that_no_history_follows_is_not_withheld_for_it(tmp_path):
    keys = library.keys(12)
    for classification, withheld in (("Internal", False), ("Public", True)):
        folder = tmp_path / classification
        folder.mkdir()
        result = library.profile(
            folder, library.contract(classification=classification), library.branches(keys),
            library.cards(_spread(keys, 60)),
        )  # fmt: skip
        said = [what for what, _ in result.not_measured if what.startswith("children per parent")]
        assert bool(said) is withheld


# -- a reader of the document, and the records ---------------------------------------------------
#
# The reader below reads the profile and nothing else: for each cell the key's history writes it
# forms the range of records the cell may hold (a published cell's band; for a withheld one, what
# its group's size leaves, and at least the rule's minimum cell where its siblings are published).
# A named count it pins below the minimum cell would be a disclosure.


def _truth(members: Sequence[Sequence[str]]) -> Tuple[Counter[str], Counter[Tuple[str, str]]]:
    firsts: Counter[str] = Counter(chain[0] for chain in members)
    moves: Counter[Tuple[str, str]] = Counter(
        (before, after)
        for chain in members
        for before, after in zip(chain, chain[1:], strict=False)
        if before != after
    )
    return firsts, moves


def _ranges(group: Dict[str, Any], skip: Optional[int]) -> List[Tuple[int, int]]:
    """The least and most each cell of a published group may hold, from what it publishes."""
    cells = group["levelCounts"]
    least = disclosure.sample_size(group["k"], RULE)["min"]
    most = disclosure.sample_size(group["k"], RULE)["max"]
    bands = {
        at: disclosure.sample_size(count, RULE)
        for at, count in enumerate(cells)
        if count is not None and at != skip
    }
    hidden = [at for at, count in enumerate(cells) if count is None and at != skip]
    found: List[Tuple[int, int]] = []
    for at, count in enumerate(cells):
        if at == skip:
            found.append((0, 0))
        elif count is not None:
            found.append((bands[at]["min"], bands[at]["max"]))
        else:
            low = (
                0
                if len(hidden) > 1
                else max(RULE.minimum_cell, least - sum(each["max"] for each in bands.values()))
            )
            found.append((low, most - sum(each["min"] for each in bands.values())))
    return found


def _check_the_document(result: Any, members: Sequence[Sequence[str]], keys: Sequence[str]) -> None:
    follow = _tracked(result)["branch_id"]
    firsts, moves = _truth(members)
    minimum = RULE.minimum_cell
    levels = follow["initial"]["levels"]
    assert levels == sorted(keys)
    assert set(_statuses(result)) == {"C"}  # followed, so withheld, whatever the counts
    published = [
        (follow["initial"]["groups"][0], None, lambda at: firsts[levels[at]]),
        *(
            (
                group,
                levels.index(group["group"]),
                lambda at, source=group["group"]: moves[(source, levels[at])],
            )
            for group in follow["transitions"]["groups"]
        ),
    ]
    for group, skip, truth in published:
        if group["status"] != "F":
            continue
        cells = group["levelCounts"]
        # every published cell is a band of at least the minimum cell
        assert all(
            count is None or count >= minimum for at, count in enumerate(cells) if at != skip
        )
        hidden = [at for at, count in enumerate(cells) if count is None and at != skip]
        # and the cells it withholds hold at least the minimum cell together
        assert not hidden or sum(truth(at) for at in hidden) >= minimum
        # a reader forming each cell's range finds none that a small count pins
        for at, (low, high) in enumerate(_ranges(group, skip)):
            if at != skip:
                assert not (low == high and low < minimum), (group["group"], levels[at])


@pytest.mark.parametrize("seed", range(8))
def test_a_followed_key_withholds_its_relationship_and_pins_no_small_count(tmp_path, seed):
    rng = random.Random(seed)
    followed = 0
    for trial in range(25):
        keys = library.keys(rng.randint(3, 12))
        members = _random_members(rng, keys) if trial % 2 else _single_version_members(rng, keys)
        if not members:
            continue
        folder = tmp_path / str(trial)
        folder.mkdir()
        result = library.profile(
            folder, library.contract(), library.branches(keys), library.cards(members)
        )
        if "branch_id" in _tracked(result):
            followed += 1
            _check_the_document(result, members, keys)
        else:
            assert "F" in _statuses(result) or _said(result) != ""
    assert followed > 0


def _random_members(rng: random.Random, keys: Sequence[str]) -> List[List[str]]:
    members: List[List[str]] = []
    for key in keys:
        for _ in range(rng.choice([0, 0, 3, 12, 20, 30, 45])):
            chain = [key]
            for _ in range(rng.choice([1, 1, 2, 3]) - 1):
                chain.append(keys[rng.randrange(len(keys))] if rng.random() < 0.7 else chain[-1])
            members.append(chain)
    return members


def _single_version_members(rng: random.Random, keys: Sequence[str]) -> List[List[str]]:
    """One version a member, a few of the branches large: the first cells are likely published."""
    sizes = [0, 1, 2, 3, 5, 8, 10, 12, 15, 18, 20, 22, 30, 45]
    return [[key] for key in keys for _ in range(rng.choice(sizes))]


# -- the shape of a withheld relationship, in the document and beside a calendar ------------------


def _withheld_statistic(status: str) -> Dict[str, Any]:
    edges = ["1672531200000", "1675209600000", "1677628800000"]
    return {
        "definition": "marginalHistogram/1",
        "sampleSize": {"min": 0},
        "disclosure": "acro-default",
        "bins": ["0", "1"],
        "groupByBins": {"column": "d", "bins": edges},
        "groups": [{"group": edge, "status": status} for edge in edges[:-1]],
    }


@pytest.mark.parametrize("status", ["C", "D"])
def test_a_relationship_written_withheld_whole_is_read_beside_a_calendar(status):
    from fluid_build.profiler import calendars

    held = {
        "parent": {"table": "t", "columns": ["id"]},
        "statistics": [_withheld_statistic(status)],
    }
    assert [each for each, _ in calendars._binned_by([held], "t", "d")] == held["statistics"]
    doc: Dict[str, Any] = {
        "calendar": [
            {"column": "d", "month": {"groups": [{"group": "", "k": 100, "status": "F"}]}}
        ],
        "periods": [],
    }
    calendars.withhold_beside_relationships({"t": doc}, [held], (Counter({"F": 1}), Counter()))
    assert doc["calendar"][0]["month"]["groups"] == [{"group": "", "status": "C"}]
