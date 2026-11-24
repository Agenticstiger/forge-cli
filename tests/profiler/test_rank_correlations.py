# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Rank correlations of column pairs: ``rankCorrelations`` of a request of format 1-2-0.

Kendall's ``S`` and Spearman's ``A4`` are checked against a reference that follows the
definition word for word, over every small data set, ties included, through the registry and
through the stand-in. The profile a request of format 1-2-0 gets is read by the registry's own
reader.
"""

from __future__ import annotations

import itertools
from fractions import Fraction
from math import comb

import pytest

from fluid_build.cli import main
from fluid_build.profiler import _stats
from fluid_build.profiler._stats import RequestRankCorrelation, load_backend
from fluid_build.profiler.document import SCHEMA_URL, SCHEMA_URL_1_4_0
from fluid_build.profiler.runner import ProfileError
from tests.profiler import _shop as shop
from tests.profiler._fake_backend import FakeBackend

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.rank_correlation")

KENDALL, SPEARMAN = "kendallTau/1", "spearmanRho/1"

#: The orders the profiles read: a few dozen, since the stand-in forms ``S`` pair by pair.
ORDERS = shop.orders()[:90]


def _profile(directory, asked, backend, *, rows=None, tables=None, **options):
    tables = tables or {"customers": shop.customers(), "orders": rows or [dict(r) for r in ORDERS]}
    return shop.profile(directory, asked, backend, tables=tables, **options)


def _sign(value):
    return (value > 0) - (value < 0)


def reference_s(pairs):
    """The sum over every pair of records of ``sign(x_i - x_j) * sign(y_i - y_j)``."""
    return sum(_sign(x - u) * _sign(y - v) for (x, y), (u, v) in itertools.combinations(pairs, 2))


def _mid_ranks(values):
    """The mean of the positions, counted from 1, the records of each value hold."""
    ordered = sorted(values)
    return [
        Fraction(
            sum(i + 1 for i, held in enumerate(ordered) if held == value), ordered.count(value)
        )
        for value in values
    ]


def reference_a4(pairs):
    """Four times ``sum (R_i - (n + 1) / 2) * (Q_i - (n + 1) / 2)`` over the mid-ranks."""
    n = len(pairs)
    left = _mid_ranks([x for x, _ in pairs])
    right = _mid_ranks([y for _, y in pairs])
    total = sum(
        (r - Fraction(n + 1, 2)) * (q - Fraction(n + 1, 2))
        for r, q in zip(left, right, strict=True)
    )
    assert (4 * total).denominator == 1
    return int(4 * total)


def _data_sets():
    """Every data set of up to 5 records over 3 values, and of 3 or fewer over 5: ties abound."""
    for size, values in ((1, 3), (2, 3), (3, 3), (4, 3), (5, 3), (2, 5), (3, 5)):
        yield from itertools.product(itertools.product(range(values), repeat=2), repeat=size)


@pytest.mark.parametrize("backend", [load_backend, FakeBackend], ids=["flux-stats", "stand-in"])
def test_s_and_a4_equal_the_definitions_over_every_small_data_set(backend):
    backend = backend()
    seen = 0
    for pairs in _data_sets():
        pairs = list(pairs)
        kendall = backend.rank_group(KENDALL, pairs)
        spearman = backend.rank_group(SPEARMAN, pairs)
        assert kendall == {"group": "", "k": len(pairs), "S": str(reference_s(pairs))}
        assert spearman == {"group": "", "k": len(pairs), "A4": str(reference_a4(pairs))}
        seen += 1
    assert seen == sum(9**n for n in (1, 2, 3, 4, 5)) + 25**2 + 25**3


def test_the_reference_is_the_known_values_on_small_examples():
    assert reference_s([(1, 3), (2, 2), (3, 1)]) == -3
    assert reference_s([(1, 1), (2, 2), (3, 3)]) == 3
    # A pair tied in either column adds nothing.
    assert reference_s([(1, 1), (1, 2), (2, 2)]) == 1
    # The centred ranks are -1, 0 and 1: 4 * (1 + 0 + 1) = 8.
    assert reference_a4([(1, 1), (2, 2), (3, 3)]) == 8
    assert reference_a4([(1, 3), (2, 2), (3, 1)]) == -8
    # Two tied records share the mid-rank 1.5: centred ranks -1/2, -1/2, 1 give 4 * 3/2.
    assert reference_a4([(1, 1), (1, 1), (2, 2)]) == 6


def _kendall_asked(*, columns=("quantity", "amount", "placed_at"), spearman=None):
    asked = [RequestRankCorrelation(KENDALL, columns)]
    if spearman is not None:
        asked.append(RequestRankCorrelation(SPEARMAN, spearman))
    return shop.request(rank=asked)


def _grid_points(value):
    return int(Fraction(value) * 2**16 // 1)


def _expected_pairs(rows, first, second):
    """What the two columns of *rows* give a reference: floored values of the records both hold."""
    from datetime import datetime

    def read(row, name):
        value = row[name]
        if value is None:
            return None
        if name == "placed_at":
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(moment.timestamp() * 1000)
        return Fraction(str(value))

    pairs = []
    for row in rows:
        x, y = read(row, first), read(row, second)
        if x is not None and y is not None:
            pairs.append((_grid_points(x), _grid_points(y)))
    return pairs


@pytest.mark.parametrize("backend", [load_backend, FakeBackend], ids=["flux-stats", "stand-in"])
def test_the_profile_holds_the_pair_statistics_the_references_give(tmp_path, backend):
    rows = [dict(row) for row in ORDERS]
    rows[3]["amount"] = None  # a record with one value unknown is not counted
    asked = _kendall_asked(spearman=("quantity", "units"))
    result = _profile(
        tmp_path, asked, backend(), tables={"customers": shop.customers(), "orders": rows}
    )
    document = result.document
    assert document["_schemaURL"] == SCHEMA_URL_1_4_0
    entries = document["tables"]["orders"]["columnPairs"]
    # Each unordered pair of the columns listed, the one listed first first, in request order.
    assert [entry["columns"] for entry in entries] == [
        ["quantity", "amount"],
        ["quantity", "placed_at"],
        ["amount", "placed_at"],
        ["quantity", "units"],
    ]
    for entry in entries:
        first, second = entry["columns"]
        pairs = _expected_pairs(rows, first, second)
        reference = {KENDALL: reference_s, SPEARMAN: reference_a4}
        for statistic in entry["statistics"]:
            (group,) = statistic["groups"]
            assert group["status"] == "F" and group["group"] == "" and group["k"] == len(pairs)
            assert "rowFloor" not in statistic and "noise" not in statistic
            key = "S" if statistic["definition"] == KENDALL else "A4"
            assert group[key] == str(reference[statistic["definition"]](pairs))
    assert [s["definition"] for s in entries[3]["statistics"]] == [SPEARMAN]
    assert len(rows) - 1 == entries[0]["statistics"][0]["groups"][0]["k"]


def test_both_definitions_of_one_pair_share_an_entry_and_follow_one_order(tmp_path):
    asked = shop.request(
        rank=[
            RequestRankCorrelation(SPEARMAN, ("quantity", "amount")),
            RequestRankCorrelation(KENDALL, ("quantity", "amount")),
        ]
    )
    result = _profile(tmp_path, asked, FakeBackend())
    (entry,) = result.document["tables"]["orders"]["columnPairs"]
    assert entry["columns"] == ["quantity", "amount"]
    assert [each["definition"] for each in entry["statistics"]] == [KENDALL, SPEARMAN]


def test_the_registrys_reader_accepts_the_document_and_reads_the_values(tmp_path):
    from flux_stats.profile import load_profile_text

    result = _profile(tmp_path, _kendall_asked(spearman=("quantity", "units")), load_backend())
    profile, _ = load_profile_text(result.canonical.decode("utf-8"))
    pairs = profile.tables["orders"].pairs
    assert [entry.columns for entry in pairs][0] == ("quantity", "amount")
    kendall = pairs[0].statistics[0].groups[0]
    n = kendall.statistic.k
    assert kendall.value == Fraction(kendall.statistic.rank_s, comb(n, 2))


@pytest.mark.parametrize("backend", [load_backend, FakeBackend], ids=["flux-stats", "stand-in"])
def test_a_row_floor_is_written_and_forms_the_value(tmp_path, backend):
    from flux_stats.profile import load_profile_text

    result = _profile(tmp_path, _kendall_asked(), backend(), row_floor=10_000)
    pairs = result.document["tables"]["orders"]["columnPairs"]
    assert all(
        statistic["rowFloor"] == 10_000 for entry in pairs for statistic in entry["statistics"]
    )
    profile, _ = load_profile_text(result.canonical.decode("utf-8"))
    group = profile.tables["orders"].pairs[0].statistics[0].groups[0]
    # N = max(n, floor): the floor is above the records, so the value is shrunk.
    assert group.value == Fraction(group.statistic.rank_s, comb(10_000, 2))
    assert result.document["_schemaURL"] == SCHEMA_URL_1_4_0


@pytest.mark.parametrize("floor", [1, 0, -5, 2**53])
def test_a_row_floor_below_two_is_refused_before_a_record_is_read(tmp_path, floor):
    with pytest.raises(ProfileError) as caught:
        _profile(tmp_path, _kendall_asked(), FakeBackend(), row_floor=floor)
    assert caught.value.event == "profile_row_floor_invalid"


def test_a_pair_of_fewer_than_minimum_cell_records_is_withheld_with_its_status(tmp_path):
    tables = {"customers": shop.customers(), "orders": ORDERS[:7]}
    result = _profile(
        tmp_path, _kendall_asked(spearman=("quantity", "units")), FakeBackend(), tables=tables
    )
    for entry in result.document["tables"]["orders"]["columnPairs"]:
        for statistic in entry["statistics"]:
            assert statistic["groups"] == [{"group": "", "status": "A"}]
    assert result.withheld.get("A", 0) >= 4


def test_a_pair_with_fewer_than_two_records_is_never_published(tmp_path):
    from fluid_build.profiler import disclosure

    permissive = disclosure.DisclosureRule("permissive", 1, False, 1, 2, "0.90", "0.10")
    tables = {"customers": shop.customers(), "orders": ORDERS[:1]}
    result = _profile(tmp_path, _kendall_asked(), FakeBackend(), tables=tables, rule=permissive)
    for entry in result.document["tables"]["orders"]["columnPairs"]:
        assert entry["statistics"][0]["groups"] == [{"group": "", "status": "A"}]


def test_a_key_of_a_counted_relationship_takes_no_rank_correlation(tmp_path):
    asked = shop.request(
        rank=[RequestRankCorrelation(KENDALL, ("customer_id", "quantity", "units"))]
    )
    result = _profile(tmp_path, asked, FakeBackend())
    pairs = result.document["tables"]["orders"]["columnPairs"]
    assert [entry["columns"] for entry in pairs] == [["quantity", "units"]]
    said = dict(result.not_measured)
    assert (
        "key of a counted relationship" in said["kendallTau/1 of orders.customer_id and quantity"]
    )
    assert "orders.customer_id and units" in " ".join(said)


def test_a_column_that_is_not_a_number_or_timestamp_is_reported(tmp_path):
    asked = shop.request(rank=[RequestRankCorrelation(KENDALL, ("quantity", "status"))])
    result = _profile(tmp_path, asked, FakeBackend())
    assert "columnPairs" not in result.document["tables"]["orders"]
    ((what, reason),) = [item for item in result.not_measured if "kendallTau" in item[0]]
    assert what == "kendallTau/1 of orders.quantity and status"
    assert "not a number or timestamp" in reason


def test_a_request_that_asks_none_keeps_its_format_and_writes_no_pair(tmp_path):
    result = _profile(tmp_path, shop.request(), FakeBackend())
    assert result.document["_schemaURL"] == SCHEMA_URL
    assert "columnPairs" not in result.document["tables"]["orders"]


def test_no_value_is_named_in_what_is_left_out(tmp_path):
    asked = shop.request(rank=[RequestRankCorrelation(KENDALL, ("customer_id", "quantity"))])
    result = _profile(tmp_path, asked, FakeBackend())
    text = " ".join(f"{what} {why}" for what, why in result.not_measured)
    assert "12.50" not in text and "2026-07" not in text


def _argv(*more):
    return [
        "profile",
        "shop.fluid.yaml",
        "--request",
        "shop.request.yml",
        "--source",
        "customers=customers.ndjson",
        "--source",
        "orders=orders.ndjson",
        *more,
    ]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "shop.fluid.yaml").write_text(shop.CONTRACT_TEXT, encoding="utf-8")
    (tmp_path / "shop.request.yml").write_text(shop.request_text(), encoding="utf-8")
    shop.write(tmp_path, {"customers": shop.customers(), "orders": ORDERS})
    backend = FakeBackend()
    monkeypatch.setattr(_stats, "load_backend", lambda: backend)
    return tmp_path


def test_the_cli_takes_a_row_floor_and_says_what_it_published(workspace, capsys):
    import json

    rc = main(_argv("--out", "p.json", "--row-floor", "500"))
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Rank correlations: 4 of 4 published, each with rowFloor 500." in out
    document = json.loads((workspace / "p.json").read_text(encoding="utf-8"))
    (first, *_), *_ = [e["statistics"] for e in document["tables"]["orders"]["columnPairs"]]
    assert first["rowFloor"] == 500
    assert document["_schemaURL"] == SCHEMA_URL_1_4_0


def test_the_cli_writes_no_row_floor_unless_given(workspace, capsys):
    import json

    assert main(_argv("--out", "p.json")) == 0
    document = json.loads((workspace / "p.json").read_text(encoding="utf-8"))
    for entry in document["tables"]["orders"]["columnPairs"]:
        assert all("rowFloor" not in each for each in entry["statistics"])
    assert "each with rowFloor" not in capsys.readouterr().out


@pytest.mark.parametrize("floor", ["1", "0", "-3", "x", "9007199254740992"])
def test_the_cli_refuses_a_row_floor_that_is_not_a_count_of_two_or_more(workspace, floor, capsys):
    with pytest.raises(SystemExit) as caught:
        main(_argv("--row-floor", floor))
    assert caught.value.code == 2
    assert "--row-floor" in capsys.readouterr().err


def test_the_cli_refuses_a_row_floor_without_a_request(workspace, capsys):
    rc = main(
        [
            "profile",
            "shop.fluid.yaml",
            "--expose",
            "orders",
            "--source",
            "orders.ndjson",
            "--row-floor",
            "10",
        ]
    )
    captured = capsys.readouterr()
    assert rc != 0
    assert "profile_row_floor_needs_request" in captured.out + captured.err


def test_the_stand_in_writes_the_bytes_the_registry_does(tmp_path):
    asked = shop.request(
        rank=[
            RequestRankCorrelation(KENDALL, ("quantity", "amount", "placed_at")),
            RequestRankCorrelation(SPEARMAN, ("quantity", "units")),
        ],
        clique=("status", "quantity", "paid"),
        group_by=("region", "age"),
        linked=[("status", "region")],
    )
    real = _profile(tmp_path, asked, load_backend(), row_floor=20)
    fake = _profile(tmp_path, asked, FakeBackend(), row_floor=20)
    assert fake.canonical == real.canonical
    assert fake.not_measured == real.not_measured
