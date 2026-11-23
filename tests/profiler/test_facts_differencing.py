# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The facts of one table, checked together so that no combination of them gives back a small cell.

A fact publishes the rows its rule covers, and of those the rows that satisfy it and the rows
that do not, all rounded; the table publishes its row count. Facts that overlap can be
differenced, so the runner checks every combination of a table's facts' states on the exact
counts (:func:`fluid_build.profiler.disclosure.suppress_differencing`) and withholds the facts
that would give back a small cell, or a zero cell where zeros are disclosive. Which empty
combinations the rules' texts rule out is :func:`fluid_build.profiler.facts.states_together`.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from fluid_build.profiler import _stats, disclosure, facts
from fluid_build.profiler.facts import FAILS, HOLDS, NOT_COVERED, STATES
from fluid_build.profiler.runner import ExposeSource, profile_exposes
from tests.profiler._fake_backend import FakeBackend

pytestmark = pytest.mark.unit

RULES: Dict[str, Dict[str, str]] = {
    "big": {"check": "amount >= 10"},
    "big_or_x": {"check": "amount >= 10 OR id = 'x'"},
    "open": {"check": "status = 'open'"},
    "big_or_open": {"check": "amount >= 10 OR status = 'open'"},
    "not_big": {"check": "NOT (amount >= 10)"},
    "five": {"check": "amount >= 5"},
    "paid_big": {"when": "status = 'paid'", "check": "amount >= 10"},
    "paid_five": {"when": "status = 'paid'", "check": "amount >= 5"},
    "paid_small": {"when": "status = 'paid'", "check": "NOT (amount >= 10)"},
    "xor": {
        "check": "(amount >= 10 AND NOT (status = 'open')) OR (NOT (amount >= 10) AND status = 'open')"
    },
    "fifteen": {"check": " OR ".join(f"amount = {n}" for n in range(1, 16))},
    "fifteen_or_open": {
        "check": " OR ".join(f"amount = {n}" for n in range(1, 16)) + " OR status = 'open'"
    },
    "fifteen_again": {"check": " OR ".join(f"amount = {n}" for n in range(15, 0, -1))},
    "many": {"check": " OR ".join(f"amount = {n}" for n in range(1, 18))},
    "many_or_open": {
        "check": " OR ".join(f"amount = {n}" for n in range(1, 18)) + " OR status = 'open'"
    },
}

CONTRACT: Dict[str, Any] = {
    "exposes": [
        {
            "exposeId": "orders",
            "contract": {
                "schema": [
                    {"name": "id", "type": "string"},
                    {"name": "amount", "type": "decimal(10,2)"},
                    {"name": "status", "type": "string"},
                    {"name": "seen", "type": "string"},
                ],
                "invariants": [{"id": rule, **body} for rule, body in RULES.items()],
            },
        }
    ]
}

ZEROS_SAFE = replace(disclosure.ACRO_DEFAULT, name="zeros-safe", zeros_disclosive=False)


def _plans(*rules: str) -> List[facts.FactPlan]:
    return [facts.plan_fact(CONTRACT, f"orders.{rule}", "orders", rule) for rule in rules]


def _together(*rules: str):
    return facts.states_together(CONTRACT, _plans(*rules))


def _rows(*groups: Tuple[int, str, str]) -> List[Dict[str, Any]]:
    """Rows from ``(count, amount, status)`` groups, each row with an id of its own."""
    rows: List[Dict[str, Any]] = []
    for count, amount, status in groups:
        for _ in range(count):
            rows.append({"id": f"r{len(rows)}", "amount": amount, "status": status, "seen": "s"})
    return rows


def _x(amount: str) -> Dict[str, Any]:
    return {"id": "x", "amount": amount, "status": "paid", "seen": "s"}


def _profile(tmp_path: Path, rows, *rules: str, rule=disclosure.ACRO_DEFAULT):
    path = tmp_path / "orders.ndjson"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    table = _stats.RequestTable("orders", False, (_stats.RequestColumn("seen", "category"),))
    asked = tuple(_stats.RequestFact(f"orders.{each}", "orders", each) for each in rules)
    return profile_exposes(
        CONTRACT,
        [ExposeSource("orders", str(path))],
        backend=FakeBackend(),
        rule=rule,
        request=_stats.Request(None, 16, (table,), (), asked),
    )


def _fared(result) -> Dict[str, Tuple[bool, Any]]:
    return {each.name.split(".", 1)[1]: (each.published, each.code) for each in result.facts}


def _statistic(result, rule: str) -> Dict[str, Any]:
    (entry,) = [each for each in result.document["invariants"] if each["name"] == f"orders.{rule}"]
    (statistic,) = entry["statistics"]
    return statistic


# ---------------------------------------------------------------------------
# What the texts rule out
# ---------------------------------------------------------------------------


def test_a_rule_or_more_cannot_fail_where_the_rule_holds():
    together = _together("big", "big_or_x")
    assert not together({"orders.big": HOLDS, "orders.big_or_x": FAILS})
    assert together({"orders.big": FAILS, "orders.big_or_x": HOLDS})
    assert together({"orders.big": HOLDS, "orders.big_or_x": HOLDS})


def test_a_rule_without_a_when_covers_every_row_and_one_with_a_when_need_not():
    together = _together("big", "paid_big")
    assert not together({"orders.big": NOT_COVERED})
    assert together({"orders.paid_big": NOT_COVERED})


def test_three_rules_are_read_together():
    together = _together("big", "open", "big_or_open")
    neither = {"orders.big": FAILS, "orders.open": FAILS}
    assert not together({**neither, "orders.big_or_open": HOLDS})
    assert together({**neither, "orders.big_or_open": FAILS})


def test_not_is_read_as_the_evaluator_reads_it_where_a_null_is_false():
    together = _together("big", "not_big")
    assert not together({"orders.big": HOLDS, "orders.not_big": HOLDS})
    assert not together({"orders.big": FAILS, "orders.not_big": FAILS})
    assert facts.fact_states(
        CONTRACT, *_plans("not_big"), {"amount": ["20.00", "1.00", None]}
    ) == bytes([FAILS, HOLDS, HOLDS])


def test_what_the_parts_mean_is_not_read():
    # amount >= 10 implies amount >= 5, but the two comparisons are two parts.
    assert _together("big", "five")({"orders.big": HOLDS, "orders.five": FAILS})


def test_a_when_and_a_check_share_their_parts():
    together = _together("paid_big", "paid_five", "open")
    assert not together({"orders.paid_big": NOT_COVERED, "orders.paid_five": HOLDS})
    assert together({"orders.paid_big": HOLDS, "orders.paid_five": FAILS})


def test_the_text_check_stops_at_a_number_of_readings_not_of_parts():
    pair = {"orders.fifteen": HOLDS, "orders.fifteen_or_open": FAILS}
    # 16 parts, 2^16 truths read for each of 2 facts: within the limit, so ruled out.
    assert not _together("fifteen", "fifteen_or_open")(pair)
    # A third fact over the same parts makes 3 x 2^16 readings: not read, nothing ruled out.
    assert _together("fifteen", "fifteen_or_open", "fifteen_again")(pair)


def test_rules_that_share_more_parts_than_it_reads_rule_nothing_out():
    together = _together("many", "many_or_open")
    assert together({"orders.many": HOLDS, "orders.many_or_open": FAILS})


def test_the_factors_are_the_linked_facts_with_what_they_can_be_together():
    together = _together("big", "big_or_open", "paid_big", "five")
    linked, alone = together.factors(["orders.big", "orders.big_or_open", "orders.five"])
    assert linked == (
        ("orders.big", "orders.big_or_open"),
        {(HOLDS, HOLDS), (FAILS, HOLDS), (FAILS, FAILS)},
    )
    assert alone == (("orders.five",), {(FAILS,), (HOLDS,)})


def test_the_facts_of_a_group_too_large_to_read_are_read_one_by_one():
    names = ["orders.fifteen", "orders.fifteen_or_open", "orders.fifteen_again"]
    factors = _together("fifteen", "fifteen_or_open", "fifteen_again").factors(names)
    assert factors == [((name,), {(FAILS,), (HOLDS,)}) for name in names]


def test_a_fact_state_is_one_byte_per_row():
    states = facts.fact_states(
        CONTRACT,
        *_plans("paid_big"),
        {"status": ["paid", "paid", "open", None], "amount": ["20.00", "1.00", "20.00", "20.00"]},
    )
    assert states == bytes([HOLDS, FAILS, NOT_COVERED, NOT_COVERED])


# ---------------------------------------------------------------------------
# The fixpoint
# ---------------------------------------------------------------------------


COVERED = {(FAILS,), (HOLDS,)}


def _free(order):
    """Factors of facts free of each other, each covering every row."""
    return [((position,), COVERED) for position in order]


def _linked(*groups):
    """Factors from ``(positions, allowed)`` groups, each cut down to the positions shown."""

    def factors(order):
        shown = set(order)
        out = []
        for positions, allowed in groups:
            keep = [at for at, position in enumerate(positions) if position in shown]
            if keep:
                kept = tuple(positions[at] for at in keep)
                out.append((kept, {tuple(states[at] for at in keep) for states in allowed}))
        return out

    return factors


def test_a_small_cell_withholds_the_facts_whose_change_merges_it():
    cells = {(HOLDS, HOLDS, HOLDS): 3, (HOLDS, FAILS, HOLDS): 30, (FAILS, HOLDS, HOLDS): 30}
    cells[(FAILS, FAILS, FAILS)] = 37
    withheld = disclosure.suppress_differencing(cells, [0, 1, 2], ZEROS_SAFE, _free, STATES)
    assert withheld == {0, 1}


def test_a_small_cell_no_one_change_merges_withholds_what_it_differs_in_from_the_nearest():
    cells = {
        (HOLDS, HOLDS, HOLDS): 3,
        (HOLDS, FAILS, FAILS): 20,
        (FAILS, HOLDS, FAILS): 20,
        (FAILS, FAILS, HOLDS): 20,
        (FAILS, FAILS, FAILS): 37,
    }
    withheld = disclosure.suppress_differencing(cells, [0, 1, 2], ZEROS_SAFE, _free, STATES)
    assert withheld == {0, 1, 2}


def test_a_zero_cell_counts_only_where_the_texts_allow_it_and_zeros_are_disclosive():
    cells = {(HOLDS, HOLDS): 50, (FAILS, FAILS): 50}
    allows = _linked(((0, 1), {(HOLDS, HOLDS), (FAILS, FAILS), (FAILS, HOLDS)}))
    rule = disclosure.ACRO_DEFAULT
    assert disclosure.suppress_differencing(cells, [0, 1], rule, allows, STATES) == {0, 1}
    assert disclosure.suppress_differencing(cells, [0, 1], ZEROS_SAFE, allows, STATES) == set()
    neither = _linked(((0, 1), {(HOLDS, HOLDS), (FAILS, FAILS)}))
    assert disclosure.suppress_differencing(cells, [0, 1], rule, neither, STATES) == set()


def test_a_zero_cell_no_one_fact_change_reaches_is_still_found():
    # The texts allow (NOT_COVERED, NOT_COVERED) and neither (HOLDS, NOT_COVERED) nor any
    # other combination one change from a held one: the zero is two changes away.
    cells = {(HOLDS, FAILS): 50, (FAILS, HOLDS): 50}
    texts = _linked(((0, 1), {(NOT_COVERED, NOT_COVERED), (HOLDS, FAILS), (FAILS, HOLDS)}))
    rule = disclosure.ACRO_DEFAULT
    assert disclosure.suppress_differencing(cells, [0, 1], rule, texts, STATES) == {0, 1}


def test_a_zero_cell_across_factors_is_found():
    # Two factors, each allowing (NOT_COVERED, NOT_COVERED), (HOLDS, FAILS), (FAILS, HOLDS):
    # the records hold only the three combinations where both factors agree, so every
    # combination of one factor's state beside the other's different state is empty,
    # and no change of one fact reaches a held combination from any of them.
    pair = {(NOT_COVERED, NOT_COVERED), (HOLDS, FAILS), (FAILS, HOLDS)}
    cells = {first + first: 40 for first in pair}
    texts = _linked(((0, 1), pair), ((2, 3), pair))
    rule = disclosure.ACRO_DEFAULT
    assert disclosure.suppress_differencing(cells, range(4), rule, texts, STATES) == {0, 1, 2, 3}


def test_small_cells_are_settled_before_the_empty_combinations_around_them():
    # Facts 0 and 1 set one record apart; fact 2 splits every other cell. The combination
    # fact 2's other state would make beside that record is empty, but it goes when
    # facts 0 and 1 are withheld, so it does not withhold fact 2.
    cells = {
        (HOLDS, HOLDS, HOLDS): 25,
        (HOLDS, HOLDS, FAILS): 25,
        (FAILS, FAILS, HOLDS): 25,
        (FAILS, FAILS, FAILS): 24,
        (FAILS, HOLDS, FAILS): 1,
    }

    texts = _linked(((0, 1), {(HOLDS, HOLDS), (FAILS, FAILS), (FAILS, HOLDS)}), ((2,), COVERED))
    rule = disclosure.ACRO_DEFAULT
    assert disclosure.suppress_differencing(cells, [0, 1, 2], rule, texts, STATES) == {0, 1}


def test_a_fact_that_failed_its_own_check_is_left_out_of_the_combinations():
    cells = {(HOLDS, HOLDS): 3, (FAILS, HOLDS): 30, (FAILS, FAILS): 67}
    assert disclosure.suppress_differencing(cells, [1], ZEROS_SAFE, _free, STATES) == set()
    assert disclosure.suppress_differencing(cells, [0, 1], ZEROS_SAFE, _free, STATES) == {0}


def test_the_fixpoint_does_not_depend_on_the_facts_order():
    cells = {
        (HOLDS, HOLDS, HOLDS, FAILS): 3,
        (HOLDS, FAILS, HOLDS, FAILS): 30,
        (FAILS, HOLDS, HOLDS, HOLDS): 30,
        (FAILS, FAILS, FAILS, HOLDS): 37,
        (FAILS, FAILS, HOLDS, FAILS): 12,
    }
    names = "abcd"
    found = set()
    for order in itertools.permutations(range(4)):
        moved = {tuple(cell[at] for at in order): count for cell, count in cells.items()}
        withheld = disclosure.suppress_differencing(moved, range(4), ZEROS_SAFE, _free, STATES)
        found.add(frozenset(names[order[at]] for at in withheld))
    assert found == {frozenset("b")}


# ---------------------------------------------------------------------------
# A profile's facts
# ---------------------------------------------------------------------------


def test_a_fact_beside_the_same_fact_or_one_row_outside_it_is_withheld(tmp_path):
    rows = _rows((50, "20.00", "paid"), (49, "1.00", "paid")) + [_x("1.00")]
    for alone in ("big", "big_or_x"):
        assert _fared(_profile(tmp_path, rows, alone)) == {alone: (True, None)}
    result = _profile(tmp_path, rows, "big", "big_or_x")
    assert _fared(result) == {"big": (False, "D"), "big_or_x": (False, "D")}
    for rule in ("big", "big_or_x"):
        assert _statistic(result, rule)["groups"] == [{"group": "", "status": "C"}]


def test_a_fact_the_pair_does_not_involve_stays_published(tmp_path):
    rows = _rows((25, "20.00", "open"), (25, "20.00", "paid"), (25, "1.00", "open"))
    rows += _rows((24, "1.00", "paid")) + [_x("1.00")]
    result = _profile(tmp_path, rows, "big", "big_or_x", "open")
    assert _fared(result) == {
        "big": (False, "D"),
        "big_or_x": (False, "D"),
        "open": (True, None),
    }


def test_two_facts_that_together_cover_every_row_are_withheld_beside_the_row_count(tmp_path):
    # Every row is paid: alone, paid_big is withheld as its when covers every row. Beside
    # paid_small, no single change of a fact reaches the empty combination where neither
    # covers a row, but the texts allow it, so both are withheld.
    rows = _rows((50, "20.00", "paid"), (50, "1.00", "paid"))
    assert _fared(_profile(tmp_path, rows, "paid_big")) == {"paid_big": (False, "D")}
    result = _profile(tmp_path, rows, "paid_big", "paid_small")
    assert _fared(result) == {"paid_big": (False, "D"), "paid_small": (False, "D")}
    safe = _profile(tmp_path, rows, "paid_big", "paid_small", rule=ZEROS_SAFE)
    assert _fared(safe) == {"paid_big": (True, None), "paid_small": (True, None)}


def test_an_empty_combination_two_changes_from_every_held_one_is_found(tmp_path):
    # big, open and their exclusive or: |big AND open| = |big| + |open| - |xor| = 0, and the
    # texts allow a row that is big and open.
    rows = _rows((50, "20.00", "paid"), (50, "1.00", "open"), (50, "1.00", "paid"))
    result = _profile(tmp_path, rows, "big", "open", "xor")
    assert _fared(result) == {"big": (False, "D"), "open": (False, "D"), "xor": (False, "D")}
    safe = _profile(tmp_path, rows, "big", "open", "xor", rule=ZEROS_SAFE)
    assert all(shown for shown, _ in _fared(safe).values())


def test_a_fact_beside_the_same_fact_or_one_row_inside_it_is_withheld_where_zeros_are_disclosive(
    tmp_path,
):
    rows = _rows((49, "20.00", "paid"), (50, "1.00", "paid")) + [_x("20.00")]
    result = _profile(tmp_path, rows, "big", "big_or_x")
    assert _fared(result) == {"big": (False, "D"), "big_or_x": (False, "D")}
    safe = _profile(tmp_path, rows, "big", "big_or_x", rule=ZEROS_SAFE)
    assert _fared(safe) == {"big": (True, None), "big_or_x": (True, None)}


def test_three_facts_whose_pairs_difference_to_large_counts_still_give_a_small_cell(tmp_path):
    # |big| = 33, |open| = 33, |big OR open| = 63: each fact and each nested pair's
    # difference (30 and 30) holds 10 records or more, yet 33 + 33 - 63 = 3.
    rows = _rows((3, "20.00", "open"), (30, "20.00", "paid"), (30, "1.00", "open"))
    rows += _rows((37, "1.00", "paid"))
    result = _profile(tmp_path, rows, "big", "open", "big_or_open")
    assert _fared(result) == {
        "big": (False, "D"),
        "open": (False, "D"),
        "big_or_open": (True, None),
    }
    wide = _rows((30, "20.00", "open"), (30, "20.00", "paid"), (30, "1.00", "open"))
    wide += _rows((37, "1.00", "paid"))
    assert all(
        shown
        for shown, _ in _fared(_profile(tmp_path, wide, "big", "open", "big_or_open")).values()
    )


def test_what_is_withheld_does_not_depend_on_the_order_of_the_facts(tmp_path):
    rows = _rows((3, "20.00", "open"), (30, "20.00", "paid"), (30, "1.00", "open"))
    rows += _rows((36, "1.00", "paid")) + [_x("1.00")]
    listed = ("big", "open", "big_or_open", "big_or_x")
    fared = {
        tuple(sorted(_fared(_profile(tmp_path, rows, *order)).items()))
        for order in itertools.permutations(listed)
    }
    assert len(fared) == 1


def test_a_nested_pair_whose_cells_all_hold_ten_records_is_published(tmp_path):
    rows = _rows((20, "20.00", "paid"), (15, "1.00", "open"), (25, "1.00", "paid"))
    result = _profile(tmp_path, rows, "big", "big_or_open")
    assert _fared(result) == {"big": (True, None), "big_or_open": (True, None)}
    (group,) = _statistic(result, "big_or_open")["groups"]
    assert group["levelCounts"] == [25, 35] and group["k"] == 60


def test_the_counts_are_checked_before_they_are_rounded(tmp_path):
    # 101 and 102 records round to 100 and 100, 102 and 103 to 100 and 105: either way
    # the exact counts differ by the one row x.
    for big in (101, 102):
        rows = _rows((big, "20.00", "paid"), (60, "1.00", "paid")) + [_x("1.00")]
        result = _profile(tmp_path, rows, "big", "big_or_x")
        assert _fared(result) == {"big": (False, "D"), "big_or_x": (False, "D")}


def test_a_when_that_leaves_few_rows_uncovered_is_withheld_beside_the_row_count(tmp_path):
    few = _rows((50, "20.00", "paid"), (47, "1.00", "paid"), (3, "1.00", "open"))
    result = _profile(tmp_path, few, "paid_big")
    assert _fared(result) == {"paid_big": (False, "D")}
    assert _statistic(result, "paid_big")["sampleSize"] == {"min": 0}
    enough = _rows((50, "20.00", "paid"), (47, "1.00", "paid"), (30, "1.00", "open"))
    assert _fared(_profile(tmp_path, enough, "paid_big")) == {"paid_big": (True, None)}


def test_a_withheld_fact_writes_the_band_of_its_rows_only_where_it_covers_every_row(tmp_path):
    rows = _rows((97, "20.00", "paid"), (3, "1.00", "open"))
    result = _profile(tmp_path, rows, "big", "paid_big")
    fared = _fared(result)
    assert fared["big"] == (False, "A") and fared["paid_big"][0] is False
    assert _statistic(result, "big")["sampleSize"] == {"min": 98, "max": 102}
    assert _statistic(result, "paid_big")["sampleSize"] == {"min": 0}
