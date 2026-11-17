# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Render a FLUID SQL/1 predicate as a CEL expression (``RFC-data-rules.md`` §2.8).

The sibling of :func:`~fluid_build.fluid_sql.render_duckdb`: the CEL text is printed
from the tree, never from the raw text, and is TRUE exactly when the predicate is
TRUE. Where there is no exact CEL form, :func:`render_cel` raises
:class:`CelUntranslatable` and says why; it never approximates.

Where the text runs. Confluent's ``CelExecutor`` (data-contract rules,
``io.confluent.kafka.schemaregistry.rules.cel``) binds the record to the variable
``message``. ``CelUtils.toCelValue`` (schema-registry ``schema-rules``,
``CelUtils.java``) converts it: an Avro record becomes a Map, ``Utf8`` / ``CharSequence``
a String, ``Integer`` / ``Short`` / ``Byte`` a long, ``Float`` a double, and a null a CEL
``NullValue``. A ``CONDITION`` rule passes only when its expression is TRUE, and an
evaluation error raises a ``RuleException``: the record fails. The executor treats the
text before the FIRST ``;`` of an expression as a guard, so the expression emitted here
contains no ``;`` at all, not even inside a string literal (a check whose literal holds
one is refused).

The engine is Google's cel-java, which type-checks, so the text never mixes types and
never writes the ``null`` literal: every value is a pair, a presence predicate and a
value expression that is only read where the presence predicate holds. Where a branch
has no value (a ``CASE`` with no ``ELSE``, a NULL literal) the value is a placeholder of
the branch's own type (``0``, ``0.0``, ``""``, ``false``) that its presence predicate
makes unreachable. Every compound sub-expression is parenthesised wherever it is an
operand: CEL's relational operators are left-associative, so ``a > 0 == b > 1`` would
read ``((a > 0) == b) > 1``.

FLUID SQL/1's own semantics, not SQL's three-valued logic. FLUID SQL/1 is two-valued
(§2.5): a comparison with a NULL operand is FALSE and ``NOT`` flips it, so
``NOT (x = 1)`` is TRUE when ``x`` is NULL. :func:`~fluid_build.fluid_sql.evaluate` is
the authority, and the rendering agrees with it on every row: each predicate form is a
CEL boolean that is TRUE or FALSE and never null, so ``!``, ``&&`` and ``||`` only ever
see the two values. A value is read only after its presence test, and CEL's ``&&`` and
``||`` absorb an error when the other side decides the result (CEL spec, language
definition, "Logical Operators" and "Errors"). The null test of a column is
``type(x) == null_type`` and never ``x == null``: cel-python 0.5.0 has no heterogeneous
equality (``x != null`` raises for an int ``x``), and the type test is standard CEL that
cel-go and cel-java run too. A column the contract declares ``required`` is never null
in its Avro record, so it gets no null test.

Exact where the two agree, refused where they do not:

- columns are strings, the integer types, ``double`` / ``float`` and booleans;
  decimals (Avro carries them as bytes), dates and timestamps are refused;
- numbers are integer literals only (FLUID SQL/1 numbers are exact rationals and CEL
  doubles are not); an integer literal beside a double column is written ``N.0``;
  an int-typed operand and a double-typed one are never compared or mixed (CEL has no
  mixed comparison here, and ``double(x)`` rounds above 2^53 where FLUID compares exactly);
- ``/``, arithmetic on a double column, ``ROUND``, the date differences and ``DATE``
  literals are refused (``Checked.division``, ``Checked.float_arithmetic``);
- a name through a relationship is refused: a record holds one row;
- a text longer than :data:`MAX_CEL_CHARS` is refused, and the limit is enforced while the
  text is built, so a deeply nested check costs a bounded amount of work.

Integer arithmetic stays, with one known consequence: FLUID SQL/1 integers are unbounded
and CEL ``int`` is 64-bit, and CEL raises on an overflow of ANY intermediate result. A
valid row whose intermediate ``+``, ``-`` or ``*`` result overflows int64 is therefore
refused by the rule, though FLUID SQL/1 evaluates it. That fails safe: an error fails the
record, so such a row is never wrongly accepted.

Borrowed: the CEL language definition (github.com/google/cel-spec, ``doc/langdef.md``:
logical operators and errors, ``null_type``); Confluent's data-contract rules
documentation, ``CelExecutor`` and ``CelUtils.java`` for the ``message`` binding, the value
conversion and the ``;`` guard split; :mod:`fluid_build.fluid_sql._render` for the shape of
a renderer. The differential proof is ``tests/fluid_sql/test_render_cel.py``: cel-python
evaluates the rendering against :func:`~fluid_build.fluid_sql.evaluate` on generated rows
(a test-only dependency).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from . import _nodes as n
from ._checker import PREDICATE, check
from ._types import BOOL, NUMBER, STRING, Model

#: The largest text emitted. A value is written into its presence test and its use, so
#: nested ``CASE`` and ``COALESCE`` grow fast; the limit is checked on every piece built.
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
_BLANK = {"int": "0", "double": "0.0", "string": '""', "bool": "false"}
_ABSENT_TEST = re.compile(r"\(type\((.*)\) != null_type\)\Z")


class CelUntranslatable(ValueError):
    """A predicate with no exact CEL form; ``reason`` says why, in words for a contract author."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _cap(text: str) -> str:
    if len(text) > MAX_CEL_CHARS:
        raise CelUntranslatable(
            f"its CEL text is longer than {MAX_CEL_CHARS:,} characters: split the check"
        )
    return text


@dataclass(frozen=True)
class _V:
    """A rendered value: ``pres``, CEL that is TRUE when it has a value (not NULL), and
    ``val``, the CEL of the value itself, read only where ``pres`` holds. Both are atoms
    (a literal, a name, a call or a parenthesised expression)."""

    pres: str
    val: str

    def __post_init__(self) -> None:
        _cap(self.pres)
        _cap(self.val)


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
    return out


# -- folding: constant-true and constant-false parts are dropped ---------------------


def _and(*parts: str) -> str:
    kept = [p for p in parts if p != "true"]
    if "false" in kept:
        return "false"
    if not kept:
        return "true"
    return kept[0] if len(kept) == 1 else _cap("(" + " && ".join(kept) + ")")


def _or(*parts: str) -> str:
    kept = [p for p in parts if p != "false"]
    if "true" in kept:
        return "true"
    if not kept:
        return "false"
    return kept[0] if len(kept) == 1 else _cap("(" + " || ".join(kept) + ")")


def _not(part: str) -> str:
    if part == "true":
        return "false"
    if part == "false":
        return "true"
    swapped = _ABSENT_TEST.match(part)
    if swapped and swapped.group(1).count("(") == swapped.group(1).count(")"):
        return f"(type({swapped.group(1)}) == null_type)"
    return _cap(f"!{part}" if part.startswith("(") else f"!({part})")


def _ternary(condition: str, then: str, otherwise: str) -> str:
    if condition == "true" or then == otherwise:
        return then
    if condition == "false":
        return otherwise
    return _cap(f"({condition} ? {then} : {otherwise})")


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
    return _cap('"' + "".join(out) + '"')


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
        """The class the literals among ``nodes`` take: that of the typed operand beside them."""
        found = {c for c in (self.cls(node) for node in nodes) if c != "lit"}
        if {"int", "double"} <= found:
            raise CelUntranslatable(
                "it compares an integer-typed value with a double-typed one, which CEL does "
                "not do exactly"
            )
        return found.pop() if found else None

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
            items = ", ".join(self.value(item, want).val for item in node.items)
            found = _cap(f"({left.val} in [{items}])")
            return _and(left.pres, _not(found) if node.negated else found)
        if isinstance(node, n.Between):
            want = self.want(node.left, node.low, node.high)
            x = self.value(node.left, want)
            low, high = self.value(node.low, want), self.value(node.high, want)
            if node.negated:
                return _or(self.compare("<", x, low), self.compare(">", x, high))
            return _and(self.compare(">=", x, low), self.compare("<=", x, high))
        if isinstance(node, n.IsNull):
            operand = self.value(node.operand, self.want(node.operand))
            return operand.pres if node.negated else _not(operand.pres)
        if isinstance(node, n.DistinctFrom):
            want = self.want(node.left, node.right)
            left, right = self.value(node.left, want), self.value(node.right, want)
            same = _or(
                _and(_not(left.pres), _not(right.pres)),
                _and(left.pres, right.pres, _cap(f"({left.val} == {right.val})")),
            )
            return same if node.negated else _not(same)
        value = self.value(node, "bool")  # a bool column, a bool CASE or COALESCE
        return _and(value.pres, value.val)

    def compare(self, op: str, left: _V, right: _V) -> str:
        return _and(left.pres, right.pres, _cap(f"({left.val} {_OPERATORS[op]} {right.val})"))

    # -- values ------------------------------------------------------------------

    def value(self, node: n.Node, want: Optional[str]) -> _V:  # noqa: C901 - one per construct
        if isinstance(node, _PREDICATES):
            return _V("true", self.predicate(node))
        if isinstance(node, n.Number):
            number = self.integer(node)
            if want == "double":
                if abs(number) > _EXACT_DOUBLE_INT:
                    raise CelUntranslatable(
                        f"the number {node.text} is not exactly a CEL double (beyond 2^53)"
                    )
                text = f"{number}.0"
            else:
                text = str(number)
            return _V("true", f"({text})" if number < 0 else text)
        if isinstance(node, n.String):
            return _V("true", _string(node.value))
        if isinstance(node, n.Bool):
            return _V("true", "true" if node.value else "false")
        if isinstance(node, n.Null):
            return _V("false", _BLANK.get(want or "int", "0"))
        if isinstance(node, n.Column):
            self.column_class(node)
            column = self.model.columns[node.name]
            name = self.column(node.name)
            return _V("true" if column.required else f"(type({name}) != null_type)", name)
        if isinstance(node, n.Neg):
            operand = self.value(node.operand, want)
            return _V(operand.pres, _cap(f"(-{operand.val})"))
        if isinstance(node, n.Arith):
            self.cls(node)  # refuses a division and a double operand
            left, right = self.value(node.left, "int"), self.value(node.right, "int")
            return _V(_and(left.pres, right.pres), _cap(f"({left.val} {node.op} {right.val})"))
        if isinstance(node, n.Case):
            return self.case(node, want)
        if isinstance(node, n.Coalesce):
            return self.coalesce(node, want)
        self.cls(node)  # Round, DateDiff, Date: refused with the reason
        raise TypeError(f"not a FLUID SQL/1 node: {node!r}")  # pragma: no cover

    def branch_class(self, nodes: List[n.Node], outer: Optional[str]) -> str:
        merged = self.merge(nodes)
        return merged if merged != "lit" else (outer or "int")

    def case(self, node: n.Case, outer: Optional[str]) -> _V:
        values = [then for _when, then in node.whens]
        if node.else_ is not None:
            values.append(node.else_)
        want = self.branch_class(values, outer)
        tail = _V("false", _BLANK[want]) if node.else_ is None else self.value(node.else_, want)
        for when, then in reversed(node.whens):
            condition = self.predicate(when)
            branch = self.value(then, want)
            tail = _V(
                _ternary(condition, branch.pres, tail.pres),
                _ternary(condition, branch.val, tail.val),
            )
        return tail

    def coalesce(self, node: n.Coalesce, outer: Optional[str]) -> _V:
        want = self.branch_class(list(node.args), outer)
        args = [
            v for arg in node.args if not isinstance(arg, n.Null) for v in [self.value(arg, want)]
        ]
        if not args:
            return _V("false", _BLANK[want])
        tail = args[-1].val
        for arg in reversed(args[:-1]):
            tail = _ternary(arg.pres, arg.val, tail)
        return _V(_or(*(a.pres for a in args)), tail)

    def column(self, name: str) -> str:
        if _IDENT.match(name) and name not in _CEL_RESERVED:
            return f"{self.record}.{name}"
        return f"{self.record}[{_string(name)}]"
