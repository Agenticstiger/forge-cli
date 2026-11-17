# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Render a FLUID SQL/1 predicate as a CEL expression (``RFC-data-rules.md`` §2.8).

The sibling of :func:`~fluid_build.fluid_sql.render_duckdb`: the CEL text is printed
from the tree, never from the raw text, and is TRUE exactly when the predicate is
TRUE. Where there is no exact CEL form, :func:`render_cel` raises
:class:`CelUntranslatable` and says why; it never approximates.

Where the text runs. Confluent's ``CelExecutor`` (data-contract rules,
``io.confluent.kafka.schemaregistry.rules.cel``) binds the record to the variable
``message`` (an Avro record is a map; a NULL Avro field reads as CEL ``null``). A
``CONDITION`` rule passes only when its expression is TRUE, and an evaluation error
raises a ``RuleException``: the record fails. The executor treats the text before the
FIRST ``;`` of an expression as a guard, so the expression emitted here contains no
``;`` at all, not even inside a string literal (a check whose literal holds one is
refused).

FLUID SQL/1's own semantics, not SQL's three-valued logic. FLUID SQL/1 is two-valued
(§2.5): a comparison with a NULL operand is FALSE and ``NOT`` flips it, so
``NOT (x = 1)`` is TRUE when ``x`` is NULL. :func:`~fluid_build.fluid_sql.evaluate` is
the authority, and the rendering agrees with it on every row: each predicate form is a
CEL boolean that is TRUE or FALSE and never null, so ``!``, ``&&`` and ``||`` only ever
see the two values. A value that may be NULL (a nullable column, a ``CASE`` with no
``ELSE``, ``a + b``) is tested before it is used; CEL's ``&&`` and ``||`` absorb an
error when the other side decides the result (CEL spec, language definition, "Logical
Operators" and "Errors"), so ``type(x) != null_type && x > 1`` is FALSE for a null
``x``. The null test is ``type(x) == null_type`` and never ``x == null``: cel-python
0.5.0 has no heterogeneous equality (``x != null`` raises for an int ``x``), and the
type test is standard CEL that cel-go and cel-java run too. A column the contract
declares ``required`` is never null in its Avro record, so it gets no null test.

Exact where the two agree, refused where they do not:

- columns are strings, the integer types, ``double`` / ``float`` and booleans;
  decimals (Avro carries them as bytes), dates and timestamps are refused;
- numbers are integer literals only (FLUID SQL/1 numbers are exact rationals and CEL
  doubles are not); an integer literal beside a double column is written ``N.0``;
  an int-typed operand and a double-typed one are never compared or mixed (CEL has no
  mixed comparison here, and ``double(x)`` rounds above 2^53 where FLUID compares exactly);
- ``/``, arithmetic on a double column, ``ROUND``, the date differences and ``DATE``
  literals are refused (``Checked.division``, ``Checked.float_arithmetic``);
- a name through a relationship is refused: a record holds one row.

The one known divergence: FLUID SQL/1 integers are unbounded and CEL ``int`` is 64-bit,
so a ``+``, ``-`` or ``*`` that overflows raises in CEL. The record then FAILS the rule
(an error is a failure, never a pass), so an overflowing row is refused, never wrongly
accepted.

Borrowed: the CEL language definition (github.com/google/cel-spec, ``doc/langdef.md``:
logical operators and errors, ``null_type``); Confluent's data-contract rules
documentation and ``CelExecutor`` for the ``message`` binding and the ``;`` guard split;
:mod:`fluid_build.fluid_sql._render` for the shape of a renderer. The differential proof
is ``tests/fluid_sql/test_render_cel.py``: cel-python evaluates the rendering against
:func:`~fluid_build.fluid_sql.evaluate` on generated rows (a test-only dependency).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from . import _nodes as n
from ._checker import PREDICATE, check
from ._types import BOOL, NUMBER, STRING, Model

#: The largest text emitted: a value tested for NULL is written twice, so nested
#: ``CASE`` expressions grow quickly, and a rule that large is no rule to keep.
MAX_CEL_CHARS = 8000

_INT64_MAX = 2**63 - 1
_EXACT_DOUBLE_INT = 2**53
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CEL_RESERVED = frozenset(
    {"as", "break", "const", "continue", "else", "false", "for", "function", "if", "import"}
    | {"in", "let", "loop", "namespace", "null", "package", "return", "true", "var", "void"}
    | {"while"}
)
_PREDICATES = (n.Compare, n.In, n.Between, n.IsNull, n.DistinctFrom, n.Not, n.And, n.Or)
_OPERATORS = {"=": "==", "<>": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}


class CelUntranslatable(ValueError):
    """A predicate with no exact CEL form; ``reason`` says why, in words for a contract author."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _V:
    """A rendered value: its CEL text, whether it may be null, and its class
    (``int``, ``double``, ``string``, ``bool``, or ``null`` for the NULL literal)."""

    expr: str
    nullable: bool
    cls: str


_NULL = _V("null", True, "null")


def render_cel(node: n.Node, model: Model, *, record: str = "message") -> str:
    """A CEL expression over ``record`` that is TRUE exactly when ``node`` is TRUE for it.

    ``node`` is a FLUID SQL/1 predicate of ``model`` (one expose). FALSE and NULL both
    give ``false``, and the expression raises on no row of the declared column types
    (apart from the integer overflow the module docstring names). Raises
    :class:`CelUntranslatable` when there is no exact CEL form, and
    :class:`~fluid_build.fluid_sql.FluidSqlError` when ``node`` does not check.
    """
    if not _IDENT.match(record) or record in _CEL_RESERVED:
        raise ValueError(f"not a CEL variable name: {record!r}")
    checked = check(node, model, PREDICATE)
    if checked.division:
        raise CelUntranslatable(
            "it divides, and a quotient is exact in FLUID SQL/1 but approximate in CEL"
        )
    if checked.float_arithmetic:
        raise CelUntranslatable(
            "it does arithmetic on a double column, which is exact in FLUID SQL/1 and "
            "binary in CEL"
        )
    out = _Cel(model, record).predicate(node)
    if ";" in out:  # every ';' source is refused where it is written; never emit one
        raise CelUntranslatable(
            "its CEL text holds a ';', which Confluent reads as the end of a guard"
        )
    if len(out) > MAX_CEL_CHARS:
        raise CelUntranslatable(
            f"its CEL text is {len(out):,} characters long (at most {MAX_CEL_CHARS:,}): "
            "split the check"
        )
    return out


# -- folding: constant-true and constant-false parts are dropped ---------------------


def _and(*parts: str) -> str:
    kept = [p for p in parts if p != "true"]
    if "false" in kept:
        return "false"
    if not kept:
        return "true"
    return kept[0] if len(kept) == 1 else "(" + " && ".join(kept) + ")"


def _or(*parts: str) -> str:
    kept = [p for p in parts if p != "false"]
    if "true" in kept:
        return "true"
    if not kept:
        return "false"
    return kept[0] if len(kept) == 1 else "(" + " || ".join(kept) + ")"


def _not(part: str) -> str:
    if part == "true":
        return "false"
    if part == "false":
        return "true"
    return f"!({part})"


def _present(value: _V) -> str:
    """CEL that is TRUE when ``value`` is not null."""
    if value.cls == "null":
        return "false"
    return f"type({value.expr}) != null_type" if value.nullable else "true"


def _absent(value: _V) -> str:
    """CEL that is TRUE when ``value`` is null."""
    if value.cls == "null":
        return "true"
    return f"type({value.expr}) == null_type" if value.nullable else "false"


def _string(text: str) -> str:
    """A CEL double-quoted string literal."""
    if ";" in text:
        raise CelUntranslatable(
            "a text holds a ';', which Confluent reads as the end of a rule's guard"
        )
    out: List[str] = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


class _Cel:
    def __init__(self, model: Model, record: str) -> None:
        self.model = model
        self.record = record

    # -- classes -----------------------------------------------------------------

    def column_class(self, node: n.Column) -> str:
        if node.relationship is not None:
            raise CelUntranslatable(
                f"it reads {node.relationship}.{node.name} through a relationship, and a "
                "record holds one row"
            )
        column = self.model.columns.get(node.name)
        if column is None or column.type is None:
            raise CelUntranslatable(f"{node.name} has no FLUID SQL/1 type")
        kind = column.type.type.kind
        if kind == STRING:
            return "string"
        if kind == BOOL:
            return "bool"
        if kind == NUMBER:
            if column.type.is_float:
                return "double"
            if (
                column.type.declared_scale
                and column.type.precision is None
                and column.type.type.scale == 0
            ):
                return "int"
            raise CelUntranslatable(
                f"column {node.name} is a decimal: Avro carries it as bytes, which a CEL rule "
                "cannot compare exactly"
            )
        raise CelUntranslatable(
            f"column {node.name} is a {kind}: Avro carries it as a number of days or ticks, "
            "which a CEL rule cannot compare to a FLUID SQL/1 value exactly"
        )

    def cls(self, node: n.Node) -> str:  # noqa: C901 - one branch per construct
        """``int``, ``double``, ``string``, ``bool`` or ``lit`` (a literal that takes the
        class of what it sits beside); refuses what has no CEL form."""
        if isinstance(node, n.Number):
            self.integer(node)
            return "lit"
        if isinstance(node, n.Null):
            return "lit"
        if isinstance(node, n.String):
            return "string"
        if isinstance(node, (n.Bool, *_PREDICATES)):
            return "bool"
        if isinstance(node, n.Column):
            return self.column_class(node)
        if isinstance(node, n.Neg):
            return self.cls(node.operand)
        if isinstance(node, n.Arith):
            if node.op == "/":
                raise CelUntranslatable("it divides (Checked.division)")
            for operand in (node.left, node.right):
                if self.cls(operand) == "double":
                    raise CelUntranslatable(
                        "it does arithmetic on a double column (Checked.float_arithmetic)"
                    )
            return "int"
        if isinstance(node, n.Case):
            values = [then for _when, then in node.whens]
            if node.else_ is not None:
                values.append(node.else_)
            return self.merge(values)
        if isinstance(node, n.Coalesce):
            return self.merge(list(node.args))
        if isinstance(node, n.Round):
            raise CelUntranslatable("it uses ROUND, which is exact in FLUID SQL/1 only")
        if isinstance(node, n.DateDiff):
            raise CelUntranslatable(
                f"it uses {n.DATE_DIFF_FUNCTIONS[node.unit]}, and a CEL rule has no exact date "
                "arithmetic"
            )
        if isinstance(node, n.Date):
            raise CelUntranslatable("it holds a DATE literal, which a CEL rule cannot compare")
        raise TypeError(f"not a FLUID SQL/1 node: {node!r}")  # pragma: no cover

    def merge(self, nodes: List[n.Node]) -> str:
        """The class of the values of a ``CASE`` or ``COALESCE``."""
        found = {c for c in (self.cls(node) for node in nodes) if c != "lit"}
        if {"int", "double"} <= found:
            raise CelUntranslatable(
                "it mixes an integer-typed value and a double-typed one, which CEL does not "
                "compare exactly"
            )
        return found.pop() if found else "lit"

    def want(self, *nodes: n.Node) -> Optional[str]:
        """The class an integer literal among ``nodes`` takes: ``double`` beside a double."""
        found = {c for c in (self.cls(node) for node in nodes) if c != "lit"}
        if {"int", "double"} <= found:
            raise CelUntranslatable(
                "it compares an integer-typed value with a double-typed one, which CEL does "
                "not do exactly"
            )
        return "double" if "double" in found else None

    def integer(self, node: n.Number) -> int:
        if "." in node.text:
            raise CelUntranslatable(
                f"the number {node.text} is not an integer: FLUID SQL/1 numbers are exact and "
                "CEL doubles are not"
            )
        number = int(node.text)
        if abs(number) > _INT64_MAX:
            raise CelUntranslatable(f"the number {node.text} does not fit a 64-bit integer")
        return number

    # -- predicates --------------------------------------------------------------

    def predicate(self, node: n.Node) -> str:  # noqa: C901 - one branch per construct
        if isinstance(node, n.Bool):
            return "true" if node.value else "false"
        if isinstance(node, n.Null):
            return "false"
        if isinstance(node, n.Not):
            return _not(self.predicate(node.operand))
        if isinstance(node, n.And):
            return _and(self.predicate(node.left), self.predicate(node.right))
        if isinstance(node, n.Or):
            return _or(self.predicate(node.left), self.predicate(node.right))
        if isinstance(node, n.Compare):
            want = self.want(node.left, node.right)
            return self.compare(node.op, self.value(node.left, want), self.value(node.right, want))
        if isinstance(node, n.In):
            want = self.want(node.left, *node.items)
            left = self.value(node.left, want)
            items = ", ".join(self.value(item, want).expr for item in node.items)
            found = f"{left.expr} in [{items}]"
            return _and(_present(left), _not(found) if node.negated else found)
        if isinstance(node, n.Between):
            want = self.want(node.left, node.low, node.high)
            x = self.value(node.left, want)
            low, high = self.value(node.low, want), self.value(node.high, want)
            if node.negated:
                return _or(self.compare("<", x, low), self.compare(">", x, high))
            return _and(self.compare(">=", x, low), self.compare("<=", x, high))
        if isinstance(node, n.IsNull):
            operand = self.value(node.operand, None)
            return _present(operand) if node.negated else _absent(operand)
        if isinstance(node, n.DistinctFrom):
            want = self.want(node.left, node.right)
            left, right = self.value(node.left, want), self.value(node.right, want)
            same = _or(
                _and(_absent(left), _absent(right)),
                _and(_present(left), _present(right), f"{left.expr} == {right.expr}"),
            )
            return same if node.negated else _not(same)
        value = self.value(node, None)  # a bool column, a bool CASE or COALESCE
        return _and(_present(value), value.expr)

    def compare(self, op: str, left: _V, right: _V) -> str:
        return _and(_present(left), _present(right), f"{left.expr} {_OPERATORS[op]} {right.expr}")

    # -- values ------------------------------------------------------------------

    def value(self, node: n.Node, want: Optional[str]) -> _V:  # noqa: C901 - one per construct
        if isinstance(node, _PREDICATES):
            return _V(self.predicate(node), False, "bool")
        if isinstance(node, n.Number):
            number = self.integer(node)
            if want == "double":
                if abs(number) > _EXACT_DOUBLE_INT:
                    raise CelUntranslatable(
                        f"the number {node.text} is not exactly a CEL double (beyond 2^53)"
                    )
                text = f"{number}.0"
                return _V(f"({text})" if number < 0 else text, False, "double")
            return _V(f"({number})" if number < 0 else str(number), False, "int")
        if isinstance(node, n.String):
            return _V(_string(node.value), False, "string")
        if isinstance(node, n.Bool):
            return _V("true" if node.value else "false", False, "bool")
        if isinstance(node, n.Null):
            return _NULL
        if isinstance(node, n.Column):
            cls = self.column_class(node)
            column = self.model.columns[node.name]
            return _V(self.column(node.name), not column.required, cls)
        if isinstance(node, n.Neg):
            operand = self.value(node.operand, want)
            if operand.cls == "null":
                return _NULL
            if not operand.nullable:
                return _V(f"(-{operand.expr})", False, operand.cls)
            return _V(f"({_present(operand)} ? -{operand.expr} : null)", True, operand.cls)
        if isinstance(node, n.Arith):
            self.cls(node)  # refuses a division and a double operand
            left, right = self.value(node.left, "int"), self.value(node.right, "int")
            expr = f"({left.expr} {node.op} {right.expr})"
            guard = _and(_present(left), _present(right))
            if guard == "true":
                return _V(expr, False, "int")
            if guard == "false":
                return _NULL
            return _V(f"({guard} ? {expr} : null)", True, "int")
        if isinstance(node, n.Case):
            return self.case(node, want)
        if isinstance(node, n.Coalesce):
            return self.coalesce(node, want)
        self.cls(node)  # Round, DateDiff, Date: refused with the reason
        raise TypeError(f"not a FLUID SQL/1 node: {node!r}")  # pragma: no cover

    def case(self, node: n.Case, outer: Optional[str]) -> _V:
        values = [then for _when, then in node.whens]
        if node.else_ is not None:
            values.append(node.else_)
        want = "double" if self.merge(values) == "double" else outer
        tail = _NULL if node.else_ is None else self.value(node.else_, want)
        for when, then in reversed(node.whens):
            condition = self.predicate(when)
            branch = self.value(then, want)
            cls = branch.cls if branch.cls != "null" else tail.cls
            nullable = branch.nullable or tail.nullable
            if condition == "true":
                tail = branch
            elif condition != "false":
                tail = _V(f"({condition} ? {branch.expr} : {tail.expr})", nullable, cls)
        return tail

    def coalesce(self, node: n.Coalesce, outer: Optional[str]) -> _V:
        want = "double" if self.merge(list(node.args)) == "double" else outer
        args = [v for v in (self.value(arg, want) for arg in node.args) if v.cls != "null"]
        if not args:
            return _NULL
        tail = args[-1]
        for arg in reversed(args[:-1]):
            if not arg.nullable:
                tail = arg  # nothing after it is ever reached
            else:
                tail = _V(f"({_present(arg)} ? {arg.expr} : {tail.expr})", tail.nullable, tail.cls)
        return tail

    def column(self, name: str) -> str:
        if _IDENT.match(name) and name not in _CEL_RESERVED:
            return f"{self.record}.{name}"
        return f"{self.record}[{_string(name)}]"
