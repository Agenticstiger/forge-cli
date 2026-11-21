# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A requested fact: the share of a table's rows that satisfy a contract rule, as rounded counts.

Each fact of a profile request becomes an entry of the profile's ``invariants`` section
(PROFILE-SPEC section 5, format 1-2-0): one ungrouped ``levelHistogram/1`` over the levels
``false`` and ``true``. The two counts pass the output checks as cells do, and either cell
withheld withholds the group. The measuring is :mod:`fluid_build.profiler.facts`; these tests
are about what the profile does with it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.cli import main
from fluid_build.profiler import _stats
from fluid_build.profiler.runner import ExposeSource, ProfileError, profile_exposes
from tests.profiler._fake_backend import FakeBackend

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.request")

INVARIANTS: List[Dict[str, Any]] = [
    {"id": "non_negative", "check": "amount >= 0"},
    {"id": "paid_has_date", "when": "status = 'paid'", "check": "paid_at IS NOT NULL"},
    {"id": "total_is_price", "derive": "total", "expr": "price"},
    {"id": "lines", "derive": "qty", "aggregate": {"function": "count", "from": "l", "via": "o"}},
    {"id": "same", "dependency": {"from": ["status"], "to": ["amount"]}},
]

CONTRACT: Dict[str, Any] = {
    "exposes": [
        {
            "exposeId": "orders",
            "contract": {
                "schema": [
                    {"name": "amount", "type": "decimal(10,2)"},
                    {"name": "status", "type": "string"},
                    {"name": "paid_at", "type": "string"},
                    {"name": "price", "type": "decimal(10,2)"},
                    {"name": "total", "type": "decimal(10,2)"},
                    {"name": "qty", "type": "integer"},
                    {"name": "seen", "type": "string"},
                ],
                "invariants": INVARIANTS,
            },
        }
    ]
}


def _request(*rules: str, columns=("seen",)):
    facts = tuple(_stats.RequestFact(f"orders.{rule}", "orders", rule) for rule in rules)
    table = _stats.RequestTable(
        "orders", False, tuple(_stats.RequestColumn(name, "category") for name in columns)
    )
    return _stats.Request(None, 16, (table,), (), facts)


def _profile(tmp_path: Path, rows, *rules: str):
    path = tmp_path / "orders.ndjson"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return profile_exposes(
        CONTRACT,
        [ExposeSource("orders", str(path))],
        backend=FakeBackend(),
        request=_request(*rules),
    )


def _row(amount="1.00", status="open", paid_at=None, price="1.00", total="1.00"):
    return {
        "amount": amount,
        "status": status,
        "paid_at": paid_at,
        "price": price,
        "total": total,
        "qty": 1,
        "seen": "x",
    }


def _entry(result, name):
    (entry,) = [each for each in result.document["invariants"] if each["name"] == name]
    (statistic,) = entry["statistics"]
    return statistic


def test_a_check_is_counted_as_rounded_counts_and_the_document_names_format_1_2_0(tmp_path):
    rows = [_row(amount="5.00")] * 60 + [_row(amount="-1.00")] * 40
    result = _profile(tmp_path, rows, "non_negative")
    statistic = _entry(result, "orders.non_negative")
    assert statistic["definition"] == "levelHistogram/1" and statistic["levels"] == [
        "false",
        "true",
    ]
    (group,) = statistic["groups"]
    assert group["levelCounts"] == [40, 60] and group["k"] == 100
    assert result.document["_schemaURL"].endswith("/profile/1-2-0/FluxProfile.json")
    assert [(each.name, each.published) for each in result.facts] == [("orders.non_negative", True)]


def test_the_document_reads_back_and_its_share_is_what_the_engines_reader_computes(tmp_path):
    from flux_stats.profile import load_profile_text

    rows = [_row(amount="5.00")] * 60 + [_row(amount="-1.00")] * 40
    result = _profile(tmp_path, rows, "non_negative")
    loaded, _ = load_profile_text(result.canonical.decode("utf-8"))
    (entry,) = loaded.invariants
    assert entry.counts() == (60, 100)


def test_a_when_covers_only_the_rows_where_it_is_true_and_a_null_when_covers_none(tmp_path):
    rows = (
        [_row(status="paid", paid_at="2026-01-01")] * 30
        + [_row(status="paid", paid_at=None)] * 20
        + [_row(status="open", paid_at=None)] * 50
        + [{**_row(), "status": None}] * 20
    )
    result = _profile(tmp_path, rows, "paid_has_date")
    (group,) = _entry(result, "orders.paid_has_date")["groups"]
    assert group["levelCounts"] == [20, 30]


def test_a_derive_holds_when_the_column_is_not_distinct_from_the_value(tmp_path):
    rows = (
        [_row(price="2.00", total="2.00")] * 40
        + [_row(price="2.00", total="3.00")] * 30
        + [{**_row(price="1.00"), "total": None}] * 30
    )
    result = _profile(tmp_path, rows, "total_is_price")
    (group,) = _entry(result, "orders.total_is_price")["groups"]
    assert group["levelCounts"] == [60, 40]


def test_a_small_cell_withholds_both_cells_and_a_zero_does_too(tmp_path):
    few = [_row(amount="5.00")] * 95 + [_row(amount="-1.00")] * 3
    (group,) = _entry(_profile(tmp_path, few, "non_negative"), "orders.non_negative")["groups"]
    assert group == {"group": "", "status": "C"}
    none = [_row(amount="5.00")] * 100
    result = _profile(tmp_path, none, "non_negative")
    (group,) = _entry(result, "orders.non_negative")["groups"]
    assert group == {"group": "", "status": "C"}
    assert [(each.published, each.code) for each in result.facts] == [(False, "A")]


def test_a_null_in_a_compared_column_is_false_in_two_valued_truth(tmp_path):
    rows = [_row(amount="5.00")] * 50 + [{**_row(), "amount": None}] * 50
    result = _profile(tmp_path, rows, "non_negative")
    (group,) = _entry(result, "orders.non_negative")["groups"]
    assert group["levelCounts"] == [50, 50]


def test_the_rules_that_cannot_be_measured_are_reported_with_their_reasons(tmp_path):
    result = _profile(tmp_path, [_row()] * 40, "lines", "same", "non_negative")
    said = dict(result.not_measured)
    assert "aggregate reads child rows" in said["fact orders.lines"]
    assert "row order" in said["fact orders.same"]
    assert {each["name"] for each in result.document["invariants"]} == {"orders.non_negative"}


def test_a_column_a_rule_reads_is_read_and_never_profiled(tmp_path):
    result = _profile(tmp_path, [_row()] * 40, "non_negative")
    assert set(result.document["tables"]["orders"]["columns"]) == {"seen"}


def test_a_fact_the_contract_does_not_hold_is_refused_before_a_record_is_read(tmp_path):
    for rule, event in (("ghost", "profile_request_fact_rule_unknown"),):
        with pytest.raises(ProfileError) as caught:
            profile_exposes(
                CONTRACT,
                [ExposeSource("orders", str(tmp_path / "never-read.ndjson"))],
                backend=FakeBackend(),
                request=_request(rule),
            )
        assert caught.value.event == event
    other = _stats.Request(
        None,
        16,
        _request().tables,
        (),
        (_stats.RequestFact("ghost.rule", "ghost", "rule"),),
    )
    with pytest.raises(ProfileError) as caught:
        profile_exposes(
            CONTRACT,
            [ExposeSource("orders", str(tmp_path / "never-read.ndjson"))],
            backend=FakeBackend(),
            request=other,
        )
    assert caught.value.event == "profile_request_fact_table_unknown"


def test_a_value_the_rule_cannot_read_refuses_without_showing_it(tmp_path):
    rows = [_row()] * 40 + [{**_row(), "amount": "SECRET-91"}]
    with pytest.raises(ProfileError) as caught:
        _profile(tmp_path, rows, "non_negative")
    assert caught.value.event == "profile_request_fact_value_unreadable"
    assert "SECRET-91" not in str(caught.value)


def test_a_cli_run_on_the_request_fixture_reports_each_fact(tmp_path, monkeypatch, capsys):
    fixtures = Path(__file__).parent / "fixtures" / "request"
    monkeypatch.chdir(tmp_path)
    for name in ("shop.fluid.yaml", "shop.profile-request.yml"):
        (tmp_path / name).write_text((fixtures / name).read_text(encoding="utf-8"))
    orders = [
        {
            "order_id": n,
            "customer_id": n % 80,
            "status": ["open", "paid", "void"][n % 3],
            "quantity": n % 50,
            "units": n % 7,
            "amount": f"{n % 20}.50" if n % 3 else "0.00",
            "placed_at": f"2026-07-{1 + n % 20:02d}T10:00:00.000Z",
            "paid": n % 2 == 0,
            "note": "n",
        }
        for n in range(300)
    ]
    customers = [
        {"customer_id": n, "region": ["north", "south", "west"][n % 3]} for n in range(100)
    ]
    for name, rows in (("orders", orders), ("customers", customers)):
        (tmp_path / f"{name}.ndjson").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
    backend = FakeBackend()
    monkeypatch.setattr(_stats, "load_backend", lambda: backend)
    rc = main(
        [
            "profile",
            "shop.fluid.yaml",
            "--request",
            "shop.profile-request.yml",
            "--source",
            "customers=customers.ndjson",
            "--source",
            "orders=orders.ndjson",
            "--out",
            "p.json",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Fact orders.quantity_not_above_units: its share is published, as rounded counts." in out
    assert "Fact orders.paid_has_amount: " in out
    assert "Not measured: fact orders.lines_counted: an aggregate reads child rows" in out
    assert "Not measured: fact orders.customer_fixes_status: " in out
    document = json.loads((tmp_path / "p.json").read_text())
    assert {each["name"] for each in document["invariants"]} == {
        "orders.quantity_not_above_units",
        "orders.paid_has_amount",
        "orders.units_follow_quantity",
    }
