# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Date pairs, relationship dates, structural classes and exposure (PROFILE-SPEC format 1-3-0).

The real registry reads every document and runs every output check. The fixtures give every
cell the tests read at least ten records, so a withheld cell is the check's doing and not the
data's.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence

import pytest

from fluid_build.profiler import dates, disclosure, document
from fluid_build.profiler._stats import (
    Request,
    RequestColumn,
    RequestDateLink,
    RequestDatePair,
    RequestEnd,
    RequestExposure,
    RequestRelationship,
    RequestTable,
    load_backend,
)
from fluid_build.profiler.runner import ExposeSource, profile_exposes

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.profile")
from flux_stats.profile import load_profile_text  # noqa: E402

EPOCH = date(1970, 1, 1)
#: Days from opening to closing: every cell of the ladder up to 731 days gets a hundred rows
#: in a thousand and three hundred.
GAPS = (0, 1, 3, 5, 10, 20, 45, 60, 120, 200, 300, 500, 900)
ROWS = 1300
MS = 86_400_000


def _levels(entry: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """An ``order`` entry's published count per level; ``None`` where it is withheld."""
    (group,) = entry["groups"]
    assert group["status"] == "F"
    return dict(zip(entry["levels"], group["levelCounts"], strict=True))


def _cells(entry: Dict[str, Any]) -> List[Optional[int]]:
    (group,) = entry["groups"]
    assert group["status"] == "F"
    return list(group["binCounts"])


def _published_zeros(pair: Dict[str, Any]) -> int:
    """How many cells of a pair's two statistics are published as a zero."""
    found = 0
    held = [(each, "binCounts") for each in pair["statistics"]] + [(pair["order"], "levelCounts")]
    for statistic, field in held:
        for group in statistic["groups"]:
            if group["status"] == "F":
                found += sum(1 for count in group[field] if count == 0)
    return found


def _loans_contract(rule: Optional[str] = None) -> Dict[str, Any]:
    invariants = [{"id": "order", "check": rule}] if rule else []
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "p",
        "exposes": [
            {
                "exposeId": "loans",
                "kind": "table",
                "contract": {
                    "schema": [
                        {"name": "opened", "type": "date"},
                        {"name": "closed", "type": "date"},
                        {"name": "audited", "type": "timestamp"},
                    ],
                    "invariants": invariants,
                },
            }
        ],
    }


def _loans(gaps: Sequence[int] = GAPS, broken: int = 0) -> List[Dict[str, Any]]:
    """Each loan opens on a day of 2023 and closes ``gaps`` later; *broken* close the day before."""
    rows = []
    for number in range(ROWS):
        opened = date(2023, 1, 1) + timedelta(days=number % 360)
        closed = opened + timedelta(days=gaps[number % len(gaps)])
        if number < broken:
            closed = opened - timedelta(days=1)
        rows.append(
            {
                "opened": opened.isoformat(),
                "closed": closed.isoformat(),
                "audited": opened.isoformat() + "T10:00:00Z",
            }
        )
    return rows


def _write(path, rows) -> str:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return str(path)


def _profile_loans(tmp_path, contract, rows, **options: Any):
    return profile_exposes(
        contract,
        [ExposeSource("loans", _write(tmp_path / "loans.ndjson", rows))],
        backend=options.pop("backend", load_backend()),
        **options,
    )


def _pairs(result, table="loans") -> Dict[tuple, Dict[str, Any]]:
    return {
        (each["start"], each["end"]): each
        for each in result.document["tables"][table].get("datePairs", ())
    }


# -- the one function that decides a declared zero ------------------------------------------

START, END = (None, "opened"), (None, "closed")
NONE_BEFORE = {"before": 0, "same": 5, "after": 90}


def test_a_rule_that_end_is_not_before_start_empties_only_before():
    assert dates.declared_empty_levels(
        ["closed IS NULL OR closed >= opened"], START, END, NONE_BEFORE
    ) == frozenset({"before"})
    assert dates.declared_empty_levels(["closed >= opened"], START, END, NONE_BEFORE) == {"before"}
    assert dates.declared_empty_levels(["opened <= closed"], START, END, NONE_BEFORE) == {"before"}


def test_a_strict_rule_empties_before_and_same():
    levels = {"before": 0, "same": 0, "after": 90}
    assert dates.declared_empty_levels(
        ["closed IS NULL OR closed > opened"], START, END, levels
    ) == frozenset({"before", "same"})
    assert dates.declared_empty_levels(["opened < closed"], START, END, levels) == {
        "before",
        "same",
    }


def test_the_reverse_rules_empty_after():
    levels = {"before": 90, "same": 0, "after": 0}
    assert dates.declared_empty_levels(["closed <= opened"], START, END, levels) == {"after"}
    assert dates.declared_empty_levels(["closed < opened"], START, END, levels) == {
        "after",
        "same",
    }


def test_a_rule_the_records_break_empties_nothing():
    # ``>=`` is declared and one row is before: no zero is published on the rule's word.
    broken = {"before": 1, "same": 5, "after": 90}
    assert (
        dates.declared_empty_levels(["closed IS NULL OR closed >= opened"], START, END, broken)
        == frozenset()
    )
    # ``>`` empties before and same, and same holds a record: not before alone either.
    half = {"before": 0, "same": 1, "after": 90}
    assert dates.declared_empty_levels(["closed > opened"], START, END, half) == frozenset()


def test_no_rule_empties_nothing():
    assert dates.declared_empty_levels([], START, END, NONE_BEFORE) == frozenset()
    for unrelated in (
        "closed IS NOT NULL",
        "closed >= '2020-01-01'",
        "closed <> opened",
        "closed >= other",
        "other >= opened",
        "closed >= opened OR closed <= opened",
        "not valid FLUID SQL ((",
    ):
        assert dates.declared_empty_levels([unrelated], START, END, NONE_BEFORE) == frozenset()


def test_rules_that_between_them_empty_every_level_are_not_leaned_on():
    levels = {"before": 0, "same": 0, "after": 0}
    assert (
        dates.declared_empty_levels(["closed > opened", "closed <= opened"], START, END, levels)
        == frozenset()
    )


def test_a_rule_names_the_pair_through_the_relationship_it_reads_by():
    through = ("owner", "signup")
    assert dates.declared_empty_levels(
        ["placed >= owner.signup"], through, (None, "placed"), NONE_BEFORE
    ) == {"before"}
    # The same text read through another relationship, or on the row's own column, says nothing.
    assert (
        dates.declared_empty_levels(
            ["placed >= other.signup"], through, (None, "placed"), NONE_BEFORE
        )
        == frozenset()
    )
    assert (
        dates.declared_empty_levels(["placed >= signup"], through, (None, "placed"), NONE_BEFORE)
        == frozenset()
    )


# -- date pairs -----------------------------------------------------------------------------


def test_a_date_pair_holds_its_days_and_its_order_and_names_format_1_3_0(tmp_path):
    result = _profile_loans(
        tmp_path, _loans_contract("closed IS NULL OR closed >= opened"), _loans()
    )
    assert result.document["_schemaURL"] == document.SCHEMA_URL_1_3_0
    (pair,) = _pairs(result).values()  # the timestamp column forms none
    assert (pair["start"], pair["end"]) == ("opened", "closed")
    (days,) = pair["statistics"]
    assert days["definition"] == "marginalHistogram/1"
    # The ladder is trimmed to the bins from the first holding a record to the last: 0 to 1460.
    assert days["bins"] == [str(edge) for edge in dates.DAY_LADDER[8:18]]
    assert pair["order"]["definition"] == "levelHistogram/1"
    assert pair["order"]["levels"] == ["after", "before", "same"]
    # The counts are of the rows where both dates are known, rounded as every count is.
    assert pair["order"]["groups"][0]["k"] == disclosure.round_count(ROWS, disclosure.ACRO_DEFAULT)
    profile, _ = load_profile_text(result.canonical.decode("utf-8"))
    table = profile.tables["loans"]
    assert [(each.start, each.end) for each in table.date_pairs] == [("opened", "closed")]


def test_the_levels_the_trimmed_edges_leave_out_are_published_as_zero(tmp_path):
    # Every loan closes on or after its opening, a hundred or more on the first and the last bin
    # of the trimmed ladder: the outer edges stand on those records, and the order's "before"
    # lies wholly under them. It is published as 0 whether or not a rule says it.
    for rule in ("closed IS NULL OR closed >= opened", None):
        result = _profile_loans(tmp_path, _loans_contract(rule), _loans())
        (pair,) = _pairs(result).values()
        order = _levels(pair["order"])
        assert order["before"] == 0
        assert order["after"] is not None and order["same"] is not None
        (group,) = pair["statistics"][0]["groups"]
        assert group["status"] == "F"
        assert all(count for count in group["binCounts"])  # no empty cell is left to protect
        assert set(group.get("cellStatus", "F")) == {"F"}


def test_when_no_offset_is_below_one_before_and_same_are_implied_zeros(tmp_path):
    gaps = tuple(gap for gap in GAPS if gap >= 1)
    result = _profile_loans(tmp_path, _loans_contract(), _loans(gaps=gaps))
    (pair,) = _pairs(result).values()
    assert pair["statistics"][0]["bins"][0] == "1"
    order = _levels(pair["order"])
    assert order["before"] == 0 and order["same"] == 0
    assert order["after"] is not None and order["after"] > 0


def test_a_negative_offset_keeps_before_a_cell_of_the_check(tmp_path):
    # A hundred loans close the day before they open: the trimmed ladder starts at -1, and
    # "before" is a counted cell, not an implied zero.
    result = _profile_loans(tmp_path, _loans_contract(), _loans(broken=100))
    (pair,) = _pairs(result).values()
    assert pair["statistics"][0]["bins"][0] == "-1"
    before = _levels(pair["order"])["before"]
    assert before is not None and before > 0


def test_an_end_bin_under_the_least_cell_publishes_no_day_histogram(tmp_path):
    # Three loans close the day before they open: the first bin holds fewer than ten records,
    # so no single record may place an outer edge. The order is checked alone.
    result = _profile_loans(tmp_path, _loans_contract(), _loans(broken=3))
    (pair,) = _pairs(result).values()
    assert pair["statistics"] == []
    assert _published_zeros(pair) == 0
    before = pair["order"]["groups"][0]
    assert before["status"] == "C" or before["levelCounts"][1] is None
    load_profile_text(result.canonical.decode("utf-8"))
    said = dict(result.not_measured)
    assert "fewer than the least cell" in said["days of date pair opened to closed of loans"]


def test_a_rule_the_records_break_publishes_no_zero(tmp_path):
    contract = _loans_contract("closed IS NULL OR closed >= opened")
    # Three loans close the day before they open: a cell of three is small, and stays withheld.
    result = _profile_loans(tmp_path, contract, _loans(broken=3))
    (pair,) = _pairs(result).values()
    assert _published_zeros(pair) == 0


def test_without_a_day_histogram_a_rule_the_records_keep_publishes_its_order_zeros(tmp_path):
    # Three loans close the day after they open and the rest five days after: the first bin
    # holds three records, so the days have no outer edge, and the order is checked alone. The
    # strict rule empties "before" and "same", and the records keep both empty.
    rows = _loans(gaps=(5,))
    for row in rows[:3]:
        row["closed"] = (date.fromisoformat(row["opened"]) + timedelta(days=1)).isoformat()
    result = _profile_loans(tmp_path, _loans_contract("closed IS NULL OR closed > opened"), rows)
    (pair,) = _pairs(result).values()
    assert pair["statistics"] == []
    order = _levels(pair["order"])
    assert order["before"] == 0 and order["same"] == 0


def test_a_rule_that_holds_zeros_not_disclosive_publishes_them_as_the_check_does(tmp_path):
    rule = disclosure.DisclosureRule(
        name="zeros-ok",
        minimum_cell=10,
        zeros_disclosive=False,
        rounding_base=5,
        dominance_n=2,
        dominance_k="0.85",
        p_ratio="0.10",
    )
    result = _profile_loans(
        tmp_path, _loans_contract("closed IS NULL OR closed >= opened"), _loans(), rule=rule
    )
    (pair,) = _pairs(result).values()
    assert _levels(pair["order"])["before"] == 0


def test_the_days_and_the_order_are_checked_in_one_call_on_one_partition(tmp_path):
    seen: List[Any] = []

    class Spy:
        def __init__(self, backend):
            self._backend = backend

        def __getattr__(self, name):
            return getattr(self._backend, name)

        def check_edge_counts(self, statistics, **kwargs):
            seen.append(list(statistics))
            return self._backend.check_edge_counts(statistics, **kwargs)

    backend = load_backend()
    _profile_loans(
        tmp_path,
        _loans_contract("closed IS NULL OR closed >= opened"),
        _loans(),
        backend=Spy(backend),
    )
    encode = backend.encoder(16)
    pair_calls = [
        call
        for call in seen
        if len(call) == 2
        and all(each.identifier == "marginalHistogram/1" for each in call)
        and len(call[0].edges) != 169  # not a calendar's hours and days of the week
    ]
    assert len(pair_calls) == 1  # one call for the one pair: the days and the order together
    (days, order) = pair_calls[0]
    # The days are the ladder from its first bin holding a record to its last; the order is the
    # same partition coarsened at day 1 (day 0 is its first edge), on the same outer edges.
    assert days.edges == tuple(encode(Fraction(each)) for each in dates.DAY_LADDER[8:18])
    assert order.edges == tuple(encode(Fraction(each)) for each in (0, 1, 1461))
    assert set(order.edges) <= set(days.edges)
    assert order.edges[0] == days.edges[0] and order.edges[-1] == days.edges[-1]


def test_a_timestamp_pair_is_named_and_not_formed(tmp_path):
    result = _profile_loans(tmp_path, _loans_contract(), _loans())
    said = dict(result.not_measured)
    assert said["date pairs of loans.audited"].startswith("its order is on the millisecond")
    assert all("audited" not in pair for pair in _pairs(result))


def test_the_pair_of_a_value_that_is_not_a_whole_day_is_not_formed(tmp_path):
    rows = _loans()
    rows[0]["closed"] = "2023-02-01T10:30:00Z"
    result = _profile_loans(tmp_path, _loans_contract(), rows)
    assert not _pairs(result)
    assert any("not a whole day" in reason for _, reason in result.not_measured)


# -- request mode ---------------------------------------------------------------------------


def _request(*tables: RequestTable, relationships: Sequence[RequestRelationship] = ()) -> Request:
    return Request(
        contract_digest=None, fractional_bits=16, tables=tables, relationships=tuple(relationships)
    )


def test_a_request_forms_only_the_pairs_it_lists(tmp_path):
    contract = _loans_contract()
    contract["exposes"][0]["contract"]["schema"].append({"name": "reviewed", "type": "date"})
    rows = [{**row, "reviewed": row["closed"]} for row in _loans()]
    asked = _request(
        RequestTable(
            "loans",
            False,
            (
                RequestColumn("opened", "timestamp"),
                RequestColumn("closed", "timestamp"),
                RequestColumn("reviewed", "timestamp"),
                RequestColumn("audited", "timestamp"),
            ),
            date_pairs=(
                RequestDatePair("opened", "closed"),
                RequestDatePair("opened", "audited"),
            ),
        )
    )
    result = _profile_loans(tmp_path, contract, rows, request=asked)
    assert list(_pairs(result)) == [("opened", "closed")]  # not closed to reviewed, not default
    said = dict(result.not_measured)
    assert said["date pair opened to audited of loans"].startswith(
        "its order is on the millisecond"
    )
    assert "date pair opened to closed of loans" not in said
    # Nothing a request does not list is a date pair; without a pair listed there is none.
    bare = _request(
        RequestTable("loans", False, (RequestColumn("opened", "timestamp"),)),
    )
    assert not _pairs(_profile_loans(tmp_path, contract, rows, request=bare))


def test_a_listed_pair_keeps_the_direction_the_request_gives(tmp_path):
    asked = _request(
        RequestTable(
            "loans",
            False,
            (RequestColumn("opened", "timestamp"), RequestColumn("closed", "timestamp")),
            date_pairs=(RequestDatePair("closed", "opened"),),
        )
    )
    result = _profile_loans(
        tmp_path, _loans_contract("closed IS NULL OR closed >= opened"), _loans(), request=asked
    )
    (pair,) = _pairs(result).values()
    assert (pair["start"], pair["end"]) == ("closed", "opened")
    order = _levels(pair["order"])
    # opened is never after closed, so from closed to opened nothing is "after": the rule is
    # read the other way round, and says only that "after" is empty.
    assert order["after"] == 0


# -- the format -----------------------------------------------------------------------------


def test_the_format_is_the_least_that_holds_the_document():
    assert document.schema_url_for({"t": {"columns": {}}}) == document.SCHEMA_URL
    assert document.schema_url_for({"t": {"columns": {}, "datePairs": [{}]}}) == (
        document.SCHEMA_URL_1_3_0
    )
    assert document.schema_url_for(
        {"t": {"columns": {}}}, relationships=[{"statistics": [], "dates": [{}]}]
    ) == (document.SCHEMA_URL_1_3_0)
    for member in ("structural", "exposure"):
        assert document.schema_url_for({"t": {"columns": {}}}, relationships=[{member: []}]) == (
            document.SCHEMA_URL_1_3_0
        )
    # A document that holds a 1-4-0 form stays 1-4-0, with or without a date pair.
    ranked = {
        "columns": {},
        "datePairs": [{}],
        "columnPairs": [{"statistics": [{"definition": "kendallTau/1"}]}],
    }
    assert document.schema_url_for({"t": ranked}) == document.SCHEMA_URL_1_4_0
    assert document.schema_url_for(
        {"t": {"columns": {}}},
        relationships=[{"statistics": [{"groupBy": "c"}], "dates": [{}]}],
    ) == (document.SCHEMA_URL_1_4_0)


# -- relationships --------------------------------------------------------------------------

CUSTOMERS = 300


def _shop_contract(
    *,
    derive: bool = False,
    check: bool = False,
    filtered: bool = False,
    signup_rules: Optional[List[Dict[str, Any]]] = None,
    rule: bool = True,
) -> Dict[str, Any]:
    parent_rules: List[Dict[str, Any]] = [
        {"id": "left", "check": "left_on IS NULL OR left_on >= signup"}
    ]
    if derive:
        aggregate: Dict[str, Any] = {"function": "count", "from": "orders", "via": "owner"}
        if filtered:
            aggregate["filter"] = "status = 'open'"
        parent_rules.append({"id": "count", "derive": "order_count", "aggregate": aggregate})
    if check:
        parent_rules.append({"id": "has", "check": "order_count >= 1"})
    signup: Dict[str, Any] = {"name": "signup", "type": "date"}
    if signup_rules:
        signup["validationRules"] = signup_rules
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "p",
        "exposes": [
            {
                "exposeId": "customers",
                "contract": {
                    "schema": [
                        {"name": "customer_id", "type": "int"},
                        signup,
                        {"name": "left_on", "type": "date"},
                        {"name": "order_count", "type": "int"},
                        {"name": "status", "type": "enum('open','closed')"},
                    ],
                    "invariants": parent_rules,
                },
            },
            {
                "exposeId": "orders",
                "contract": {
                    "schema": [
                        {"name": "order_id", "type": "int"},
                        {"name": "customer_id", "type": "int"},
                        {"name": "placed", "type": "date"},
                    ],
                    "invariants": (
                        [{"id": "after", "check": "placed >= owner.signup"}] if rule else []
                    ),
                },
                "semantics": {
                    "relationships": [
                        {
                            "name": "owner",
                            "to": "customers",
                            "fromColumns": ["customer_id"],
                            "toColumns": ["customer_id"],
                            "cardinality": "many_to_one",
                        }
                    ]
                },
            },
        ],
    }


def _signup(number: int) -> date:
    return date(2023, 1, 1) + timedelta(days=(number * 7) % 700)


def _customers() -> List[Dict[str, Any]]:
    return [
        {
            "customer_id": n,
            "signup": _signup(n).isoformat(),
            "left_on": (_signup(n) + timedelta(days=30 + n % 400)).isoformat() if n % 3 else None,
            "order_count": 2,
            "status": "open" if n % 2 else "closed",
        }
        for n in range(CUSTOMERS)
    ]


#: Each customer's orders come the same few days after sign-up, all after it.
LAGS = (0, 1, 3, 5, 10, 20, 45, 60, 120, 200, 300)


def _orders(skip: Sequence[int] = ()) -> List[Dict[str, Any]]:
    rows = []
    for customer in range(CUSTOMERS):
        if customer in skip:
            continue
        for lag in LAGS[customer % 3 :: 3] or (0,):
            rows.append(
                {
                    "order_id": len(rows),
                    "customer_id": customer,
                    "placed": (_signup(customer) + timedelta(days=lag)).isoformat(),
                }
            )
    return rows


def _profile_shop(tmp_path, contract, orders=None, customers=None, **options: Any):
    return profile_exposes(
        contract,
        [
            ExposeSource("customers", _write(tmp_path / "c.ndjson", customers or _customers())),
            ExposeSource("orders", _write(tmp_path / "o.ndjson", orders or _orders())),
        ],
        backend=load_backend(),
        **options,
    )


def _relationship(result) -> Dict[str, Any]:
    (entry,) = result.document["relationships"]
    return entry


def test_relationship_dates_state_each_parent_and_child_date_pair(tmp_path):
    result = _profile_shop(tmp_path, _shop_contract())
    entry = _relationship(result)
    assert result.document["_schemaURL"] == document.SCHEMA_URL_1_3_0
    assert {(each["parent"], each["child"]) for each in entry["dates"]} == {
        ("signup", "placed"),
        ("left_on", "placed"),
    }
    load_profile_text(result.canonical.decode("utf-8"))
    signup = next(each for each in entry["dates"] if each["parent"] == "signup")
    # ``placed >= owner.signup`` is the child table's rule: no order is before its customer's
    # sign-up, and the cells under day 0 are published as the zeros the contract declares.
    assert _levels(signup["order"])["before"] == 0
    assert signup["statistics"][0]["bins"][0] == "0"
    # The same cells without the rule: the trimmed edges leave "before" out, as the records do.
    plain = _profile_shop(tmp_path, _shop_contract(rule=False))
    unruled = next(each for each in _relationship(plain)["dates"] if each["parent"] == "signup")
    assert _levels(unruled["order"])["before"] == 0


def test_relationship_dates_are_formed_only_for_the_pairs_a_request_lists(tmp_path):
    asked = _request(
        RequestTable(
            "customers",
            False,
            (RequestColumn("customer_id", "number"), RequestColumn("signup", "timestamp")),
        ),
        RequestTable(
            "orders",
            False,
            (RequestColumn("customer_id", "number"), RequestColumn("placed", "timestamp")),
        ),
        relationships=[
            RequestRelationship(
                RequestEnd("customers", ("customer_id",)),
                RequestEnd("orders", ("customer_id",)),
                (),
                dates=(RequestDateLink("signup", "placed"),),
            )
        ],
    )
    result = _profile_shop(tmp_path, _shop_contract(), request=asked)
    entry = _relationship(result)
    assert [(each["parent"], each["child"]) for each in entry["dates"]] == [("signup", "placed")]
    assert entry["statistics"] == []  # no children per parent were asked
    assert "structural" not in entry and "exposure" not in entry
    load_profile_text(result.canonical.decode("utf-8"))


def test_childless_is_stated_only_for_a_declared_count_that_is_checked_at_least_one(tmp_path):
    declared = _relationship(_profile_shop(tmp_path, _shop_contract(derive=True, check=True)))
    assert declared["structural"] == ["childless"]
    for missing in (
        _shop_contract(derive=True),  # counted, but never checked
        _shop_contract(check=True),  # checked, but never derived from the children
        _shop_contract(derive=True, check=True, filtered=True),  # counts a part of the children
        _shop_contract(),
    ):
        assert "structural" not in _relationship(_profile_shop(tmp_path, missing))


def test_childless_is_not_stated_when_a_parent_has_no_child(tmp_path):
    result = _profile_shop(
        tmp_path, _shop_contract(derive=True, check=True), orders=_orders(skip=(5,))
    )
    assert "structural" not in _relationship(result)


def _active(start: date, end: Optional[date], low: date, high: date) -> int:
    first = max(start, low)
    last = high if end is None or end < start else min(end, high)
    return max((last - first).days + 1, 0)


def _expected_exposure(entry, customers, orders):
    window = (int(entry["window"]["start"]), int(entry["window"]["end"]))
    low, high = (EPOCH + timedelta(days=each // MS) for each in window)
    return (
        len(orders),
        sum(
            _active(
                date.fromisoformat(row["signup"]),
                date.fromisoformat(row["left_on"]) if row["left_on"] and each_end else None,
                low,
                high,
            )
            for row in customers
            for each_end in [entry.get("end") == "left_on"]
        ),
    )


def _by_lifetime(entry: Dict[str, Any]) -> Dict[tuple, Dict[str, Any]]:
    return {(each["start"], each.get("end")): each for each in entry["exposure"]}


def _withheld_whole_exposure(exposure: Dict[str, Any]) -> bool:
    """Both counts withheld under ``D``, with the band every such exposure shares."""
    return all(
        exposure[key]["status"] == disclosure.SECONDARY
        and "value" not in exposure[key]
        and exposure[key]["sampleSize"] == {"min": 0}
        for key in ("children", "activeDays")
    )


def test_exposure_states_children_and_active_days_inside_the_published_span(tmp_path):
    customers, orders = _customers(), _orders()
    result = _profile_shop(tmp_path, _shop_contract(), orders=orders, customers=customers)
    exposure = _by_lifetime(_relationship(result))
    # The table counts one lifetime: a pair a rule orders comes before a column alone.
    lifetime = exposure.pop(("signup", "left_on"))
    assert ("signup", None) in exposure
    assert all(_withheld_whole_exposure(each) for each in exposure.values())
    # The window is the outer edges of the published span of the parent's start column.
    (span,) = [
        each
        for each in result.document["tables"]["customers"]["columns"]["signup"]["statistics"]
        if each["definition"] == "marginalHistogram/1"
    ]
    assert lifetime["window"] == {"start": span["bins"][0], "end": span["bins"][-1]}
    assert exposure[("signup", None)]["window"] == lifetime["window"]
    children, days = _expected_exposure(lifetime, customers, orders)
    assert lifetime["children"]["definition"] == "count/1"
    assert lifetime["children"]["value"] == disclosure.round_count(
        children, disclosure.ACRO_DEFAULT
    )
    assert lifetime["activeDays"]["value"] == days
    load_profile_text(result.canonical.decode("utf-8"))


def test_exposure_takes_its_window_from_a_declared_range_without_a_span(tmp_path):
    rules = [{"type": "range", "constraint": ">= '2022-12-01' and <= '2025-12-31'"}]
    result = _profile_shop(tmp_path, _shop_contract(signup_rules=rules))
    exposure = [each for each in _relationship(result)["exposure"] if each["start"] == "signup"]
    assert exposure
    for entry in exposure:
        low = (date(2022, 12, 1) - EPOCH).days * MS
        high = (date(2025, 12, 31) - EPOCH).days * MS
        assert entry["window"] == {"start": str(low), "end": str(high)}


def test_exposure_is_named_and_not_formed_with_no_window(tmp_path):
    # Two customers each sign up long before the rest: neither end month nor year holds ten
    # records, so the column has no published span, and the contract declares no range.
    customers = _customers()
    for number in range(3):
        customers[number]["signup"] = "2019-03-01"
    result = _profile_shop(tmp_path, _shop_contract(), customers=customers)
    entry = _relationship(result)
    assert "signup" not in {each["start"] for each in entry.get("exposure", ())}
    said = dict(result.not_measured)
    what = next(each for each in said if each.startswith("exposure customers.signup of"))
    assert said[what].startswith("an exposure states its window")


def test_a_request_forms_only_the_exposures_it_lists(tmp_path):
    asked = _request(
        RequestTable(
            "customers",
            False,
            (
                RequestColumn("customer_id", "number"),
                # The window is the span of the start column: the request asks for its histogram.
                RequestColumn("signup", "timestamp", (), ("marginalHistogram/1",)),
                RequestColumn("left_on", "timestamp"),
            ),
        ),
        RequestTable(
            "orders",
            False,
            (RequestColumn("customer_id", "number"), RequestColumn("placed", "timestamp")),
        ),
        relationships=[
            RequestRelationship(
                RequestEnd("customers", ("customer_id",)),
                RequestEnd("orders", ("customer_id",)),
                (),
                exposure=(RequestExposure("signup", "left_on"),),
            )
        ],
    )
    result = _profile_shop(tmp_path, _shop_contract(), request=asked)
    entry = _relationship(result)
    assert [(each["start"], each.get("end")) for each in entry["exposure"]] == [
        ("signup", "left_on")
    ]
    assert "dates" not in entry


def test_a_private_release_names_the_asks_it_does_not_form():
    from fluid_build.profiler import request as asked_for

    asked = _request(
        RequestTable(
            "loans",
            False,
            (RequestColumn("opened", "timestamp"), RequestColumn("closed", "timestamp")),
            date_pairs=(RequestDatePair("opened", "closed"),),
        )
    )
    assert asked_for.unformed(asked) == []
    assert asked_for.unformed(asked, private=True) == [
        ("date pair opened to closed of loans", asked_for.NO_DATE_PAIRS)
    ]


def test_the_report_counts_published_entries_and_names_no_record_count(tmp_path):
    from fluid_build.cli import profile_cmd

    result = _profile_shop(tmp_path, _shop_contract(derive=True, check=True))
    lines: List[str] = []
    profile_cmd._report_dates(lambda text, **_: lines.append(text), result)
    joined = "\n".join(lines)
    assert "exposes[customers]: Date pairs: " in joined and " of 1 published." in joined
    assert "Dates of exposes[customers] in exposes[orders]:" in joined
    assert "Structural classes of exposes[customers] in exposes[orders]: childless" in joined
    assert "Exposure of exposes[customers] in exposes[orders]:" in joined
    # No line holds a number but how many entries were published and how many there are.
    assert not any(str(CUSTOMERS) in line for line in lines)


def test_counting_the_cells_of_a_histogram_equals_counting_its_records():
    backend = load_backend()
    edges = (0, 1, 2, 8, 31)
    values = [0, 0, 1, 3, 3, 3, 9, 30, 30]
    by_record = backend.count_histogram(values, edges, 16)
    by_cell = backend.count_cells([2, 1, 3, 3], edges, 16)
    assert by_cell.document == by_record.document
    assert by_cell.cells == by_record.cells and by_cell.k == by_record.k
    with pytest.raises(ValueError):
        backend.count_cells([1, 2], edges, 16)


# -- a pair no cell of which may be published ----------------------------------------------


def _is_withheld_whole(pair: Dict[str, Any]) -> bool:
    """A pair with no statistic whose order group is withheld as a small count."""
    (group,) = pair["order"]["groups"]
    return pair["statistics"] == [] and group["status"] == "C"


def test_a_table_too_small_to_publish_a_pair_writes_it_withheld_whole(tmp_path):
    result = _profile_loans(tmp_path, _loans_contract(), _loans()[:5])
    (pair,) = _pairs(result).values()
    assert _is_withheld_whole(pair)
    load_profile_text(result.canonical.decode("utf-8"))


def test_a_pair_no_record_holds_both_dates_of_is_written_like_a_small_one(tmp_path):
    # The list of pairs is public: leaving this one out would publish that no record holds both.
    rows = _loans()
    for row in rows:
        row["closed"] = None
    result = _profile_loans(tmp_path, _loans_contract(), rows)
    (pair,) = _pairs(result).values()
    assert _is_withheld_whole(pair)
    # Told apart from a small pair by nothing: the same order entry, the same note.
    small = _profile_loans(tmp_path, _loans_contract(), _loans()[:5])
    assert pair == next(iter(_pairs(small).values()))
    days = [each for each in result.not_measured if each[0].startswith("days of date pair")]
    assert days == [each for each in small.not_measured if each[0].startswith("days of date pair")]
    assert not any("no record holds both dates" in why for _, why in result.not_measured)
    load_profile_text(result.canonical.decode("utf-8"))


def test_a_relationship_with_too_few_joined_rows_writes_its_dates_withheld_whole(tmp_path):
    result = _profile_shop(tmp_path, _shop_contract(), orders=_orders()[:6])
    entry = _relationship(result)
    assert entry["dates"]
    assert all(_is_withheld_whole(each) for each in entry["dates"])
    load_profile_text(result.canonical.decode("utf-8"))


def test_a_relationship_no_joined_row_holds_both_dates_of_is_written_withheld(tmp_path):
    customers = _customers()
    for row in customers:
        row["left_on"] = None
    result = _profile_shop(tmp_path, _shop_contract(), customers=customers)
    entry = _relationship(result)
    assert {(each["parent"], each["child"]) for each in entry["dates"]} == {
        ("signup", "placed"),
        ("left_on", "placed"),
    }
    held = {each["parent"]: each for each in entry["dates"]}
    assert _is_withheld_whole(held["left_on"])
    assert not _is_withheld_whole(held["signup"])
    assert not any("no record holds both dates" in why for _, why in result.not_measured)
    load_profile_text(result.canonical.decode("utf-8"))


# -- one lifetime's exposure per parent table ----------------------------------------------


def _closed(count: int) -> List[Dict[str, Any]]:
    """The customers, of whom only *count* have a left_on, spread over many days."""
    rows = _customers()
    for number, row in enumerate(rows):
        closed = number < count
        row["left_on"] = (_signup(number) + timedelta(days=30 + number * 11 % 400)).isoformat()
        row["left_on"] = row["left_on"] if closed else None
    return rows


def test_a_parent_table_counts_the_exposure_of_one_lifetime(tmp_path):
    # (signup) and (signup, left_on) differ by the days of the one closed customer only: the
    # difference of the two sums would be one record's date, so one of them is counted.
    result = _profile_shop(tmp_path, _shop_contract(), customers=_closed(1))
    exposure = _by_lifetime(_relationship(result))
    counted = exposure.pop(("signup", "left_on"))
    assert counted["children"]["status"] == counted["activeDays"]["status"] == "F"
    assert ("signup", None) in exposure
    assert all(_withheld_whole_exposure(each) for each in exposure.values())
    load_profile_text(result.canonical.decode("utf-8"))


def test_every_relationship_into_a_table_counts_the_same_lifetime(tmp_path):
    contract = _shop_contract()
    orders = contract["exposes"][1]
    orders["semantics"]["relationships"].append(
        {**orders["semantics"]["relationships"][0], "name": "buyer"}
    )
    result = _profile_shop(tmp_path, contract, customers=_closed(1))
    counted = []
    for entry in result.document["relationships"]:
        exposure = _by_lifetime(entry)
        counted.append(exposure.pop(("signup", "left_on")))
        assert all(_withheld_whole_exposure(each) for each in exposure.values())
    assert len(counted) == 2
    # The same parents on the same window: the second relationship repeats the first's days.
    assert counted[0]["activeDays"]["status"] == "F"
    assert counted[0]["activeDays"] == counted[1]["activeDays"]


def test_a_request_counts_the_lifetime_that_comes_first_in_the_contract_order(tmp_path):
    def ask(*exposure: RequestExposure) -> Request:
        return _request(
            RequestTable(
                "customers",
                False,
                (
                    RequestColumn("customer_id", "number"),
                    RequestColumn("signup", "timestamp", (), ("marginalHistogram/1",)),
                    RequestColumn("left_on", "timestamp", (), ("marginalHistogram/1",)),
                ),
            ),
            RequestTable("orders", False, (RequestColumn("customer_id", "number"),)),
            relationships=[
                RequestRelationship(
                    RequestEnd("customers", ("customer_id",)),
                    RequestEnd("orders", ("customer_id",)),
                    (),
                    exposure=exposure,
                )
            ],
        )

    # Listed second, the pair with an end still comes before a column alone.
    result = _profile_shop(
        tmp_path,
        _shop_contract(),
        request=ask(RequestExposure("signup", None), RequestExposure("signup", "left_on")),
    )
    exposure = _by_lifetime(_relationship(result))
    assert list(exposure) == [("signup", None), ("signup", "left_on")]
    assert exposure[("signup", "left_on")]["activeDays"]["status"] == "F"
    assert _withheld_whole_exposure(exposure[("signup", None)])
    # Two columns alone: the schema's order, not the request's.
    result = _profile_shop(
        tmp_path,
        _shop_contract(),
        request=ask(RequestExposure("left_on", None), RequestExposure("signup", None)),
    )
    exposure = _by_lifetime(_relationship(result))
    assert exposure[("signup", None)]["activeDays"]["status"] == "F"
    assert _withheld_whole_exposure(exposure[("left_on", None)])


# -- four exposures of three dates ---------------------------------------------------------

#: The one bed that never sprouts, and the day it is harvested.
UNSPROUTED, HARVESTED = 17, date(2023, 7, 19)


def _garden_contract() -> Dict[str, Any]:
    beds: Dict[str, Any] = {
        "exposeId": "beds",
        "contract": {
            "schema": [
                {"name": "bed_id", "type": "int"},
                {"name": "planted", "type": "date"},
                {"name": "sprouted", "type": "date"},
                {"name": "harvested", "type": "date"},
            ],
            "invariants": [
                {"id": "sprouts", "check": "sprouted IS NULL OR sprouted >= planted"},
                {"id": "grows", "check": "harvested IS NULL OR harvested >= planted"},
                {
                    "id": "ripens",
                    "check": "harvested IS NULL OR sprouted IS NULL OR harvested >= sprouted",
                },
            ],
        },
    }
    waterings = {
        "exposeId": "waterings",
        "contract": {
            "schema": [{"name": "watering_id", "type": "int"}, {"name": "bed_id", "type": "int"}]
        },
        "semantics": {
            "relationships": [
                {
                    "name": "bed",
                    "to": "beds",
                    "fromColumns": ["bed_id"],
                    "toColumns": ["bed_id"],
                    "cardinality": "many_to_one",
                }
            ]
        },
    }
    return {"fluidVersion": "0.7.7", "kind": "DataProduct", "id": "g", "exposes": [beds, waterings]}


def _beds() -> List[Dict[str, Any]]:
    rows = []
    for number in range(300):
        planted = date(2023, 1, 1) + timedelta(days=(number * 7) % 340)
        sprouted = planted + timedelta(days=number % 11)
        harvested = sprouted + timedelta(days=20 + (number * 13) % 150)
        rows.append(
            {
                "bed_id": number,
                "planted": planted.isoformat(),
                "sprouted": sprouted.isoformat(),
                "harvested": harvested.isoformat() if number % 2 == 0 else None,
            }
        )
    rows[UNSPROUTED].update(planted="2023-06-01", sprouted=None, harvested=HARVESTED.isoformat())
    return rows


def _profile_garden(tmp_path, contract: Dict[str, Any]):
    waterings = [{"watering_id": number, "bed_id": number % 300} for number in range(900)]
    return profile_exposes(
        contract,
        [
            ExposeSource("beds", _write(tmp_path / "b.ndjson", _beds())),
            ExposeSource("waterings", _write(tmp_path / "w.ndjson", waterings)),
        ],
        backend=load_backend(),
    )


def _counts(exposure: Dict[tuple, Dict[str, Any]], key: str) -> Dict[tuple, Optional[int]]:
    """Each lifetime's published count *key*, ``None`` where it is withheld."""
    return {
        lifetime: int(each[key]["value"]) if each[key]["status"] == "F" else None
        for lifetime, each in exposure.items()
    }


def test_four_exposures_of_three_dates_do_not_give_one_parents_date_back(tmp_path):
    # Published together, (planted) - (planted, harvested) - (sprouted) + (sprouted, harvested)
    # is the days from the one unsprouted bed's harvest to the window's end, and
    # (planted) - (planted, sprouted) - (sprouted) the exact number of beds that sprouted.
    result = _profile_garden(tmp_path, _garden_contract())
    exposure = _by_lifetime(_relationship(result))
    combinations = (
        [
            ("planted", None),
            ("planted", "harvested"),
            ("sprouted", None),
            ("sprouted", "harvested"),
        ],
        [("planted", None), ("planted", "sprouted"), ("sprouted", None)],
    )
    assert {lifetime for each in combinations for lifetime in each} <= set(exposure)
    days, children = _counts(exposure, "activeDays"), _counts(exposure, "children")
    # The first pair a rule orders is the one lifetime counted; every other is withheld whole.
    assert [each for each, value in days.items() if value is not None] == [("planted", "sprouted")]
    assert [each for each, value in children.items() if value is not None] == [
        ("planted", "sprouted")
    ]
    for lifetime, each in exposure.items():
        assert lifetime == ("planted", "sprouted") or _withheld_whole_exposure(each)
    # So neither combination can be formed, and no two children counts can be set side by side.
    for combination in combinations:
        assert any(days[lifetime] is None for lifetime in combination)
    load_profile_text(result.canonical.decode("utf-8"))
