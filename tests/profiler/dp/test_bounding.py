# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Contribution bounding: the differencing property, independence, planning, encoding."""

from __future__ import annotations

import random
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from fluid_build.profiler.dp.bounding import (
    KEYED,
    RELATED,
    ROWS,
    UnitPlan,
    UnitRefused,
    bound_rows,
    capped_counts,
    encode_values,
    plan_unit,
    repeated_keys,
    selection_distance,
    sensitivity_squared,
)

pytestmark = pytest.mark.unit

SEED = bytes(range(32))
OTHER_SEED = bytes(range(1, 33))

Row = Tuple[Optional[bytes], bytes, int]  # unit, content, cell


def _plan(c: int, k: int, mode: str = RELATED) -> UnitPlan:
    return UnitPlan("u", "t", mode, ("a",), "r", c, k)


def _release(rows: Sequence[Row], size: int, c: int, k: int, seed: bytes = SEED) -> List[int]:
    units = [row[0] for row in rows]
    kept = bound_rows(units, [row[1] for row in rows], seed, c)
    return capped_counts([units[i] for i in kept], [rows[i][2] for i in kept], size, k)


def _unbounded(rows: Sequence[Row], size: int) -> List[int]:
    counts = [0] * size
    for _, _, cell in rows:
        counts[cell] += 1
    return counts


def _distance2(a: Sequence[int], b: Sequence[int]) -> int:
    return sum((x - y) ** 2 for x, y in zip(a, b, strict=True))


def _random_release(rng: random.Random) -> Tuple[List[Row], int, int, int]:
    size = rng.randint(1, 12)
    c = rng.randint(1, 8)
    k = rng.randint(1, c)
    rows: List[Row] = []
    for unit in range(rng.randint(1, 6)):
        key = f"unit-{unit}".encode()
        for _ in range(rng.randint(1, 30)):
            rows.append((key, rng.randbytes(rng.choice([1, 4])), rng.randrange(size)))
    rng.shuffle(rows)
    return rows, size, c, k


CASES = 400


def test_differencing_property():
    widest_unbounded = 0
    for case in range(CASES):
        rng = random.Random(1000 + case)
        rows, size, c, k = _random_release(rng)
        bound = sensitivity_squared(_plan(c, k))
        assert bound == sensitivity_squared(_plan(c, k, KEYED))
        assert selection_distance(_plan(c, k)) == selection_distance(_plan(c, k, KEYED))
        base = _release(rows, size, c, k)
        sizes: Dict[Optional[bytes], int] = {}
        for unit, _, _ in rows:
            sizes[unit] = sizes.get(unit, 0) + 1
        largest = max(sizes, key=lambda unit: (sizes[unit], unit or b""))
        victims = {largest, rng.choice(list(sizes))}
        for victim in victims:
            rest = [row for row in rows if row[0] != victim]
            assert _distance2(base, _release(rest, size, c, k)) <= bound
        widest_unbounded = max(
            widest_unbounded,
            _distance2(
                _unbounded(rows, size),
                _unbounded([row for row in rows if row[0] != largest], size),
            )
            - bound,
        )
        for _ in range(3):
            i = rng.randrange(len(rows))
            assert _distance2(base, _release(rows[:i] + rows[i + 1 :], size, c, k)) <= bound
            extra = (rng.choice(list(sizes)), rng.randbytes(4), rng.randrange(size))
            j = rng.randint(0, len(rows))
            assert _distance2(base, _release(rows[:j] + [extra] + rows[j:], size, c, k)) <= bound
    assert widest_unbounded > 0, "without bounding the largest unit must move the counts further"


def test_rows_mode_one_row_moves_counts_by_one():
    for case in range(CASES):
        rng = random.Random(5000 + case)
        size = rng.randint(1, 12)
        rows: List[Row] = [
            (None, rng.randbytes(3), rng.randrange(size)) for _ in range(rng.randint(1, 30))
        ]
        base = _release(rows, size, 1, 1)
        assert base == _unbounded(rows, size)
        i = rng.randrange(len(rows))
        assert _distance2(base, _release(rows[:i] + rows[i + 1 :], size, 1, 1)) <= 1
        assert sensitivity_squared(_plan(1, 1, ROWS)) == 1


def test_unbounded_counts_move_more_than_the_bound():
    rows: List[Row] = [(b"big", bytes([i]), 0) for i in range(20)] + [(b"small", b"x", 1)]
    gap = _distance2(_unbounded(rows, 2), _unbounded(rows[20:], 2))
    assert gap == 400
    assert gap > sensitivity_squared(_plan(3, 2))


def test_independence_of_other_units_and_order():
    for case in range(100):
        rng = random.Random(9000 + case)
        c = rng.randint(1, 5)
        mine = [rng.randbytes(3) for _ in range(rng.randint(1, 15))]
        alone = bound_rows([b"me"] * len(mine), mine, SEED, c)
        kept_alone = sorted(mine[i] for i in alone)
        others = [(f"o{rng.randint(0, 3)}".encode(), rng.randbytes(3)) for _ in range(20)]
        mixed: List[Tuple[Optional[bytes], bytes]] = [(b"me", m) for m in mine] + list(others)
        rng.shuffle(mixed)
        kept = bound_rows([u for u, _ in mixed], [m for _, m in mixed], SEED, c)
        assert sorted(mixed[i][1] for i in kept if mixed[i][0] == b"me") == kept_alone


def test_bound_rows_determinism_and_seed_variation():
    units: List[Optional[bytes]] = [b"u"] * 40
    contents = [bytes([i]) for i in range(40)]
    first = bound_rows(units, contents, SEED, 5)
    assert first == bound_rows(units, contents, SEED, 5)
    assert first == sorted(first) and len(first) == 5
    assert first != bound_rows(units, contents, OTHER_SEED, 5)


def test_small_units_and_none_units_keep_everything():
    units: List[Optional[bytes]] = [b"a", b"a", b"b", None, None, None, b"a"]
    contents = [bytes([i]) for i in range(7)]
    assert bound_rows(units, contents, SEED, 3) == list(range(7))
    kept = bound_rows(units, contents, SEED, 1)
    assert {3, 4, 5} <= set(kept)
    assert len([i for i in kept if units[i] == b"a"]) == 1


def test_ties_break_by_content_then_index():
    units: List[Optional[bytes]] = [b"u", b"u", b"u"]
    contents = [b"same", b"same", b"same"]
    assert bound_rows(units, contents, SEED, 2) == [0, 1]


def test_bound_rows_refusals():
    with pytest.raises(ValueError):
        bound_rows([b"a"], [], SEED, 1)
    with pytest.raises(ValueError):
        bound_rows([b"a"], [b"x"], SEED, 0)


def test_capped_counts():
    units: List[Optional[bytes]] = [b"a", b"a", b"a", b"b", None, None]
    assert capped_counts(units, [0, 0, 1, 0, 0, 0], 3, 1) == [4, 1, 0]
    assert capped_counts(units, [0, 0, 1, 0, 0, 0], 3, 2) == [5, 1, 0]
    with pytest.raises(ValueError):
        capped_counts([b"a"], [3], 3, 1)
    with pytest.raises(ValueError):
        capped_counts([b"a"], [-1], 3, 1)
    with pytest.raises(ValueError):
        capped_counts([b"a"], [0], 3, 0)


@pytest.mark.parametrize(
    ("c", "k", "squared", "distance"),
    [(1, 1, 2, 2), (2, 2, 4, 2), (3, 1, 3, 3), (5, 2, 9, 5), (4, 4, 16, 4)],
)
def test_sensitivity_and_distance(c: int, k: int, squared: int, distance: int):
    plan = _plan(c, k)
    assert sensitivity_squared(plan) == squared
    assert selection_distance(plan) == distance
    own = _plan(1, 1, ROWS)
    assert sensitivity_squared(own) == 1 and selection_distance(own) == 1


def test_encode_values_is_injective_on_tricky_pairs():
    pairs: List[Tuple[List[Any], List[Any]]] = [
        (["a", "b"], ["ab"]),
        ([1], ["1"]),
        ([None], ["None"]),
        ([Fraction(1, 2)], ["1/2"]),
        ([""], []),
        (["", ""], [""]),
        ([None, None], [None]),
    ]
    for left, right in pairs:
        assert encode_values(left) != encode_values(right)
    same = ["é", 7, None, Fraction(3, 4)]
    assert encode_values(same) == encode_values(list(same))
    assert encode_values(["a"]) == b"S" + (1).to_bytes(8, "big") + b"a"


def test_encode_values_refuses_other_types():
    for bad in (True, 1.5, b"x", ["a"], object()):
        with pytest.raises(TypeError):
            encode_values([bad])


def _contract(**overrides: Any) -> Dict[str, Any]:
    customers: Dict[str, Any] = {
        "exposeId": "customers",
        "contract": {"schema": [{"name": "id"}, {"name": "region"}]},
        "semantics": {"entities": [{"name": "id", "type": "primary"}]},
    }
    orders: Dict[str, Any] = {
        "exposeId": "orders",
        "contract": {"schema": [{"name": "cust"}, {"name": "tag"}]},
        "semantics": {
            "relationships": [
                {"name": "by", "to": "customers", "fromColumns": ["cust"], "toColumns": ["id"]}
            ]
        },
    }
    lines: Dict[str, Any] = {
        "exposeId": "lines",
        "semantics": {
            "relationships": [
                {"name": "of", "to": "orders", "fromColumns": ["o"], "toColumns": ["id"]}
            ]
        },
    }
    return {"exposes": [customers, {**orders, **overrides.get("orders", {})}, lines]}


def test_plan_unit_rows_mode():
    plan = plan_unit(_contract(), "customers")
    assert (plan.mode, plan.key_columns, plan.relationship) == (ROWS, ("id",), None)
    assert (plan.max_rows_per_unit, plan.max_rows_per_cell) == (1, 1)
    assert plan == plan_unit(_contract(), "customers", "customers")
    keyless = plan_unit(_contract(), "orders", "orders")
    assert keyless.mode == ROWS and keyless.key_columns == ()


def test_plan_unit_related_mode():
    plan = plan_unit(_contract(), "customers", "orders")
    assert plan.mode == RELATED and plan.key_columns == ("cust",) and plan.relationship == "by"
    assert (plan.max_rows_per_unit, plan.max_rows_per_cell) == (1, 1)
    plan = plan_unit(_contract(), "customers", "orders", max_rows_per_unit=6)
    assert (plan.max_rows_per_unit, plan.max_rows_per_cell) == (6, 6)
    plan = plan_unit(_contract(), "customers", "orders", max_rows_per_unit=6, max_rows_per_cell=2)
    assert (plan.max_rows_per_unit, plan.max_rows_per_cell) == (6, 2)


def _refusal(*args: Any, **kwargs: Any) -> UnitRefused:
    with pytest.raises(UnitRefused) as caught:
        plan_unit(*args, **kwargs)
    return caught.value


def test_plan_unit_refusals():
    assert _refusal(_contract(), "nope").event == "unit_expose_unknown"
    assert _refusal(_contract(), "customers", "nope").event == "unit_expose_unknown"
    refused = _refusal(_contract(), "customers", "customers", max_rows_per_unit=2)
    assert refused.event == "unit_bound_in_rows_mode"
    assert _refusal(_contract(), "customers", max_rows_per_cell=1).event == (
        "unit_bound_in_rows_mode"
    )
    none = _refusal(_contract(), "customers", "lines")
    assert none.event == "unit_relationship_missing"
    assert "one hop" in str(none) and "'lines'" in str(none) and "'customers'" in str(none)
    assert none.context["unit"] == "customers"


def _with(*relationships: Dict[str, Any]) -> Dict[str, Any]:
    return _contract(orders={"semantics": {"relationships": list(relationships)}})


def test_plan_unit_two_relationships_and_malformed():
    base = _contract()["exposes"][1]["semantics"]["relationships"][0]
    second = {"name": "also", "to": "customers", "fromColumns": ["tag"], "toColumns": ["id"]}
    two = _refusal(_with(base, second), "customers", "orders")
    assert two.event == "unit_relationship_ambiguous" and "by" in str(two) and "also" in str(two)
    composite = {**base, "fromColumns": ["cust", "tag"], "toColumns": ["id", "region"]}
    assert plan_unit(_with(composite), "customers", "orders").key_columns == ("cust", "tag")
    for broken in (
        {**base, "fromColumns": ["cust", "tag"]},
        {**base, "fromColumns": []},
        {**base, "toColumns": "id"},
        {**base, "fromColumns": [1], "toColumns": [2]},
        {key: value for key, value in base.items() if key != "fromColumns"},
    ):
        refused = _refusal(_with(broken), "customers", "orders")
        assert refused.event == "unit_relationship_malformed" and "'by'" in str(refused)


def test_plan_unit_bound_ranges():
    for kwargs in (
        {"max_rows_per_unit": 0},
        {"max_rows_per_unit": 1001},
        {"max_rows_per_unit": True},
        {"max_rows_per_unit": 2.0},
        {"max_rows_per_unit": 3, "max_rows_per_cell": 4},
        {"max_rows_per_unit": 3, "max_rows_per_cell": 0},
        {"max_rows_per_unit": 3, "max_rows_per_cell": True},
        {"max_rows_per_cell": 2},
    ):
        refused = _refusal(_contract(), "customers", "orders", **kwargs)
        assert refused.event == "unit_bound_out_of_range"
    top = plan_unit(_contract(), "customers", "orders", max_rows_per_unit=1000)
    assert top.max_rows_per_unit == 1000


def test_plan_unit_keyed_mode():
    plan = plan_unit(_contract(), "orders", unit_key=["cust"])
    assert (plan.mode, plan.key_columns, plan.relationship) == (KEYED, ("cust",), None)
    assert (plan.unit_expose, plan.release_table) == ("orders", "orders")
    assert (plan.max_rows_per_unit, plan.max_rows_per_cell) == (1, 1)
    plan = plan_unit(_contract(), "orders", "orders", unit_key=("tag", "cust"), max_rows_per_unit=5)
    assert plan.key_columns == ("tag", "cust")
    assert (plan.max_rows_per_unit, plan.max_rows_per_cell) == (5, 5)
    plan = plan_unit(
        _contract(), "orders", unit_key=["cust"], max_rows_per_unit=5, max_rows_per_cell=2
    )
    assert sensitivity_squared(plan) == 9 and selection_distance(plan) == 5


def test_plan_unit_keyed_refusals():
    assert _refusal(_contract(), "orders", unit_key=["nope"]).event == "unit_key_column_unknown"
    assert _refusal(_contract(), "lines", unit_key=["o"]).event == "unit_key_column_unknown"
    for bad in ([], ["cust", "cust"], "cust", [""], [1]):
        assert _refusal(_contract(), "orders", unit_key=bad).event == "unit_key_malformed"
    other = _refusal(_contract(), "customers", "orders", unit_key=["id"])
    assert other.event == "unit_key_with_other_table" and "relationship" in str(other)
    refused = _refusal(_contract(), "orders", unit_key=["cust"], max_rows_per_unit=0)
    assert refused.event == "unit_bound_out_of_range"
    refused = _refusal(
        _contract(), "orders", unit_key=["cust"], max_rows_per_unit=2, max_rows_per_cell=3
    )
    assert refused.event == "unit_bound_out_of_range"
    assert _refusal(_contract(), "nope", unit_key=["cust"]).event == "unit_expose_unknown"


def test_plan_unit_primary_entity_must_name_a_column():
    contract = _contract()
    contract["exposes"][0]["semantics"]["entities"] = [
        {"name": "customer", "type": "primary", "expr": "missing"}
    ]
    assert _refusal(contract, "customers").event == "unit_key_unresolved"
    contract["exposes"][0]["semantics"]["entities"] = [
        {"name": "customer", "type": "primary", "expr": "id"},
        {"name": "region", "type": "foreign"},
    ]
    assert plan_unit(contract, "customers").key_columns == ("id",)


def test_repeated_keys():
    assert repeated_keys([]) == (0, 0)
    assert repeated_keys([b"a", b"b", b"c"]) == (0, 0)
    assert repeated_keys([b"a", b"a", b"a", b"b", b"b", b"c"]) == (2, 0)
    assert repeated_keys([None, b"a", None, b"a"]) == (1, 2)
