# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The canonical text of a FLUID SQL/1 tree (EXPR-SPEC-2 §11's canonical form).

Keywords and function names in upper case; names as written, double-quoted only
where the grammar requires it (a keyword, or not ``[A-Za-z_][A-Za-z0-9_]*``);
one space around a binary operator and after a comma; the fewest parentheses
the grammar needs; ``!=`` written ``<>``; and ``ROUND``'s default mode left out.
``parse(canonical(parse(x)))`` is ``parse(x)``, so a canonical text is a fixed
point.
"""

from __future__ import annotations

import re

from . import _nodes as n
from ._lexer import KEYWORDS

_BARE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# Binding levels, loosest first: OR, AND, NOT, the predicates, + -, * /, unary -,
# and the primaries (literals, names, calls, CASE).
_OR, _AND, _NOT, _PRED, _ADD, _MUL, _NEG, _PRIMARY = range(1, 9)


def canonical(node: n.Node) -> str:
    """The canonical text of ``node``."""
    return _text(node, _OR)


def quote_name(name: str) -> str:
    """``name`` as FLUID SQL/1 spells it: bare when it can be, else double-quoted."""
    if _BARE_NAME.match(name) and name.upper() not in KEYWORDS:
        return name
    return '"' + name.replace('"', '""') + '"'


def quote_string(value: str) -> str:
    """``value`` as a FLUID SQL/1 string literal."""
    return "'" + value.replace("'", "''") + "'"


def _level(node: n.Node) -> int:
    if isinstance(node, n.Or):
        return _OR
    if isinstance(node, n.And):
        return _AND
    if isinstance(node, n.Not):
        return _NOT
    if isinstance(node, (n.Compare, n.In, n.Between, n.IsNull, n.DistinctFrom)):
        return _PRED
    if isinstance(node, n.Arith):
        return _ADD if node.op in "+-" else _MUL
    if isinstance(node, n.Neg):
        return _NEG
    return _PRIMARY


def _text(node: n.Node, at_least: int) -> str:
    """``node``'s text, parenthesized when it binds looser than ``at_least``."""
    text = _bare(node)
    return f"({text})" if _level(node) < at_least else text


def _bare(node: n.Node) -> str:
    if isinstance(node, n.Number):
        return node.text
    if isinstance(node, n.String):
        return quote_string(node.value)
    if isinstance(node, n.Bool):
        return "TRUE" if node.value else "FALSE"
    if isinstance(node, n.Null):
        return "NULL"
    if isinstance(node, n.Date):
        return f"DATE {quote_string(node.text)}"
    if isinstance(node, n.Column):
        if node.relationship is None:
            return quote_name(node.name)
        return f"{quote_name(node.relationship)}.{quote_name(node.name)}"
    if isinstance(node, n.Or):
        return f"{_text(node.left, _OR)} OR {_text(node.right, _AND)}"
    if isinstance(node, n.And):
        return f"{_text(node.left, _AND)} AND {_text(node.right, _NOT)}"
    if isinstance(node, n.Not):
        return f"NOT {_text(node.operand, _NOT)}"
    if isinstance(node, n.Compare):
        return f"{_text(node.left, _ADD)} {node.op} {_text(node.right, _ADD)}"
    if isinstance(node, n.In):
        items = ", ".join(_bare(item) for item in node.items)
        keyword = "NOT IN" if node.negated else "IN"
        return f"{_text(node.left, _ADD)} {keyword} ({items})"
    if isinstance(node, n.Between):
        keyword = "NOT BETWEEN" if node.negated else "BETWEEN"
        return (
            f"{_text(node.left, _ADD)} {keyword} {_text(node.low, _ADD)} "
            f"AND {_text(node.high, _ADD)}"
        )
    if isinstance(node, n.IsNull):
        return f"{_text(node.operand, _ADD)} IS {'NOT ' if node.negated else ''}NULL"
    if isinstance(node, n.DistinctFrom):
        middle = "IS NOT DISTINCT FROM" if node.negated else "IS DISTINCT FROM"
        return f"{_text(node.left, _ADD)} {middle} {_text(node.right, _ADD)}"
    if isinstance(node, n.Arith):
        own = _ADD if node.op in "+-" else _MUL
        return f"{_text(node.left, own)} {node.op} {_text(node.right, own + 1)}"
    if isinstance(node, n.Neg):
        return f"-{_text(node.operand, _PRIMARY)}"
    if isinstance(node, n.Case):
        parts = ["CASE"]
        for when, then in node.whens:
            parts.append(f"WHEN {_text(when, _OR)} THEN {_text(then, _OR)}")
        if node.else_ is not None:
            parts.append(f"ELSE {_text(node.else_, _OR)}")
        parts.append("END")
        return " ".join(parts)
    if isinstance(node, n.Coalesce):
        return "COALESCE(" + ", ".join(_text(arg, _OR) for arg in node.args) + ")"
    if isinstance(node, n.Round):
        mode = "" if node.mode == n.DEFAULT_ROUND_MODE else f", {quote_string(node.mode)}"
        return f"ROUND({_text(node.arg, _ADD)}, {node.scale}{mode})"
    if isinstance(node, n.DateDiff):
        name = n.DATE_DIFF_FUNCTIONS[node.unit]
        return f"{name}({_text(node.from_, _ADD)}, {_text(node.to, _ADD)})"
    raise TypeError(f"not a FLUID SQL/1 node: {node!r}")
