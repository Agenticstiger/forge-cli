# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The cells a private release may measure, and the parts added to it.

A release holds a count table for each part, and refuses when its parts hold more cells than
``selection.MOST_RELEASE_CELLS``. A date pair's days and order, a column the contract does not
declare required, and a link statistic whose counted child column may hold a null are the added
parts: the release fits them in, in that order, while its cells stay within the cap, once its
cliques are chosen. The one that does not fit is left out, and so is every added part after it. The
parts the release always held keep their place, and the refusal when they alone are too many.
"""

from __future__ import annotations

import copy
import json
import logging
from fractions import Fraction
from typing import Any, Dict, List, Sequence, Tuple

import pytest

from fluid_build.cli import main
from fluid_build.profiler.dp import follow_release as fr
from fluid_build.profiler.dp import release, selection
from tests.profiler._fake_backend import FakeBackend
from tests.profiler.test_release_dates_nulls import (  # noqa: F401  (workspace is a fixture)
    CONTRACT,
    LOAN_COLUMNS,
    SEED,
    _again,
    _argv,
    _asked,
    _columns,
    _event,
    _follow_plan,
    _numbers,
    _plan,
    _request,
    _secrets,
    loans,
    members,
    workspace,
)

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.noise")

F = Fraction
PRODUCER = "https://example.test/producer"

#: The digests of the two default releases (cap untouched), as they were drawn before the added
#: parts were fitted into the cap.
OWN_DIGEST = "sha256:5f714cc1eb8645f75345ce02e9da7d1ee25bd1bc26516f1e9b6a7e8386c12cfe"
FOLLOW_DIGEST = "sha256:1b7feeafd16bd60c6435fd1e91843c66522329fbed407ae98eb73ab3f9000dc5"


def _reason(cap: int) -> str:
    return f"the release's parts would hold more cells than the {cap:,} a release may measure"


# ---- the parts of each kind of release ------------------------------------------------------------


def _own_added(plan: release.ReleasePlan) -> List[Tuple[str, ...]]:
    """The labels of the added parts as units, in the order they are fitted in."""
    units: List[Tuple[str, ...]] = [
        (f"pair/days/{start}/{end}", f"pair/order/{start}/{end}") for start, end in plan.date_pairs
    ]
    units += [(release.noise_label((c.name,)),) for c in plan.columns if c.nullable]
    return units


def _own_cells(plan: release.ReleasePlan) -> Dict[str, int]:
    return {part.label: part.cells for part in release.release_parts(plan, ())}


def _own_fixed(plan: release.ReleasePlan) -> int:
    cells = _own_cells(plan)
    added = {label for unit in _own_added(plan) for label in unit}
    return sum(count for label, count in cells.items() if label not in added)


def _follow_added(plan: fr.FollowReleasePlan) -> List[Tuple[str, ...]]:
    pairs: List[Tuple[str, ...]] = []
    columns: List[Tuple[str, ...]] = []
    for each in plan.tables:
        for start, end in each.plan.date_pairs:
            pairs.append(
                (
                    f"{each.table}/pair/days/{start}/{end}",
                    f"{each.table}/pair/order/{start}/{end}",
                )
            )
        for column in each.plan.columns:
            if column.nullable:
                columns.append((f"{each.table}/{release.noise_label((column.name,))}",))
    links = [(link.label,) for link in plan.links if link.column.nullable]
    return [*pairs, *columns, *links]


def _follow_cells(plan: fr.FollowReleasePlan) -> Dict[str, int]:
    return {each.part.label: each.part.cells for each in fr.follow_parts(plan, {})}


def _follow_fixed(plan: fr.FollowReleasePlan) -> int:
    cells = _follow_cells(plan)
    added = {label for unit in _follow_added(plan) for label in unit}
    return sum(count for label, count in cells.items() if label not in added)


def _set_cap(monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    monkeypatch.setattr(selection, "MOST_RELEASE_CELLS", cap)


def _draw(plan: release.ReleasePlan, rows=None, backend=None):
    backend = backend or FakeBackend()
    encoded = release.encode_rows(plan, _columns(rows or loans(), LOAN_COLUMNS), backend)
    drawn = release.draw(plan, encoded, SEED, backend, cliques=(), producer=PRODUCER)
    return drawn, encoded


def _follow_draw(plan: fr.FollowReleasePlan):
    backend = FakeBackend()
    wanted = dict(plan.reads)
    values = {"member": members(), "loan": loans()}
    read = {
        table: _columns_of(values[table], [column.name for column in wanted[table]])
        for table in wanted
    }
    encoded = fr.encode_follow_rows(plan, read, backend)
    drawn = fr.draw_follow(plan, encoded, SEED, backend, cliques={}, producer=PRODUCER)
    return drawn, encoded


def _columns_of(rows: Sequence[Dict[str, Any]], names: Sequence[str]):
    return _columns(rows, names)


def _reads(document_text: bytes) -> None:
    from flux_stats.profile import load_profile_text

    load_profile_text(document_text.decode("utf-8"))


def _shares_hold(drawn: Any, rho: Fraction) -> None:
    assert sum((F(each.share) for each in drawn.tables), F(0)) <= rho


# ---- nothing changes when no added part is over the cap -----------------------------------------


def test_a_release_whose_added_parts_all_fit_is_the_release_it_was():
    plan = _plan()
    drawn, _ = _draw(plan)
    assert drawn.left_out == ()
    assert drawn.digest == OWN_DIGEST
    followed, _ = _follow_draw(_follow_plan())
    assert followed.left_out == ()
    assert followed.digest == FOLLOW_DIGEST


# ---- own rows -------------------------------------------------------------------------------------


def test_the_plan_does_not_refuse_for_the_cells_of_a_column_that_may_hold_a_null(monkeypatch):
    # the required column holds two cells; the nullable ones are fitted in when the cliques are known
    _set_cap(monkeypatch, 2)
    plan = _plan()
    assert {each.name for each in plan.columns if each.nullable} == {"shelf", "returned_on"}


def test_added_parts_that_do_not_fit_are_left_out_named_and_the_rest_is_released(monkeypatch):
    plan = _plan()
    cells = _own_cells(plan)
    pairs = sum(cells[label] for unit in _own_added(plan)[:2] for label in unit)
    cap = _own_fixed(plan) + pairs + cells["noise/shelf"]
    _set_cap(monkeypatch, cap)
    plan = _plan()
    drawn, _ = _draw(plan)
    assert dict(drawn.left_out) == {"noise/returned_on": _reason(cap)}
    labels = [each.label for each in drawn.tables]
    assert "noise/shelf" in labels and "noise/returned_on" not in labels
    assert [each for each in labels if each.startswith("pair/")] == [
        "pair/days/borrowed_on/due_on",
        "pair/order/borrowed_on/due_on",
        "pair/days/borrowed_on/returned_on",
        "pair/order/borrowed_on/returned_on",
    ]
    assert sum(each.cells for each in drawn.tables) == cap
    _shares_hold(drawn, plan.rho)
    assert drawn.share == release.split_budget(plan.rho, False, len(drawn.tables))[1]
    block = drawn.document["tables"]["loan"]
    assert "statistics" in block["columns"]["shelf"]
    assert "statistics" not in block["columns"]["returned_on"]  # named with its domain only
    assert len(block["datePairs"]) == 2
    _reads(drawn.canonical)


def test_a_part_left_out_is_not_measured_and_a_rederivation_draws_the_same_release(monkeypatch):
    plan = _plan()
    cells = _own_cells(plan)
    cap = _own_fixed(plan) + sum(cells[label] for unit in _own_added(plan)[:2] for label in unit)
    _set_cap(monkeypatch, cap)
    plan = _plan()
    drawn, encoded = _draw(plan)
    assert {label for label, _ in drawn.left_out} == {"noise/shelf", "noise/returned_on"}
    kept = list(range(len(encoded.contents)))
    assert set(release.measure_counts(drawn.plan, encoded, kept, drawn.tables)) == {
        each.label for each in drawn.tables if each.kind != release.RANK
    }
    record = release.build_record(plan, drawn, SEED, request_bytes=b"r", contract_digest=None)
    assert all(each["label"] not in ("noise/shelf", "noise/returned_on") for each in record.tables)
    assert release.check_record(plan, record) == ()
    again = release.draw(plan, encoded, SEED, FakeBackend(), cliques=(), producer=PRODUCER)
    assert again.digest == drawn.digest and again.left_out == drawn.left_out


def test_a_record_that_disagrees_with_the_cells_the_cap_leaves_is_refused(monkeypatch):
    plan = _plan()
    big, _ = _draw(plan)
    assert big.left_out == ()
    cells = _own_cells(plan)
    cap = _own_fixed(plan) + sum(cells[label] for unit in _own_added(plan)[:2] for label in unit)
    whole = release.build_record(plan, big, SEED, request_bytes=b"r", contract_digest=None)
    _set_cap(monkeypatch, cap)
    narrow = _plan()
    small, _ = _draw(narrow)
    assert _event(lambda: release.check_record(narrow, whole)) == "profile_release_record_mismatch"
    stated = release.build_record(narrow, small, SEED, request_bytes=b"r", contract_digest=None)
    assert release.check_record(narrow, stated) == ()
    monkeypatch.undo()
    assert _event(lambda: release.check_record(plan, stated)) == "profile_release_record_mismatch"


def test_the_added_parts_are_a_prefix_a_later_small_one_never_takes_the_place_of_a_larger(
    monkeypatch,
):
    plan = _plan()
    cells = _own_cells(plan)
    first, second = _own_added(plan)[:2]
    size = lambda unit: sum(cells[label] for label in unit)  # noqa: E731
    cap = _own_fixed(plan) + size(first) + size(second) - 1
    assert cap - _own_fixed(plan) - size(first) >= cells["noise/shelf"]  # the small one would fit
    _set_cap(monkeypatch, cap)
    drawn, _ = _draw(_plan())
    assert [label for label, _ in drawn.left_out] == [
        *second,
        "noise/shelf",
        "noise/returned_on",
    ]
    assert {reason for _, reason in drawn.left_out} == {_reason(cap)}
    labels = [each.label for each in drawn.tables]
    assert first[0] in labels and second[0] not in labels and "noise/shelf" not in labels
    assert [
        (each["start"], each["end"]) for each in drawn.document["tables"]["loan"]["datePairs"]
    ] == [("borrowed_on", "due_on")]


def test_required_parts_that_alone_hold_too_many_cells_still_refuse(monkeypatch):
    plan = _plan()
    _set_cap(monkeypatch, _own_fixed(plan) - 1)
    assert _event(lambda: _draw(_plan())) == "profile_release_too_many_cells"


def test_the_cliques_chosen_count_with_the_parts_the_release_always_held(monkeypatch):
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
    clique = [("kind", "audience")]
    parts = release.release_parts(plan, clique)
    added = {label for unit in _own_added(plan) for label in unit}
    fixed = sum(part.cells for part in parts if part.label not in added)
    assert fixed == 4  # one clique of two columns of two levels each
    _set_cap(monkeypatch, fixed + 1)
    backend = FakeBackend()
    encoded = release.encode_rows(
        plan, _columns(_with_audience(loans()), (*LOAN_COLUMNS, "audience")), backend
    )
    drawn = release.draw(plan, encoded, SEED, backend, cliques=clique, producer=PRODUCER)
    assert {label for label, _ in drawn.left_out} == added
    assert [each.label for each in drawn.tables] == ["noise/kind/audience"]


def _with_audience(rows):
    return [dict(row, audience=("adult", "child")[n % 2]) for n, row in enumerate(rows)]


# ---- a unit that follows its child rows -----------------------------------------------------------


def _link_request() -> Dict[str, Any]:
    request = _request(member=True, member_pairs=(("joined_on", "left_on"),))
    request["relationships"] = [
        {
            "parent": {"table": "member", "columns": ["member_id"]},
            "child": {"table": "loan", "columns": ["member_id"]},
            "statistics": ["marginalHistogram/1"],
            "linked": [{"column": "shelf", "parentColumn": "branch"}],
        }
    ]
    return request


def test_a_followed_release_leaves_out_the_added_parts_that_do_not_fit_and_names_them(monkeypatch):
    plan = _follow_plan(_link_request())
    (link,) = plan.links
    cells = _follow_cells(plan)
    units = _follow_added(plan)
    assert units[-1] == (link.label,)
    cap = _follow_fixed(plan) + sum(cells[label] for unit in units[:-1] for label in unit)
    _set_cap(monkeypatch, cap)
    plan = _follow_plan(_link_request())
    drawn, _ = _follow_draw(plan)
    assert dict(drawn.left_out) == {link.label: _reason(cap)}
    labels = [each.label for each in drawn.tables]
    assert link.label not in labels and "loan/noise/shelf" in labels
    assert sum(each.cells for each in drawn.tables) == cap
    _shares_hold(drawn, plan.rho)
    assert drawn.share == release.split_budget(plan.rho, plan.selects, len(drawn.tables))[1]
    shelf = drawn.document["tables"]["loan"]["columns"]["shelf"]["statistics"]
    assert len(shelf) == 1 and "groupByLink" not in shelf[0]
    _reads(drawn.canonical)
    record = fr.build_follow_record(plan, drawn, SEED, request_bytes=b"r", contract_digest=None)
    assert all(each["label"] != link.label for each in record.tables)
    assert fr.check_follow_record(plan, record) == {}
    again, _ = _follow_draw(plan)
    assert again.digest == drawn.digest
    monkeypatch.undo()
    assert _event(lambda: fr.check_follow_record(plan, record)) == "profile_release_record_mismatch"


def test_a_followed_release_fits_the_added_parts_as_a_prefix(monkeypatch):
    plan = _follow_plan(_link_request())
    cells = _follow_cells(plan)
    units = _follow_added(plan)
    pairs = [unit for unit in units if "/pair/" in unit[0]]
    size = lambda unit: sum(cells[label] for label in unit)  # noqa: E731
    cap = _follow_fixed(plan) + sum(size(unit) for unit in pairs) - 1
    smallest = min(size(unit) for unit in units if "/pair/" not in unit[0])
    assert cap - _follow_fixed(plan) - sum(size(unit) for unit in pairs[:-1]) >= smallest
    _set_cap(monkeypatch, cap)
    drawn, _ = _follow_draw(_follow_plan(_link_request()))
    assert [label for label, _ in drawn.left_out] == [
        label for unit in units[len(pairs) - 1 :] for label in unit
    ]
    assert {reason for _, reason in drawn.left_out} == {_reason(cap)}
    assert sum(each.cells for each in drawn.tables) <= cap


def test_the_plan_of_a_followed_release_does_not_refuse_for_nullable_columns(monkeypatch):
    # the widest required column of the followed tables holds three cells
    _set_cap(monkeypatch, 3)
    plan = _follow_plan()
    assert any(column.nullable for each in plan.tables for column in each.plan.columns)


def test_followed_parts_that_alone_hold_too_many_cells_still_refuse(monkeypatch):
    plan = _follow_plan()
    _set_cap(monkeypatch, _follow_fixed(plan) - 1)
    assert _event(lambda: _follow_draw(_follow_plan())) == "profile_release_too_many_cells"


# ---- the command line -------------------------------------------------------------------------------


@pytest.mark.parametrize("follow", [False, True], ids=["own rows", "follow"])
def test_the_report_names_what_was_left_out_and_no_count_appears(
    workspace, capsys, caplog, monkeypatch, follow
):
    if follow:
        plan = _follow_plan()
        total = sum(_follow_cells(plan).values())
        last = [
            f"{each.table}/{release.noise_label((column.name,))}"
            for each in plan.tables
            for column in each.plan.columns
            if column.nullable
        ][-1]
    else:
        plan = _plan()
        total = sum(_own_cells(plan).values())
        last = "noise/returned_on"
    cap = total - 1
    assert cap not in _secrets()
    _set_cap(monkeypatch, cap)
    caplog.set_level(logging.DEBUG)
    assert main(_argv(follow=follow)) == 0
    seen = capsys.readouterr()
    text = seen.out + seen.err + caplog.text
    flat = " ".join(text.split())
    assert f"Not released ({_reason(cap)}): {last}." in flat
    shown = _numbers(text, workspace)
    for secret in _secrets():
        assert str(secret) not in shown, secret
    name = "member" if follow else "loan"
    first = (workspace / f"{name}.profile.json").read_bytes()
    stated = json.loads((workspace / "rec.json").read_text(encoding="utf-8"))
    assert all(each["label"] != last for each in stated["tables"])
    (workspace / f"{name}.profile.json").unlink()
    assert main(_again(follow=follow)) == 0
    assert (workspace / f"{name}.profile.json").read_bytes() == first
