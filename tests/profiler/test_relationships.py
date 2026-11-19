# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Children per parent: a histogram with the parent as unit, for each declared relationship."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fluid_build.profiler import disclosure, runner
from fluid_build.profiler.runner import CHILDREN_EDGES, ExposeSource, profile_exposes
from tests.profiler._fake_backend import FakeBackend

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.fidelity")


def _contract(child_key="int", relationship=None, validity=False):
    org = {"exposeId": "org", "contract": {"schema": [{"name": "org_id", "type": "int"}]}}
    if validity:
        org["semantics"] = {
            "dimensions": [
                {
                    "name": "from",
                    "type": "time",
                    "typeParams": {"validityParams": {"isStart": True}},
                }
            ]
        }
    account = {
        "exposeId": "account",
        "contract": {
            "schema": [{"name": "account_id", "type": "int"}, {"name": "org_id", "type": child_key}]
        },
        "semantics": {
            "relationships": [
                relationship
                or {
                    "name": "owner",
                    "to": "org",
                    "fromColumns": ["org_id"],
                    "toColumns": ["org_id"],
                    "cardinality": "many_to_one",
                }
            ]
        },
    }
    return {"fluidVersion": "0.7.7", "kind": "DataProduct", "id": "p", "exposes": [org, account]}


def _write(path: Path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return str(path)


def _children(counts):
    """Child rows giving parent n the number of children ``counts[n]``."""
    rows, next_id = [], 0
    for parent, count in enumerate(counts):
        for _ in range(count):
            rows.append({"account_id": str(next_id), "org_id": str(parent)})
            next_id += 1
    return rows


def _profile(tmp_path, contract, counts, child_rows=None, parents=None):
    org = _write(
        tmp_path / "org.ndjson", parents or [{"org_id": str(n)} for n in range(len(counts))]
    )
    account = _write(tmp_path / "account.ndjson", child_rows or _children(counts))
    return profile_exposes(
        contract,
        [ExposeSource("org", org), ExposeSource("account", account)],
        backend=FakeBackend(),
    )


def test_each_parent_counts_once_by_its_number_of_children(tmp_path):
    # 20 parents with none, 30 with one, 20 with two, 10 with four, 10 with 25.
    counts = [0] * 20 + [1] * 30 + [2] * 20 + [4] * 10 + [25] * 10
    result = _profile(tmp_path, _contract(), counts)
    (entry,) = result.document["relationships"]
    assert entry["parent"] == {"table": "org", "columns": ["org_id"]}
    assert entry["child"] == {"table": "account", "columns": ["org_id"]}
    (statistic,) = entry["statistics"]
    assert statistic["definition"] == "marginalHistogram/1"
    assert statistic["bins"] == [str(edge) for edge in CHILDREN_EDGES]
    assert statistic["bins"][-1] == "9007199254740991"  # 20 or more, never cut
    (group,) = statistic["groups"]
    assert group["k"] == 90  # the parents, rounded as the parent's rowCount is
    # 0, 1, 2, 3-4, 5-9, 10-19, 20+: the empty 5-9 and 10-19 are withheld, and so is a
    # neighbour, or the total would give them back.
    assert group["status"] == "F"
    assert group["binCounts"][:3] == [20, 30, 20]
    assert group["cellStatus"][4:6] == ["C", "C"]
    (counted,) = result.relationships
    assert counted.relationship.name == "owner" and counted.cells == 7


def test_the_foreign_keys_distinct_count_is_withheld_beside_it(tmp_path):
    counts = [0] * 20 + [1] * 30 + [2] * 20
    result = _profile(tmp_path, _contract(), counts)
    metrics = result.document["tables"]["account"]["columns"]["org_id"]["metrics"]
    # The parents with a child: beside the parents' rowCount, it would count those with none.
    assert metrics["distinctCount"] == {
        "definition": "distinctCount/1",
        "status": "D",
        "sampleSize": metrics["count"]["sampleSize"],
        "disclosure": "acro-default",
    }
    # Its sum, extremes and quantiles describe each parent's children key by key, and it
    # forms no mean/1.
    for name in ("sum", "min", "max", "quantiles"):
        assert metrics[name]["status"] == "D"
    assert not result.document["tables"]["account"]["columns"]["org_id"].get("statistics")
    # The parent's own key keeps it.
    assert (
        result.document["tables"]["org"]["columns"]["org_id"]["metrics"]["distinctCount"]["status"]
        == "F"
    )


def test_a_null_or_unmatched_key_is_no_parents_child(tmp_path, monkeypatch):
    counted = []
    audit = runner._audited_children

    def spy(decision, group, *rest):
        counted.append(group.cells)
        return audit(decision, group, *rest)

    monkeypatch.setattr(runner, "_audited_children", spy)
    counts = [1] * 20 + [2] * 20
    rows = _children(counts) + [
        {"account_id": "1001", "org_id": None},  # no parent
        {"account_id": "1002", "org_id": "999"},  # a parent the table does not hold
    ]
    parents = [{"org_id": str(n)} for n in range(40)] + [{"org_id": None}] * 10
    result = _profile(tmp_path, _contract(), counts, child_rows=rows, parents=parents)
    ((group,),) = [entry["statistics"][0]["groups"] for entry in result.document["relationships"]]
    # The ten parents whose key is null have no child, 20 have one and 20 two; the two
    # children that match no parent are counted for none.
    assert counted == [(10, 20, 20, 0, 0, 0, 0)]
    assert group["k"] == 50
    # Beside about 60 children, the parents with one child would show that none has 20
    # or more, so the audit withholds that cell too.
    assert group["binCounts"][:2] == [10, None]


LOWER = CHILDREN_EDGES[:-1]


def test_the_audit_finds_a_high_zero_the_childrens_counts_give_back():
    # 503 parents: 100 with no child, 400 with one, 3 with two, so 406 children. With only
    # the cell of no child published, the other 403 parents hold 406 children: none can
    # hold 10, whatever a reader moves.
    cells = (100, 400, 3, 0, 0, 0, 0)
    pinned = disclosure.pinned_children_cells(
        LOWER, cells, [True] + [False] * 6, 406, disclosure.ACRO_DEFAULT
    )
    assert {5, 6} <= pinned


def test_a_cell_with_room_to_move_is_not_pinned():
    # 60 parents: 20 with no child, 20 with one, 20 with 25. A reader who knows the 520
    # children exactly can still move children between the parents of the top cell.
    cells = (20, 20, 0, 0, 0, 0, 20)
    published = [True, True, False, False, False, False, False]
    pinned = disclosure.pinned_children_cells(LOWER, cells, published, 520, disclosure.ACRO_DEFAULT)
    assert pinned == set()


def test_the_childrens_total_bounds_the_cells_from_below_too():
    # One child among 30 parents: a parent must hold it. With every other cell published,
    # the cell of no child cannot gain the one parent that holds it.
    cells = (29, 1, 0, 0, 0, 0, 0)
    published = [False] + [True] * 6
    pinned = disclosure.pinned_children_cells(LOWER, cells, published, 1, disclosure.ACRO_DEFAULT)
    assert pinned == {0}


def test_the_check_never_leaves_a_small_total_withheld():
    # The one withheld cell holds 10 parents: the check would have withheld more had it
    # held fewer, so a reader knows it cannot have lost a parent to another cell.
    cells = (10, 0, 0, 10, 0, 0, 0)
    published = [True, True, True, False, True, True, True]
    pinned = disclosure.pinned_children_cells(LOWER, cells, published, 35, disclosure.ACRO_DEFAULT)
    assert pinned == {3}


def test_a_table_the_readers_model_does_not_hold_claims_nothing_safe():
    # 12 parents with a null key cannot all sit in a cell of 11 parents: every withheld
    # cell is returned, so the audit withholds the histogram whole.
    cells = (11, 12, 0, 0, 0, 0, 0)
    pinned = disclosure.pinned_children_cells(
        LOWER, cells, [True] + [False] * 6, 12, disclosure.ACRO_DEFAULT, childless=12
    )
    assert pinned == set(range(1, 7))


@pytest.mark.parametrize(
    "counts",
    [[0] * 100 + [1] * 400 + [2] * 3, [0] * 12 + [1] * 11],
    ids=["503 parents", "23 parents"],
)
def test_no_withheld_cell_follows_from_the_childrens_counts(tmp_path, counts):
    result = _profile(tmp_path, _contract(), counts)
    ((group,),) = [entry["statistics"][0]["groups"] for entry in result.document["relationships"]]
    # The check published the cell of no child; beside the children's counts it pinned a
    # high bin's zero, so the histogram is withheld whole.
    assert group["status"] == "C"


def test_an_enum_of_the_parents_does_not_give_a_withheld_cell_back(tmp_path):
    # The parents' level histogram bounds their number (10 + 13 + 18, and at least 10
    # withheld), tighter than their row count: beside the 41 children, no parent can hold
    # 20. The strongest reader knows that number exactly, so the histogram is withheld.
    counts = [0] * 27 + [1] * 14 + [2] * 6 + [3] * 5
    tiers = ["a"] * 10 + ["b"] * 13 + ["c"] * 18 + ["d"] * 2 + ["e"] * 9
    contract = _contract()
    contract["exposes"][0]["contract"]["schema"].append(
        {"name": "tier", "type": "enum('a', 'b', 'c', 'd', 'e')"}
    )
    parents = [{"org_id": str(n), "tier": tier} for n, tier in enumerate(tiers)]
    result = _profile(tmp_path, contract, counts, parents=parents)
    tier = result.document["tables"]["org"]["columns"]["tier"]["statistics"][0]["groups"][0]
    assert tier["levelCounts"][:3] == [10, 15, 20]
    ((group,),) = [entry["statistics"][0]["groups"] for entry in result.document["relationships"]]
    assert group["status"] == "C"


def test_two_relationships_into_one_parent_do_not_give_each_other_away(tmp_path):
    # Each relationship's cells bound the parents' number for the other one.
    first = [0] * 27 + [1] * 14 + [2] * 6 + [3] * 5
    second = [0] * 10 + [1] * 13 + [2] * 18 + [3] * 2 + [5] * 9
    relationship = {
        "name": "owner",
        "to": "org",
        "fromColumns": ["org_id"],
        "toColumns": ["org_id"],
        "cardinality": "many_to_one",
    }
    contract = _contract()
    contract["exposes"].append(
        {
            "exposeId": "invoice",
            "contract": {"schema": [{"name": "org_id", "type": "int"}]},
            "semantics": {"relationships": [dict(relationship, name="billed")]},
        }
    )
    org = _write(tmp_path / "org.ndjson", [{"org_id": str(n)} for n in range(52)])
    account = _write(tmp_path / "account.ndjson", _children(first))
    invoice = _write(
        tmp_path / "invoice.ndjson",
        [{"org_id": str(n)} for n, count in enumerate(second) for _ in range(count)],
    )
    result = profile_exposes(
        contract,
        [
            ExposeSource("org", org),
            ExposeSource("account", account),
            ExposeSource("invoice", invoice),
        ],
        backend=FakeBackend(),
    )
    groups = {
        entry["child"]["table"]: entry["statistics"][0]["groups"][0]
        for entry in result.document["relationships"]
    }
    assert groups["account"]["status"] == "C"
    for name, counts in (("account", first), ("invoice", second)):
        cells = [0] * len(LOWER)
        for count in counts:
            cells[max(i for i, edge in enumerate(LOWER) if count >= edge)] += 1
        group = groups[name]
        if group["status"] == "F":  # what it publishes leaves no withheld cell pinned
            published = [status == "F" for status in group["cellStatus"]]
            assert not disclosure.pinned_children_cells(
                LOWER, cells, published, sum(counts), disclosure.ACRO_DEFAULT
            )


def test_a_relationship_whose_parent_is_not_profiled_is_named_and_left_out(tmp_path):
    account = _write(tmp_path / "account.ndjson", _children([1] * 20))
    result = profile_exposes(_contract(), [ExposeSource("account", account)], backend=FakeBackend())
    assert "relationships" not in result.document
    assert result.relationships_left_out == (
        (
            "account",
            "owner",
            "its target is not profiled in this call: profile both exposes together",
        ),
    )
    # No parent is counted, so the key's distinct count is not withheld for it.
    metrics = result.document["tables"]["account"]["columns"]["org_id"]["metrics"]
    assert metrics["distinctCount"]["status"] != "D"


def test_keys_read_in_different_domains_are_named_and_left_out(tmp_path):
    result = _profile(tmp_path, _contract(child_key="string"), [1] * 20)
    assert "relationships" not in result.document
    ((child, name, reason),) = result.relationships_left_out
    assert (child, name) == ("account", "owner") and "different domains" in reason


@pytest.mark.parametrize("columns", [5, "org_id", {"name": "org_id"}])
def test_key_columns_that_are_not_lists_are_named_and_left_out(tmp_path, columns):
    relationship = {"name": "owner", "to": "org", "fromColumns": columns, "toColumns": columns}
    result = _profile(tmp_path, _contract(relationship=relationship), [1] * 20)
    assert "relationships" not in result.document
    ((_, name, reason),) = result.relationships_left_out
    assert name == "owner" and "not two lists" in reason


def test_the_document_does_not_depend_on_the_order_the_exposes_are_named_in(tmp_path):
    def expose(name, parent=None):
        schema = [{"name": f"{name}_id", "type": "int"}]
        entry = {"exposeId": name, "contract": {"schema": schema}}
        if parent is not None:
            key = f"{parent}_id"
            schema.append({"name": key, "type": "int"})
            entry["semantics"] = {
                "relationships": [
                    {"name": f"of_{parent}", "to": parent, "fromColumns": [key], "toColumns": [key]}
                ]
            }
        return entry

    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "p",
        "exposes": [expose("org"), expose("account", "org"), expose("invoice", "account")],
    }
    sources = {
        "org": _write(tmp_path / "org.ndjson", [{"org_id": str(n)} for n in range(20)]),
        "account": _write(
            tmp_path / "account.ndjson",
            [{"account_id": str(n), "org_id": str(n % 20)} for n in range(40)],
        ),
        "invoice": _write(
            tmp_path / "invoice.ndjson",
            [{"invoice_id": str(n), "account_id": str(n % 40)} for n in range(80)],
        ),
    }
    digests = set()
    for order in (["org", "account", "invoice"], ["invoice", "account", "org"]):
        result = profile_exposes(
            contract, [ExposeSource(each, sources[each]) for each in order], backend=FakeBackend()
        )
        children = [entry["child"]["table"] for entry in result.document["relationships"]]
        assert children == ["account", "invoice"]  # the contract's order
        digests.add(result.digest)
    assert len(digests) == 1


def _edge_statistics(result, table, column):
    statistics = result.document["tables"][table]["columns"][column].get("statistics", [])
    return [each["definition"] for each in statistics if each["definition"] != "mean/1"]


def test_an_enum_key_takes_no_level_histogram_beside_children_per_parent(tmp_path):
    # An enum key's level histogram counts each key's rows, and the child key's rows are
    # each parent's children: the keys take none, and the relationship is counted.
    keys = [f"L{n:02d}" for n in range(30)]
    enum = "enum(" + ", ".join(f"'{key}'" for key in keys) + ")"
    contract = _contract(child_key=enum)
    contract["exposes"][0]["contract"]["schema"][0]["type"] = enum
    counts = [0] * 10 + [12] * 20
    parents = [{"org_id": key} for key in keys]
    rows = [
        {"account_id": str(n), "org_id": key}
        for n, (key, count) in enumerate(zip(keys, counts, strict=True))
        for _ in range(count)
    ]
    result = _profile(tmp_path, contract, counts, child_rows=rows, parents=parents)
    assert result.relationships and not result.relationships_left_out
    assert _edge_statistics(result, "account", "org_id") == []
    assert _edge_statistics(result, "org", "org_id") == []
    uncovered = {name: why for table in result.tables for name, why in table.uncovered}
    assert "counted relationship" in uncovered["org_id"]


@pytest.mark.parametrize(
    "rules", [[], [{"type": "range", "constraint": "between 0 and 29"}]], ids=["ladder", "range"]
)
def test_a_number_key_takes_no_histogram_or_quantile(tmp_path, rules):
    # A declared range would give each key a bin of its own, and the ladder's bins near 0
    # are one unit wide: either would count each parent's children beside the cells.
    contract = _contract()
    for expose in contract["exposes"]:
        for column in expose["contract"]["schema"]:
            if column["name"] == "org_id":
                column["validationRules"] = rules
    result = _profile(tmp_path, contract, [0] * 10 + [12] * 20)
    assert result.relationships
    assert _edge_statistics(result, "account", "org_id") == []
    assert _edge_statistics(result, "org", "org_id") == []
    # A column that is no key keeps its statistics.
    assert _edge_statistics(result, "account", "account_id") != []


def test_a_target_with_a_validity_window_is_left_out(tmp_path):
    result = _profile(tmp_path, _contract(validity=True), [1] * 20)
    assert "relationships" not in result.document
    assert "validity window" in result.relationships_left_out[0][2]


def test_a_self_join_counts_each_row_as_a_parent(tmp_path):
    contract = {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "p",
        "exposes": [
            {
                "exposeId": "employee",
                "contract": {
                    "schema": [{"name": "id", "type": "int"}, {"name": "manager", "type": "int"}]
                },
                "semantics": {
                    "relationships": [
                        {
                            "name": "reports_to",
                            "to": "employee",
                            "fromColumns": ["manager"],
                            "toColumns": ["id"],
                            "cardinality": "many_to_one",
                        }
                    ]
                },
            }
        ],
    }
    # 10 managers with 3 reports each; the 30 reports and the managers manage nobody else.
    rows = [{"id": str(m), "manager": None} for m in range(10)] + [
        {"id": str(100 + n), "manager": str(n % 10)} for n in range(30)
    ]
    source = _write(tmp_path / "employee.ndjson", rows)
    result = profile_exposes(contract, [ExposeSource("employee", source)], backend=FakeBackend())
    ((group,),) = [entry["statistics"][0]["groups"] for entry in result.document["relationships"]]
    assert group["k"] == 40
    assert group["binCounts"][0] == 30  # the reports have no one under them
    assert result.relationships[0].relationship.parent == "employee"


def test_the_document_reads_back_with_its_relationship(tmp_path):
    from flux_stats.profile import load_profile_text

    counts = [0] * 20 + [1] * 30 + [3] * 20
    result = _profile(tmp_path, _contract(), counts)
    profile, _ = load_profile_text(result.canonical.decode("utf-8"))
    (relationship,) = profile.relationships
    assert (relationship.parent_table, relationship.child_table) == ("org", "account")
    assert relationship.statistics[0].identifier == "marginalHistogram/1"


def test_the_summary_names_each_relationship(tmp_path, capsys):
    from fluid_build.cli.profile_cmd import _report

    result = _profile(tmp_path, _contract(), [0] * 20 + [1] * 30 + [2] * 20)
    lines = []
    _report(lambda text, **_: lines.append(text), result, tmp_path / "out.json")
    text = " ".join(lines)
    assert "Children per parent of exposes[org] in exposes[account] (owner):" in text
    assert runner.RelationshipResult is type(result.relationships[0])
