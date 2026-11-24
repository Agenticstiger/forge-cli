# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What a request of format 1-2-0 asks beside rank correlations, answered by an exact profile.

* ``cliqueColumns``: two-way histograms over pairs of the columns listed (``groupBy`` of a
  category, ``groupByBins`` of a number with a declared range);
* a relationship's ``groupBy``: children per parent grouped by a parent column;
* a relationship's ``linked``: a child column's histogram grouped through the link
  (``groupByLink``).

The real registry reads every document, and each count a group publishes is checked against
a count made here from the records.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from fluid_build.cli import main
from fluid_build.profiler import _stats, grouped
from fluid_build.profiler._stats import RequestTable, load_backend
from fluid_build.profiler.document import SCHEMA_URL, SCHEMA_URL_1_3_0, SCHEMA_URL_1_4_0
from fluid_build.profiler.runner import CHILDREN_EDGES
from tests.profiler import _shop as shop
from tests.profiler._fake_backend import FakeBackend

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.rank_correlation")


def _round5(count):
    quotient, remainder = divmod(count, 5)
    if 2 * remainder > 5 or (2 * remainder == 5 and quotient % 2 == 1):
        quotient += 1
    return quotient * 5


def _statistics(document, expose, column):
    return document["tables"][expose]["columns"][column].get("statistics", [])


def _grouped(document, expose, column, form):
    return [each for each in _statistics(document, expose, column) if form in each]


def _group(statistic, key):
    return next(group for group in statistic["groups"] if group["group"] == key)


# -- request 1-2-0, read by the registry ---------------------------------------------------


@pytest.mark.parametrize("backend", [load_backend, FakeBackend], ids=["flux-stats", "stand-in"])
def test_a_request_of_format_1_2_0_carries_each_ask(backend):
    asked = backend().read_request(shop.request_text(), name="shop.request.yml")
    orders = asked.table("orders")
    assert [(each.definition, each.columns) for each in orders.rank_correlations] == [
        ("kendallTau/1", ("quantity", "amount", "placed_at")),
        ("spearmanRho/1", ("quantity", "units")),
    ]
    assert orders.clique_columns == ("status", "quantity", "paid")
    assert asked.table("customers").rank_correlations == ()
    assert asked.table("customers").clique_columns == ()
    (link,) = asked.relationships
    assert link.group_by == ("region", "age")
    assert [(each.column, each.parent_column) for each in link.linked] == [
        ("status", "region"),
        ("paid", "region"),
    ]
    assert link.statistics == ("marginalHistogram/1",)


def test_a_request_of_an_earlier_format_asks_none_of_it():
    document = json.loads(shop.request_text())
    document["_schemaURL"] = document["_schemaURL"].replace("1-2-0", "1-0-0")
    for table in document["tables"].values():
        table.pop("rankCorrelations", None)
        table.pop("cliqueColumns", None)
    for link in document["relationships"]:
        link.pop("groupBy"), link.pop("linked")
    asked = load_backend().read_request(shop.request_text(document), name="old.yml")
    assert all(not t.rank_correlations and not t.clique_columns for t in asked.tables)
    (link,) = asked.relationships
    assert link.group_by == () and link.linked == ()
    assert link.statistics == ("marginalHistogram/1",)


def test_the_requests_of_the_tests_are_what_the_registry_reads():
    backend = load_backend()
    assert backend.read_request(shop.request_text(), name="r.yml").relationships


# -- cliqueColumns -------------------------------------------------------------------------


def test_a_category_pair_is_a_group_by_and_keeps_the_first_format(tmp_path):
    asked = shop.request(clique=("status", "paid"), relationship=False)
    result = shop.profile(tmp_path, asked, load_backend())
    assert result.document["_schemaURL"] == SCHEMA_URL
    (statistic,) = _grouped(result.document, "orders", "status", "groupBy")
    assert statistic["definition"] == "levelHistogram/1" and statistic["groupBy"] == "paid"
    assert statistic["levels"] == ["open", "paid", "void"]
    rows = shop.orders()
    held = Counter((str(row["paid"]).lower(), row["status"]) for row in rows)
    for key in ("false", "true"):
        group = _group(statistic, key)
        assert group["status"] == "F"
        total = sum(held[key, level] for level in statistic["levels"])
        assert group["k"] == _round5(total)
        assert group["levelCounts"] == [_round5(held[key, level]) for level in statistic["levels"]]
    assert [each["group"] for each in statistic["groups"]] == ["false", "true"]


def test_a_number_pair_is_a_group_by_bins_and_names_format_1_4_0(tmp_path):
    asked = shop.request(clique=("status", "quantity"), relationship=False)
    result = shop.profile(tmp_path, asked, load_backend())
    assert result.document["_schemaURL"] == SCHEMA_URL_1_4_0
    (statistic,) = _grouped(result.document, "orders", "status", "groupByBins")
    bins = statistic["groupByBins"]
    assert bins["column"] == "quantity"
    assert bins["bins"] == [str(edge) for edge in range(0, 101, 10)]
    # Every bin is a group, keyed by its lower edge, sorted as text.
    assert [each["group"] for each in statistic["groups"]] == sorted(bins["bins"][:-1])
    rows = shop.orders()
    held = Counter(((row["quantity"] // 10) * 10, row["status"]) for row in rows)
    for group in statistic["groups"]:
        assert group["status"] == "F"
        low = int(group["group"])
        assert group["levelCounts"] == [_round5(held[low, level]) for level in statistic["levels"]]


def test_the_first_column_of_a_pair_is_the_histogram_and_the_second_the_grouping(tmp_path):
    asked = shop.request(clique=("quantity", "paid", "status"), relationship=False)
    result = shop.profile(tmp_path, asked, load_backend())
    document = result.document
    quantity = _grouped(document, "orders", "quantity", "groupBy")
    assert sorted(each["groupBy"] for each in quantity) == ["paid", "status"]
    assert all(each["definition"] == "marginalHistogram/1" for each in quantity)
    paid = _grouped(document, "orders", "paid", "groupBy")
    assert [each["groupBy"] for each in paid] == ["status"]
    # No pair is listed twice, and none holds a column with itself.
    assert not _statistics(document, "orders", "status")


def test_the_registrys_reader_accepts_every_grouping(tmp_path):
    from flux_stats.profile import load_profile_text

    asked = shop.request(
        clique=("status", "quantity", "paid"),
        group_by=("region", "age"),
        linked=[("status", "region")],
    )
    result = shop.profile(tmp_path, asked, load_backend())
    profile, _ = load_profile_text(result.canonical.decode("utf-8"))
    assert result.document["_schemaURL"] == SCHEMA_URL_1_4_0
    status = profile.tables["orders"].columns["status"].statistics
    assert {s.group_by for s in status if s.group_by} == {"paid"}
    assert {s.group_by_bins.column for s in status if s.group_by_bins} == {"quantity"}
    assert {s.group_by_link.column for s in status if s.group_by_link} == {"region"}


def test_a_column_with_no_public_structure_or_a_key_is_left_out_with_its_reason(tmp_path):
    asked = shop.request(clique=("status", "units", "customer_id", "amount"))
    said = dict(shop.profile(tmp_path, asked, FakeBackend()).not_measured)
    for second in ("units", "amount"):
        assert "no public levels or bins" in said[f"status grouped by {second} of orders"]
    assert "key of a counted relationship" in said["status grouped by customer_id of orders"]
    assert "units grouped by amount of orders" in said


def _release_bins(column):
    """The edges, as the document spells them, the private release cuts *column* on."""
    from fluid_build.profiler import runner
    from fluid_build.profiler.dp import release

    backend = load_backend()
    expose = next(each for each in shop.CONTRACT["exposes"] if each["exposeId"] == "orders")
    plan = runner.plan_expose(shop.CONTRACT, "orders")
    structures = release.public_structures(expose, plan.columns, backend, 16)
    entry = next(each for each in expose["contract"]["schema"] if each["name"] == column)
    each = next(each for each in plan.columns if each.name == column)
    return list(structures.column(each, entry, backend).edge_texts)


def test_a_timestamp_is_grouped_on_the_public_bins_the_release_uses(tmp_path):
    asked = shop.request(clique=("status", "placed_at"), relationship=False)
    result = shop.profile(tmp_path, asked, load_backend())
    assert not [each for each in result.not_measured if "placed_at" in each[0]]
    (statistic,) = _grouped(result.document, "orders", "status", "groupByBins")
    bins = statistic["groupByBins"]
    assert bins["column"] == "placed_at"
    # the ladder is one bin for each year from 1900 to 2100, between the first and last instants
    assert len(bins["bins"]) == 203
    assert bins["bins"] == _release_bins("placed_at")
    assert [each["group"] for each in statistic["groups"]] == sorted(bins["bins"][:-1])
    # and the profile writes no histogram of the timestamp of its own
    assert not [
        each
        for each in _statistics(result.document, "orders", "placed_at")
        if each["definition"] == "marginalHistogram/1" and "groupBy" not in each
    ]


def test_a_pair_past_the_first_32_columns_is_left_out_and_named(tmp_path):
    names = tuple(f"c{index}" for index in range(34))
    table = RequestTable("orders", False, (), clique_columns=names)
    found, left_out = grouped.pair_statistics(
        table, {}, None, None, backend=FakeBackend(), fractional_bits=16
    )
    assert found == []
    past = [what for what, _ in left_out if "past the first 32" in _]
    assert past == [
        "c32 of the cliqueColumns of orders",
        "c33 of the cliqueColumns of orders",
    ]
    assert len(left_out) == 32 * 31 // 2 + 2


def test_an_unlisted_clique_asks_no_grouping_and_changes_nothing(tmp_path):
    result = shop.profile(tmp_path, shop.request(relationship=False), FakeBackend())
    assert result.document["_schemaURL"] == SCHEMA_URL
    for column in result.document["tables"]["orders"]["columns"].values():
        for each in column.get("statistics", ()):
            assert not {"groupBy", "groupByBins", "groupByLink"} & set(each)


def test_the_distinct_count_of_a_grouping_column_is_withheld(tmp_path):
    asked = shop.request(
        clique=("status", "quantity"), relationship=False, metrics=("distinctCount", "sum")
    )
    columns = shop.profile(tmp_path, asked, FakeBackend()).document["tables"]["orders"]["columns"]
    for name in ("quantity", "status"):
        assert columns[name]["metrics"]["distinctCount"]["status"] == "D"
    assert columns["quantity"]["metrics"]["sum"]["status"] == "D"


# -- a relationship's groupBy ----------------------------------------------------------------


def _children_per_customer():
    counts = Counter(row["customer_id"] for row in shop.orders() if row["customer_id"] is not None)
    return {row["customer_id"]: counts.get(row["customer_id"], 0) for row in shop.customers()}


def _cell(count):
    return max(index for index, edge in enumerate(CHILDREN_EDGES[:-1]) if count >= edge)


def test_children_per_parent_by_a_level_and_by_bins(tmp_path):
    asked = shop.request(group_by=("region", "age"))
    result = shop.profile(tmp_path, asked, load_backend())
    document = result.document
    assert document["_schemaURL"] == SCHEMA_URL_1_4_0
    (link,) = document["relationships"]
    plain, by_region, by_age = link["statistics"]
    assert "groupBy" not in plain and "groupByBins" not in plain
    assert by_region["groupBy"] == "region"
    assert by_age["groupByBins"]["column"] == "age"
    assert by_age["groupByBins"]["bins"] == [str(edge) for edge in range(0, 101, 10)]
    children = _children_per_customer()
    for statistic, key_of in (
        (by_region, lambda row: row["region"]),
        (by_age, lambda row: str((row["age"] // 10) * 10)),
    ):
        keys = [each["group"] for each in statistic["groups"]]
        assert keys == sorted(set(keys))
        for row_key in {key_of(row) for row in shop.customers()}:
            members = [row for row in shop.customers() if key_of(row) == row_key]
            cells = Counter(_cell(children[row["customer_id"]]) for row in members)
            group = _group(statistic, row_key)
            assert group["status"] == "F", row_key
            assert group["k"] == _round5(len(members))
            published = [count for count in group["binCounts"]]
            assert published == [_round5(cells[cell]) for cell in range(len(CHILDREN_EDGES) - 1)]
    assert plain["groups"][0]["k"] == _round5(shop.CUSTOMERS)


def test_a_parent_whose_value_is_null_is_in_no_group(tmp_path):
    customers = shop.customers()
    for row in customers[::10]:
        row["age"] = None
    asked = shop.request(group_by=("age",))
    result = shop.profile(
        tmp_path, asked, load_backend(), tables={"customers": customers, "orders": shop.orders()}
    )
    (link,) = result.document["relationships"]
    by_age = link["statistics"][1]
    assert [each["group"] for each in by_age["groups"]] == sorted(
        by_age["groupByBins"]["bins"][:-1]
    )
    assert "" not in [each["group"] for each in by_age["groups"]]
    children = _children_per_customer()
    for group in by_age["groups"]:
        if group["status"] != "F":
            continue
        members = [
            row
            for row in customers
            if row["age"] is not None and str((row["age"] // 10) * 10) == group["group"]
        ]
        assert group["k"] == _round5(len(members))
        cells = Counter(_cell(children[row["customer_id"]]) for row in members)
        assert group["binCounts"] == [_round5(cells[i]) for i in range(len(CHILDREN_EDGES) - 1)]
    assert any(each["status"] == "F" for each in by_age["groups"])


def test_a_small_group_withholds_its_siblings_in_a_relationship_grouping(tmp_path):
    customers = shop.customers()
    for row in customers:
        row["region"] = "north" if row["customer_id"] >= 4 else "south"
    result = shop.profile(
        tmp_path,
        shop.request(group_by=("region",)),
        load_backend(),
        tables={"customers": customers, "orders": shop.orders()},
    )
    by_region = result.document["relationships"][0]["statistics"][1]
    assert {each["group"]: each["status"] for each in by_region["groups"]} == {
        "north": "C",
        "south": "C",
        "west": "C",
    }
    assert all(set(each) == {"group", "status"} for each in by_region["groups"])


def test_a_parent_column_with_no_public_groups_is_reported(tmp_path):
    asked = shop.request(group_by=("customer_id", "region"))
    result = shop.profile(tmp_path, asked, FakeBackend())
    said = dict(result.not_measured)
    (reason,) = [why for what, why in said.items() if "grouped by customers.customer_id" in what]
    assert "no public levels or bins" in reason
    statistics = result.document["relationships"][0]["statistics"]
    assert [each.get("groupBy") for each in statistics] == [None, "region"]


def test_a_group_by_is_reported_when_the_histogram_is_not_listed(tmp_path):
    asked = shop.request(group_by=("region",), statistics=("mean/1",))
    result = shop.profile(tmp_path, asked, FakeBackend())
    assert "relationships" not in result.document
    said = dict(result.not_measured)
    (reason,) = [why for what, why in said.items() if what.startswith("groupBy of children")]
    assert "marginalHistogram/1 is listed" in reason


def test_the_parent_column_of_a_grouping_withholds_its_distinct_count(tmp_path):
    asked = shop.request(
        group_by=("region", "age"), metrics=("distinctCount", "sum", "min", "max", "quantiles")
    )
    columns = shop.profile(tmp_path, asked, FakeBackend()).document["tables"]["customers"][
        "columns"
    ]
    assert columns["region"]["metrics"]["distinctCount"]["status"] == "D"
    for name in ("sum", "min", "max", "quantiles", "distinctCount"):
        assert columns["age"]["metrics"][name]["status"] == "D"


# -- a relationship's linked -------------------------------------------------------------------


def _by_link(rows, customers, column):
    region = {row["customer_id"]: row["region"] for row in customers}
    held = Counter()
    for row in rows:
        if row["customer_id"] in region:
            held[region[row["customer_id"]], str(row[column]).lower()] += 1
    return held


def test_a_child_column_is_grouped_by_the_parent_row_its_key_reaches(tmp_path):
    asked = shop.request(linked=[("status", "region"), ("paid", "region")], statistics=("mean/1",))
    rows = shop.orders([999, None] * 15)  # 30 orders no customer row holds, or with no customer
    result = shop.profile(
        tmp_path,
        asked,
        load_backend(),
        tables={"customers": shop.customers(), "orders": rows},
    )
    document = result.document
    assert document["_schemaURL"] == SCHEMA_URL_1_3_0
    assert "relationships" not in document  # histograms were not listed, so not counted
    for column, levels in (("status", ["open", "paid", "void"]), ("paid", ["false", "true"])):
        (statistic,) = _grouped(document, "orders", column, "groupByLink")
        assert statistic["groupByLink"] == {
            "parent": {"table": "customers", "columns": ["customer_id"]},
            "columns": ["customer_id"],
            "column": "region",
        }
        held = _by_link(rows, shop.customers(), column)
        assert [each["group"] for each in statistic["groups"]] == list(shop.REGIONS)
        for group in statistic["groups"]:
            assert group["status"] == "F"
            expected = [_round5(held[group["group"], level]) for level in levels]
            cells = group.get("levelCounts") or group["binCounts"]
            assert cells == expected


def test_an_unreached_group_too_small_to_publish_withholds_every_group(tmp_path):
    asked = shop.request(linked=[("status", "region")], statistics=("mean/1",))
    rows = shop.orders([999, None, 999])  # 3 records in no group: a group of too few
    result = shop.profile(
        tmp_path,
        asked,
        load_backend(),
        tables={"customers": shop.customers(), "orders": rows},
    )
    (statistic,) = _grouped(result.document, "orders", "status", "groupByLink")
    assert [each["status"] for each in statistic["groups"]] == ["C", "C", "C"]
    assert all(set(each) == {"group", "status"} for each in statistic["groups"])
    assert "" not in [each["group"] for each in statistic["groups"]]


def test_a_link_lists_the_parents_levels_whatever_the_records_hold(tmp_path):
    customers = [row for row in shop.customers() if row["region"] != "west"]
    keep = {row["customer_id"] for row in customers}
    rows = [row for row in shop.orders() if row["customer_id"] in keep]
    asked = shop.request(linked=[("status", "region")], statistics=("mean/1",))
    result = shop.profile(
        tmp_path, asked, load_backend(), tables={"customers": customers, "orders": rows}
    )
    (statistic,) = _grouped(result.document, "orders", "status", "groupByLink")
    assert [each["group"] for each in statistic["groups"]] == list(shop.REGIONS)


def test_a_link_by_a_parent_column_without_levels_is_reported(tmp_path):
    asked = shop.request(linked=[("status", "age"), ("units", "region")], statistics=("mean/1",))
    said = dict(shop.profile(tmp_path, asked, FakeBackend()).not_measured)
    assert "only the levels an enum" in said["status of orders grouped by customers.age"]
    assert "no public levels or bins" in said["units of orders grouped by customers.region"]


def test_a_child_that_reaches_two_parents_in_different_groups_reaches_none():
    reached = grouped.link_assignment(
        [(1,), (2,), (3,), (None,), (4,)],
        [(1,), (1,), (2,), (2,), (None,), (3,)],
        ["a", "b", "c", "c", "a", None],
    )
    assert reached == (None, "c", None, None, None)


# -- what the profile leaves alone -------------------------------------------------------------


def test_the_group_keys_come_from_the_contract_not_the_records(tmp_path):
    customers = shop.customers()
    result = shop.profile(
        tmp_path,
        shop.request(group_by=("age", "region"), clique=("status", "quantity")),
        load_backend(),
        tables={"customers": customers, "orders": shop.orders()[:50]},
    )
    text = json.dumps(result.document)
    for statistic in result.document["relationships"][0]["statistics"][1:]:
        assert [each["group"] for each in statistic["groups"]] in (
            list(shop.REGIONS),
            sorted(statistic["groupByBins"]["bins"][:-1]) if "groupByBins" in statistic else [],
        )
    assert "999" not in text.replace("9007199254740991", "")


def test_a_record_outside_the_levels_a_grouping_declares_refuses_the_profile(tmp_path):
    from fluid_build.profiler.runner import ProfileError

    rows = shop.orders()
    rows[7]["status"] = "lost"
    with pytest.raises(ProfileError) as caught:
        shop.profile(
            tmp_path,
            shop.request(clique=("paid", "status"), relationship=False),
            FakeBackend(),
            tables={"customers": shop.customers(), "orders": rows},
        )
    assert caught.value.event == "profile_record_outside_declared_domain"
    assert "lost" not in str(caught.value)
    assert "row 7" in str(caught.value)


# -- the command ----------------------------------------------------------------------------


def test_the_cli_answers_the_asks_and_names_what_it_leaves_out(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    asks = json.loads(shop.request_text())
    asks["tables"]["orders"]["cliqueColumns"].append("units")
    (tmp_path / "shop.fluid.yaml").write_text(shop.CONTRACT_TEXT, encoding="utf-8")
    (tmp_path / "shop.request.yml").write_text(shop.request_text(asks), encoding="utf-8")
    shop.write(tmp_path, {"customers": shop.customers(), "orders": shop.orders()})
    backend = load_backend()
    monkeypatch.setattr(_stats, "load_backend", lambda: backend)
    argv = [
        "profile",
        "shop.fluid.yaml",
        "--request",
        "shop.request.yml",
        "--source",
        "customers=customers.ndjson",
        "--source",
        "orders=orders.ndjson",
        "--out",
        "p.json",
    ]
    assert main(argv) == 0
    out = capsys.readouterr().out
    document = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert document["_schemaURL"] == SCHEMA_URL_1_4_0
    assert "Rank correlations: 4 of 4 published." in out
    assert "quantity bins" in out and "customers.region through its link" in out
    assert "Not measured: status grouped by units of orders: units: it has no public levels" in out


def test_the_help_text_names_each_ask():
    import argparse

    from fluid_build.cli import profile_cmd

    parser = argparse.ArgumentParser()
    profile_cmd.register(parser.add_subparsers())
    text = profile_cmd._DESCRIPTION
    for word in ("rankCorrelations", "cliqueColumns", "groupBy", "linked", "--row-floor", "1-4-0"):
        assert word in text
