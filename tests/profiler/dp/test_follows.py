# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A unit that follows its child rows: the tree, the truncation and what a unit moves."""

from __future__ import annotations

import copy
import random
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import pytest

from fluid_build.profiler import _stats
from fluid_build.profiler.dp import follow_release as fr
from fluid_build.profiler.dp import release
from fluid_build.profiler.dp.follows import parse_follow, plan_follows
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.dp import _follow_data as fd

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.noise")

BACKEND = FakeBackend()


def _contract() -> Dict[str, Any]:
    """The contract with two more relationships: a cycle between invoice and line, and a self one."""
    contract = copy.deepcopy(fd.CONTRACT)
    exposes = {each["exposeId"]: each for each in contract["exposes"]}
    exposes["invoice"]["contract"]["schema"].append({"name": "last_line", "type": "INT"})
    exposes["invoice"]["semantics"]["relationships"].append(
        {
            "name": "last",
            "to": "line",
            "fromColumns": ["last_line"],
            "toColumns": ["line_id"],
            "cardinality": "many_to_one",
        }
    )
    exposes["account"]["contract"]["schema"].append({"name": "referrer", "type": "INT"})
    exposes["account"]["semantics"]["relationships"] = [
        {
            "name": "referred_by",
            "to": "account",
            "fromColumns": ["referrer"],
            "toColumns": ["id"],
            "cardinality": "many_to_one",
        }
    ]
    return contract


def _plan_follows(*texts: str) -> Any:
    return plan_follows(_contract(), "account", fd.specs(*texts), BACKEND)


# ---------------------------------------------------------------------------
# The flag and the tree
# ---------------------------------------------------------------------------


def test_a_follow_flag_is_read_and_a_malformed_one_is_refused():
    spec = parse_follow("account.id=invoice.account_id:3")
    assert (spec.parent_table, spec.parent_columns) == ("account", ("id",))
    assert (spec.child_table, spec.child_columns, spec.max_children) == (
        "invoice",
        ("account_id",),
        3,
    )
    two = parse_follow("a.x,y=b.p,q:12")
    assert two.parent_columns == ("x", "y") and two.child_columns == ("p", "q")
    assert two.text() == "a.x,y=b.p,q:12"
    for bad in (
        "account.id=invoice.account_id",
        "account.id=invoice.account_id:0",
        "account.id=invoice.account_id:-1",
        "account=invoice.account_id:3",
        "account.id,id=invoice.account_id,x:3",
        "a.x,y=b.p:3",
        "account.id=invoice.account_id:three",
        "",
    ):
        with pytest.raises(ValueError):
            parse_follow(bad)


def test_the_reach_is_the_product_of_the_caps_along_the_path():
    tree = _plan_follows(*fd.FOLLOWS)
    assert dict(tree.reach) == {"account": 1, "invoice": 3, "line": 6}
    assert tree.order == ("account", "invoice", "line")
    assert [each.name for each in tree.edges] == ["placed_by", "in_invoice"]
    # the order the flags are given in does not matter
    again = _plan_follows(fd.FOLLOW_LINES, fd.FOLLOW_INVOICES)
    assert again.order == tree.order and dict(again.reach) == dict(tree.reach)


@pytest.mark.parametrize(
    ("follows", "why"),
    [
        (
            # two followed keys into one table
            ("account.id=invoice.account_id:1", "product.product_id=invoice.product_id:1"),
            "child of one followed relationship only",
        ),
        (
            # a followed parent that the unit does not reach
            ("invoice.invoice_id=line.invoice_id:2",),
            "the unit's expose or a table another followed relationship reaches",
        ),
        (
            # a cycle that never reaches the expose
            ("invoice.invoice_id=line.invoice_id:2", "line.line_id=invoice.last_line:2"),
            "no cycle",
        ),
        (
            # the expose as a child
            ("account.id=account.referrer:2",),
            "never the child of a followed relationship",
        ),
        (
            # a relationship the contract does not declare
            ("account.id=line.invoice_id:2",),
            "declares no relationship",
        ),
        (
            # the same relationship twice
            (fd.FOLLOW_INVOICES, fd.FOLLOW_INVOICES),
            "child of one followed relationship only",
        ),
    ],
)
def test_a_follow_that_is_not_a_tree_rooted_at_the_expose_is_refused_naming_the_entry(follows, why):
    with pytest.raises(release.ReleaseRefused) as refused:
        _plan_follows(*follows)
    assert refused.value.event == "profile_release_follow_refused"
    message = str(refused.value)
    assert why in message
    assert any(f"--follow {text}" in message for text in follows)
    assert refused.value.context["read"] == "nothing: no record was read"


def test_the_refusal_names_the_entry_that_breaks_the_tree():
    with pytest.raises(release.ReleaseRefused) as refused:
        _plan_follows(
            fd.FOLLOW_INVOICES, fd.FOLLOW_LINES, "product.product_id=invoice.product_id:1"
        )
    assert "--follow product.product_id=invoice.product_id:1" in str(refused.value)


def test_the_unit_must_be_an_expose_of_the_contract():
    with pytest.raises(release.ReleaseRefused) as refused:
        plan_follows(fd.CONTRACT, "nowhere", fd.specs(fd.FOLLOW_INVOICES), BACKEND)
    assert refused.value.event == "profile_release_unit_unknown"


def test_the_seam_names_the_entry_of_the_registrys_refusal():
    entries = [e.entry() for e in fd.specs(fd.FOLLOW_INVOICES, fd.FOLLOW_INVOICES)]
    with pytest.raises(_stats.FollowRefused) as refused:
        BACKEND.follow_reach("account", entries)
    assert str(refused.value).startswith("follows[1]")


# ---------------------------------------------------------------------------
# The truncation
# ---------------------------------------------------------------------------


def _rows(plan, data):
    return fr.encode_follow_rows(plan, fd.source_columns(plan, data), BACKEND)


def _ids(data: Mapping[str, Sequence[Mapping[str, Any]]], kept, table: str, column: str):
    return [data[table][at][column] for at in kept[table]]


def _kept(plan, data, seed=fd.SEED):
    return fr.truncated(plan, _rows(plan, data), seed)


def test_truncation_keeps_at_most_the_cap_of_each_parents_children_and_cascades():
    plan = fd.plan()
    data = fd.tables(40, heavy=15)
    kept = _kept(plan, data)
    invoices = _ids(data, kept, "invoice", "account_id")
    assert max(invoices.count(owner) for owner in set(invoices)) == 3
    assert invoices.count(1) == 3  # the heavy account holds 15 and keeps 3
    lines = _ids(data, kept, "line", "invoice_id")
    assert max(lines.count(each) for each in set(lines)) <= 2
    # a dropped invoice takes its lines, and a line follows a kept invoice only
    kept_invoices = set(_ids(data, kept, "invoice", "invoice_id"))
    assert set(lines) <= kept_invoices
    dropped = {each["invoice_id"] for each in data["invoice"]} - kept_invoices
    assert dropped
    assert not set(lines) & dropped
    # every account is kept: the expose is the unit
    assert len(kept["account"]) == len(data["account"])


def test_rows_that_follow_no_kept_row_are_dropped():
    plan = fd.plan()
    data = fd.tables(20)
    data["invoice"].append(
        {"invoice_id": 9001, "account_id": 777, "product_id": 1, "status": "open", "qty": 1}
    )
    data["line"].append({"line_id": 9001, "invoice_id": 9001, "kind": "a"})
    data["line"].append({"line_id": 9002, "invoice_id": 8888, "kind": "a"})
    kept = _kept(plan, data)
    assert 9001 not in _ids(data, kept, "invoice", "invoice_id")
    assert not {9001, 9002} & set(_ids(data, kept, "line", "line_id"))


def test_what_a_unit_keeps_depends_on_the_seed_and_on_that_unit_alone():
    plan = fd.plan()
    data = fd.tables(30, heavy=12)
    base = _kept(plan, data)
    whole = {
        owner: [
            each["invoice_id"]
            for at in base["invoice"]
            if (each := data["invoice"][at])["account_id"] == owner
        ]
        for owner in range(1, 31)
    }
    # take another unit away, and add one: nobody else's children change
    other = copy.deepcopy(data)
    other["account"] = [each for each in other["account"] if each["id"] != 2]
    other["invoice"] = [each for each in other["invoice"] if each["account_id"] != 2]
    other["account"].append({"id": 99, "segment": "business", "age": 3})
    other["invoice"].extend(
        {"invoice_id": 7000 + n, "account_id": 99, "product_id": 1, "status": "open", "qty": 1}
        for n in range(8)
    )
    after = _kept(plan, other)
    for owner in range(1, 31):
        if owner == 2:
            continue
        mine = [
            each["invoice_id"]
            for at in after["invoice"]
            if (each := other["invoice"][at])["account_id"] == owner
        ]
        assert mine == whole[owner]
    # the same seed draws the same rows; another seed keeps other children of the heavy account
    assert _kept(plan, data) == base
    changed = [
        _ids(data, _kept(plan, data, bytes([n]) * 32), "invoice", "invoice_id") for n in range(1, 5)
    ]
    assert any(each != _ids(data, base, "invoice", "invoice_id") for each in changed)


def test_what_a_unit_keeps_does_not_depend_on_the_order_of_the_records():
    plan = fd.plan()
    data = fd.tables(30, heavy=12)
    base = _kept(plan, data)
    shuffled = {table: list(rows) for table, rows in data.items()}
    rng = random.Random(3)
    for rows in shuffled.values():
        rng.shuffle(rows)
    again = _kept(plan, shuffled)
    # a row is what the release reads of it: two lines of one invoice and one kind are the same row
    for table, columns in (
        ("invoice", ("invoice_id", "status", "qty")),
        ("line", ("invoice_id", "kind")),
        ("account", ("id", "segment", "age")),
    ):
        assert sorted(_read(data, base, table, columns)) == sorted(
            _read(shuffled, again, table, columns)
        )


def _read(data, kept, table, columns):
    return [tuple(data[table][at][name] for name in columns) for at in kept[table]]


# ---------------------------------------------------------------------------
# What the plan charges
# ---------------------------------------------------------------------------


def _parts(plan):
    return {each.part.label: each for each in fr.follow_parts(plan, {})}


def test_each_table_is_charged_its_reach_and_each_relationship_its_case():
    plan = fd.plan()
    assert dict(plan.reach) == {"account": 1, "invoice": 3, "line": 6}
    parts = _parts(plan)
    assert (parts["account/noise/segment"].l1, parts["account/noise/segment"].sensitivity) == (1, 1)
    assert (parts["invoice/noise/status"].l1, parts["invoice/noise/status"].sensitivity) == (3, 9)
    assert (parts["line/noise/kind"].l1, parts["line/noise/kind"].sensitivity) == (6, 36)
    # a followed relationship: the parent is reached and the child only through it, m_P = 1
    followed = parts["relationship/account.id/invoice.account_id"]
    assert (followed.l1, followed.sensitivity) == (1, 1)
    grouped = parts["relationship/account.id/invoice.account_id/by/segment"]
    assert (grouped.l1, grouped.sensitivity) == (1, 1)
    # a parent the unit reaches and a child it does not: m_P again
    fixed_child = parts["relationship/account.id/ticket.account_id"]
    assert (fixed_child.l1, fixed_child.sensitivity) == (1, 1)
    # a parent the unit does not reach and a child it does: 2 m_C, and 2 m_C^2 squared
    child_only = parts["relationship/product.product_id/invoice.product_id"]
    assert (child_only.l1, child_only.sensitivity) == (6, 18)
    # a statistic grouped through a link is a count table of the child's table
    link = parts["link/invoice.status/by/account.segment/via/account_id"]
    assert (link.l1, link.sensitivity) == (3, 9)
    other = parts["link/invoice.status/by/product.category/via/product_id"]
    assert (other.l1, other.sensitivity) == (3, 9)


def test_the_bins_of_children_per_parent_are_the_caps_or_a_public_ladder():
    plan = fd.plan()
    by = {each.label: each for each in plan.relations}
    followed = by["relationship/account.id/invoice.account_id"]
    assert followed.edges == (0, 1, 2, 3, 4) and followed.cap == 3  # no bin starts above the cap
    ladder = by["relationship/account.id/ticket.account_id"]
    assert ladder.edges == fr.CHILDREN_LADDER and ladder.edges[-1] == 10**9
    assert ladder.cap is None
    assert by["relationship/product.product_id/invoice.product_id"].edges == fr.CHILDREN_LADDER


def test_a_cap_with_more_bins_than_the_cell_limit_refuses_naming_the_entry():
    entry = "account.id=invoice.account_id:1000000000"
    with pytest.raises(release.ReleaseRefused) as refused:
        fd.plan(follows=(entry, fd.FOLLOW_LINES))
    assert refused.value.event == "profile_release_too_many_cells"
    assert entry in str(refused.value)


def test_a_count_beyond_the_ladder_refuses_naming_the_relationship_only():
    plan = fd.plan()
    relation = next(
        each for each in plan.relations if each.label == "relationship/account.id/ticket.account_id"
    )
    ok = fr._children_cells(relation, [0, 1, 10**9], BACKEND, 16)
    assert ok[0] == 0 and ok[-1] == len(relation.edges) - 2
    with pytest.raises(release.ReleaseRefused) as refused:
        fr._children_cells(relation, [3, 10**9 + 1], BACKEND, 16)
    assert refused.value.event == "profile_release_children_beyond_ladder"
    assert "ticket(account_id) to account(id)" in str(refused.value)
    assert "1000000001" not in str(refused.value)


def test_a_reached_table_is_never_charged_less_than_its_reach_and_a_fixed_one_not_at_all():
    plan = fd.plan()
    labels = [each.part.label for each in fr.follow_parts(plan, {})]
    assert not any(label.startswith(("product/", "ticket/")) for label in labels)
    assert plan.fixed and {name for name, _ in plan.fixed} == {"product", "ticket"}
    assert {each.table: each.reach for each in plan.tables} == {
        "account": 1,
        "invoice": 3,
        "line": 6,
    }


def test_the_groups_of_a_noisy_statistic_are_contract_levels_or_public_bins():
    release_plan = fd.plan()
    drawn = fd.draw(release_plan, fd.tables())
    document = drawn.document
    segments = document["relationships"][0]["statistics"]
    by_level = next(each for each in segments if each.get("groupBy") == "segment")
    assert [group["group"] for group in by_level["groups"]] == ["business", "personal"]
    by_bins = next(each for each in segments if "groupByBins" in each)
    edges = by_bins["groupByBins"]["bins"]
    assert [group["group"] for group in by_bins["groups"]] == edges[:-1]
    assert all("variance" not in each for each in _walk(document))
    assert document["privacy"]["publicStructure"] is True


def _walk(value):
    if isinstance(value, dict):
        yield value
        for each in value.values():
            yield from _walk(each)
    elif isinstance(value, list):
        for each in value:
            yield from _walk(each)


def test_a_parent_column_with_no_public_levels_is_not_released_and_a_labelled_one_warns():
    plan = fd.plan()
    # the product's category is labelled pii and the product table is fixed
    assert len(plan.warnings) == 1
    assert "product.category" in plan.warnings[0] and "P1" in plan.warnings[0]
    asked = fd.request(
        relationships=[
            {
                "parent": {"table": "account", "columns": ["id"]},
                "child": {"table": "invoice", "columns": ["account_id"]},
                "statistics": ["marginalHistogram/1"],
                "groupBy": ["id"],
                "linked": [{"column": "qty", "parentColumn": "age"}],
            }
        ]
    )
    plan = fd.plan(asked=asked)
    reasons = dict(plan.not_released)
    assert any("a key of a relationship" in why for why in reasons.values())
    assert any("only the levels an enum" in why for why in reasons.values())
    assert plan.warnings == ()


def test_a_link_under_a_parent_the_unit_reaches_without_following_it_is_not_released():
    contract = _contract()
    exposes = {each["exposeId"]: each for each in contract["exposes"]}
    exposes["ticket"]["contract"]["schema"].append({"name": "invoice_id", "type": "INT"})
    exposes["ticket"]["semantics"]["relationships"].append(
        {
            "name": "about",
            "to": "invoice",
            "fromColumns": ["invoice_id"],
            "toColumns": ["invoice_id"],
            "cardinality": "many_to_one",
        }
    )
    document = fd.request_document(
        relationships=[
            {
                "parent": {"table": "invoice", "columns": ["invoice_id"]},
                "child": {"table": "ticket", "columns": ["invoice_id"]},
                "statistics": ["marginalHistogram/1"],
                "linked": [{"column": "severity", "parentColumn": "status"}],
            }
        ],
    )
    document["tables"]["ticket"]["columns"]["invoice_id"] = {"domain": "number"}
    import json

    asked = BACKEND.read_request(json.dumps(document), name="x")
    # the tickets of an account are followed too, so the ticket and the invoice are both reached
    plan = fr.plan_follow_release(
        contract,
        asked,
        BACKEND,
        unit="account",
        rho=Fraction(1),
        follows=fd.specs(fd.FOLLOW_INVOICES, "account.id=ticket.account_id:2"),
    )
    reasons = dict(plan.not_released)
    assert any("severity of ticket grouped by invoice.status" in what for what in reasons)
    assert any("another way" in why for why in reasons.values())
    assert not plan.relations and not plan.links


def test_without_a_part_to_release_the_plan_refuses_before_a_record_is_read():
    document = fd.request_document(relationships=[])
    for table in document["tables"].values():
        for column in table["columns"].values():
            column.pop("statistics", None)
    import json

    asked = BACKEND.read_request(json.dumps(document), name="x")
    with pytest.raises(release.ReleaseRefused) as refused:
        fd.plan(asked=asked)
    assert refused.value.event == "profile_release_nothing_to_release"


def test_a_row_floor_is_refused_when_no_reached_table_holds_a_rank_correlation():
    with pytest.raises(release.ReleaseRefused) as refused:
        fd.plan(row_floor=100)
    assert refused.value.event == "profile_release_row_floor_unused"


# ---------------------------------------------------------------------------
# What one unit moves: every neighbour of a small domain
# ---------------------------------------------------------------------------

PRODUCTS = (1, 2)


def _tickets(fresh: int) -> List[Dict[str, Any]]:
    """Fixed tickets: two for the unit about to be added, one for an old unit, an orphan."""
    return [
        {"ticket_id": 1, "account_id": fresh, "severity": "lo"},
        {"ticket_id": 2, "account_id": fresh, "severity": "hi"},
        {"ticket_id": 3, "account_id": 1, "severity": "hi"},
        {"ticket_id": 4, "account_id": 555, "severity": "lo"},
    ]


def _product_rows() -> List[Dict[str, Any]]:
    return [
        {"product_id": 1, "category": "x", "tier": "lo"},
        {"product_id": 2, "category": "y", "tier": "hi"},
        {"product_id": 3, "category": "z", "tier": "lo"},
    ]


def _invoice(number: int, owner: int, product: int, status: str) -> Dict[str, Any]:
    return {
        "invoice_id": number,
        "account_id": owner,
        "product_id": product,
        "status": status,
        "channel": ("web", "shop")[number % 2],
        "qty": number % 11,
        "units": number % 6,
    }


def _unit(
    fresh: int, segment: str, invoices: Sequence[Tuple[int, int]], first: int
) -> Dict[str, List[Dict[str, Any]]]:
    """One unit: an account and its invoices ``(product, lines)``, ids from *first*."""
    found: Dict[str, List[Dict[str, Any]]] = {"account": [], "invoice": [], "line": []}
    found["account"].append({"id": fresh, "segment": segment, "age": 10 + fresh % 80})
    for place, (product, lines) in enumerate(invoices):
        number = first + 10 * place
        status = ("open", "paid")[(product + lines) % 2]
        found["invoice"].append(_invoice(number, fresh, product, status))
        for each in range(lines):
            found["line"].append(
                {
                    "line_id": number + each + 1,
                    "invoice_id": number,
                    "kind": "ab"[(number + each) % 2],
                }
            )
    return found


def _combine(*parts: Mapping[str, Sequence[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    found: Dict[str, List[Dict[str, Any]]] = {
        "account": [],
        "invoice": [],
        "line": [],
        "ticket": [],
        "product": [],
    }
    for each in parts:
        for table, rows in each.items():
            found[table].extend(rows)
    return found


def _bases(fresh: int) -> List[Dict[str, List[Dict[str, Any]]]]:
    small = _combine(
        _unit(1, "business", [(1, 1), (1, 0)], 100),
        _unit(2, "personal", [], 200),
        {"ticket": _tickets(fresh), "product": _product_rows()},
    )
    # orphans: an invoice no account holds, a line no invoice holds
    small["invoice"].append(_invoice(901, 777, 2, "open"))
    small["line"].append({"line_id": 902, "invoice_id": 901, "kind": "a"})
    big = _combine(
        _unit(1, "business", [(1, 3)] * 5, 300),
        _unit(2, "personal", [(2, 2), (1, 1), (2, 0)], 400),
        _unit(3, "business", [(2, 3)], 500),
        _unit(4, "personal", [(1, 0)] * 4, 600),
        {"ticket": _tickets(fresh), "product": _product_rows()},
    )
    return [small, big]


def _measure(plan, data, seed=fd.SEED):
    rows = _rows(plan, data)
    kept = fr.truncated(plan, rows, seed)
    _, tables = fr.describe(plan, {})
    return fr.measure_counts(plan, rows, kept, tables, BACKEND), tables


def _check_every_neighbour(plan, units, bases, fresh):
    parts = _parts(plan)
    worst: Dict[str, Tuple[int, int]] = {}
    for base in bases:
        before, tables = _measure(plan, base)
        for unit in units:
            added = _combine(base, unit)
            for table in ("account", "invoice", "line"):
                # the unit's rows come last, and then first: the order of the records is no matter
                added[table] = list(base[table]) + list(unit[table])
            after, _ = _measure(plan, added)
            for table in tables:
                label = table.label
                if table.kind == "rank":
                    continue
                before_cells, after_cells = before[label], after[label]
                assert len(before_cells) == len(after_cells)
                change = [b - a for a, b in zip(before_cells, after_cells, strict=True)]
                l1, l2 = sum(map(abs, change)), sum(each * each for each in change)
                part = parts[label]
                assert l1 <= part.l1, (label, l1, part.l1)
                assert l2 <= part.sensitivity, (label, l2, part.sensitivity)
                low = worst.get(label, (0, 0))
                worst[label] = (max(low[0], l1), max(low[1], l2))
    return worst


def test_one_unit_moves_every_table_by_at_most_what_it_is_charged_two_tables():
    plan = fd.plan(follows=(fd.FOLLOW_INVOICES,))
    fresh = 100
    units = []
    for segment in ("business", "personal"):
        for count in range(0, 5):  # more than the cap of 3, so the truncation acts
            for chosen in _product_sequences((1, 2, 3), count):
                units.append(_unit(fresh, segment, [(p, 0) for p in chosen], 1000))
    worst = _check_every_neighbour(plan, units, _bases(fresh), fresh)
    # the bounds are reached, not merely respected, by the count tables and the followed histogram
    assert worst["account/noise/segment"] == (1, 1)
    assert worst["invoice/noise/status"][0] == 3 and worst["invoice/noise/status"][1] == 9
    assert worst["relationship/account.id/invoice.account_id"] == (1, 1)
    assert worst["relationship/account.id/ticket.account_id"] == (1, 1)
    # three children on three products leave one bin and enter another: 2 m_C, reached
    assert worst["relationship/product.product_id/invoice.product_id"][0] == 6
    assert worst["link/invoice.status/by/account.segment/via/account_id"][0] == 3


def _product_sequences(products: Sequence[int], count: int):
    if count == 0:
        yield ()
        return
    for head in products:
        for rest in _product_sequences(products, count - 1):
            yield (head, *rest)


def test_one_unit_moves_every_table_by_at_most_what_it_is_charged_three_tables():
    plan = fd.plan(
        follows=("account.id=invoice.account_id:2", "invoice.invoice_id=line.invoice_id:2")
    )
    assert dict(plan.reach) == {"account": 1, "invoice": 2, "line": 4}
    fresh = 100
    units = []
    for segment in ("business", "personal"):
        for count in range(0, 4):  # three invoices, one more than the cap of 2
            for chosen in _product_sequences(PRODUCTS, count):
                for lines in _product_sequences((0, 1, 2, 3), count):  # up to three lines each
                    units.append(_unit(fresh, segment, list(zip(chosen, lines, strict=True)), 1000))
    worst = _check_every_neighbour(plan, units, _bases(fresh), fresh)
    assert worst["line/noise/kind"][0] == 4 and worst["line/noise/kind"][1] <= 16
    assert worst["invoice/noise/status"][0] == 2
    assert worst["relationship/product.product_id/invoice.product_id"][1] <= 8
    assert max(sensitivity for _, sensitivity in worst.values()) >= 4


def test_a_unit_that_is_one_row_with_no_children_moves_the_tables_by_one():
    plan = fd.plan(follows=(fd.FOLLOW_INVOICES,))
    before, _ = _measure(plan, _bases(100)[0])
    after, _ = _measure(plan, _combine(_bases(100)[0], _unit(100, "business", [], 1000)))
    assert (
        sum(
            abs(a - b)
            for a, b in zip(
                before["account/noise/segment"], after["account/noise/segment"], strict=True
            )
        )
        == 1
    )


# ---------------------------------------------------------------------------
# Rank correlations and cliques of a reached table scale by its reach
# ---------------------------------------------------------------------------


def _rich_plan(**options):
    return fd.plan(asked=fd.request(rich=True), **options)


def test_a_rank_correlation_of_a_reached_table_is_charged_its_reach_times_the_registrys():
    plan = _rich_plan(row_floor=100)
    invoice = plan.table("invoice")
    assert invoice.reach == 3 and plan.row_floor == 100
    assert invoice.plan.delta("kendallTau/1") == 3 * Fraction(4, 101)
    assert invoice.plan.delta("spearmanRho/1") == 3 * Fraction(9, 101)
    assert not plan.table("account").plan.rank_pairs and not plan.table("line").plan.rank_pairs
    _, tables = fr.describe(plan, {})
    ranks = [each for each in tables if each.kind == "rank"]
    assert [each.table for each in ranks] == ["invoice", "invoice"]
    share = Fraction(ranks[0].share)
    for each in ranks:
        assert each.scale_units == fr.rank_noise.scale_units(share, each.delta * each.delta)
    drawn = fd.draw(plan, fd.tables(40), select=_choose_status_channel)
    pairs = drawn.document["tables"]["invoice"]["columnPairs"]
    assert [each["definition"] for each in pairs[0]["statistics"]] == [
        "kendallTau/1",
        "spearmanRho/1",
    ]
    assert all(each["rowFloor"] == 100 for each in pairs[0]["statistics"])


SELECTION_CALLS: List[Tuple[Any, ...]] = []


def _choose_status_channel(rows, names, levels, weights, rho, distance, method="aim"):
    SELECTION_CALLS.append((tuple(names), rho, distance, method))
    return [("status", "channel")]


def test_cliques_are_chosen_on_the_tables_the_unit_changes_with_the_distance_of_their_reach():
    SELECTION_CALLS.clear()
    plan = _rich_plan()
    assert plan.selects
    assert [each.table for each in plan.tables if each.plan.selects] == ["invoice"]
    drawn = fd.draw(plan, fd.tables(40), select=_choose_status_channel)
    assert [(names, distance) for names, _, distance, _ in SELECTION_CALLS] == [
        (("status", "channel"), 3)
    ]
    # the tenth of the total is spent choosing, and the table states it, once
    block = drawn.document["tables"]["invoice"]
    assert block["cliqueSelection"]["selection"]["privacyLoss"]["rho"] == "0.1"
    assert block["cliques"][0]["columns"] == ["status", "channel"]
    assert "cliques" not in drawn.document["tables"]["account"]
    assert "cliqueSelection" not in drawn.document["tables"]["line"]
    assert "product" not in {each.table for each in drawn.tables}
    # a clique is a count table of the table: its squared L2 sensitivity is m_T^2
    clique = next(each for each in drawn.tables if each.kind == "clique")
    assert clique.sensitivity == 9
    assert drawn.selected == {"invoice": (("status", "channel"),)}


def test_the_tenth_spent_choosing_is_split_over_the_tables_that_choose_cliques():
    contract = copy.deepcopy(fd.CONTRACT)
    exposes = {each["exposeId"]: each for each in contract["exposes"]}
    exposes["account"]["contract"]["schema"].append(
        {"name": "tier", "type": "enum('lo','hi')", "required": True}
    )
    document = fd.request_document(rich=True)
    document["tables"]["account"]["columns"]["tier"] = {
        "domain": "category",
        "statistics": ["levelHistogram/1"],
    }
    document["tables"]["account"]["cliqueColumns"] = ["segment", "tier"]
    import json

    asked = BACKEND.read_request(json.dumps(document), name="x")
    plan = fr.plan_follow_release(
        contract,
        asked,
        BACKEND,
        unit="account",
        rho=Fraction(1),
        follows=fd.specs(),
    )
    shares = [release.selection_share(each.plan) for each in plan.tables if each.plan.selects]
    assert shares == [Fraction(1, 20)] * 2
    assert sum(shares) <= Fraction(1, 10)
