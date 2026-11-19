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
        ("qty > 0", "(message.qty > 0)"),
        ("amount > 0", "((type(message.amount) != null_type) && (message.amount > 0))"),
        ("price >= 0", "((type(message.price) != null_type) && (message.price >= 0.0))"),
        ("amount IS NULL", "(type(message.amount) == null_type)"),
        ("qty IS NULL", "false"),
        ("id IN ('a', 'b')", '(message.id in ["a", "b"])'),
        (
            "note NOT IN ('x')",
            '((type(message.note) != null_type) && !(message.note in ["x"]))',
        ),
        ("ok", "((type(message.ok) != null_type) && message.ok)"),
        ("NOT ok", "!((type(message.ok) != null_type) && message.ok)"),
        ("qty BETWEEN 1 AND 5", "((message.qty >= 1) && (message.qty <= 5))"),
        ("id = 'q\"\\'", '(message.id == "q\\"\\\\")'),
        (
            "COALESCE(amount, 0) + qty <= 100",
            "((((type(message.amount) != null_type) ? message.amount : 0) + message.qty) <= 100)",
        ),
        ("(qty > 0) = (qty > 1)", "((message.qty > 0) == (message.qty > 1))"),
        (
            "CASE WHEN qty > 1 THEN amount END = 5",
            "(((message.qty > 1) ? (type(message.amount) != null_type) : false) "
            "&& (((message.qty > 1) ? message.amount : 0) == 5))",
        ),
    ],
)
def test_pinned_cel(text: str, expected: str) -> None:
    assert cel(text) == expected


def test_a_name_that_is_not_a_cel_identifier_is_indexed() -> None:
    m = model({"in": ("long", True), "a b": ("long", True)})
    assert fs.render_cel(fs.parse('"in" > 1'), m) == '(message["in"] > 1)'
    assert fs.render_cel(fs.parse('"a b" > 1'), m, record="rec") == '(rec["a b"] > 1)'


@pytest.mark.parametrize(
    "text,reason",
    [
        ("amount / 2 > 1", "divides"),
        ("price + 1 > 1", "double column"),
        ("ROUND(qty, 0) = 1", "ROUND"),
        ("DAYS_BETWEEN(day, day) > 1", "DAYS_BETWEEN"),
        ("ADD_DAYS(day, 1) = day", "ADD_DAYS"),
        ("ADD_MONTHS(day, qty) = day", "ADD_MONTHS"),
        ("ADD_YEARS(day, 1) = day", "ADD_YEARS"),
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
    with pytest.raises(fs.CelUntranslatable, match="longer than"):
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
_TRANSLATED: List[str] = []
_SHAPES: Dict[str, int] = {}


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


def agree(declared: Dict[str, Any], tree: n.Node, rows: List[Dict[str, Any]]) -> str:
    """Render ``tree``, and assert cel-python and the evaluator agree on every row."""
    expr = fs.render_cel(tree, model(declared))
    assert ";" not in expr
    assert "null" not in expr.replace("null_type", ""), expr  # cel-java type-checks: no literal
    columns = {name: _KINDS[k] for name, (k, _r) in declared.items()}
    for row in rows:
        got = evaluate_cel(expr, row)
        want = fs.evaluate(tree, evaluator_row(row, columns), predicate=True)
        assert isinstance(got, celpy.celtypes.BoolType) and bool(got) is want, (
            fs.canonical(tree),
            expr,
            row,
        )
    return expr


@st.composite
def models(draw: Any) -> Dict[str, Any]:
    count = draw(st.integers(2, 5))
    kinds = [draw(st.sampled_from(sorted(_KINDS))) for _ in range(count)]
    required = [draw(st.booleans()) for _ in range(count)]
    return {f"c{i}": (k, r) for i, (k, r) in enumerate(zip(kinds, required, strict=True))}


class Gen:
    """Typed FLUID SQL/1 trees over a model, built so every one passes the checker.

    Every class (int, double, string, bool) has value shapes: columns, literals, NEG,
    arithmetic (int), CASE (several WHENs, with or without ELSE, NULL branches), nested
    COALESCE, and for bool also predicates used as values (compared with ``=``, ``<>``,
    ``IN (TRUE)``, ``IS DISTINCT FROM`` and ``IS NULL``).
    """

    def __init__(self, draw: Any, declared: Dict[str, Any]) -> None:
        self.draw = draw
        self.columns = {name: _KINDS[k] for name, (k, _r) in declared.items()}

    def note(self, shape: str) -> None:
        _SHAPES[shape] = _SHAPES.get(shape, 0) + 1

    def column(self, cls: str) -> Optional[n.Node]:
        names = [name for name, c in self.columns.items() if c == cls]
        return n.Column(self.draw(st.sampled_from(names))) if names else None

    def literal(self, cls: str) -> n.Node:
        if cls == "string":
            return n.String(self.draw(st.sampled_from(["a", "b", "ab", "é", ""])))
        if cls == "bool":
            return n.Bool(self.draw(st.booleans()))
        return n.Number(str(self.draw(st.sampled_from([0, 1, -1, 2, 7, 100]))))

    def value(self, cls: str, depth: int) -> n.Node:  # noqa: C901 - one branch per shape
        options = ["column", "column", "literal"]
        if depth > 0:
            options += ["case", "coalesce", "coalesce"]
            if cls == "int":
                options += ["arith", "arith", "neg"]
            if cls == "double":
                options += ["neg"]
            if cls == "bool":
                options += ["predicate", "predicate"]
        kind = self.draw(st.sampled_from(options))
        column = self.column(cls)
        if kind == "column" and column is not None:
            return column
        if kind == "case":
            self.note("case")
            return self.case(cls, depth)
        if kind == "coalesce":
            self.note("coalesce")
            args = [self.value(cls, depth - 1) for _ in range(self.draw(st.integers(2, 3)))]
            if self.draw(st.booleans()):
                args.insert(self.draw(st.integers(0, len(args))), n.Null())
            return n.Coalesce(tuple(args))
        if kind == "arith":
            self.note("arith")
            op = self.draw(st.sampled_from("+-*"))
            return n.Arith(op, self.value(cls, depth - 1), self.value(cls, depth - 1))
        if kind == "neg":
            return n.Neg(self.value(cls, depth - 1))
        if kind == "predicate":
            self.note("predicate-as-value")
            return self.predicate(depth - 1)
        return self.literal(cls)

    def case(self, cls: str, depth: int) -> n.Node:
        whens = tuple(
            (self.predicate(depth - 1), self.value(cls, depth - 1))
            for _ in range(self.draw(st.integers(1, 2)))
        )
        else_: Optional[n.Node] = None
        pick = self.draw(st.integers(0, 2))
        if pick == 1:
            else_ = self.value(cls, depth - 1)
        elif pick == 2:
            else_ = n.Null()
        if else_ is None:
            self.note("case-without-else")
        return n.Case(whens, else_)

    def predicate(self, depth: int) -> n.Node:  # noqa: C901 - one branch per shape
        options = ["compare", "in", "between", "null", "distinct", "bool"]
        if depth > 0:
            options += ["not", "and", "or", "compare", "distinct"]
        kind = self.draw(st.sampled_from(options))
        classes = sorted(set(self.columns.values()) | {"int", "bool"})
        cls = self.draw(st.sampled_from(classes))
        d = min(depth, 3)
        if kind == "bool":
            return self.value("bool", 0)
        if kind == "not":
            return n.Not(self.predicate(depth - 1))
        if kind in ("and", "or"):
            node = n.And if kind == "and" else n.Or
            return node(self.predicate(depth - 1), self.predicate(depth - 1))
        if kind == "compare":
            ops = ["=", "<>"] if cls == "bool" else ["=", "<>", "<", "<=", ">", ">="]
            return n.Compare(
                self.draw(st.sampled_from(ops)), self.value(cls, d), self.value(cls, d)
            )
        if kind == "in":
            pool = {
                "string": [n.String(p) for p in ("a", "b", "é")],
                "bool": [n.Bool(True), n.Bool(False)],
            }.get(cls, [n.Number(p) for p in ("0", "1", "-1", "7", "100")])
            items = tuple(self.draw(st.lists(st.sampled_from(pool), min_size=1, max_size=3)))
            return n.In(self.value(cls, d), items, self.draw(st.booleans()))
        if kind == "between":
            if cls == "bool":
                cls = "int"
            return n.Between(
                self.value(cls, d), self.value(cls, d), self.value(cls, d), self.draw(st.booleans())
            )
        if kind == "null":
            return n.IsNull(self.value(cls, d), self.draw(st.booleans()))
        return n.DistinctFrom(self.value(cls, d), self.value(cls, d), self.draw(st.booleans()))


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
    max_examples=400,
    deadline=None,
    derandomize=True,
    suppress_health_check=list(HealthCheck),
)
@given(cases())
def test_cel_agrees_with_the_evaluator(case: Any) -> None:
    declared, tree, rows = case
    try:
        expr = agree(declared, tree, rows)
    except fs.CelUntranslatable as refused:  # a mixed int and double pair: refused, not wrong
        assert "double" in refused.reason or "characters" in refused.reason
        return
    _TRANSLATED.append(expr)


def test_the_differential_test_translated_most_cases_and_built_every_shape() -> None:
    _TRANSLATED.clear()
    _SHAPES.clear()
    test_cel_agrees_with_the_evaluator()
    assert len(_TRANSLATED) >= 250
    for shape in ("case", "coalesce", "arith", "predicate-as-value", "case-without-else"):
        assert _SHAPES.get(shape, 0) >= 20, (shape, _SHAPES)


PREDICATE_OPERANDS = [
    "(qty > 0) = (qty > 1)",
    "ok = (id IN ('a', 'b'))",
    "(amount IS NULL) = (id = 'a')",
    "(qty > 0) IS DISTINCT FROM (qty > 1)",
    "CASE WHEN qty > 1 THEN ok END = (qty > 0)",
    "(qty > 0) IN (TRUE)",
    "NOT (ok = (amount > 1))",
    "(ok <> (qty > 2)) = (note = 'a')",
]


@pytest.mark.parametrize("text", PREDICATE_OPERANDS)
def test_a_predicate_as_an_operand_is_parenthesised(text: str) -> None:
    """CEL's relational operators are left-associative: ``a > 0 == b > 1`` regroups."""
    declared = {k: v for k, v in DECLARED.items() if k not in ("total", "day")}
    rows = [
        {"id": i, "amount": a, "qty": q, "price": 0.5, "note": o, "ok": k}
        for i in ("a", "b", "c")
        for a in (None, 0, 2)
        for q in (0, 1, 2)
        for o in (None, "a")
        for k in (None, True, False)
    ]
    agree(declared, fs.parse(text), rows)


def test_a_deeply_nested_check_is_refused_quickly() -> None:
    """The size limit bounds the work, not just the output."""
    import time

    m = model({"a": ("long", False), "b": ("long", False)})
    text = "a + b"
    for _ in range(14):
        text = f"COALESCE({text}, b) + b"
    start = time.monotonic()
    with pytest.raises(fs.CelUntranslatable, match="longer than"):
        fs.render_cel(fs.parse(f"{text} > 0"), m)
    assert time.monotonic() - start < 1.0


def test_integer_overflow_fails_the_record() -> None:
    """A valid row whose intermediate result overflows int64 is refused (fail-safe)."""
    m = model({"a": ("long", True)})
    expr = fs.render_cel(fs.parse("a * 4 > 0"), m)
    with pytest.raises(Exception):  # noqa: B017 - cel-python's own error type
        evaluate_cel(expr, {"a": 2**62})
