# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The syntax tree of a FLUID SQL/1 text, and its JSON form.

One node per construct of the grammar in ``RFC-data-rules.md`` §2.1. The JSON
form (:func:`to_json`) is the ``tree`` of ``fluid-sql-vectors.json``, which
flux-expr/2 reproduces node for node, so its shape is part of the language:

=================  =====================================================
``number``         ``value``: the digits as written (``"1.50"``); an IN
                   item may carry a leading ``-``
``string``         ``value``: the text, NFC-normalized
``bool``           ``value``: ``true`` or ``false``
``null``           no field
``date``           ``value``: ``"YYYY-MM-DD"``
``column``         ``name``, and ``relationship`` for ``rel.col``
``neg``            ``operand``
``arith``          ``op`` (``+ - * /``), ``left``, ``right``
``compare``        ``op`` (``= <> < <= > >=``; ``!=`` reads as ``<>``)
``in``             ``left``, ``items`` (literals), ``negated``
``between``        ``left``, ``low``, ``high``, ``negated``
``is_null``        ``operand``, ``negated`` (``IS NOT NULL``)
``distinct_from``  ``left``, ``right``, ``negated`` (``IS NOT DISTINCT FROM``)
``not``            ``operand``
``and``, ``or``    ``left``, ``right`` (left-associative)
``case``           ``whens``: ``[{when, then}]``, and ``else`` when written
``coalesce``       ``args``
``round``          ``arg``, ``scale`` (its digits, a string), ``mode``
                   (``HALF_AWAY_FROM_ZERO`` or ``HALF_TO_EVEN``)
``date_diff``      ``unit`` (``day``, ``month`` or ``year``), ``from``, ``to``
=================  =====================================================

Parentheses leave no node. Positions (``pos``, an index into the NFC text) and
nesting depths are kept on the Python nodes for messages and limits; neither is
part of a node's equality or of its JSON form.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional, Tuple

#: The rounding mode ``ROUND(x, s)`` uses when none is written (founder pick, 2026-11-16).
DEFAULT_ROUND_MODE = "HALF_AWAY_FROM_ZERO"
ROUND_MODES = (DEFAULT_ROUND_MODE, "HALF_TO_EVEN")


@dataclass(frozen=True)
class Node:
    """A node; ``pos`` and ``depth`` are bookkeeping, never compared."""

    pos: int = field(default=0, compare=False, repr=False, kw_only=True)
    depth: int = field(default=1, compare=False, repr=False, kw_only=True)

    def children(self) -> Iterator["Node"]:
        """The nodes directly inside this one, in text order."""
        return iter(())


@dataclass(frozen=True)
class Number(Node):
    text: str

    @property
    def scale(self) -> int:
        """The digits after the point: the literal's static scale."""
        return len(self.text.partition(".")[2])


@dataclass(frozen=True)
class String(Node):
    value: str


@dataclass(frozen=True)
class Bool(Node):
    value: bool


@dataclass(frozen=True)
class Null(Node):
    pass


@dataclass(frozen=True)
class Date(Node):
    text: str


@dataclass(frozen=True)
class Column(Node):
    name: str
    relationship: Optional[str] = None


@dataclass(frozen=True)
class Neg(Node):
    operand: Node

    def children(self) -> Iterator[Node]:
        yield self.operand


@dataclass(frozen=True)
class Arith(Node):
    op: str
    left: Node
    right: Node

    def children(self) -> Iterator[Node]:
        yield self.left
        yield self.right


@dataclass(frozen=True)
class Compare(Node):
    op: str
    left: Node
    right: Node

    def children(self) -> Iterator[Node]:
        yield self.left
        yield self.right


@dataclass(frozen=True)
class In(Node):
    left: Node
    items: Tuple[Node, ...]
    negated: bool = False

    def children(self) -> Iterator[Node]:
        yield self.left
        yield from self.items


@dataclass(frozen=True)
class Between(Node):
    left: Node
    low: Node
    high: Node
    negated: bool = False

    def children(self) -> Iterator[Node]:
        yield self.left
        yield self.low
        yield self.high


@dataclass(frozen=True)
class IsNull(Node):
    operand: Node
    negated: bool = False

    def children(self) -> Iterator[Node]:
        yield self.operand


@dataclass(frozen=True)
class DistinctFrom(Node):
    left: Node
    right: Node
    negated: bool = False

    def children(self) -> Iterator[Node]:
        yield self.left
        yield self.right


@dataclass(frozen=True)
class Not(Node):
    operand: Node

    def children(self) -> Iterator[Node]:
        yield self.operand


@dataclass(frozen=True)
class And(Node):
    left: Node
    right: Node

    def children(self) -> Iterator[Node]:
        yield self.left
        yield self.right


@dataclass(frozen=True)
class Or(Node):
    left: Node
    right: Node

    def children(self) -> Iterator[Node]:
        yield self.left
        yield self.right


@dataclass(frozen=True)
class Case(Node):
    whens: Tuple[Tuple[Node, Node], ...]
    else_: Optional[Node] = None

    def children(self) -> Iterator[Node]:
        for when, then in self.whens:
            yield when
            yield then
        if self.else_ is not None:
            yield self.else_


@dataclass(frozen=True)
class Coalesce(Node):
    args: Tuple[Node, ...]

    def children(self) -> Iterator[Node]:
        yield from self.args


@dataclass(frozen=True)
class Round(Node):
    arg: Node
    scale: int
    mode: str = DEFAULT_ROUND_MODE

    def children(self) -> Iterator[Node]:
        yield self.arg


@dataclass(frozen=True)
class DateDiff(Node):
    unit: str  # "day", "month" or "year"
    from_: Node
    to: Node

    def children(self) -> Iterator[Node]:
        yield self.from_
        yield self.to


#: The function name of each date difference, by unit.
DATE_DIFF_FUNCTIONS = {"day": "DAYS_BETWEEN", "month": "MONTHS_BETWEEN", "year": "YEARS_BETWEEN"}


def walk(node: Node) -> Iterator[Node]:
    """``node`` and every node inside it, parents first, in text order."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(list(current.children())))


def to_json(node: Node) -> Dict[str, Any]:
    """The JSON form of ``node`` (the vectors' ``tree``)."""
    if isinstance(node, Number):
        return {"node": "number", "value": node.text}
    if isinstance(node, String):
        return {"node": "string", "value": node.value}
    if isinstance(node, Bool):
        return {"node": "bool", "value": node.value}
    if isinstance(node, Null):
        return {"node": "null"}
    if isinstance(node, Date):
        return {"node": "date", "value": node.text}
    if isinstance(node, Column):
        out: Dict[str, Any] = {"node": "column", "name": node.name}
        if node.relationship is not None:
            out = {"node": "column", "relationship": node.relationship, "name": node.name}
        return out
    if isinstance(node, Neg):
        return {"node": "neg", "operand": to_json(node.operand)}
    if isinstance(node, Arith):
        return {
            "node": "arith",
            "op": node.op,
            "left": to_json(node.left),
            "right": to_json(node.right),
        }
    if isinstance(node, Compare):
        return {
            "node": "compare",
            "op": node.op,
            "left": to_json(node.left),
            "right": to_json(node.right),
        }
    if isinstance(node, In):
        return {
            "node": "in",
            "left": to_json(node.left),
            "items": [to_json(item) for item in node.items],
            "negated": node.negated,
        }
    if isinstance(node, Between):
        return {
            "node": "between",
            "left": to_json(node.left),
            "low": to_json(node.low),
            "high": to_json(node.high),
            "negated": node.negated,
        }
    if isinstance(node, IsNull):
        return {"node": "is_null", "operand": to_json(node.operand), "negated": node.negated}
    if isinstance(node, DistinctFrom):
        return {
            "node": "distinct_from",
            "left": to_json(node.left),
            "right": to_json(node.right),
            "negated": node.negated,
        }
    if isinstance(node, Not):
        return {"node": "not", "operand": to_json(node.operand)}
    if isinstance(node, (And, Or)):
        return {
            "node": "and" if isinstance(node, And) else "or",
            "left": to_json(node.left),
            "right": to_json(node.right),
        }
    if isinstance(node, Case):
        out = {
            "node": "case",
            "whens": [{"when": to_json(w), "then": to_json(t)} for w, t in node.whens],
        }
        if node.else_ is not None:
            out["else"] = to_json(node.else_)
        return out
    if isinstance(node, Coalesce):
        return {"node": "coalesce", "args": [to_json(arg) for arg in node.args]}
    if isinstance(node, Round):
        return {
            "node": "round",
            "arg": to_json(node.arg),
            "scale": str(node.scale),
            "mode": node.mode,
        }
    if isinstance(node, DateDiff):
        return {
            "node": "date_diff",
            "unit": node.unit,
            "from": to_json(node.from_),
            "to": to_json(node.to),
        }
    raise TypeError(f"not a FLUID SQL/1 node: {node!r}")


def from_json(data: Any) -> Node:
    """The node a JSON tree describes (the inverse of :func:`to_json`)."""
    if not isinstance(data, dict) or not isinstance(data.get("node"), str):
        raise ValueError(f"not a FLUID SQL/1 tree: {data!r}")
    kind = data["node"]
    builders = {
        "number": lambda d: Number(d["value"]),
        "string": lambda d: String(d["value"]),
        "bool": lambda d: Bool(bool(d["value"])),
        "null": lambda d: Null(),
        "date": lambda d: Date(d["value"]),
        "column": lambda d: Column(d["name"], d.get("relationship")),
        "neg": lambda d: Neg(from_json(d["operand"])),
        "arith": lambda d: Arith(d["op"], from_json(d["left"]), from_json(d["right"])),
        "compare": lambda d: Compare(d["op"], from_json(d["left"]), from_json(d["right"])),
        "in": lambda d: In(
            from_json(d["left"]), tuple(from_json(i) for i in d["items"]), bool(d["negated"])
        ),
        "between": lambda d: Between(
            from_json(d["left"]), from_json(d["low"]), from_json(d["high"]), bool(d["negated"])
        ),
        "is_null": lambda d: IsNull(from_json(d["operand"]), bool(d["negated"])),
        "distinct_from": lambda d: DistinctFrom(
            from_json(d["left"]), from_json(d["right"]), bool(d["negated"])
        ),
        "not": lambda d: Not(from_json(d["operand"])),
        "and": lambda d: And(from_json(d["left"]), from_json(d["right"])),
        "or": lambda d: Or(from_json(d["left"]), from_json(d["right"])),
        "case": lambda d: Case(
            tuple((from_json(w["when"]), from_json(w["then"])) for w in d["whens"]),
            from_json(d["else"]) if "else" in d else None,
        ),
        "coalesce": lambda d: Coalesce(tuple(from_json(a) for a in d["args"])),
        "round": lambda d: Round(from_json(d["arg"]), int(d["scale"]), d["mode"]),
        "date_diff": lambda d: DateDiff(d["unit"], from_json(d["from"]), from_json(d["to"])),
    }
    if kind not in builders:
        raise ValueError(f"unknown FLUID SQL/1 node {kind!r}")
    return builders[kind](data)
