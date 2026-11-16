# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Parse a FLUID SQL/1 text: exactly the grammar of ``RFC-data-rules.md`` §2.1.

A recursive-descent parser with one function per grammar level, loosest
first::

    expr      = or_expr
    or_expr   = and_expr { OR and_expr }
    and_expr  = not_expr { AND not_expr }
    not_expr  = { NOT } predicate
    predicate = value [ cmp_op value | [NOT] IN (...) | [NOT] BETWEEN value AND value
                       | IS [NOT] NULL | IS [NOT] DISTINCT FROM value ]
    value     = term { (+ | -) term }
    term      = factor { (* | /) factor }
    factor    = [ - ] primary
    primary   = literal | column_ref | call | case_expr | ( expr )

Comparisons do not chain, and a predicate is never an operand of another
without parentheses. A text the grammar does not derive is refused with reason
``syntax`` and the position of the first token that cannot be read.

**Depth.** A literal or a column reference is 1 deep; every other construct,
and every pair of parentheses, is one deeper than the deepest thing directly
inside it. A text deeper than 64 is refused (``too-deep``). The parser checks
the depth as it builds each node, and refuses on entry any parenthesis,
``NOT``, minus, call or ``CASE`` that would already put a leaf below 64 levels,
so its own recursion stays bounded.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
from typing import List, Optional, Tuple

from . import _nodes as n
from ._errors import FluidSqlError
from ._lexer import FUNCTIONS, MAX_DEPTH, Token, check_limits, normalize, tokenize

_CMP_OPS = frozenset({"=", "<>", "<", "<=", ">", ">="})
_DATE_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")

#: What to write instead of SQL a reader reaches for, by the word that starts it.
_HINTS = {
    "CAST": "FLUID SQL/1 has no CAST",
    "TRY_CAST": "FLUID SQL/1 has no CAST",
    "CONVERT": "FLUID SQL/1 has no CAST",
    "NVL": "write COALESCE(a, b)",
    "IFNULL": "write COALESCE(a, b)",
    "ISNULL": "write COALESCE(a, b), or x IS NULL",
    "NVL2": "write CASE WHEN x IS NOT NULL THEN a ELSE b END",
    "IFF": "write CASE WHEN c THEN a ELSE b END",
    "IF": "write CASE WHEN c THEN a ELSE b END",
    "DECODE": "write CASE WHEN ... THEN ... END",
    "NULLIF": "write CASE WHEN a = b THEN NULL ELSE a END",
    "ZEROIFNULL": "write COALESCE(x, 0)",
    "SUM": "an aggregate is not written inside an expression: declare it as aggregate",
    "COUNT": "an aggregate is not written inside an expression: declare it as aggregate",
    "MIN": "an aggregate is not written inside an expression: declare it as aggregate",
    "MAX": "an aggregate is not written inside an expression: declare it as aggregate",
    "AVG": "an aggregate is not written inside an expression: declare it as aggregate",
    "DATEDIFF": "write DAYS_BETWEEN, MONTHS_BETWEEN or YEARS_BETWEEN (whole units)",
    "DATE_DIFF": "write DAYS_BETWEEN, MONTHS_BETWEEN or YEARS_BETWEEN (whole units)",
    "DATE_SUB": "write DAYS_BETWEEN, MONTHS_BETWEEN or YEARS_BETWEEN (whole units)",
    "TIMESTAMPDIFF": "write DAYS_BETWEEN, MONTHS_BETWEEN or YEARS_BETWEEN (whole units)",
    "CURRENT_DATE": "FLUID SQL/1 never reads the clock: use a column or a DATE literal",
    "CURRENT_TIMESTAMP": "FLUID SQL/1 never reads the clock: use a column or a DATE literal",
    "NOW": "FLUID SQL/1 never reads the clock: use a column or a DATE literal",
    "GETDATE": "FLUID SQL/1 never reads the clock: use a column or a DATE literal",
    "SYSDATE": "FLUID SQL/1 never reads the clock: use a column or a DATE literal",
    "TODAY": "FLUID SQL/1 never reads the clock: use a column or a DATE literal",
    "SELECT": "FLUID SQL/1 has no subqueries",
    "EXISTS": "FLUID SQL/1 has no subqueries",
    "TIMESTAMP": "FLUID SQL/1 has no timestamp literals",
    "INTERVAL": "FLUID SQL/1 has no interval literals or date arithmetic",
    "LIKE": "FLUID SQL/1 has no pattern matching",
    "ILIKE": "FLUID SQL/1 has no pattern matching",
    "RLIKE": "FLUID SQL/1 has no pattern matching",
    "REGEXP": "FLUID SQL/1 has no pattern matching",
    "SIMILAR": "FLUID SQL/1 has no pattern matching",
    "GLOB": "FLUID SQL/1 has no pattern matching",
    "OVER": "windows are FLUX-only, never in a contract",
}
_STRING_FUNCTIONS = frozenset(
    {"LOWER", "UPPER", "TRIM", "LTRIM", "RTRIM", "SUBSTR", "SUBSTRING", "LENGTH", "LEN"}
    | {"CONCAT", "REPLACE", "LEFT", "RIGHT", "SPLIT_PART", "STRPOS", "POSITION"}
)
_NUMERIC_FUNCTIONS = frozenset(
    {"ABS", "LEAST", "GREATEST", "FLOOR", "CEIL", "CEILING", "TRUNC", "TRUNCATE"}
    | {"MOD", "POWER", "SQRT", "SIGN", "EXP", "LN", "LOG", "BROUND"}
)
_WINDOW_FUNCTIONS = frozenset(
    {"ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE"}
)
_OUTSIDE = "; SQL outside FLUID SQL/1 belongs in contract.quality[]"


def parse(text: str) -> n.Node:
    """The tree of ``text``, or :class:`FluidSqlError` with the reason it is refused."""
    if not isinstance(text, str):
        raise FluidSqlError("syntax", "a FLUID SQL/1 text is a string")
    source = normalize(text)
    check_limits(source)
    parser = _Parser(tokenize(source))
    if parser.peek().kind == "end":
        raise FluidSqlError("syntax", "the text is empty", 0)
    tree = parser.expr()
    end = parser.peek()
    if end.kind != "end":
        raise _unexpected(end, "the end of the expression")
    return tree


def _describe(token: Token) -> str:
    if token.kind == "end":
        return "the end of the text"
    if token.kind == "keyword":
        return f"the keyword {token.value}"
    if token.kind == "string":
        return f"the string {token.raw}"
    if token.kind == "number":
        return f"the number {token.raw}"
    if token.kind in ("name", "qname"):
        return f"the name {token.raw}"
    return f"'{token.raw}'"


def _unexpected(token: Token, expected: str) -> FluidSqlError:
    hint = ""
    if token.kind == "name" and token.value.upper() in _HINTS:
        hint = f": {_HINTS[token.value.upper()]}"
    return FluidSqlError(
        "syntax", f"expected {expected}, found {_describe(token)}{hint}{_OUTSIDE}", token.pos
    )


class _Parser:
    def __init__(self, tokens: List[Token]) -> None:
        self.tokens = tokens
        self.i = 0
        self.level = 0

    # -- token helpers -------------------------------------------------------

    def peek(self, ahead: int = 0) -> Token:
        return self.tokens[min(self.i + ahead, len(self.tokens) - 1)]

    def take(self) -> Token:
        token = self.peek()
        self.i += 1
        return token

    def at_keyword(self, *words: str, ahead: int = 0) -> bool:
        token = self.peek(ahead)
        return token.kind == "keyword" and token.value in words

    def at_op(self, *ops: str) -> bool:
        token = self.peek()
        return token.kind == "op" and token.value in ops

    def expect_op(self, op: str, what: str) -> Token:
        if not self.at_op(op):
            raise _unexpected(self.peek(), what)
        return self.take()

    def expect_keyword(self, word: str, what: str) -> Token:
        if not self.at_keyword(word):
            raise _unexpected(self.peek(), what)
        return self.take()

    # -- depth ---------------------------------------------------------------

    def enter(self, pos: int) -> None:
        self.level += 1
        if self.level >= MAX_DEPTH:
            raise FluidSqlError("too-deep", f"the text nests deeper than {MAX_DEPTH} levels", pos)

    def leave(self) -> None:
        self.level -= 1

    @staticmethod
    def make(node: n.Node) -> n.Node:
        depth = 1 + max((child.depth for child in node.children()), default=0)
        if depth > MAX_DEPTH:
            raise FluidSqlError(
                "too-deep", f"the text nests deeper than {MAX_DEPTH} levels", node.pos
            )
        return dataclasses.replace(node, depth=depth) if depth != node.depth else node

    # -- grammar -------------------------------------------------------------

    def expr(self) -> n.Node:
        left = self.and_expr()
        while self.at_keyword("OR"):
            pos = self.take().pos
            left = self.make(n.Or(left, self.and_expr(), pos=pos))
        return left

    def and_expr(self) -> n.Node:
        left = self.not_expr()
        while self.at_keyword("AND"):
            pos = self.take().pos
            left = self.make(n.And(left, self.not_expr(), pos=pos))
        return left

    def not_expr(self) -> n.Node:
        positions: List[int] = []
        while self.at_keyword("NOT"):
            token = self.take()
            self.enter(token.pos)
            positions.append(token.pos)
        operand = self.predicate()
        for pos in reversed(positions):
            operand = self.make(n.Not(operand, pos=pos))
            self.leave()
        return operand

    def predicate(self) -> n.Node:
        left = self.value()
        node = self.predicate_tail(left)
        if node is left:
            return left
        if self._at_predicate_operator():
            raise FluidSqlError(
                "syntax",
                f"comparisons do not chain: {_describe(self.peek())} follows a comparison "
                "(join two comparisons with AND)",
                self.peek().pos,
            )
        return node

    def _at_predicate_operator(self) -> bool:
        token = self.peek()
        if token.kind == "op" and token.value in _CMP_OPS:
            return True
        if self.at_keyword("IN", "BETWEEN", "IS"):
            return True
        return self.at_keyword("NOT") and self.at_keyword("IN", "BETWEEN", ahead=1)

    def predicate_tail(self, left: n.Node) -> n.Node:
        token = self.peek()
        if token.kind == "op" and token.value in _CMP_OPS:
            self.take()
            right = self.value()
            return self.make(n.Compare(token.value, left, right, pos=token.pos))
        negated = False
        if self.at_keyword("NOT") and self.at_keyword("IN", "BETWEEN", ahead=1):
            self.take()
            negated = True
        if self.at_keyword("IN"):
            pos = self.take().pos
            return self.make(n.In(left, self.in_items(), negated, pos=pos))
        if self.at_keyword("BETWEEN"):
            pos = self.take().pos
            low = self.value()
            self.expect_keyword("AND", "AND between the bounds of BETWEEN")
            high = self.value()
            return self.make(n.Between(left, low, high, negated, pos=pos))
        if self.at_keyword("IS"):
            pos = self.take().pos
            is_not = False
            if self.at_keyword("NOT"):
                self.take()
                is_not = True
            if self.at_keyword("NULL"):
                self.take()
                return self.make(n.IsNull(left, is_not, pos=pos))
            if self.at_keyword("DISTINCT"):
                self.take()
                self.expect_keyword("FROM", "FROM after IS DISTINCT")
                right = self.value()
                return self.make(n.DistinctFrom(left, right, is_not, pos=pos))
            after = self.peek()
            hint = ""
            if after.kind == "keyword" and after.value in ("TRUE", "FALSE"):
                hint = " (FLUID SQL/1 is two-valued: write the bool itself, or NOT it)"
            raise FluidSqlError(
                "syntax",
                f"IS takes NULL or DISTINCT FROM, not {_describe(after)}{hint}",
                after.pos,
            )
        return left

    def in_items(self) -> Tuple[n.Node, ...]:
        self.expect_op("(", "'(' to open the IN list")
        items = [self.in_item()]
        while self.at_op(","):
            self.take()
            items.append(self.in_item())
        self.expect_op(")", "',' or ')' in the IN list")
        return tuple(items)

    def in_item(self) -> n.Node:
        token = self.peek()
        if token.kind == "op" and token.value == "-" and self.peek(1).kind == "number":
            self.take()
            number = self.take()
            return n.Number("-" + number.value, pos=token.pos)
        if token.kind == "number":
            self.take()
            return n.Number(token.value, pos=token.pos)
        if token.kind == "string":
            self.take()
            return n.String(token.value, pos=token.pos)
        if self.at_keyword("TRUE", "FALSE"):
            self.take()
            return n.Bool(token.value == "TRUE", pos=token.pos)
        if self.at_keyword("DATE"):
            return self.date_literal()
        if self.at_keyword("NULL"):
            raise FluidSqlError(
                "null-literal",
                "the NULL literal is never an IN item: x IN (..., NULL) never matches NULL "
                "(write x IS NULL OR x IN (...))",
                token.pos,
            )
        raise FluidSqlError(
            "syntax",
            f"an IN list holds literals only, not {_describe(token)}{_OUTSIDE}",
            token.pos,
        )

    def value(self) -> n.Node:
        left = self.term()
        while self.at_op("+", "-"):
            token = self.take()
            left = self.make(n.Arith(token.value, left, self.term(), pos=token.pos))
        return left

    def term(self) -> n.Node:
        left = self.factor()
        while self.at_op("*", "/"):
            token = self.take()
            left = self.make(n.Arith(token.value, left, self.factor(), pos=token.pos))
        return left

    def factor(self) -> n.Node:
        if self.at_op("-"):
            token = self.take()
            if self.at_op("-"):
                raise FluidSqlError(
                    "syntax", "a minus sign cannot follow a minus sign: write -(-1)", token.pos
                )
            self.enter(token.pos)
            node = self.make(n.Neg(self.primary(), pos=token.pos))
            self.leave()
            return node
        return self.primary()

    def primary(self) -> n.Node:
        token = self.peek()
        if token.kind == "number":
            self.take()
            return n.Number(token.value, pos=token.pos)
        if token.kind == "string":
            self.take()
            return n.String(token.value, pos=token.pos)
        if self.at_keyword("TRUE", "FALSE"):
            self.take()
            return n.Bool(token.value == "TRUE", pos=token.pos)
        if self.at_keyword("NULL"):
            self.take()
            return n.Null(pos=token.pos)
        if self.at_keyword("DATE"):
            return self.date_literal()
        if self.at_keyword("CASE"):
            return self.case_expr()
        if token.kind == "op" and token.value == "(":
            self.take()
            self.enter(token.pos)
            inner = self.expr()
            self.expect_op(")", "')' to close the '(' at character " + str(token.pos + 1))
            self.leave()
            if inner.depth + 1 > MAX_DEPTH:
                raise FluidSqlError(
                    "too-deep", f"the text nests deeper than {MAX_DEPTH} levels", token.pos
                )
            return dataclasses.replace(inner, depth=inner.depth + 1)
        if token.kind == "name" and self.peek(1).kind == "op" and self.peek(1).value == "(":
            return self.call()
        if token.kind in ("name", "qname"):
            return self.column_ref()
        if token.kind == "keyword" and token.value == "NOT":
            raise FluidSqlError(
                "syntax",
                "NOT applies to a whole predicate: write NOT (a = b), or parenthesize (NOT b)",
                token.pos,
            )
        raise _unexpected(token, "a value")

    def date_literal(self) -> n.Node:
        token = self.take()  # DATE
        text = self.peek()
        if text.kind != "string":
            raise FluidSqlError(
                "syntax",
                "DATE starts a date literal (DATE '2026-11-16'); double-quote a column named "
                'date ("date")',
                token.pos,
            )
        self.take()
        _check_date(text.value, text.pos)
        return n.Date(text.value, pos=token.pos)

    def column_ref(self) -> n.Node:
        first = self.take()
        if not self.at_op("."):
            return n.Column(first.value, pos=first.pos)
        self.take()
        second = self.peek()
        if second.kind not in ("name", "qname"):
            raise _unexpected(second, "a column name after '.'")
        self.take()
        if self.at_op("."):
            raise FluidSqlError(
                "multi-link-path",
                f"{first.raw}.{second.raw}.… crosses more than one link: FLUID SQL/1 paths cross "
                "one (paths over several links are FLUX-only; declare a sharedAttribute)",
                first.pos,
            )
        return n.Column(second.value, first.value, pos=first.pos)

    def call(self) -> n.Node:
        token = self.take()
        name = token.value.upper()
        if name not in FUNCTIONS:
            raise FluidSqlError("syntax", _function_message(name), token.pos)
        self.take()  # "("
        self.enter(token.pos)
        if name == "ROUND":
            node = self.round_call(token)
        elif name == "COALESCE":
            args = [self.expr()]
            self.expect_op(",", "',' and a second argument: COALESCE takes two or more")
            args.append(self.expr())
            while self.at_op(","):
                self.take()
                args.append(self.expr())
            self.expect_op(")", "',' or ')' in COALESCE")
            node = self.make(n.Coalesce(tuple(args), pos=token.pos))
        else:
            unit = {"DAYS_BETWEEN": "day", "MONTHS_BETWEEN": "month", "YEARS_BETWEEN": "year"}
            start = self.value()
            self.expect_op(",", f"',' between the two dates of {name}")
            end = self.value()
            self.expect_op(")", f"')': {name} takes two dates")
            node = self.make(n.DateDiff(unit[name], start, end, pos=token.pos))
        self.leave()
        return node

    def round_call(self, token: Token) -> n.Node:
        arg = self.value()
        self.expect_op(",", "',' and the number of decimal places: ROUND(x, 2)")
        places = self.peek()
        if places.kind != "number" or "." in places.value:
            raise FluidSqlError(
                "syntax",
                f"ROUND's decimal places are a non-negative integer literal, not "
                f"{_describe(places)}",
                places.pos,
            )
        self.take()
        mode = n.DEFAULT_ROUND_MODE
        if self.at_op(","):
            self.take()
            literal = self.peek()
            if literal.kind != "string" or literal.value not in n.ROUND_MODES:
                raise FluidSqlError(
                    "syntax",
                    "ROUND's rounding mode is 'HALF_AWAY_FROM_ZERO' (the default) or "
                    f"'HALF_TO_EVEN', not {_describe(literal)}",
                    literal.pos,
                )
            self.take()
            mode = literal.value
        self.expect_op(")", "')' after ROUND's arguments")
        return self.make(n.Round(arg, int(places.value), mode, pos=token.pos))

    def case_expr(self) -> n.Node:
        token = self.take()  # CASE
        if not self.at_keyword("WHEN"):
            raise FluidSqlError(
                "syntax",
                "only the searched CASE is in FLUID SQL/1: write CASE WHEN x = 1 THEN ... END",
                self.peek().pos,
            )
        self.enter(token.pos)
        whens: List[Tuple[n.Node, n.Node]] = []
        while self.at_keyword("WHEN"):
            self.take()
            condition = self.expr()
            self.expect_keyword("THEN", "THEN after the WHEN condition")
            whens.append((condition, self.expr()))
        else_: Optional[n.Node] = None
        if self.at_keyword("ELSE"):
            self.take()
            else_ = self.expr()
        self.expect_keyword("END", "WHEN, ELSE or END in CASE")
        node = self.make(n.Case(tuple(whens), else_, pos=token.pos))
        self.leave()
        return node


def _check_date(text: str, pos: int) -> None:
    """Refuse a DATE literal that is not a real calendar date written YYYY-MM-DD."""
    ok = bool(_DATE_TEXT.match(text))
    if ok:
        try:
            datetime.date.fromisoformat(text)
        except ValueError:
            ok = False
    if not ok:
        raise FluidSqlError(
            "bad-date",
            f"DATE '{text}' is not a calendar date written YYYY-MM-DD, from 0001-01-01 to "
            "9999-12-31",
            pos,
        )


def _function_message(name: str) -> str:
    if name in _HINTS:
        why = _HINTS[name]
    elif name in _STRING_FUNCTIONS:
        why = "FLUID SQL/1 has no string functions"
    elif name in _NUMERIC_FUNCTIONS:
        why = "it is not a FLUID SQL/1 function (they are " + ", ".join(FUNCTIONS) + ")"
    elif name in _WINDOW_FUNCTIONS:
        why = "windows are FLUX-only, never in a contract"
    else:
        why = "the functions of FLUID SQL/1 are " + ", ".join(FUNCTIONS)
    return f"{name}(...) is not in FLUID SQL/1: {why}{_OUTSIDE}"
