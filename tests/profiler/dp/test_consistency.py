# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Exact consistency of noisy count tables, the junction forest and decimal spelling."""

from __future__ import annotations

import itertools
import math
import random
import time
from fractions import Fraction
from typing import Dict, List, Sequence, Tuple

import pytest

from fluid_build.profiler.dp.consistency import (
    CountTable,
    junction_forest,
    make_consistent,
    to_decimal_text,
)

pytestmark = pytest.mark.unit

F = Fraction


def _table(label, columns, sizes, values, variance=1):
    return CountTable(label, tuple(columns), tuple(sizes), tuple(F(v) for v in values), F(variance))


def _random_table(rng, label, columns, sizes, fractional=False):
    n = math.prod(sizes[c] for c in columns)
    if fractional:
        values = [F(rng.randint(-400, 4000), rng.randint(1, 9)) for _ in range(n)]
    else:
        values = [rng.randint(-5, 60) for _ in range(n)]
    variance = F(rng.choice([1, 1, 2, 3, 5]), rng.choice([1, 2, 3]))
    return _table(label, columns, [sizes[c] for c in columns], values, variance)


def _project(table: CountTable, values: Sequence[F], names: Sequence[str]) -> Dict[tuple, F]:
    names = sorted(names)
    pos = [table.columns.index(n) for n in names]
    out: Dict[tuple, F] = {}
    for digits, v in zip(itertools.product(*(range(s) for s in table.sizes)), values, strict=False):
        key = tuple(digits[p] for p in pos)
        out[key] = out.get(key, F(0)) + F(v)
    return out


def _assert_agree(tables, all_values):
    for (ta, va), (tb, vb) in itertools.combinations(zip(tables, all_values, strict=False), 2):
        shared = set(ta.columns) & set(tb.columns)
        assert _project(ta, va, shared) == _project(tb, vb, shared)
    for table, values in zip(tables, all_values, strict=False):
        assert sum(F(v) for v in values) == sum(F(v) for v in all_values[0])


def _release(rng, fractional):
    """A random release: a chain or star of cliques plus a one-column table."""
    sizes = {c: rng.randint(1, 3) for c in "abcdefg"}
    shape = rng.choice(["chain", "star", "pair"])
    if shape == "chain":
        cols = [("a", "b"), ("b", "c"), ("c", "d")]
    elif shape == "star":
        cols = [("a", "b", "c"), ("b", "d"), ("c", "e"), ("a", "f")]
    else:
        cols = [("a", "b"), ("b", "a", "c")]
    tables = [_random_table(rng, f"t{i}", c, sizes, fractional) for i, c in enumerate(cols)]
    used = {c for cs in cols for c in cs}
    spare = [c for c in "abcdefg" if c not in used]
    tables.append(_random_table(rng, "one", (spare[0],), sizes, fractional))
    return tables


@pytest.mark.parametrize("fractional", [False, True])
@pytest.mark.parametrize("seed", range(12))
def test_agreement_exact_and_after_rounding(seed, fractional):
    rng = random.Random(seed * 2 + fractional)
    tables = _release(rng, fractional)
    values = make_consistent(tables)
    _assert_agree(tables, values)
    forest = junction_forest(tables)
    places = 12
    text = to_decimal_text(tables, values, forest, places)
    read = [[F(s) for s in row] for row in text]
    _assert_agree(tables, read)
    u = F(1, 10**places)
    for table, exact, got in zip(tables, values, read, strict=False):
        for e, g in zip(exact, got, strict=False):
            assert abs(e - g) <= len(table.values) * u
    for row in text:
        for s in row:
            assert "e" not in s.lower() and "+" not in s and s != "-0"


def test_agreement_with_coarse_grid():
    rng = random.Random(5)
    for _ in range(10):
        tables = _release(rng, True)
        values = make_consistent(tables)
        text = to_decimal_text(tables, values, junction_forest(tables), 2)
        _assert_agree(tables, [[F(s) for s in row] for row in text])
        assert all(len(s.partition(".")[2]) <= 2 for row in text for s in row)


def test_optimality_matches_closed_form():
    for v1, v2 in [(1, 1), (1, 3), (F(1, 2), 5)]:
        rng = random.Random(7)
        y1 = [F(rng.randint(0, 30)) for _ in range(6)]  # (a, b): 2 x 3
        y2 = [F(rng.randint(0, 30)) for _ in range(12)]  # (b, c): 3 x 4
        t1 = _table("one", "ab", (2, 3), y1, v1)
        t2 = _table("two", "bc", (3, 4), y2, v2)
        got1, got2 = make_consistent([t1, t2])
        # Lagrange: x1 = y1 - v1 * lam[b], x2 = y2 + v2 * lam[b], with P1 x1 = P2 x2.
        n1, n2 = 2, 4
        p1 = [y1[b] + y1[3 + b] for b in range(3)]
        p2 = [sum(y2[b * 4 : b * 4 + 4]) for b in range(3)]
        lam = [(p1[b] - p2[b]) / (n1 * v1 + n2 * v2) for b in range(3)]
        want1 = [y1[a * 3 + b] - F(v1) * lam[b] for a in range(2) for b in range(3)]
        want2 = [y2[b * 4 + c] + F(v2) * lam[b] for b in range(3) for c in range(4)]
        assert list(got1) == want1
        assert list(got2) == want2


def test_idempotent():
    rng = random.Random(3)
    for _ in range(6):
        tables = _release(rng, True)
        once = make_consistent(tables)
        again = make_consistent(
            [
                CountTable(t.label, t.columns, t.sizes, tuple(v), t.variance)
                for t, v in zip(tables, once, strict=False)
            ]
        )
        assert once == again


def _shapes():
    yield [("a", "b"), ("b", "c"), ("c", "d"), ("e",)]
    yield [("a", "b"), ("b", "c"), ("a", "c", "d")]
    yield [("a", "b", "c"), ("b", "c", "d"), ("c", "d", "e")]
    yield [("a", "b"), ("a", "c"), ("a", "d"), ("b", "e")]


def _quad(tables, vectors):
    return sum(
        sum(F(x) ** 2 for x in vec) / t.variance for t, vec in zip(tables, vectors, strict=False)
    )


def test_contraction_on_basis_and_random_vectors():
    rng = random.Random(11)
    for shape in _shapes():
        sizes = {c: 2 for c in "abcde"}
        variances = [F(1), F(2), F(1, 3), F(5)]
        tables = [
            _table(f"t{i}", cols, [sizes[c] for c in cols], [0] * 2 ** len(cols), variances[i])
            for i, cols in enumerate(shape)
        ]

        def run(flat: List[F]):
            parts, at = [], 0
            for t in tables:
                n = len(t.values)
                parts.append(flat[at : at + n])
                at += n
            cur = [
                CountTable(t.label, t.columns, t.sizes, tuple(p), t.variance)
                for t, p in zip(tables, parts, strict=False)
            ]
            return cur, make_consistent(cur)

        total = sum(len(t.values) for t in tables)
        inputs = [[F(int(i == j)) for j in range(total)] for i in range(total)]
        inputs += [
            [F(rng.randint(-20, 20), rng.randint(1, 5)) for _ in range(total)] for _ in range(8)
        ]
        for vec in inputs:
            cur, out = run(vec)
            assert _quad(cur, out) <= _quad(cur, [c.values for c in cur])


def _linear_check():
    rng = random.Random(2)
    t = [_table("x", "ab", (2, 2), [0] * 4, 1), _table("y", "bc", (2, 2), [0] * 4, 3)]

    def f(vec):
        cur = [
            CountTable("x", t[0].columns, t[0].sizes, tuple(vec[:4]), t[0].variance),
            CountTable("y", t[1].columns, t[1].sizes, tuple(vec[4:]), t[1].variance),
        ]
        return [v for row in make_consistent(cur) for v in row]

    a = [F(rng.randint(-9, 9)) for _ in range(8)]
    b = [F(rng.randint(-9, 9)) for _ in range(8)]
    s = [p + 2 * q for p, q in zip(a, b, strict=False)]
    return f(s) == [p + 2 * q for p, q in zip(f(a), f(b), strict=False)]


def test_map_is_linear():
    assert _linear_check()


def _labels(forest):
    return [(n.label, n.parent, n.separator) for n in forest.nodes]


def _plain(label, columns):
    return _table(label, columns, [2] * len(columns), [0] * 2 ** len(columns))


def test_forest_chain_and_root_rule():
    tables = [_plain("bc", "bc"), _plain("ab", "ab"), _plain("cd", "cd")]
    # Same size: the first sorted names are (a, b), so it is the root.
    assert _labels(junction_forest(tables)) == [
        ("ab", None, ()),
        ("bc", "ab", ("b",)),
        ("cd", "bc", ("c",)),
    ]


def test_forest_largest_clique_is_root():
    tables = [_plain("small", "ab"), _plain("big", "bcd")]
    assert _labels(junction_forest(tables)) == [
        ("big", None, ()),
        ("small", "big", ("b",)),
    ]


def test_forest_star_orders_children_by_rank():
    tables = [
        _plain("hub", "abc"),
        _plain("z", "cz"),
        _plain("y", "by"),
        _plain("x", "ax"),
    ]
    assert _labels(junction_forest(tables)) == [
        ("hub", None, ()),
        ("x", "hub", ("a",)),
        ("y", "hub", ("b",)),
        ("z", "hub", ("c",)),
    ]


def test_forest_two_trees_with_one_column_tables_left_out():
    tables = [
        _plain("p2", "cd"),
        _plain("p1", "ab"),
        _plain("q", "xy"),
        _plain("p3", "bc"),
        _table("solo", "e", (2,), [0, 0]),
    ]
    forest = junction_forest(tables)
    assert [n.label for n in forest.nodes] == ["p1", "p3", "p2", "q"]
    assert [n.parent for n in forest.nodes] == [None, "p1", "p3", None]


def test_forest_refuses_cycle_and_shared_single_column():
    cycle = [_plain("ab", "ab"), _plain("bc", "bc"), _plain("ac", "ac")]
    with pytest.raises(ValueError) as info:
        junction_forest(cycle)
    assert "running intersection" in str(info.value)
    assert "'a'" in str(info.value) or "'c'" in str(info.value)
    with pytest.raises(ValueError, match="one column"):
        junction_forest([_plain("ab", "ab"), _table("a", "a", (2,), [0, 0])])


def _decimal(tables, values, places):
    return to_decimal_text(tables, values, junction_forest(tables), places)


def test_spelling_edges():
    one = _table("one", "a", (4,), [F(-3, 2), F(1, 1000), F(-1, 1000), F(2)], 1)
    two = _table("two", "b", (2,), [F(1, 2), 0], 1)
    assert sum(one.values) == sum(two.values)
    forest = junction_forest([one, two])
    text = to_decimal_text([one, two], [one.values, two.values], forest, 12)
    assert text[0] == ("-1.5", "0.001", "-0.001", "2")
    assert text[1] == ("0.5", "0")


def test_values_within_u_of_zero_and_never_minus_zero():
    u = F(1, 10**12)
    one = _table("one", "a", (3,), [u / 4, -u / 4, u / 2], 1)
    out = to_decimal_text([one], [one.values], junction_forest([one]), 12)
    # The total u/2 rounds half up to u; the largest remainder (u/2) takes the step.
    assert out == (("0", "0", "0.000000000001"),)
    one = _table("one", "a", (2,), [-u / 4, -u / 4], 1)
    out = to_decimal_text([one], [one.values], junction_forest([one]), 12)
    assert out == (("0", "0"),)  # total -u/2 rounds half up to zero


def test_negative_balance_and_coarse_places():
    # Total 1.00 (rounded), cells 0.4, 0.4, 0.2 on a 0.1 grid are exact; use 2 places.
    one = _table("one", "a", (3,), [F(333, 1000), F(333, 1000), F(334, 1000)], 1)
    out = to_decimal_text([one], [one.values], junction_forest([one]), 2)
    assert out == (("0.33", "0.33", "0.34"),)
    two = _table("two", "ab", (1, 3), [F(6, 10), F(25, 100), F(15, 100)], 1)
    out = to_decimal_text([two], [two.values], junction_forest([two]), 1)
    assert sum(F(s) for s in out[0]) == 1
    assert all(len(s.partition(".")[2]) <= 1 for s in out[0])


def test_negative_balance_from_parent_margin():
    # Parent cells round up on the parent's own grid, forcing the child to round down.
    parent = _table("p", "ab", (2, 1), [F(5, 10), F(5, 10)], 1)
    child = _table("c", "bc", (1, 3), [F(1, 3), F(1, 3), F(1, 3)], 1)
    tables = [parent, child]
    values = make_consistent(tables)
    out = to_decimal_text(tables, values, junction_forest(tables), 0)
    read = [[F(s) for s in row] for row in out]
    _assert_agree(tables, read)
    assert read[0] == [F(1), F(0)] or read[0] == [F(0), F(1)]
    assert sum(read[1]) == 1


def test_negative_totals_and_rounding_half_up():
    one = _table("one", "a", (2,), [F(-5, 2), F(-1, 2)], 1)
    out = to_decimal_text([one], [one.values], junction_forest([one]), 0)
    assert sum(F(s) for s in out[0]) == -3
    assert all("." not in s for s in out[0])
    half = _table("h", "a", (1,), [F(5, 2)], 1)
    assert to_decimal_text([half], [half.values], junction_forest([half]), 0) == (("3",),)
    neg = _table("n", "a", (1,), [F(-5, 2)], 1)
    assert to_decimal_text([neg], [neg.values], junction_forest([neg]), 0) == (("-2",),)


def test_unequal_totals_refused():
    a = _table("a", "a", (1,), [1])
    b = _table("b", "b", (1,), [2])
    with pytest.raises(ValueError, match="total"):
        to_decimal_text([a, b], [a.values, b.values], junction_forest([a, b]), 3)


def test_refusals_of_make_consistent():
    ok = _plain("ok", "ab")
    for tables in [
        [],
        [ok, _plain("ok", "bc")],
        [ok, _table("bad", "bc", (3, 2), [0] * 6)],
        [_table("short", "ab", (2, 2), [0] * 3)],
        [_table("zero", "ab", (2, 2), [0] * 4, 0)],
        [_table("neg", "ab", (2, 2), [0] * 4, -1)],
        [_table("rep", "aa", (2, 2), [0] * 4)],
        [_table("nocols", "", (), [0])],
        [_table("empty", "a", (0,), [])],
    ]:
        with pytest.raises(ValueError):
            make_consistent(tables)
    with pytest.raises(ValueError) as info:
        make_consistent([ok, _table("bad", "bc", (3, 2), [7] * 6)])
    message = str(info.value)
    assert "ok" in message and "bad" in message and "7" not in message


def test_largest_release_runs_fast():
    rng = random.Random(1)
    sizes = {c: 8 for c in "abcdef"}
    cols = [("a", "b", "c"), ("c", "d", "e"), ("e", "f"), ("a", "b")]
    tables = [_random_table(rng, f"t{i}", c, sizes, True) for i, c in enumerate(cols)]
    started = time.perf_counter()
    values = make_consistent(tables)
    text = to_decimal_text(tables, values, junction_forest(tables), 12)
    _assert_agree(tables, [[F(s) for s in row] for row in text])
    assert time.perf_counter() - started < 30


def test_result_is_tuples_in_input_order():
    tables = [_plain("ab", "ab"), _plain("bc", "bc")]
    out = make_consistent(tables)
    assert isinstance(out, tuple) and all(isinstance(row, tuple) for row in out)
    assert [len(row) for row in out] == [4, 4]
