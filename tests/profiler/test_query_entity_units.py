# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A consumer query whose unit counts the entities a table's rows belong to.

A unit ``SELECT customer_id, count(*) AS n FROM orders GROUP BY customer_id`` has one record per
customer that has an order, and a query ``... WHERE n >= 20`` keeps the customers with twenty
orders or more. The unit's records are checked as the population they are: the parents of a
counted relationship, with each parent's bin of the children-per-parent histogram, or the
distinct values of the entity column otherwise. Every other unit that aggregates is refused before
any read. The tables are customers and their orders (``_shop``'s records, grouped as it groups).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from fluid_build.profiler import _stats, consumer_queries, query_rates
from fluid_build.profiler import request as asked_for
from fluid_build.profiler.runner import ExposeSource, ProfileError, profile_exposes
from tests.profiler._fake_backend import FakeBackend

pytestmark = pytest.mark.unit

pytest.importorskip("flux_stats.request")
pytest.importorskip("sqlglot")
pytest.importorskip("duckdb")

ORDERS_BY_CUSTOMER = {
    "semantics": {
        "relationships": [
            {
                "name": "placed_by",
                "to": "customers",
                "fromColumns": ["customer_id"],
                "toColumns": ["customer_id"],
                "cardinality": "many_to_one",
            }
        ]
    }
}


def _contract(
    *, dependency: bool = False, validity: bool = False, invariant: bool = False
) -> Dict[str, Any]:
    orders: Dict[str, Any] = {
        "exposeId": "orders",
        "contract": {
            "schema": [
                {"name": "order_id", "type": "int"},
                {"name": "customer_id", "type": "int"},
                {"name": "status", "type": "enum('open','paid','void')"},
                {"name": "quantity", "type": "int"},
                {"name": "amount", "type": "decimal(10,2)"},
                {"name": "note", "type": "string"},
            ]
        },
        **ORDERS_BY_CUSTOMER,
    }
    if dependency:
        orders["contract"]["invariants"] = [
            {"id": "customer_fixes_status", "dependency": {"from": ["customer_id"], "to": ["note"]}}
        ]
    if invariant:
        orders["contract"].setdefault("invariants", []).append(
            {"id": "large", "check": "quantity > 50"}
        )
    customers: Dict[str, Any] = {
        "exposeId": "customers",
        "contract": {
            "schema": [
                {"name": "customer_id", "type": "int"},
                {"name": "region", "type": "enum('north','south','west')"},
            ]
        },
    }
    return {"exposes": [customers, orders]}


CONTRACT = _contract()

#: The unit of the headline query, and the query over it.
UNIT = "SELECT customer_id, count(*) AS n FROM orders GROUP BY customer_id"
ENTITY_ONLY = query_rates.ENTITY_ONLY


def _over(unit: str, where: str, column: str = "customer_id") -> str:
    return f'WITH "v" AS ({unit}) SELECT "v"."{column}" AS "{column}" FROM "v" AS "v" WHERE {where}'


MANY = _over(UNIT, '"v"."n" >= 20')

CUSTOMERS = 1400
#: A customer has 0, 1, 2, 3, 6, 12 or 25 orders by ``(customer_id // 3) % 7``: 200 customers in
#: each bin of children per parent.
COUNTS = (0, 1, 2, 3, 6, 12, 25)


def _customers() -> List[Dict[str, Any]]:
    return [
        {"customer_id": n, "region": ("north", "south", "west")[n % 3]} for n in range(CUSTOMERS)
    ]


def _orders(
    counts: Optional[Dict[int, int]] = None, status=lambda n: ("open", "paid", "void")[n % 3]
) -> List[Dict[str, Any]]:
    owners = [
        customer
        for customer in range(CUSTOMERS)
        for _ in range((counts or {}).get(customer, COUNTS[(customer // 3) % 7]))
    ]
    return [
        {
            "order_id": n,
            "customer_id": owner,
            "status": status(n),
            "quantity": n % 100,
            "amount": f"{n % 20}.50",
            "note": "n",
        }
        for n, owner in enumerate(owners)
    ]


def _write(directory: Path, name: str, rows: Sequence[Dict[str, Any]]) -> str:
    path = directory / f"{name}.ndjson"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return str(path)


def _column(name: str, domain: str) -> _stats.RequestColumn:
    return _stats.RequestColumn(name, domain, ())


def _request(*queries, counted: bool = True, facts=()) -> _stats.Request:
    link = _stats.RequestRelationship(
        _stats.RequestEnd("customers", ("customer_id",)),
        _stats.RequestEnd("orders", ("customer_id",)),
        ("marginalHistogram/1",),
    )
    return _stats.Request(
        None,
        16,
        (
            _stats.RequestTable(
                "customers",
                False,
                (_column("customer_id", "number"), _column("region", "category")),
            ),
            _stats.RequestTable("orders", False, (_column("customer_id", "number"),)),
        ),
        (link,) if counted else (),
        tuple(facts),
        tuple(queries),
    )


def _on_unit(name: str, sql: str, unit: str = UNIT) -> _stats.RequestQuery:
    return _stats.RequestQuery(name, sql, unit_sql=unit)


def _profile(
    tmp_path: Path,
    *queries,
    contract=None,
    customers=None,
    orders=None,
    counted: bool = True,
    strict: bool = False,
    gone: bool = False,
    facts=(),
):
    if gone:
        sources = [ExposeSource("customers", str(tmp_path / "a")), ExposeSource("orders", "b")]
    else:
        sources = [
            ExposeSource("customers", _write(tmp_path, "customers", customers or _customers())),
            ExposeSource("orders", _write(tmp_path, "orders", orders or _orders())),
        ]
    return profile_exposes(
        contract or CONTRACT,
        sources,
        backend=FakeBackend(),
        request=_request(*queries, counted=counted, facts=facts),
        strict=strict,
    )


def _entry(result, name: str) -> Dict[str, Any]:
    (found,) = [each for each in result.document["queries"] if each["name"] == name]
    (statistic,) = found["statistics"]
    return statistic


def _said(result) -> Dict[str, str]:
    return dict(result.not_measured)


def _withheld(result, name: str) -> bool:
    return all(group["status"] != "F" for group in _entry(result, name)["groups"])


# ---- answered ---------------------------------------------------------------------------------


def test_a_query_over_the_entities_of_a_table_is_answered_with_exact_counts(tmp_path):
    result = _profile(tmp_path, _on_unit("many", MANY))
    (group,) = _entry(result, "many")["groups"]
    # 1,200 customers have an order, 200 of them twenty or more; the 200 with none are the rest
    assert group == {"group": "", "status": "F", "k": 1200, "levelCounts": [1000, 200]}
    assert not _said(result)
    (outcome,) = result.queries
    assert (outcome.name, outcome.published) == ("many", True)


def test_the_engines_reader_accepts_the_document(tmp_path):
    from flux_stats.profile import load_profile_text

    result = _profile(tmp_path, _on_unit("many", MANY))
    profile, _ = load_profile_text(result.canonical.decode("utf-8"))
    (entry,) = profile.queries
    counts = entry.statistics[0].groups[0].statistic.level_counts
    assert (sum(counts), counts[1]) == (1200, 200)


def test_an_entity_unit_with_a_filter_and_a_having_is_answered(tmp_path):
    unit = (
        "SELECT customer_id, count(*) AS n, sum(quantity) AS q FROM orders "
        "WHERE status = 'open' GROUP BY customer_id HAVING count(*) >= 1"
    )
    result = _profile(
        tmp_path, _on_unit("open", _over(unit, '"v"."q" >= 50 AND "v"."n" >= 2'), unit)
    )
    assert not _said(result)
    (group,) = _entry(result, "open")["groups"]
    assert group["status"] == "F"


def test_a_relationship_the_profile_does_not_count_makes_the_entities_the_distinct_keys(tmp_path):
    unit = (
        "SELECT customer_id, count(*) AS n FROM orders WHERE status = 'open' GROUP BY customer_id"
    )
    result = _profile(tmp_path, _on_unit("busy", _over(unit, '"v"."n" >= 2'), unit), counted=False)
    assert not _said(result)
    (group,) = _entry(result, "busy")["groups"]
    rows = _orders()
    opened: Dict[int, int] = {}
    for row in rows:
        if row["status"] == "open":
            opened[row["customer_id"]] = opened.get(row["customer_id"], 0) + 1
    # no parent table is read: the records are the customer ids the orders hold
    entities = len({row["customer_id"] for row in rows})
    assert group["status"] == "F"
    assert group["k"] == round(len(opened) / 5) * 5
    assert group["levelCounts"][1] == round(sum(n >= 2 for n in opened.values()) / 5) * 5
    assert entities > len(opened)


# ---- withheld when a cell would give a small or a withheld count back -------------------------


def test_a_unit_whose_missing_entities_are_a_small_count_is_withheld(tmp_path):
    counts = {n: max(COUNTS[(n // 3) % 7], 1) for n in range(CUSTOMERS)}
    for customer in (0, 1, 2):
        counts[customer] = 0
    result = _profile(tmp_path, _on_unit("many", MANY), orders=_orders(counts))
    assert _withheld(result, "many")


def test_a_query_that_would_give_a_withheld_bin_of_children_per_parent_back_is_withheld(tmp_path):
    # the bin of 10 to 19 children holds four customers, so the histogram withholds it and a cell
    # beside it: the query's own counts are large, the bin it equals is not published
    counts = {
        n: (4 if COUNTS[(n // 3) % 7] == 12 and n % 50 else COUNTS[(n // 3) % 7])
        for n in range(CUSTOMERS)
    }
    result = _profile(tmp_path, _on_unit("many", MANY), orders=_orders(counts))
    histogram = result.document["relationships"][0]["statistics"][0]["groups"][0]
    assert any(count is None for count in histogram["binCounts"])
    assert _withheld(result, "many")


def test_two_units_that_differ_by_one_entity_are_both_withheld(tmp_path):
    other = f"{UNIT.replace('GROUP BY', 'WHERE customer_id <> 5 GROUP BY')}"
    result = _profile(
        tmp_path,
        _on_unit("all", MANY),
        _on_unit("most", _over(other, '"v"."n" >= 20'), other),
    )
    assert _withheld(result, "all")
    assert _withheld(result, "most")


# ---- refused before any read ------------------------------------------------------------------

REFUSED = [
    ("non-key column", "SELECT status, count(*) AS n FROM orders GROUP BY status", "status", "n"),
    (
        "join",
        "SELECT a.customer_id AS customer_id, count(*) AS n FROM orders AS a "
        "JOIN orders AS b ON b.order_id = a.order_id GROUP BY a.customer_id",
        "customer_id",
        "n",
    ),
    (
        "nested aggregate",
        "SELECT customer_id, sum(count(*)) AS n FROM orders GROUP BY customer_id",
        "customer_id",
        "n",
    ),
    (
        "window",
        "SELECT customer_id, count(*) OVER (PARTITION BY customer_id) AS n FROM orders "
        "GROUP BY customer_id",
        "customer_id",
        "n",
    ),
    (
        "distinct",
        "SELECT customer_id, count(DISTINCT status) AS n FROM orders GROUP BY customer_id",
        "customer_id",
        "n",
    ),
    (
        "order by",
        "SELECT customer_id, count(*) AS n FROM orders GROUP BY customer_id ORDER BY customer_id",
        "customer_id",
        "n",
    ),
    (
        "limit",
        "SELECT customer_id, count(*) AS n FROM orders GROUP BY customer_id "
        "ORDER BY customer_id, n LIMIT 5",
        "customer_id",
        "n",
    ),
    (
        "subquery",
        "SELECT customer_id, count(*) AS n FROM orders WHERE customer_id IN "
        "(SELECT customer_id FROM orders) GROUP BY customer_id",
        "customer_id",
        "n",
    ),
    (
        "two keys",
        "SELECT customer_id, status, count(*) AS n FROM orders GROUP BY customer_id, status",
        "customer_id",
        "n",
    ),
    (
        "no grouping",
        "SELECT count(*) AS n FROM orders",
        "n",
        "n",
    ),
    (
        "not an entity",
        "SELECT quantity, count(*) AS n FROM orders GROUP BY quantity",
        "quantity",
        "n",
    ),
]


@pytest.mark.parametrize(
    ("label", "unit", "first", "second"), REFUSED, ids=[each[0] for each in REFUSED]
)
def test_a_unit_that_aggregates_other_than_by_an_entity_is_refused_before_any_read(
    tmp_path, label, unit, first, second
):
    each = _on_unit("odd", _over(unit, f'"v"."{second}" IS NOT NULL', first), unit)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, each, strict=True, gone=True)
    assert refused.value.event == "profile_request_not_formed"
    text = str(refused.value.context)
    assert "query odd" in text
    assert unit not in text
    result = _profile(tmp_path, each)
    assert _said(result)["query odd"]
    assert "queries" not in result.document


def test_the_refusal_of_an_aggregate_says_what_is_answered(tmp_path):
    unit = REFUSED[0][1]
    result = _profile(tmp_path, _on_unit("odd", _over(unit, '"v"."n" > 1', "status"), unit))
    assert _said(result)["query odd"] == ENTITY_ONLY
    assert "groups one table's rows by an entity they belong to" in ENTITY_ONLY
    assert not asked_for.decided_by_records(ENTITY_ONLY)


def test_a_having_that_can_fail_on_a_value_is_refused_before_any_read(tmp_path):
    unit = f"{UNIT} HAVING min(status) > 5"
    each = _on_unit("odd", _over(unit, '"v"."n" > 1'), unit)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, each, strict=True, gone=True)
    assert query_rates.CAN_FAIL in str(refused.value.context)


def test_a_where_of_the_unit_that_can_fail_is_refused_before_any_read(tmp_path):
    unit = UNIT.replace("GROUP BY", "WHERE quantity > 'x' GROUP BY")
    each = _on_unit("odd", _over(unit, '"v"."n" > 1'), unit)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, each, strict=True, gone=True)
    assert query_rates.CAN_FAIL in str(refused.value.context)


def test_a_computed_condition_of_the_unit_is_refused_before_any_read(tmp_path):
    unit = UNIT.replace("GROUP BY", "WHERE quantity + 1 > 3 GROUP BY")
    each = _on_unit("odd", _over(unit, '"v"."n" > 1'), unit)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, each, strict=True, gone=True)
    assert ENTITY_ONLY in str(refused.value.context)


def test_a_query_over_an_aggregate_the_fence_cannot_type_is_refused_before_any_read(tmp_path):
    unit = "SELECT customer_id, max(note) AS top FROM orders GROUP BY customer_id"
    each = _on_unit("odd", _over(unit, '"v"."top" > 5'), unit)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, each, strict=True, gone=True)
    assert query_rates.CAN_FAIL in str(refused.value.context)


def test_a_table_that_declares_a_history_is_not_grouped_by_its_natural_entity(tmp_path):
    contract = _contract()
    contract["exposes"][1]["semantics"] = {
        "entities": [{"name": "order", "type": "natural", "expr": "order_id"}],
        "dimensions": [
            {
                "name": "valid_from",
                "type": "time",
                "expr": "quantity",
                "typeParams": {"timeGranularity": "day", "validityParams": {"isStart": True}},
            },
            {
                "name": "valid_to",
                "type": "time",
                "expr": "amount",
                "typeParams": {"timeGranularity": "day", "validityParams": {"isEnd": True}},
            },
        ],
        **ORDERS_BY_CUSTOMER["semantics"],
    }
    unit = "SELECT order_id, count(*) AS n FROM orders GROUP BY order_id"
    each = _on_unit("odd", _over(unit, '"v"."n" > 1', "order_id"), unit)
    planned = query_rates.plan_queries(contract, _request(each), FakeBackend().enum_levels)
    (plan,) = planned.plans
    assert plan.reason == query_rates.HISTORY_ENTITY


def test_an_entity_that_also_keys_per_entity_statistics_is_refused_before_any_read(tmp_path):
    each = _on_unit("many", MANY)
    planned = query_rates.plan_queries(
        _contract(dependency=True), _request(each), FakeBackend().enum_levels
    )
    (plan,) = planned.plans
    assert plan.reason == query_rates.PER_ENTITY_KEY


def test_a_private_release_names_the_query_as_not_released():
    request = _request(_on_unit("many", MANY))
    assert asked_for.unformed(request, private=True) == [
        ("query many", asked_for.NO_QUERIES_PRIVATE)
    ]


# ---- the run-wide failure rule covers these statements ----------------------------------------


@pytest.mark.parametrize("status", ["error", "timedOut", "outOfMemory", "crashed"])
def test_a_statement_that_fails_on_the_records_withholds_every_query_of_the_run(
    tmp_path, monkeypatch, status
):
    from dataclasses import replace

    real = consumer_queries.run_in_child

    def wrapped(tables, statements, **kwargs):
        run = real(tables, statements, **kwargs)
        results = tuple(
            (
                replace(each, status=status, reason="x", count=None, groups=None)
                if each.id.startswith("e")
                else each
            )
            for each in run.results
        )
        return replace(run, results=results)

    monkeypatch.setattr(consumer_queries, "run_in_child", wrapped)
    result = _profile(tmp_path, _on_unit("many", MANY))
    assert _said(result) == {"query many": query_rates.RUN_FAILED}
    assert "queries" not in result.document


@pytest.mark.parametrize(("counted", "delta"), [(True, -1), (True, 1), (False, 1), (False, -1)])
def test_rows_that_are_not_the_units_records_withhold_the_run(
    tmp_path, monkeypatch, counted, delta
):
    from dataclasses import replace

    real = consumer_queries.run_in_child

    def wrapped(tables, statements, **kwargs):
        run = real(tables, statements, **kwargs)
        results = tuple(
            replace(each, count=(each.count or 0) + delta) if each.id.startswith("k") else each
            for each in run.results
        )
        return replace(run, results=results)

    monkeypatch.setattr(consumer_queries, "run_in_child", wrapped)
    result = _profile(tmp_path, _on_unit("many", MANY), counted=counted)
    assert _said(result) == {"query many": query_rates.RUN_FAILED}


def test_nothing_printed_names_a_count_of_records(tmp_path):
    result = _profile(tmp_path, _on_unit("many", MANY))
    lines = "\n".join(query_rates.report_lines(result.queries, 1))
    assert lines == "Queries: 1 of 1 published whole."


def _with_unmatched(orphans: int = 60, nulls: int = 3) -> List[Dict[str, Any]]:
    """The orders, and some whose customer key is null or names no customer."""
    orders = _orders()
    for at in range(orphans):
        orders.append({**orders[0], "order_id": len(orders), "customer_id": CUSTOMERS + 7 + at})
    for _ in range(nulls):
        orders.append({**orders[0], "order_id": len(orders), "customer_id": None})
    return orders


def _rounded(count: int) -> int:
    return round(count / 5) * 5


def test_a_group_that_names_no_parent_is_a_record_of_the_unit(tmp_path):
    # sixty orphan keys and the null key each form a group of the unit: 61 records beside the
    # 1,200 customers that have an order, none of them with twenty orders
    result = _profile(tmp_path, _on_unit("many", MANY), orders=_with_unmatched())
    assert not _said(result)
    (group,) = _entry(result, "many")["groups"]
    assert group["status"] == "F"
    assert group["k"] == _rounded(1200 + 61)
    assert group["levelCounts"] == [_rounded(1200 + 61 - 200), 200]


def test_a_group_that_names_no_parent_is_a_record_when_a_bin_is_withheld(tmp_path):
    counts = {
        n: (4 if COUNTS[(n // 3) % 7] == 12 and n % 50 else COUNTS[(n // 3) % 7])
        for n in range(CUSTOMERS)
    }
    orders = _orders(counts)
    for at in range(60):
        orders.append({**orders[0], "order_id": len(orders), "customer_id": CUSTOMERS + 7 + at})
    result = _profile(tmp_path, _on_unit("many", MANY), orders=orders)
    # the withheld bin still withholds the query; nothing fails the run
    assert query_rates.RUN_FAILED not in _said(result).values()


@pytest.mark.parametrize("key", [None, 999_999])
def test_one_record_with_a_null_or_orphan_key_does_not_decide_whether_the_run_answers(
    tmp_path, key
):
    target = 5
    unit = (
        f"SELECT customer_id, count(*) AS n FROM orders WHERE order_id = {target} "
        "AND quantity >= 50 GROUP BY customer_id"
    )
    query = _over(unit, '"v"."n" >= 1')
    other = _stats.RequestQuery(
        "other", "SELECT customer_id FROM customers WHERE region = 'north'", table="customers"
    )
    seen = []
    for secret in (10, 70):
        orders = _orders()
        orders[target]["customer_id"] = key
        orders[target]["quantity"] = secret
        result = _profile(tmp_path, other, _on_unit("oracle", query, unit), orders=orders)
        assert query_rates.RUN_FAILED not in _said(result).values()
        seen.append(sorted(each["name"] for each in result.document.get("queries", [])))
    assert seen[0] == seen[1]


def test_two_parents_that_hold_one_key_do_not_fail_the_run(tmp_path):
    customers = _customers()
    customers.append({"customer_id": 10, "region": "north"})
    result = _profile(tmp_path, _on_unit("many", MANY), customers=customers)
    assert query_rates.RUN_FAILED not in _said(result).values()


# ---- the combinations of a population are bounded before any read -----------------------------


def _many_queries(count: int, unit: str = UNIT) -> List[_stats.RequestQuery]:
    return [_on_unit(f"q{at}", _over(unit, f'"v"."n" >= {at + 1}'), unit) for at in range(count)]


def test_a_population_past_the_bound_is_cut_at_plan_time_in_request_order():
    from fluid_build.profiler.consumer_queries import MAX_GROUPS

    queries = _many_queries(40)
    planned = query_rates.plan_queries(CONTRACT, _request(*queries), FakeBackend().enum_levels)
    answered = [each.name for each in planned.answerable()]
    cut = [each for each in planned.plans if each.reason]
    assert answered == [each.name for each in queries[: len(answered)]]
    assert cut and [each.name for each in cut] == [each.name for each in queries[len(answered) :]]
    assert {each.reason for each in cut} == {query_rates.ENTITIES_PAST_BOUND}
    assert not asked_for.decided_by_records(query_rates.ENTITIES_PAST_BOUND)
    # the states of the bin factor times two to the units and queries stay inside the child's cap
    assert 8 * 2 ** (1 + len(answered)) <= MAX_GROUPS < 8 * 2 ** (2 + len(answered))


def test_a_request_past_the_bound_is_refused_by_strict_before_any_read(tmp_path):
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, *_many_queries(40), strict=True, gone=True)
    assert query_rates.ENTITIES_PAST_BOUND in str(refused.value.context)


def test_a_bound_population_does_not_change_with_one_record(tmp_path):
    seen = []
    for note in ("n", "y"):
        orders = _orders()
        orders[0]["note"] = note
        orders[0]["customer_id"] = None
        result = _profile(tmp_path, *_many_queries(40), orders=orders)
        seen.append(sorted(_said(result).items()))
        assert query_rates.RUN_FAILED not in _said(result).values()
    assert seen[0] == seen[1]
    assert len(seen[0]) > 0


# ---- wide decimals load exactly and their sums stay inside the fence --------------------------


def _wide_contract(precision: int = 38, scale: int = 0) -> Dict[str, Any]:
    import copy

    contract = copy.deepcopy(_contract())
    (orders,) = [each for each in contract["exposes"] if each["exposeId"] == "orders"]
    orders["contract"]["schema"].append({"name": "big", "type": f"decimal({precision},{scale})"})
    return contract


@pytest.mark.parametrize("digits", [28, 29, 37])
def test_a_decimal_with_many_digits_loads_exactly(digits):
    from decimal import Decimal

    text = "9" * digits
    loaded = query_rates._loaded("DECIMAL(38,0)", [text, "-" + text, None])
    assert loaded == [Decimal(text), Decimal("-" + text), None]
    fraction = "9" * (digits - 2) + ".25"
    assert query_rates._loaded("DECIMAL(38,2)", [fraction]) == [Decimal(fraction)]


def test_a_decimal_past_its_precision_or_scale_still_does_not_load():
    assert query_rates._loaded("DECIMAL(38,0)", ["1" + "0" * 38]) is None
    assert query_rates._loaded("DECIMAL(38,2)", ["1.255"]) is None


@pytest.mark.parametrize("digits", [28, 29, 37])
def test_a_run_over_a_wide_decimal_column_is_not_failed_by_loading_it(tmp_path, digits):
    unit = "SELECT customer_id, count(*) AS n, max(big) AS m FROM orders GROUP BY customer_id"
    rows = _orders()
    for row in rows:
        row["big"] = "9" * digits
    result = _profile(
        tmp_path,
        _on_unit("wide", _over(unit, '"v"."n" >= 20'), unit),
        contract=_wide_contract(),
        orders=rows,
    )
    assert query_rates.RUN_FAILED not in _said(result).values()
    assert not _said(result)


@pytest.mark.parametrize(
    ("kind", "aggregate"),
    [
        ("decimal(38,0)", "sum(big)"),
        ("decimal(19,2)", "sum(big)"),
        ("decimal(38,0)", "avg(big)"),
        ("decimal(30,4)", "avg(big)"),
    ],
)
def test_a_sum_or_average_that_could_overflow_is_refused_before_any_read(tmp_path, kind, aggregate):
    precision, scale = (int(each) for each in kind[8:-1].split(","))
    unit = f"SELECT customer_id, {aggregate} AS total FROM orders GROUP BY customer_id"
    each = _on_unit("wide", _over(unit, '"v"."total" > 0'), unit)
    planned = query_rates.plan_queries(
        _wide_contract(precision, scale), _request(each), FakeBackend().enum_levels
    )
    assert [plan.reason for plan in planned.plans] == [query_rates.SUM_CAN_OVERFLOW]
    assert not asked_for.decided_by_records(query_rates.SUM_CAN_OVERFLOW)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, each, contract=_wide_contract(precision, scale), strict=True, gone=True)
    assert query_rates.SUM_CAN_OVERFLOW in str(refused.value.context)


def test_a_sum_of_a_having_that_could_overflow_is_refused_too():
    unit = (
        "SELECT customer_id, count(*) AS n FROM orders GROUP BY customer_id " "HAVING sum(big) > 0"
    )
    each = _on_unit("wide", _over(unit, '"v"."n" > 0'), unit)
    planned = query_rates.plan_queries(_wide_contract(), _request(each), FakeBackend().enum_levels)
    assert [plan.reason for plan in planned.plans] == [query_rates.SUM_CAN_OVERFLOW]


@pytest.mark.parametrize(
    ("kind", "aggregate"),
    [("decimal(18,2)", "sum(big)"), ("decimal(18,2)", "avg(big)"), ("int", "sum(quantity)")],
)
def test_a_sum_that_cannot_overflow_is_still_answered(kind, aggregate):
    precision, scale = (int(each) for each in kind[8:-1].split(",")) if "(" in kind else (18, 2)
    unit = f"SELECT customer_id, {aggregate} AS total FROM orders GROUP BY customer_id"
    each = _on_unit("wide", _over(unit, '"v"."total" > 0'), unit)
    planned = query_rates.plan_queries(
        _wide_contract(precision, scale), _request(each), FakeBackend().enum_levels
    )
    assert [plan.reason for plan in planned.plans] == [None]


# ---- one table, counted one way ---------------------------------------------------------------

#: Customers 0 to 999 hold one order each, so their orders and their entities are the same records.
SINGLES = {**{n: 1 for n in range(1000)}, **{1000 + n: COUNTS[n % 7] for n in range(400)}}
ROWS_OVER = _stats.RequestQuery(
    "rows_over",
    "SELECT order_id FROM orders WHERE quantity > 2 AND customer_id < 998",
    table="orders",
)
MOST = "SELECT customer_id, max(quantity) AS m FROM orders GROUP BY customer_id"
ENTITIES_OVER = _on_unit(
    "entities_over",
    _over(MOST, '"v"."m" > 2 AND "v"."customer_id" < 998 AND "v"."customer_id" <> 5'),
    MOST,
)


@pytest.mark.parametrize("quantity", [5, 1])
def test_a_table_counted_as_rows_is_not_counted_as_entities_too(tmp_path, quantity):
    # Unchecked together, the two counts would differ by customer 5's one order alone.
    orders = _orders(counts=SINGLES)
    assert orders[5]["customer_id"] == 5
    orders[5]["quantity"] = quantity
    result = _profile(tmp_path, ROWS_OVER, ENTITIES_OVER, orders=orders)
    said = _said(result)
    assert said["query entities_over"] == query_rates.COUNTED_ANOTHER_WAY
    assert "query rows_over" not in said
    assert [each["name"] for each in result.document["queries"]] == ["rows_over"]


def test_a_table_counted_as_entities_is_not_counted_as_rows_too(tmp_path):
    result = _profile(tmp_path, ENTITIES_OVER, ROWS_OVER, orders=_orders(counts=SINGLES))
    said = _said(result)
    assert said["query rows_over"] == query_rates.COUNTED_ANOTHER_WAY
    assert "query entities_over" not in said
    assert [each["name"] for each in result.document["queries"]] == ["entities_over"]


def test_a_fact_of_the_table_counts_its_rows_before_any_query(tmp_path):
    fact = _stats.RequestFact("orders.large", "orders", "large")
    result = _profile(tmp_path, ENTITIES_OVER, contract=_contract(invariant=True), facts=(fact,))
    assert _said(result)["query entities_over"] == query_rates.COUNTED_ANOTHER_WAY
    assert "queries" not in result.document or not result.document["queries"]


def test_strict_refuses_a_table_counted_two_ways_before_any_read(tmp_path):
    assert not asked_for.decided_by_records(query_rates.COUNTED_ANOTHER_WAY)
    with pytest.raises(ProfileError) as refused:
        _profile(tmp_path, ROWS_OVER, ENTITIES_OVER, strict=True, gone=True)
    assert query_rates.COUNTED_ANOTHER_WAY in str(refused.value.context)


def test_one_table_is_grouped_by_one_entity_column_per_run():
    contract = _contract()
    orders = contract["exposes"][1]
    orders["contract"]["schema"].append({"name": "store_id", "type": "int"})
    sold_at = {
        "name": "sold_at",
        "to": "stores",
        "fromColumns": ["store_id"],
        "toColumns": ["store_id"],
        "cardinality": "many_to_one",
    }
    orders["semantics"] = {
        "relationships": [*ORDERS_BY_CUSTOMER["semantics"]["relationships"], sold_at]
    }
    by_store = "SELECT store_id, count(*) AS n FROM orders GROUP BY store_id"
    second = _on_unit("by_store", _over(by_store, '"v"."n" >= 2', "store_id"), by_store)
    planned = query_rates.plan_queries(
        contract, _request(_on_unit("many", MANY), second), FakeBackend().enum_levels
    )
    assert [plan.reason for plan in planned.plans] == [None, query_rates.COUNTED_ANOTHER_WAY]


def test_an_entity_unit_keyed_into_a_versioned_table_is_refused_before_any_read(tmp_path):
    # Children of a versioned table are counted one entity at a time, not one row at a time, so a
    # unit grouping by a key into it would count other records than the histogram it is checked with.
    contract = _contract()
    customers = contract["exposes"][0]
    customers["contract"]["schema"] += [
        {"name": "valid_from", "type": "date"},
        {"name": "valid_to", "type": "date"},
    ]
    customers["semantics"] = {
        "entities": [{"name": "customer", "type": "natural", "expr": "customer_id"}],
        "dimensions": [
            {
                "name": "valid_from",
                "type": "time",
                "typeParams": {"validityParams": {"isStart": True}},
            },
            {"name": "valid_to", "type": "time", "typeParams": {"validityParams": {"isEnd": True}}},
        ],
    }
    rows = [{**row, "valid_from": "2024-01-01", "valid_to": None} for row in _customers()]
    result = _profile(tmp_path, _on_unit("many", MANY), contract=contract, customers=rows)
    assert _said(result) == {"query many": query_rates.VERSIONED_PARENT}
