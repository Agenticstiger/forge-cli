# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``render_cel``: pinned CEL, every refusal, and cel-python against FLUID SQL/1's evaluator.

The differential test is the proof: on generated models, predicates and rows (nulls and
boundary values included), cel-python evaluates the rendering to exactly what
:func:`fluid_build.fluid_sql.evaluate` says, and never raises. cel-python is a test-only
dependency (the ``dev`` extra); ``fluid_build`` never imports it.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Any, Dict, List, Optional

import pytest

from fluid_build import fluid_sql as fs
from fluid_build.fluid_sql import _nodes as n

pytestmark = pytest.mark.unit

DECLARED = {
    "id": ("string", True),
    "amount": ("long", False),
    "qty": ("int", True),
    "price": ("double", False),
    "note": ("string", False),
    "ok": ("boolean", False),
    "total": ("decimal(10,2)", False),
    "day": ("date", False),
}


def model(declared: Optional[Dict[str, Any]] = None) -> fs.Model:
    out = fs.Model("e")
    for name, (type_, required) in (declared or DECLARED).items():
        out.columns[name] = fs.Column(name, type_, fs.column_type(type_), required=required)
    return out


def cel(text: str) -> str:
    return fs.render_cel(fs.parse(text), model())


@pytest.mark.parametrize(
    "text,expected",
    [
        ("qty > 0", "message.qty > 0"),
        ("amount > 0", "(type(message.amount) != null_type && message.amount > 0)"),
        ("price >= 0", "(type(message.price) != null_type && message.price >= 0.0)"),
        ("amount IS NULL", "type(message.amount) == null_type"),
        ("qty IS NULL", "false"),
        ("id IN ('a', 'b')", 'message.id in ["a", "b"]'),
        (
            "note NOT IN ('x')",
            '(type(message.note) != null_type && !(message.note in ["x"]))',
        ),
        ("ok", "(type(message.ok) != null_type && message.ok)"),
        ("NOT ok", "!((type(message.ok) != null_type && message.ok))"),
        ("qty BETWEEN 1 AND 5", "(message.qty >= 1 && message.qty <= 5)"),
        ("id = 'q\"\\'", 'message.id == "q\\"\\\\"'),
        (
            "COALESCE(amount, 0) + qty <= 100",
            "((type(message.amount) != null_type ? message.amount : 0) + message.qty) <= 100",
        ),
    ],
)
def test_pinned_cel(text: str, expected: str) -> None:
    assert cel(text) == expected


def test_a_name_that_is_not_a_cel_identifier_is_indexed() -> None:
    m = model({"in": ("long", True), "a b": ("long", True)})
    assert fs.render_cel(fs.parse('"in" > 1'), m) == 'message["in"] > 1'
    assert fs.render_cel(fs.parse('"a b" > 1'), m, record="rec") == 'rec["a b"] > 1'


@pytest.mark.parametrize(
    "text,reason",
    [
        ("amount / 2 > 1", "divides"),
        ("price + 1 > 1", "double column"),
        ("ROUND(qty, 0) = 1", "ROUND"),
        ("DAYS_BETWEEN(day, day) > 1", "DAYS_BETWEEN"),
        ("day = DATE '2026-01-01'", "date"),
        ("total > 1", "decimal"),
        ("qty > 1.5", "not an integer"),
        ("qty > 1.0", "not an integer"),
        ("qty > 9223372036854775808", "64-bit"),
        ("note = 'a;b'", "';'"),
        ("price > 9007199254740993", "2^53"),
        ("qty = price", "integer-typed value with a double-typed"),
        ("COALESCE(qty, price) > 1", "integer-typed value and a double-typed"),
    ],
)
def test_refusals(text: str, reason: str) -> None:
    with pytest.raises(fs.CelUntranslatable) as caught:
        cel(text)
    assert reason in caught.value.reason


def test_a_relationship_is_refused() -> None:
    parent = model({"k": ("long", True)})
    child = model()
    child.links["p"] = fs.Link("p", parent, "parent")
    with pytest.raises(fs.CelUntranslatable, match="relationship"):
        fs.render_cel(fs.parse("p.k > 1"), child)


def test_the_text_holds_no_semicolon_even_for_hostile_strings() -> None:
    out = cel("note IN ('a--b', '/*x*/', '{{ x }}')")
    assert ";" not in out


def test_an_oversized_text_is_refused() -> None:
    text = "amount > 1"
    for _ in range(10):
        text = f"CASE WHEN ({text}) THEN amount ELSE 1 END > 0"
    with pytest.raises(fs.CelUntranslatable, match="characters long"):
        cel(text)


# -- the differential proof ------------------------------------------------------------

celpy = pytest.importorskip("celpy")
hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

_ENV = celpy.Environment()
_KINDS = {"string": "string", "int": "int", "long": "int", "double": "double", "boolean": "bool"}
_INTS = [0, 1, -1, 2, -2, 7, 100, 1000, -1000, 2**20, -(2**20)]
_DOUBLES = [0.0, -0.0, 0.5, -1.5, 1.0, 2.0, 2.5, 100.0, -1.0]
_STRINGS = ["", "a", "b", "ab", "A", "é", "x;y", 'q"\\']
_RECORDS: List[int] = []


def evaluate_cel(expr: str, row: Dict[str, Any]) -> Any:
    program = _ENV.program(_ENV.compile(expr))
    return program.evaluate({"message": celpy.json_to_cel(row)})


def evaluator_row(row: Dict[str, Any], columns: Dict[str, str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, value in row.items():
        if isinstance(value, bool) or value is None or isinstance(value, str):
            out[name] = value
        elif columns[name] == "double":
            out[name] = Fraction(Decimal(repr(value)))  # the shortest decimal of the double
        else:
            out[name] = Fraction(value)
    return out


@st.composite
def models(draw: Any) -> Dict[str, Any]:
    count = draw(st.integers(2, 4))
    kinds = [draw(st.sampled_from(sorted(_KINDS))) for _ in range(count)]
    required = [draw(st.booleans()) for _ in range(count)]
    return {f"c{i}": (k, r) for i, (k, r) in enumerate(zip(kinds, required, strict=True))}


class Gen:
    """Typed FLUID SQL/1 trees over a model, built so every one passes the checker."""

    def __init__(self, draw: Any, declared: Dict[str, Any]) -> None:
        self.draw = draw
        self.columns = {name: _KINDS[k] for name, (k, _r) in declared.items()}

    def pick(self, cls: str) -> Optional[str]:
        names = [name for name, c in self.columns.items() if c == cls]
        return self.draw(st.sampled_from(names)) if names else None

    def number(self, cls: str, depth: int) -> n.Node:
        choice = self.draw(st.integers(0, 5 if depth > 0 else 2))
        name = self.pick(cls)
        if choice <= 1 and name:
            return n.Column(name)
        if choice <= 2:
            return n.Number(str(self.draw(st.sampled_from([0, 1, -1, 2, 7, 100]))))
        if choice == 3:
            return n.Neg(self.number(cls, depth - 1))
        if choice == 4 and cls == "int":
            op = self.draw(st.sampled_from("+-*"))
            return n.Arith(op, self.number(cls, depth - 1), self.number(cls, depth - 1))
        return self.branching(cls, depth)

    def text(self, depth: int) -> n.Node:
        name = self.pick("string")
        if name and (depth <= 0 or self.draw(st.booleans())):
            return n.Column(name)
        if depth > 0 and self.draw(st.booleans()):
            return self.branching("string", depth)
        return n.String(self.draw(st.sampled_from(["a", "b", "ab", "é", ""])))

    def branching(self, cls: str, depth: int) -> n.Node:
        make = self.text if cls == "string" else (lambda d: self.number(cls, d))
        first, second = make(depth - 1), make(depth - 1)
        if self.draw(st.booleans()):
            args = [first, second]
            if self.draw(st.booleans()):
                args.append(n.Null())
            return n.Coalesce(tuple(args))
        else_: Optional[n.Node] = None
        if self.draw(st.booleans()):
            else_ = n.Null() if self.draw(st.booleans()) else second
        return n.Case(((self.predicate(depth - 1), first),), else_)

    def predicate(self, depth: int) -> n.Node:  # noqa: C901
        names = [name for name, c in self.columns.items() if c == "bool"]
        options = ["compare", "in", "between", "null", "distinct", "bool"]
        if depth > 0:
            options += ["not", "and", "or"]
        kind = self.draw(st.sampled_from(options))
        classes = sorted(set(self.columns.values()) - {"bool"}) or ["int"]
        cls = self.draw(st.sampled_from(classes if kind != "bool" else ["bool"]))
        if kind == "bool":
            if names and self.draw(st.booleans()):
                return n.Column(self.draw(st.sampled_from(names)))
            return n.Bool(self.draw(st.booleans()))
        if kind == "not":
            return n.Not(self.predicate(depth - 1))
        if kind in ("and", "or"):
            node = n.And if kind == "and" else n.Or
            return node(self.predicate(depth - 1), self.predicate(depth - 1))
        build = self.text if cls == "string" else (lambda d: self.number(cls, d))
        d = min(depth, 2)
        if kind == "compare":
            op = self.draw(st.sampled_from(["=", "<>", "<", "<=", ">", ">="]))
            return n.Compare(op, build(d), build(d))
        if kind == "in":
            pool = ["a", "b", "é"] if cls == "string" else ["0", "1", "-1", "7", "100"]
            items = tuple(
                (n.String(p) if cls == "string" else n.Number(p))
                for p in self.draw(st.lists(st.sampled_from(pool), min_size=1, max_size=3))
            )
            return n.In(build(d), items, self.draw(st.booleans()))
        if kind == "between":
            return n.Between(build(d), build(d), build(d), self.draw(st.booleans()))
        if kind == "null":
            return n.IsNull(build(d), self.draw(st.booleans()))
        return n.DistinctFrom(build(d), build(d), self.draw(st.booleans()))


@st.composite
def cases(draw: Any) -> Any:
    declared = draw(models())
    tree = Gen(draw, declared).predicate(3)
    rows = []
    for _ in range(draw(st.integers(3, 6))):
        row: Dict[str, Any] = {}
        for name, (kind, required) in declared.items():
            if not required and draw(st.integers(0, 3)) == 0:
                row[name] = None
            elif kind in ("int", "long"):
                row[name] = draw(st.sampled_from(_INTS))
            elif kind == "double":
                row[name] = draw(st.sampled_from(_DOUBLES))
            elif kind == "boolean":
                row[name] = draw(st.booleans())
            else:
                row[name] = draw(st.sampled_from(_STRINGS))
        rows.append(row)
    return declared, tree, rows


@settings(
    max_examples=300,
    deadline=None,
    derandomize=True,
    suppress_health_check=list(HealthCheck),
)
@given(cases())
def test_cel_agrees_with_the_evaluator(case: Any) -> None:
    declared, tree, rows = case
    m = model(declared)
    try:
        expr = fs.render_cel(tree, m)
    except fs.CelUntranslatable as refused:  # a mixed int and double pair: refused, not wrong
        assert "double" in refused.reason or "characters long" in refused.reason
        return
    _RECORDS.append(1)
    columns = {name: _KINDS[k] for name, (k, _r) in declared.items()}
    for row in rows:
        got = evaluate_cel(expr, row)
        want = fs.evaluate(tree, evaluator_row(row, columns), predicate=True)
        assert isinstance(got, celpy.celtypes.BoolType) and bool(got) is want, (
            fs.canonical(tree),
            expr,
            row,
        )


def test_the_differential_test_translated_most_cases() -> None:
    test_cel_agrees_with_the_evaluator()
    assert len(_RECORDS) >= 150


def test_integer_overflow_fails_the_record() -> None:
    """The one known divergence: CEL int is 64-bit, so an overflowing row errors (fails)."""
    m = model({"a": ("long", True)})
    expr = fs.render_cel(fs.parse("a * 4 > 0"), m)
    with pytest.raises(Exception):  # noqa: B017 - cel-python's own error type
        evaluate_cel(expr, {"a": 2**62})
