# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A private release of nullable columns and date pairs (``--privacy-unit``, own rows or ``--follow``).

A column the contract does not declare required is released like a required one: its public levels
or bins, one count table of its own, counting its known values only (a row with no value lands in no
cell). It joins no clique and no consistency step. A date pair the request asks of a table the
release holds gets a noisy histogram of the days from start to end on the full public ladder, and a
noisy order, both over the rows where both dates are known.

``loan`` is the release table: a loan has a ``borrowed_on`` and a ``due_on`` date, a ``returned_on``
date that is empty while the loan is out, and a ``shelf`` the library does not always record. A
``member`` is the privacy unit of the ``--follow`` releases.
"""

from __future__ import annotations

import copy
import csv
import json
import logging
import re
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest
import yaml

from fluid_build.cli import main
from fluid_build.profiler import _stats, dates, sources
from fluid_build.profiler import request as asked_for
from fluid_build.profiler.dp import bounding, release
from fluid_build.profiler.dp import follow_release as fr
from fluid_build.profiler.dp.follows import parse_follow
from fluid_build.profiler.dp.release import ReleaseRefused
from tests.profiler._fake_backend import FakeBackend

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.noise")

F = Fraction
SEED = bytes(range(3, 35))
REQUEST_URL = (
    "https://agenticstiger.github.io/flux/engine/spec/profile-request/1-2-0/FluxProfileRequest.json"
)
FOLLOW = "member.member_id=loan.member_id:3"
START = date(2026, 1, 1)

CONTRACT: Dict[str, Any] = yaml.safe_load("""
fluidVersion: "0.7.7"
kind: DataProduct
id: library.loans
name: Library loans
metadata:
  owner:
    team: library-team
exposes:
  - exposeId: member
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/member.parquet }
    contract:
      schema:
        - { name: member_id, type: INT, required: true }
        - { name: branch, type: "enum('central','north','south')", required: true }
        - { name: joined_on, type: DATE }
        - { name: left_on, type: DATE }
    semantics:
      entities:
        - { name: member, type: primary, expr: member_id }
  - exposeId: loan
    kind: table
    binding:
      platform: local
      format: parquet
      location: { path: ./out/loan.parquet }
    contract:
      schema:
        - { name: loan_id, type: INT, required: true }
        - { name: member_id, type: INT, required: true }
        - { name: kind, type: "enum('book','disc')", required: true }
        - { name: shelf, type: "enum('a','b')" }
        - { name: borrowed_on, type: DATE, required: true }
        - { name: due_on, type: DATE, required: true }
        - { name: returned_on, type: DATE }
        - { name: scanned_at, type: TIMESTAMP }
    semantics:
      entities:
        - { name: loan, type: primary, expr: loan_id }
      relationships:
        - name: borrowed_by
          to: member
          fromColumns: [member_id]
          toColumns: [member_id]
          cardinality: many_to_one
""")

LOAN_COLUMNS = (
    "loan_id",
    "member_id",
    "kind",
    "shelf",
    "borrowed_on",
    "due_on",
    "returned_on",
    "scanned_at",
)
MEMBER_COLUMNS = ("member_id", "branch", "joined_on", "left_on")
MEMBERS = 40


def _day(offset: int) -> str:
    return (START + timedelta(days=offset)).isoformat()


def loans(count: int = 400) -> List[Dict[str, Optional[str]]]:
    """Loans with dates; every fourth is still out (no ``returned_on``), every third has no shelf."""
    rows: List[Dict[str, Optional[str]]] = []
    for n in range(count):
        borrowed = n % 28
        rows.append(
            {
                "loan_id": str(n + 1),
                "member_id": str(1 + n % MEMBERS),
                "kind": ("book", "disc")[n % 2],
                "shelf": None if n % 3 == 0 else ("a", "b")[n % 2],
                "borrowed_on": _day(borrowed),
                "due_on": _day(borrowed + 14 + n % 5),
                "returned_on": None if n % 4 == 0 else _day(borrowed + n % 20),
                "scanned_at": None,
            }
        )
    return rows


def members() -> List[Dict[str, Optional[str]]]:
    return [
        {
            "member_id": str(n + 1),
            "branch": ("central", "north", "south")[n % 3],
            "joined_on": _day(n % 30),
            "left_on": None if n % 2 == 0 else _day(40 + n % 30),
        }
        for n in range(MEMBERS)
    ]


def _request(
    *,
    pairs: Sequence[Tuple[str, str]] = (("borrowed_on", "due_on"), ("borrowed_on", "returned_on")),
    member: bool = False,
    member_pairs: Sequence[Tuple[str, str]] = (),
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    loan_columns: Dict[str, Any] = {
        "loan_id": {"domain": "number"},
        "member_id": {"domain": "number"},
        "kind": {"domain": "category", "statistics": ["levelHistogram/1"]},
        "shelf": {"domain": "category", "statistics": ["levelHistogram/1"]},
        "borrowed_on": {"domain": "timestamp"},
        "due_on": {"domain": "timestamp"},
        "returned_on": {"domain": "timestamp", "statistics": ["marginalHistogram/1"]},
        "scanned_at": {"domain": "timestamp"},
    }
    tables: Dict[str, Any] = {
        "loan": {
            "columns": loan_columns,
            "datePairs": [{"start": a, "end": b} for a, b in pairs],
        }
    }
    if member:
        tables["member"] = {
            "columns": {
                "member_id": {"domain": "number"},
                "branch": {"domain": "category", "statistics": ["levelHistogram/1"]},
                "joined_on": {"domain": "timestamp", "statistics": ["marginalHistogram/1"]},
                "left_on": {"domain": "timestamp"},
            },
            "datePairs": [{"start": a, "end": b} for a, b in member_pairs],
        }
    built: Dict[str, Any] = {
        "_schemaURL": REQUEST_URL,
        "encoding": {"fractionalBits": 16, "rounding": "floor"},
        "tables": tables,
    }
    built.update(extra or {})
    return built


def _asked(request: Optional[Mapping[str, Any]] = None, backend: Optional[FakeBackend] = None):
    return (backend or FakeBackend()).read_request(
        json.dumps(request or _request()), name="loans.profile-request.yml"
    )


def _plan(
    request: Optional[Mapping[str, Any]] = None,
    contract: Optional[Mapping[str, Any]] = None,
    rho: str = "1",
) -> release.ReleasePlan:
    backend = FakeBackend()
    return release.plan_release(
        contract or CONTRACT,
        _asked(request, backend),
        backend,
        unit="loan",
        rho=release.parse_rho(rho),
    )


def _columns(rows: Sequence[Mapping[str, Optional[str]]], names: Sequence[str]):
    return sources.SourceColumns(
        description="rows",
        row_count=len(rows),
        values={name: [row[name] for row in rows] for name in names},
    )


def _draw(
    plan: release.ReleasePlan,
    rows: Optional[Sequence[Mapping[str, Optional[str]]]] = None,
    seed: bytes = SEED,
):
    backend = FakeBackend()
    encoded = release.encode_rows(plan, _columns(rows or loans(), LOAN_COLUMNS), backend)
    return release.draw(plan, encoded, seed, backend, cliques=()), encoded


def _event(call: Any) -> str:
    with pytest.raises(ReleaseRefused) as info:
        call()
    return info.value.event


def _table(drawn: release.Release, label: str) -> release.ReleasedTable:
    return next(each for each in drawn.tables if each.label == label)


def _statistic(document: Mapping[str, Any], table: str, column: str) -> Mapping[str, Any]:
    (found,) = document["tables"][table]["columns"][column]["statistics"]
    return found  # type: ignore[no-any-return]


def _days(row: Mapping[str, Optional[str]], start: str, end: str) -> Optional[int]:
    if row[start] is None or row[end] is None:
        return None
    return (date.fromisoformat(str(row[end])) - date.fromisoformat(str(row[start]))).days


def _bin_of(day: int) -> int:
    return sum(1 for edge in dates.DAY_LADDER[1:-1] if edge <= day)


# ---- a column that may hold a null --------------------------------------------------------------


def test_a_column_the_contract_does_not_require_is_released_with_its_known_values():
    plan = _plan()
    released = {each.name: each for each in plan.columns}
    assert set(released) == {"kind", "shelf", "returned_on"}
    assert released["shelf"].nullable and released["returned_on"].nullable
    assert not released["kind"].nullable
    assert "shelf" not in dict(plan.not_released).keys()
    assert not any("required" in reason for _, reason in plan.not_released)
    assert [each.name for each in plan.eligible] == ["kind"] and not plan.selects


def test_a_nullable_column_has_a_part_of_its_own_and_joins_no_clique():
    request = _request()
    request["tables"]["loan"]["cliqueColumns"] = ["kind", "shelf"]
    plan = _plan(request)
    # kind alone may join: a clique needs two columns, so none is chosen
    assert not plan.selects
    assert "required" in dict(plan.not_released)["clique column loan.shelf"]
    parts = release.release_parts(plan, ())
    labels = [part.label for part in parts]
    assert "noise/shelf" in labels and "noise/returned_on" in labels
    assert next(part for part in parts if part.label == "noise/shelf").kind == release.LEVELS
    assert next(part for part in parts if part.label == "noise/returned_on").kind == release.BINS


def test_a_nullable_column_never_joins_a_clique_the_release_chooses(monkeypatch):
    from tests.profiler.dp import _release_run as run  # noqa: F401  (stub_selection)

    contract = copy.deepcopy(CONTRACT)
    contract["exposes"][1]["contract"]["schema"].append(
        {"name": "audience", "type": "enum('adult','child')", "required": True}
    )
    request = _request()
    request["tables"]["loan"]["columns"]["audience"] = {
        "domain": "category",
        "statistics": ["levelHistogram/1"],
    }
    plan = _plan(request, contract)
    assert [each.name for each in plan.eligible] == ["kind", "audience"] and plan.selects
    assert "shelf" not in {each.name for each in plan.eligible}


def test_a_null_lands_in_no_cell_and_the_other_rows_are_counted_where_they_belong():
    plan = _plan()
    rows = loans()
    drawn, encoded = _draw(plan, rows)
    assert encoded.cells["shelf"].count(-1) == sum(1 for row in rows if row["shelf"] is None)
    kept = list(range(len(rows)))
    exact = release.measure_counts(plan, encoded, kept, drawn.tables)
    known = sum(1 for row in rows if row["shelf"] is not None)
    assert sum(exact["noise/shelf"]) == known
    assert sum(exact["noise/returned_on"]) == sum(1 for r in rows if r["returned_on"] is not None)
    assert sum(exact["noise/kind"]) == len(rows)  # a required column counts every row


def test_one_unit_still_moves_a_nullable_count_by_at_most_its_sensitivity_and_a_null_moves_none():
    plan = _plan()
    rows = loans()
    more = rows + [dict(rows[1], loan_id="9001", shelf=None, returned_on=None)]
    one, _ = _draw(plan, rows)
    two, _ = _draw(plan, more)
    # one seed gives one noise stream per part, so the noisy parts differ as the exact ones do
    for table, a, b in zip(one.tables, one.noisy, two.noisy, strict=True):
        moved = sum(((x - y) ** 2 for x, y in zip(a, b, strict=True)), F(0))
        assert moved <= plan.sensitivity, table.label
        if table.label in ("noise/shelf", "noise/returned_on") or "returned_on" in table.label:
            assert moved == 0  # the new row has no value in either: it lands in no cell
        else:
            assert moved == 1
    bounded = [dict(rows[1], loan_id="9002", shelf="a", returned_on=_day(3))]
    three, _ = _draw(plan, rows + bounded)
    shifted = {
        table.label: sum(((x - y) ** 2 for x, y in zip(a, b, strict=True)), F(0))
        for table, a, b in zip(one.tables, one.noisy, three.noisy, strict=True)
    }
    assert shifted["noise/shelf"] == 1 and shifted["noise/returned_on"] == 1


def test_a_nullable_column_is_noised_on_its_own_and_its_total_is_not_made_to_agree():
    plan = _plan()
    drawn, _ = _draw(plan)
    totals = {
        table.label: sum(values, F(0))
        for table, values in zip(drawn.tables, drawn.consistent, strict=True)
    }
    # the consistency step makes every required table add up to one total; a nullable column's
    # known values are fewer, so its table is left out of that step
    assert totals["noise/kind"] != totals["noise/shelf"]
    assert drawn.noisy[1] == drawn.consistent[1]  # shelf: noise only, no agreement


def test_the_noise_block_of_a_nullable_column_states_the_scale_the_share_and_reach_give():
    plan = _plan()
    drawn, _ = _draw(plan)
    for column in ("shelf", "returned_on"):
        table = _table(drawn, f"noise/{column}")
        assert F(table.scale) * 10**6 == release.scale_units(F(table.share), plan.sensitivity)
        statistic = _statistic(drawn.document, "loan", column)
        assert statistic["noise"] == {
            "mechanism": "discreteGaussian",
            "scale": table.scale,
            "privacyLoss": {"rho": table.share},
        }
        assert statistic["disclosure"] == "noisy" and len(statistic["groups"]) == 1
    assert drawn.document["privacy"]["publicStructure"] is True


def test_no_null_count_or_share_is_published():
    drawn, _ = _draw(_plan())

    def keys(value: Any) -> List[str]:
        if isinstance(value, Mapping):
            return [*map(str, value), *(name for item in value.values() for name in keys(item))]
        if isinstance(value, list):
            return [name for item in value for name in keys(item)]
        return []

    assert not [name for name in keys(drawn.document) if "null" in name.lower()]
    text = " ".join(str(item) for item in drawn.document["tables"]["loan"]["columns"]["shelf"])
    assert "known" not in text  # the document states nothing about what is missing


def test_a_null_in_a_required_column_still_refuses_and_names_the_column_and_the_row():
    plan = _plan()
    rows = loans()
    rows[5] = dict(rows[5], kind=None)
    assert _event(lambda: _draw(plan, rows)) == "profile_release_null"
    rows = loans()
    rows[7] = dict(rows[7], borrowed_on=None)
    with pytest.raises(ReleaseRefused) as info:
        _draw(plan, rows)
    assert info.value.event == "profile_release_null"
    assert "column 'borrowed_on' row 7" in str(info.value)


def test_a_nullable_column_with_no_public_levels_or_bins_is_still_named_with_its_reason():
    contract = copy.deepcopy(CONTRACT)
    contract["exposes"][1]["contract"]["schema"].append({"name": "note", "type": "string"})
    request = _request()
    request["tables"]["loan"]["columns"]["note"] = {
        "domain": "category",
        "statistics": ["levelHistogram/1"],
    }
    plan = _plan(request, contract)
    assert "note" not in {each.name for each in plan.columns}
    reason = dict(plan.not_released)["levelHistogram/1 of loan.note"]
    assert "required" not in reason


# ---- a date pair --------------------------------------------------------------------------------


def test_a_date_pair_is_released_on_the_full_public_ladder_with_its_order():
    plan = _plan()
    assert plan.date_pairs == (("borrowed_on", "due_on"), ("borrowed_on", "returned_on"))
    drawn, _ = _draw(plan)
    pairs = drawn.document["tables"]["loan"]["datePairs"]
    assert [(each["start"], each["end"]) for each in pairs] == list(plan.date_pairs)
    for each in pairs:
        (days,) = each["statistics"]
        assert days["definition"] == "marginalHistogram/1"
        assert days["bins"] == [str(edge) for edge in dates.DAY_LADDER]  # never trimmed
        assert len(days["groups"][0]["noisyCounts"]) == len(dates.DAY_LADDER) - 1
        assert days["groups"][0]["status"] == "F" and days["disclosure"] == "noisy"
        order = each["order"]
        assert order["definition"] == "levelHistogram/1"
        assert order["levels"] == ["after", "before", "same"]
        assert len(order["groups"][0]["noisyCounts"]) == 3
    assert drawn.document["privacy"]["publicStructure"] is True


def test_the_exact_days_and_order_count_the_rows_where_both_dates_are_known():
    plan = _plan()
    rows = loans()
    drawn, encoded = _draw(plan, rows)
    exact = release.measure_counts(plan, encoded, list(range(len(rows))), drawn.tables)
    for start, end in plan.date_pairs:
        wanted = [d for d in (_days(row, start, end) for row in rows) if d is not None]
        by_bin = [0] * (len(dates.DAY_LADDER) - 1)
        for day in wanted:
            by_bin[_bin_of(day)] += 1
        assert exact[f"pair/days/{start}/{end}"] == by_bin
        assert exact[f"pair/order/{start}/{end}"] == [
            sum(1 for d in wanted if d > 0),
            sum(1 for d in wanted if d < 0),
            sum(1 for d in wanted if d == 0),
        ]


def test_the_order_counts_a_return_after_before_and_on_the_day_of_its_loan():
    # The default loans are all returned after the day they were borrowed; these are not.
    plan = _plan()
    rows = loans()
    gaps = {9: -3, 10: -1, 14: -2, 11: 0, 13: 0}
    for n, gap in gaps.items():
        assert rows[n]["returned_on"] is not None
        rows[n]["returned_on"] = _day(n % 28 + gap)
    drawn, encoded = _draw(plan, rows)
    exact = release.measure_counts(plan, encoded, list(range(len(rows))), drawn.tables)
    known = [d for d in (_days(row, "borrowed_on", "returned_on") for row in rows) if d is not None]
    after, before, same = exact["pair/order/borrowed_on/returned_on"]
    assert (before, same) == (3, 2)
    assert after == len(known) - 5 > 0


def test_a_pair_is_two_parts_of_their_own_charged_the_table_sensitivity():
    plan = _plan()
    drawn, _ = _draw(plan)
    labels = [each.label for each in drawn.tables]
    for start, end in plan.date_pairs:
        for kind, size in (("days", len(dates.DAY_LADDER) - 1), ("order", 3)):
            table = _table(drawn, f"pair/{kind}/{start}/{end}")
            assert table.sizes == (size,) and table.columns == (start, end)
            assert F(table.scale) * 10**6 == release.scale_units(F(table.share), plan.sensitivity)
            assert labels.count(table.label) == 1
    # the parts the document is made of are the parts the budget is split over
    assert [each.label for each in drawn.tables] == [
        part.label for part in release.release_parts(plan, ())
    ]


def test_the_parts_still_add_up_to_at_most_rho():
    for rho in ("1", "0.37", "0.000001"):
        plan = _plan(rho=rho)
        drawn, _ = _draw(plan)
        spent = sum((F(each.share) for each in drawn.tables), F(0)) + drawn.selection_rho
        assert 0 < spent <= plan.rho
        total = sum(
            F(block["noise"]["privacyLoss"]["rho"])
            for block in _blocks(drawn.document["tables"]["loan"])
        )
        assert total == sum((F(each.share) for each in drawn.tables), F(0))


def _blocks(table: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    found: List[Mapping[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            if "noise" in value:
                found.append(value)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(table)
    return found


def test_the_document_names_the_format_that_holds_a_date_pair_and_passes_the_reader():
    from flux_stats.profile import load_profile_text

    drawn, _ = _draw(_plan())
    assert drawn.document["_schemaURL"] == release.document.SCHEMA_URL_1_3_0
    profile, _ = load_profile_text(drawn.canonical.decode("utf-8"))
    assert profile is not None


def test_a_release_without_a_date_pair_or_a_nullable_column_keeps_its_format():
    request = _request(pairs=())
    request["tables"]["loan"]["columns"].pop("shelf")
    request["tables"]["loan"]["columns"].pop("returned_on")
    request["tables"]["loan"].pop("datePairs")
    drawn, _ = _draw(_plan(request))
    assert drawn.document["_schemaURL"] == release.SCHEMA_URL_1_2_0
    assert "datePairs" not in drawn.document["tables"]["loan"]


def test_a_pair_with_a_timestamp_column_or_an_unlisted_one_is_named_with_its_reason():
    plan = _plan(_request(pairs=(("borrowed_on", "scanned_at"), ("borrowed_on", "due_on"))))
    assert plan.date_pairs == (("borrowed_on", "due_on"),)
    named = dict(plan.not_released)
    reason = named["date pair borrowed_on to scanned_at of loan"]
    assert "DATE" in reason and "scanned_at" in reason


def test_a_pair_of_a_key_column_is_named_and_not_released():
    contract = copy.deepcopy(CONTRACT)
    contract["exposes"][1]["contract"]["schema"].append(
        {"name": "opened_on", "type": "DATE", "required": True}
    )
    request = _request(pairs=(("opened_on", "due_on"),))
    request["tables"]["loan"]["columns"]["opened_on"] = {"domain": "timestamp"}
    contract["exposes"][1]["semantics"]["entities"][0]["expr"] = "opened_on"
    plan = _plan(request, contract)
    assert plan.date_pairs == ()
    assert "key" in dict(plan.not_released)["date pair opened_on to due_on of loan"]


def test_calendar_dates_and_exposure_are_still_not_released_and_a_date_pair_is_no_longer_unformed():
    request = _request()
    request["tables"]["loan"]["calendar"] = ["borrowed_on"]
    asked = _asked(request)
    named = dict(asked_for.unformed(asked, private=True))
    assert any(reason == asked_for.NO_CALENDAR for reason in named.values())
    assert not any(what.startswith("date pair") for what in named)
    plan = _plan(request)
    assert dict(plan.not_released)["calendar of loan.borrowed_on"] == asked_for.NO_CALENDAR


def test_the_record_states_the_pair_and_nullable_parts_and_a_rederivation_reproduces_them():
    plan = _plan()
    drawn, encoded = _draw(plan)
    record = release.build_record(plan, drawn, SEED, request_bytes=b"request", contract_digest=None)
    kinds = [each.get("kind") for each in record.tables]
    assert kinds.count(release.PAIR_DAYS) == 2 and kinds.count(release.PAIR_ORDER) == 2
    assert release.check_record(plan, record) == ()
    again = release.draw(plan, encoded, SEED, FakeBackend(), cliques=())
    assert again.digest == drawn.digest
    tampered = copy.deepcopy(record)
    changed = [dict(each) for each in tampered.tables]
    changed[-1]["scale"] = "0.5"
    object.__setattr__(tampered, "tables", tuple(changed))
    assert _event(lambda: release.check_record(plan, tampered)) == "profile_release_record_mismatch"


def test_a_value_that_is_not_a_whole_day_refuses_the_release_naming_the_column_and_the_row():
    plan = _plan()
    rows = loans()
    # a date column is read in the timestamp domain; a time of day is no whole day
    rows[4] = dict(rows[4], returned_on="2026-01-09T10:00:00Z")
    with pytest.raises(ReleaseRefused) as info:
        _draw(plan, rows)
    assert info.value.event == "profile_record_outside_declared_domain"
    assert "column 'returned_on' row 4" in str(info.value)
    assert "10:00" not in str(info.value)


# ---- a unit that follows its child rows ---------------------------------------------------------


def _follow_plan(request: Optional[Mapping[str, Any]] = None) -> fr.FollowReleasePlan:
    backend = FakeBackend()
    return fr.plan_follow_release(
        CONTRACT,
        _asked(request or _request(member=True, member_pairs=(("joined_on", "left_on"),)), backend),
        backend,
        unit="member",
        rho=release.parse_rho("1"),
        follows=(parse_follow(FOLLOW),),
    )


def _follow_draw(
    plan: fr.FollowReleasePlan,
    loan_rows: Optional[Sequence[Mapping[str, Optional[str]]]] = None,
    seed: bytes = SEED,
):
    backend = FakeBackend()
    wanted = dict(plan.reads)
    values = {
        "member": members(),
        "loan": loan_rows if loan_rows is not None else loans(),
    }
    read = {
        table: sources.SourceColumns(
            description="rows",
            row_count=len(values[table]),
            values={
                column.name: [row[column.name] for row in values[table]] for column in wanted[table]
            },
        )
        for table in wanted
    }
    rows = fr.encode_follow_rows(plan, read, backend)
    return fr.draw_follow(plan, rows, seed, backend, cliques={}), rows


def test_a_followed_table_releases_its_nullable_columns_and_date_pairs_charged_their_reach():
    plan = _follow_plan()
    assert plan.reach == {"member": 1, "loan": 3}
    loan = plan.table("loan").plan
    assert loan.date_pairs == (("borrowed_on", "due_on"), ("borrowed_on", "returned_on"))
    assert {each.name for each in loan.columns if each.nullable} == {"shelf", "returned_on"}
    assert plan.table("member").plan.date_pairs == (("joined_on", "left_on"),)
    drawn, _ = _follow_draw(plan)
    assert drawn.document["_schemaURL"] == release.document.SCHEMA_URL_1_4_0
    by_label = {each.label: each for each in drawn.tables}
    for start, end in loan.date_pairs:
        for kind in ("days", "order"):
            part = by_label[f"loan/pair/{kind}/{start}/{end}"]
            assert part.table == "loan" and part.sensitivity == 9
            assert F(part.scale) * 10**6 == release.scale_units(F(part.share), 9)
    member_part = by_label["member/pair/days/joined_on/left_on"]
    assert member_part.sensitivity == 1
    pairs = drawn.document["tables"]["loan"]["datePairs"]
    assert [(each["start"], each["end"]) for each in pairs] == list(loan.date_pairs)
    for each in pairs:
        assert each["statistics"][0]["bins"] == [str(edge) for edge in dates.DAY_LADDER]
    assert drawn.document["tables"]["member"]["datePairs"][0]["order"]["levels"] == [
        "after",
        "before",
        "same",
    ]


def test_a_followed_release_counts_known_values_only_and_its_parts_add_up_to_at_most_rho():
    plan = _follow_plan()
    drawn, rows = _follow_draw(plan)
    kept = fr.truncated(plan, rows, SEED)
    _, tables = fr.describe(plan, {})
    exact = fr.measure_counts(plan, rows, kept, tables, FakeBackend())
    loan_rows = loans()
    assert sum(exact["loan/noise/shelf"]) <= sum(1 for row in loan_rows if row["shelf"] is not None)
    assert sum(exact["loan/noise/kind"]) >= sum(exact["loan/noise/shelf"])
    assert sum((F(each.share) for each in drawn.tables), F(0)) <= plan.rho
    record = fr.build_follow_record(plan, drawn, SEED, request_bytes=b"r", contract_digest=None)
    assert fr.check_follow_record(plan, record) == {}
    assert _follow_draw(plan)[0].digest == drawn.digest


def test_a_nullable_child_column_of_a_link_statistic_is_released_and_counts_known_values():
    request = _request(member=True)
    request["relationships"] = [
        {
            "parent": {"table": "member", "columns": ["member_id"]},
            "child": {"table": "loan", "columns": ["member_id"]},
            "statistics": ["marginalHistogram/1"],
            "linked": [{"column": "shelf", "parentColumn": "branch"}],
        }
    ]
    plan = _follow_plan(request)
    (link,) = plan.links
    assert link.column.name == "shelf" and link.column.nullable
    drawn, _ = _follow_draw(plan)
    assert drawn.document["tables"]["loan"]["columns"]["shelf"]["statistics"]


def test_a_column_that_both_groups_and_is_counted_through_a_link_may_hold_no_value():
    # loan.shelf is optional: it is counted through the link to member and groups the children
    # per parent of fine. A record with no value lands in no cell of either, so it never refuses.
    contract = copy.deepcopy(CONTRACT)
    contract["exposes"].append(
        {
            "exposeId": "fine",
            "kind": "table",
            "binding": {
                "platform": "local",
                "format": "parquet",
                "location": {"path": "./out/fine.parquet"},
            },
            "contract": {
                "schema": [
                    {"name": "fine_id", "type": "INT", "required": True},
                    {"name": "loan_id", "type": "INT", "required": True},
                ]
            },
            "semantics": {
                "entities": [{"name": "fine", "type": "primary", "expr": "fine_id"}],
                "relationships": [
                    {
                        "name": "charged_on",
                        "to": "loan",
                        "fromColumns": ["loan_id"],
                        "toColumns": ["loan_id"],
                        "cardinality": "many_to_one",
                    }
                ],
            },
        }
    )
    request = _request(member=True)
    request["tables"]["fine"] = {
        "columns": {"fine_id": {"domain": "number"}, "loan_id": {"domain": "number"}}
    }
    request["relationships"] = [
        {
            "parent": {"table": "member", "columns": ["member_id"]},
            "child": {"table": "loan", "columns": ["member_id"]},
            "statistics": ["marginalHistogram/1"],
            "linked": [{"column": "shelf", "parentColumn": "branch"}],
        },
        {
            "parent": {"table": "loan", "columns": ["loan_id"]},
            "child": {"table": "fine", "columns": ["loan_id"]},
            "statistics": ["marginalHistogram/1"],
            "groupBy": ["shelf"],
        },
    ]
    backend = FakeBackend()
    plan = fr.plan_follow_release(
        contract,
        _asked(request, backend),
        backend,
        unit="member",
        rho=release.parse_rho("1"),
        follows=(parse_follow(FOLLOW),),
    )
    assert plan.structure[("loan", "shelf")].nullable
    rows_of = {
        "member": members(),
        "loan": loans(),
        "fine": [{"fine_id": str(n + 1), "loan_id": str(1 + (n * 7) % 400)} for n in range(120)],
    }
    read = {
        table: sources.SourceColumns(
            description="rows",
            row_count=len(rows_of[table]),
            values={column.name: [row[column.name] for row in rows_of[table]] for column in cols},
        )
        for table, cols in dict(plan.reads).items()
    }
    rows = fr.encode_follow_rows(plan, read, backend)
    drawn = fr.draw_follow(plan, rows, SEED, backend, cliques={})
    fined = [each for each in drawn.document["relationships"] if each["child"]["table"] == "fine"]
    assert any(statistic.get("groupBy") == "shelf" for statistic in fined[0]["statistics"])


# ---- the command line: nothing printed holds a count ----------------------------------------------


def _write_csv(path: Path, names: Sequence[str], rows: Sequence[Mapping[str, Optional[str]]]):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(names)
        for row in rows:
            writer.writerow(["" if row[name] is None else row[name] for name in names])


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    text = yaml.safe_dump(CONTRACT, sort_keys=False)
    (tmp_path / "library.fluid.yaml").write_text(text, encoding="utf-8")
    digest = FakeBackend().contract_digest(text, name="library.fluid.yaml")
    request = {
        **_request(member=True, member_pairs=(("joined_on", "left_on"),)),
        "contract": {"digest": digest},
    }
    (tmp_path / "library.profile-request.yml").write_text(
        yaml.safe_dump(request, sort_keys=False), encoding="utf-8"
    )
    _write_csv(tmp_path / "loan.csv", LOAN_COLUMNS, loans())
    _write_csv(tmp_path / "member.csv", MEMBER_COLUMNS, members())
    backend = FakeBackend()
    monkeypatch.setattr(_stats, "load_backend", lambda: backend)
    return tmp_path


def _argv(*more: str, follow: bool = False, record: str = "rec.json") -> List[str]:
    sources_ = ["--source", "loan=loan.csv"] + (["--source", "member=member.csv"] if follow else [])
    return [
        "profile",
        "library.fluid.yaml",
        "--request",
        "library.profile-request.yml",
        *sources_,
        "--privacy-unit",
        "member" if follow else "loan",
        *(["--follow", FOLLOW] if follow else []),
        "--rho",
        "1",
        "--release-record",
        record,
        *more,
    ]


def _again(*, follow: bool = False, record: str = "rec.json") -> List[str]:
    sources_ = ["--source", "loan=loan.csv"] + (["--source", "member=member.csv"] if follow else [])
    return [
        "profile",
        "library.fluid.yaml",
        "--request",
        "library.profile-request.yml",
        *sources_,
        "--release-record",
        record,
        "--rederive",
    ]


def _numbers(text: str, workspace: Path) -> set:
    for place in {str(workspace), str(workspace.resolve())}:
        text = text.replace(place, "<workspace>")
    return set(re.findall(r"(?<![\w.])\d+(?![\w.])", text))


def _secrets() -> set:
    rows = loans()
    secrets = {
        len(rows),
        sum(1 for row in rows if row["shelf"] is not None),
        sum(1 for row in rows if row["shelf"] is None),
        sum(1 for row in rows if row["returned_on"] is not None),
        sum(1 for row in rows if row["returned_on"] is None),
        sum(
            1
            for row in rows
            if row["returned_on"] is not None
            and _days(row, "borrowed_on", "returned_on") is not None
        ),
        MEMBERS,
    }
    for start, end in (("borrowed_on", "due_on"), ("borrowed_on", "returned_on")):
        known = [d for d in (_days(row, start, end) for row in rows) if d is not None]
        secrets |= {sum(1 for d in known if d > 0), sum(1 for d in known if d == 0)}
    return {each for each in secrets if each >= 10}


@pytest.mark.parametrize("follow", [False, True], ids=["own rows", "follow"])
def test_no_count_appears_in_what_a_release_of_nullable_columns_and_date_pairs_prints_or_writes(
    workspace, capsys, caplog, follow
):
    caplog.set_level(logging.DEBUG)
    assert main(_argv(follow=follow)) == 0
    seen = capsys.readouterr()
    text = seen.out + seen.err + caplog.text
    name = "member" if follow else "loan"
    profile = (workspace / f"{name}.profile.json").read_text(encoding="utf-8")
    seed = json.loads((workspace / "rec.json").read_text(encoding="utf-8"))["seed"]
    assert seed not in text and seed not in profile
    shown = _numbers(text, workspace)
    for secret in _secrets():
        assert str(secret) not in shown, secret
    stored = json.loads((workspace / "rec.json").read_text(encoding="utf-8"))
    stored.pop("seed")
    rest = json.dumps(stored)
    for secret in _secrets():
        assert not re.search(rf"(?<![\w.]){secret}(?![\w.])", rest), secret
    # the report lists the new parts with the others, and says what a nullable histogram counts
    flat = " ".join(text.split())
    assert "pair" in flat and "borrowed_on" in flat and "returned_on" in flat
    assert "known values" in flat


@pytest.mark.parametrize("follow", [False, True], ids=["own rows", "follow"])
def test_rederive_draws_the_same_release_again_with_nullable_columns_and_date_pairs(
    workspace, capsys, follow
):
    assert main(_argv(follow=follow)) == 0
    capsys.readouterr()
    name = "member" if follow else "loan"
    first = (workspace / f"{name}.profile.json").read_bytes()
    (workspace / f"{name}.profile.json").unlink()
    assert main(_again(follow=follow)) == 0
    assert (workspace / f"{name}.profile.json").read_bytes() == first


def test_strict_refuses_a_date_pair_a_private_release_cannot_form(workspace, capsys):
    request = yaml.safe_load((workspace / "library.profile-request.yml").read_text("utf-8"))
    request["tables"]["loan"]["datePairs"].append({"start": "borrowed_on", "end": "scanned_at"})
    (workspace / "library.profile-request.yml").write_text(yaml.safe_dump(request), "utf-8")
    assert main(_argv("--strict")) == 1
    out = " ".join((lambda s: s.out + s.err)(capsys.readouterr()).split())
    assert "profile_request_not_formed" in out and "borrowed_on to scanned_at" in out
    assert not (workspace / "rec.json").exists()


def test_strict_accepts_the_date_pairs_a_private_release_forms(workspace, capsys):
    request = yaml.safe_load((workspace / "library.profile-request.yml").read_text("utf-8"))
    request["tables"]["member"]["datePairs"] = []  # a table the release does not hold: not formed
    (workspace / "library.profile-request.yml").write_text(yaml.safe_dump(request), "utf-8")
    assert main(_argv("--strict")) == 0


def test_strict_refuses_a_date_pair_of_a_table_the_release_does_not_hold(workspace, capsys):
    assert main(_argv("--strict")) == 1
    out = " ".join((lambda s: s.out + s.err)(capsys.readouterr()).split())
    assert "joined_on to left_on of member" in out


def test_the_unit_of_a_release_that_follows_names_the_entity_rule_exactly():
    from fluid_build.cli import _profile_release

    assert "profile_release_entity_units" in _profile_release.HELP
    assert "each parent key must name one row" not in _profile_release.HELP
    import inspect

    from fluid_build.cli import _profile_follow

    source = inspect.getsource(_profile_follow.report)
    assert "each followed parent key names one row)" not in source
    assert "profile_release_entity_units" in source


def test_bounding_still_refuses_a_cell_outside_a_table():
    with pytest.raises(ValueError):
        bounding.capped_counts([None], [-1], 3, 1)
