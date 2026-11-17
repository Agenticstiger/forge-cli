# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Profiling one expose end to end, with a stand-in for the flux-stats registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fluid_build.profiler import runner
from fluid_build.profiler._stats import ReadRefused, StatisticsUnavailable, load_backend
from fluid_build.profiler.document import digest_of
from fluid_build.profiler.runner import ProfileError, column_domain, plan_expose, profile_expose
from tests.profiler._fake_backend import FakeBackend

pytestmark = pytest.mark.unit


def _contract(*columns, expose_id="orders", more_exposes=()):
    exposes = [
        {
            "exposeId": expose_id,
            "kind": "table",
            "contract": {"schema": [{"name": n, "type": t} for n, t in columns]},
        }
    ]
    exposes += [{"exposeId": other, "kind": "table"} for other in more_exposes]
    return {"fluidVersion": "0.7.6", "kind": "DataProduct", "id": "p", "exposes": exposes}


ORDERS = _contract(
    ("amount", "decimal(10,2)"),
    ("market", "string"),
    ("placed_at", "timestamp"),
    ("note", "string"),
    ("payload", "json"),
    ("score", "double"),
)


def _slice(tmp_path: Path, rows) -> str:
    path = tmp_path / "orders.ndjson"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return str(path)


def _rows(count=40):
    rows = []
    for n in range(count):
        rows.append(
            {
                "amount": f"{(n % 4) * 10}.50",  # four values, each held by count/4 records
                "market": ["IE", "GB"][n % 2],
                "placed_at": f"2026-07-{1 + n % 2:02d}T10:00:00.000Z",
                "note": None if n % 10 == 0 else f"note {n}",
                "payload": {"n": n},
                "score": "0.5",  # a slice holds no JSON float, profiled column or not
            }
        )
    return rows


@pytest.mark.parametrize(
    "type_name, domain",
    [
        ("string", "category"),
        ("VARCHAR(255)", "category"),
        ("uuid", "category"),
        ("decimal(18,4)", "number"),
        ("BIGINT", "number"),
        ("year", "number"),
        ("timestamp", "timestamp"),
        ("Timestamp With Time Zone", "timestamp"),
        ("date", "timestamp"),
    ],
)
def test_a_fluid_type_is_read_in_its_domain(type_name, domain):
    assert column_domain(type_name) == (domain, None)


@pytest.mark.parametrize(
    "type_name, words",
    [
        ("double", "binary floating point"),
        ("float64", "binary floating point"),
        ("double precision", "binary floating point"),
        ("json", "nested"),
        ("array", "nested"),
        ("bytea", "bytes"),
        ("time", "time of day"),
        ("geometry", "geospatial"),
        ("boolean", "no reading for a boolean"),
        ("bit", "no reading for a boolean"),
        ("mystery", "not one fluid profile reads"),
        (None, "not one fluid profile reads"),
    ],
)
def test_a_type_with_no_domain_is_left_out_with_its_reason(type_name, words):
    domain, reason = column_domain(type_name)
    assert domain is None and words in reason


def test_the_expose_is_the_named_one_or_the_only_one():
    assert runner.resolve_expose_id(ORDERS, None) == "orders"
    assert runner.resolve_expose_id(ORDERS, "orders") == "orders"
    with pytest.raises(ProfileError) as missing:
        runner.resolve_expose_id(ORDERS, "refunds")
    assert missing.value.event == "profile_expose_not_found"
    assert missing.value.context["exposes"] == "orders"
    two = _contract(("a", "int"), more_exposes=("refunds",))
    with pytest.raises(ProfileError) as ambiguous:
        runner.resolve_expose_id(two, None)
    assert ambiguous.value.event == "profile_expose_required"


def test_the_plan_names_every_column_left_out():
    plan = plan_expose(ORDERS, "orders")
    assert [(c.name, c.domain) for c in plan.columns] == [
        ("amount", "number"),
        ("market", "category"),
        ("placed_at", "timestamp"),
        ("note", "category"),
    ]
    assert [name for name, _ in plan.skipped] == ["payload", "score"]


def test_an_expose_without_columns_or_with_a_column_twice_is_refused():
    with pytest.raises(ProfileError, match="declares no contract.schema"):
        plan_expose(_contract(), "orders")
    with pytest.raises(ProfileError) as twice:
        plan_expose(_contract(("a", "int"), ("a", "int")), "orders")
    assert twice.value.event == "profile_column_declared_twice"


def test_an_expose_with_nothing_profilable_is_refused_naming_why(tmp_path):
    contract = _contract(("blob", "bytes"))
    with pytest.raises(ProfileError) as raised:
        profile_expose(
            contract, "orders", source=_slice(tmp_path, [{"blob": "x"}]), backend=FakeBackend()
        )
    assert raised.value.event == "profile_no_column_to_profile"
    assert "blob: bytes have no profile domain" in raised.value.context["not profiled"]


def _metrics(result, column):
    return result.document["tables"]["orders"]["columns"][column]["metrics"]


def test_a_profile_publishes_what_passes_and_withholds_the_rest(tmp_path):
    backend = FakeBackend()
    result = profile_expose(ORDERS, "orders", source=_slice(tmp_path, _rows()), backend=backend)
    table = result.document["tables"]["orders"]
    assert table["rowCount"] == 40
    assert set(table["columns"]) == {"amount", "market", "placed_at", "note"}

    amount = _metrics(result, "amount")
    assert amount["count"]["value"] == 40
    assert amount["nullCount"]["status"] == "A"  # a zero is disclosive
    assert "value" not in amount["nullCount"]
    assert amount["distinctCount"]["value"] == 5  # 4, rounded to a multiple of 5
    assert amount["sum"] == {
        "definition": "sum/1",
        "status": "F",
        "sampleSize": {"min": 38, "max": 42},
        "disclosure": "acro-default",
        "value": "620",
    }
    # Every value of amount is held by 10 records, so the order statistics publish.
    assert amount["min"]["value"] == "0.5" and amount["max"]["value"] == "30.5"
    assert amount["quantiles"]["values"] == {"0.25": "0.5", "0.5": "10.5", "0.75": "20.5"}

    placed = _metrics(result, "placed_at")
    assert placed["min"]["value"] == "1782900000000"
    assert "sum" not in placed

    note = _metrics(result, "note")
    assert note["nullCount"]["status"] == "A"  # 4 nulls: a small count
    assert note["count"]["value"] == 35  # 36 rounded
    assert set(note) == {"count", "nullCount", "distinctCount"}

    # 2 distinct markets round to 0, and a published 0 would read as a zero cell.
    assert _metrics(result, "market")["distinctCount"]["status"] == "A"

    # The four nullCounts (three zeros and a 4), and the distinctCount of market and
    # placed_at (2 values each, which round to 0).
    assert result.withheld == {"A": 6}
    assert result.published == 1 + sum(
        1
        for col in table["columns"].values()
        for m in col["metrics"].values()
        if m["status"] == "F"
    )
    assert backend.checked == [result.canonical.decode("utf-8")]
    assert result.digest == digest_of(result.canonical)


def test_an_order_statistic_too_few_records_hold_is_withheld(tmp_path):
    rows = _rows()
    for n, row in enumerate(rows):
        row["amount"] = f"{n}.00"  # every value held by one record
    result = profile_expose(ORDERS, "orders", source=_slice(tmp_path, rows), backend=FakeBackend())
    amount = _metrics(result, "amount")
    for name in ("min", "max", "quantiles"):
        assert amount[name]["status"] == "A" and "value" not in amount[name]
        assert "values" not in amount[name]
    assert amount["sum"]["status"] == "F"


def test_a_sum_one_record_dominates_is_withheld(tmp_path):
    rows = _rows()
    rows[0]["amount"] = "1000000.00"
    result = profile_expose(ORDERS, "orders", source=_slice(tmp_path, rows), backend=FakeBackend())
    assert _metrics(result, "amount")["sum"]["status"] == "O"
    assert "1000000" not in result.canonical.decode("utf-8")


def test_a_small_table_publishes_nothing_but_stays_a_valid_profile(tmp_path):
    backend = FakeBackend()
    result = profile_expose(ORDERS, "orders", source=_slice(tmp_path, _rows(6)), backend=backend)
    table = result.document["tables"]["orders"]
    assert "rowCount" not in table
    statuses = {m["status"] for c in table["columns"].values() for m in c["metrics"].values()}
    assert statuses == {"A"}
    assert result.published == 0
    bands = {
        json.dumps(m["sampleSize"])
        for c in table["columns"].values()
        for m in c["metrics"].values()
    }
    assert bands == {'{"min": 0, "max": 9}'}


def test_the_same_records_give_the_same_bytes(tmp_path):
    source = _slice(tmp_path, _rows())
    first = profile_expose(ORDERS, "orders", source=source, backend=FakeBackend())
    rows = list(reversed(_rows()))
    second_path = tmp_path / "again"
    second_path.mkdir()
    second = profile_expose(
        ORDERS, "orders", source=_slice(second_path, rows), backend=FakeBackend()
    )
    assert first.canonical == second.canonical
    assert first.digest == second.digest


def test_a_value_the_reader_refuses_stops_the_profile(tmp_path):
    rows = _rows()
    rows[3]["placed_at"] = "2026-07-01 10:00:00"  # no offset
    with pytest.raises(ReadRefused, match="row 3"):
        profile_expose(ORDERS, "orders", source=_slice(tmp_path, rows), backend=FakeBackend())


def test_the_registry_is_not_wired_until_flux_stats_is_published():
    with pytest.raises(StatisticsUnavailable, match="flux-stats"):
        load_backend()


def test_the_sum_of_an_indicator_with_few_ones_is_withheld(tmp_path):
    rows = _rows()
    for n, row in enumerate(rows):
        row["amount"] = "1" if n < 3 else "0"
    result = profile_expose(ORDERS, "orders", source=_slice(tmp_path, rows), backend=FakeBackend())
    assert _metrics(result, "amount")["sum"]["status"] == "A"


def test_a_constant_column_withholds_its_sum_and_publishes_its_value(tmp_path):
    rows = _rows()
    for row in rows:
        row["amount"] = "7.00"
    result = profile_expose(ORDERS, "orders", source=_slice(tmp_path, rows), backend=FakeBackend())
    amount = _metrics(result, "amount")
    assert amount["sum"]["status"] == "C"
    assert amount["min"]["value"] == amount["max"]["value"] == "7"  # every record holds it
    assert amount["distinctCount"]["status"] == "A"  # 1 rounds to 0
