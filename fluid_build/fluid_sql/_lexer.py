# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tokens of a FLUID SQL/1 text (``RFC-data-rules.md`` §2.1, the lexical rules).

The text is NFC-normalized first, and every position after that is an index
into the normalized text. Then, before any token is read, the text is refused
when it is longer than :data:`MAX_CODE_POINTS` or holds a ``;``, ``--`` or
``/*`` outside a string literal or a quoted name: SQL reads those as a second
statement or a comment, and ``net--1 > 0`` would otherwise reach a SQL engine
as ``net``.

- Whitespace is space, tab and newline; any other character outside a string
  or a quoted name that no rule below takes is refused.
- A string is single-quoted, ``''`` is a quote inside it, and a backslash is
  an ordinary character.
- A quoted name is double-quoted, ``""`` is a quote inside it, and it is never
  empty.
- A number is digits with an optional fraction (``3``, ``1.50``): no exponent,
  no leading or trailing point.
- A word is ``[A-Za-z_][A-Za-z0-9_]*``: a keyword (:data:`KEYWORDS`, any case)
  or a name.

Hand-written after flux-expr/1's lexer (flux-engine ``flux_engine/expr/lexer.py``,
NFC first); sqlglot is not used because its tree merges constructs that FLUID
SQL/1 tells apart (founder decision, 2026-11-16).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import List, Tuple

from ._errors import FluidSqlError

MAX_CODE_POINTS = 4096
MAX_DEPTH = 64
#: The most decimal places ROUND takes (founder pick, 2026-11-16): the widest DECIMAL scale.
MAX_ROUND_PLACES = 38

#: The words of the grammar. A column named like one of them is double-quoted.
KEYWORDS = frozenset(
    {
        "AND",
        "BETWEEN",
        "CASE",
        "DATE",
        "DISTINCT",
        "ELSE",
        "END",
        "FALSE",
        "FROM",
        "IN",
        "IS",
        "NOT",
        "NULL",
        "OR",
        "THEN",
        "TRUE",
        "WHEN",
    }
)

#: The functions of FLUID SQL/1. Not keywords: a column may be called ``round``.
FUNCTIONS = (
    "ROUND",
    "COALESCE",
    "DAYS_BETWEEN",
    "MONTHS_BETWEEN",
    "YEARS_BETWEEN",
    "ADD_DAYS",
    "ADD_MONTHS",
    "ADD_YEARS",
)

_WHITESPACE = frozenset(" \t\n")
_WORD_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")
_WORD_CHARS = _WORD_START | frozenset("0123456789")
_DIGITS = frozenset("0123456789")
_SINGLE_OPS = frozenset("=+-*/(),.")


@dataclass(frozen=True)
class Token:
    """``kind`` is ``keyword``, ``name``, ``qname`` (quoted), ``number``, ``string``,
    ``op`` or ``end``; ``value`` is the keyword upper-cased, the name or string
    unquoted, the number as written, or the operator (``!=`` reads as ``<>``)."""

    kind: str
    value: str
    pos: int
    raw: str


def normalize(text: str) -> str:
    """The NFC form of ``text``: what is measured, lexed and parsed."""
    return unicodedata.normalize("NFC", text)


def check_limits(text: str) -> None:
    """Refuse ``text`` (already NFC) for its length, or for ``;``, ``--`` or ``/*``
    outside a string literal or a quoted name."""
    if len(text) > MAX_CODE_POINTS:
        raise FluidSqlError(
            "too-long",
            f"the text is {len(text):,} code points long; FLUID SQL/1 takes at most "
            f"{MAX_CODE_POINTS:,}",
        )
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in ("'", '"'):
            i, _closed = _quoted_end(text, i)
            continue
        if c == ";":
            raise FluidSqlError(
                "comment-or-semicolon",
                "';' would end the statement in SQL; FLUID SQL/1 is one expression",
                i,
            )
        if c == "-" and text.startswith("-", i + 1):
            raise FluidSqlError(
                "comment-or-semicolon",
                "'--' starts a comment in SQL; write a double negation as x - -1",
                i,
            )
        if c == "/" and text.startswith("*", i + 1):
            raise FluidSqlError(
                "comment-or-semicolon", "'/*' starts a comment in SQL; write a / (b * c)", i
            )
        i += 1


def _quoted_end(text: str, start: int) -> Tuple[int, bool]:
    """The index just past the quoted run that opens at ``start``, and whether it
    closes (``(len(text), False)`` when it never does)."""
    quote = text[start]
    i, n = start + 1, len(text)
    while i < n:
        if text[i] == quote:
            if i + 1 < n and text[i + 1] == quote:
                i += 2
                continue
            return i + 1, True
        i += 1
    return n, False


def tokenize(text: str) -> List[Token]:
    """The tokens of ``text`` (already NFC and through :func:`check_limits`)."""
    tokens: List[Token] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in _WHITESPACE:
            i += 1
            continue
        if c in ("'", '"'):
            end, closed = _quoted_end(text, i)
            if not closed:
                what = "string" if c == "'" else "quoted name"
                raise FluidSqlError("syntax", f"this {what} never ends", i)
            body = text[i + 1 : end - 1].replace(c + c, c)
            if c == '"' and not body:
                raise FluidSqlError("syntax", "a quoted name is never empty", i)
            tokens.append(Token("string" if c == "'" else "qname", body, i, text[i:end]))
            i = end
            continue
        if c in _DIGITS:
            j = i
            while j < n and text[j] in _DIGITS:
                j += 1
            if j < n and text[j] == ".":
                if j + 1 < n and text[j + 1] in _DIGITS:
                    j += 1
                    while j < n and text[j] in _DIGITS:
                        j += 1
                else:
                    raise FluidSqlError(
                        "syntax", "a number needs digits after its point (write 1.0)", i
                    )
            if j < n and (text[j] in _WORD_CHARS or text[j].isalnum()):
                raise FluidSqlError(
                    "syntax",
                    f"{text[i:j + 1]!r} is not a number: FLUID SQL/1 numbers are digits with an "
                    "optional fraction, with no exponent",
                    i,
                )
            tokens.append(Token("number", text[i:j], i, text[i:j]))
            i = j
            continue
        if c in _WORD_START:
            j = i + 1
            while j < n and text[j] in _WORD_CHARS:
                j += 1
            if j < n and text[j].isalnum():
                raise FluidSqlError(
                    "syntax",
                    f"unexpected character {text[j]!r} (U+{ord(text[j]):04X}) in a name: "
                    'double-quote a name that is not [A-Za-z_][A-Za-z0-9_]* ("Net Amount")',
                    j,
                )
            word = text[i:j]
            if word.upper() in KEYWORDS:
                tokens.append(Token("keyword", word.upper(), i, word))
            else:
                tokens.append(Token("name", word, i, word))
            i = j
            continue
        two = text[i : i + 2]
        if two in ("<=", ">=", "<>", "!="):
            tokens.append(Token("op", "<>" if two == "!=" else two, i, two))
            i += 2
            continue
        if c in ("<", ">") or c in _SINGLE_OPS:
            if c == "." and i + 1 < n and text[i + 1] in _DIGITS:
                raise FluidSqlError("syntax", "a number starts with a digit (write 0.5, not .5)", i)
            tokens.append(Token("op", c, i, c))
            i += 1
            continue
        raise FluidSqlError(
            "syntax",
            f"unexpected character {c!r} (U+{ord(c):04X}): it is not in FLUID SQL/1"
            + _character_hint(c),
            i,
        )
    tokens.append(Token("end", "", n, ""))
    return tokens


def _character_hint(c: str) -> str:
    """What to write instead, for the characters SQL readers reach for."""
    hints = {
        "|": " (no string concatenation)",
        "%": " (no modulo)",
        ":": " (no :: cast: FLUID SQL/1 has no CAST)",
        "!": " (write <> for not equal)",
        "\r": " (whitespace is space, tab and newline)",
        "`": ' (double-quote a name: "order")',
        "[": ' (double-quote a name: "order")',
        "?": " (no parameters)",
        "$": " (no parameters)",
        "@": " (no parameters)",
    }
    return hints.get(c, "")
