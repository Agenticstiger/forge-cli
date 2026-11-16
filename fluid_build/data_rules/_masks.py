# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Identifier formats (``RFC-data-rules.md`` §3): COBOL PICTURE masks and their regex.

A mask is 1 to 64 printable ASCII characters with at least one symbol: ``9`` is a
digit, ``A`` an upper-case ASCII letter, ``X`` an upper-case ASCII letter or a
digit; a backslash makes the next character literal, and every other character
stands for itself. An alternative is a list of literal prefixes (one empty
prefix when left out) followed by a mask; a column's ``identifierFormat`` is a
list of alternatives, and a value has the format when it is one of them.

**One regex per column.** :func:`column_regex` compiles the alternatives into an
anchored pattern built only from groups, alternation, character classes and
counts, so the same text works in ECMA-262 (ODCS ``logicalTypeOptions.pattern``),
POSIX ERE, RE2 and Python (with ``re.fullmatch``: Python's ``$`` also matches
before a trailing newline). XSD and I-Regexp (RFC 9485) match whole values and
take :func:`column_body`, which has no anchors. Literal characters are written so
every one of those reads them literally: ``\\`` before ``. * + ? ( ) [ ] { } | \\ ^``,
and ``$`` as the class ``[$]`` (an I-Regexp cannot escape ``$``, an ECMA-262 regex
must). An empty prefix beside others becomes an optional group, since POSIX ERE
leaves an empty alternative undefined.

**No overlap.** Each alternative expands into one fixed-length pattern per prefix:
the prefix's characters, then the mask's positions. Two patterns from different
alternatives overlap when they have one length and every position's character
sets intersect; such a value would not say which alternative made it.

Masks after COBOL PICTURE strings (IBM Enterprise COBOL, "Symbols used in the
PICTURE clause"); escaping after RFC 9485's NormalChar and SingleCharEsc.
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from typing import FrozenSet, List, Optional, Sequence, Tuple

MAX_MASK_LENGTH = 64

_DIGITS = frozenset(string.digits)
_UPPER = frozenset(string.ascii_uppercase)
SYMBOL_SETS = {"9": _DIGITS, "A": _UPPER, "X": _UPPER | _DIGITS}
_CLASSES = {"9": "[0-9]", "A": "[A-Z]", "X": "[A-Z0-9]"}
_ESCAPED = frozenset(".*+?()[]{}|\\^")


class MaskError(ValueError):
    """A mask or a prefix outside the grammar."""


@dataclass(frozen=True)
class Position:
    """One character of a mask: a ``symbol`` (``9``, ``A``, ``X``) or a ``literal``."""

    symbol: Optional[str] = None
    literal: str = ""

    def chars(self) -> FrozenSet[str]:
        return SYMBOL_SETS[self.symbol] if self.symbol else frozenset(self.literal)


def is_printable_ascii(text: str) -> bool:
    return all(" " <= c <= "~" for c in text)


def parse_mask(mask: str) -> Tuple[Position, ...]:
    """The positions of ``mask``, or :class:`MaskError` saying what is wrong."""
    if not isinstance(mask, str) or not mask:
        raise MaskError("a mask is 1 to 64 characters long")
    if len(mask) > MAX_MASK_LENGTH:
        raise MaskError(f"the mask is {len(mask)} characters long; a mask has at most 64")
    positions: List[Position] = []
    i = 0
    while i < len(mask):
        c = mask[i]
        if not is_printable_ascii(c):
            raise MaskError(
                f"{c!r} (U+{ord(c):04X}) at character {i + 1} is not printable ASCII: a mask is "
                "9, A, X, backslash escapes and printable ASCII literals"
            )
        if c == "\\":
            if i + 1 == len(mask):
                raise MaskError("the mask ends in a backslash, which escapes nothing")
            literal = mask[i + 1]
            if not is_printable_ascii(literal):
                raise MaskError(f"the backslash at character {i + 1} escapes a non-ASCII character")
            positions.append(Position(literal=literal))
            i += 2
            continue
        positions.append(Position(symbol=c) if c in SYMBOL_SETS else Position(literal=c))
        i += 1
    if not any(p.symbol for p in positions):
        raise MaskError(
            "the mask has no symbol: use 9 for a digit, A for an upper-case letter or X for either"
        )
    return tuple(positions)


def literal_regex(text: str) -> str:
    """``text`` as a portable regex that matches exactly it."""
    out = []
    for c in text:
        if c == "$":
            out.append("[$]")
        elif c in _ESCAPED:
            out.append("\\" + c)
        else:
            out.append(c)
    return "".join(out)


def mask_regex(positions: Sequence[Position]) -> str:
    """The regex of a mask: runs of one symbol become counts, literals are escaped."""
    out: List[str] = []
    i = 0
    while i < len(positions):
        position = positions[i]
        if position.symbol is None:
            out.append(literal_regex(position.literal))
            i += 1
            continue
        run = 1
        while i + run < len(positions) and positions[i + run].symbol == position.symbol:
            run += 1
        out.append(_CLASSES[position.symbol] + (f"{{{run}}}" if run > 1 else ""))
        i += run
    return "".join(out)


def prefix_regex(prefixes: Sequence[str]) -> str:
    """The regex of an alternative's prefixes (``""`` for one empty prefix)."""
    named = [p for p in prefixes if p]
    if not named:
        return ""
    body = (
        literal_regex(named[0])
        if len(named) == 1
        else "(" + "|".join(map(literal_regex, named)) + ")"
    )
    if len(named) < len(prefixes):  # an empty prefix too: the prefix is optional
        return ("(" + body + ")" if len(named) == 1 else body) + "?"
    return body


@dataclass(frozen=True)
class Alternative:
    prefixes: Tuple[str, ...]
    positions: Tuple[Position, ...]

    def regex(self) -> str:
        return prefix_regex(self.prefixes) + mask_regex(self.positions)

    def patterns(self) -> List[Tuple[FrozenSet[str], ...]]:
        """One fixed-length pattern per prefix: its characters, then the mask's."""
        tail = tuple(p.chars() for p in self.positions)
        return [tuple(frozenset(c) for c in prefix) + tail for prefix in self.prefixes]

    def capacity(self) -> int:
        """How many values it holds: |prefixes| x 10^d x 26^a x 36^x."""
        total = len(self.prefixes)
        for p in self.positions:
            if p.symbol:
                total *= len(SYMBOL_SETS[p.symbol])
        return total


def alternative(entry: object) -> Alternative:
    """An ``identifierFormat[]`` entry as an :class:`Alternative` (or :class:`MaskError`)."""
    if not isinstance(entry, dict):
        raise MaskError("an identifierFormat entry is an object with a mask")
    raw = entry.get("prefixes")
    prefixes: Tuple[str, ...] = (
        ("",) if raw is None else tuple(raw) if isinstance(raw, list) else ()
    )
    if not prefixes or not all(isinstance(p, str) for p in prefixes):
        raise MaskError("prefixes is a list of strings")
    for prefix in prefixes:
        if not is_printable_ascii(prefix):
            raise MaskError(f"the prefix {prefix!r} is not printable ASCII")
    return Alternative(prefixes, parse_mask(entry.get("mask")))  # type: ignore[arg-type]


def column_body(alternatives: Sequence[Alternative]) -> str:
    """The unanchored regex of a column's formats (XSD, I-Regexp)."""
    return "(" + "|".join(a.regex() for a in alternatives) + ")"


def column_regex(alternatives: Sequence[Alternative]) -> str:
    """The anchored regex of a column's formats (ECMA-262, POSIX ERE, RE2, Python)."""
    return "^" + column_body(alternatives) + "$"


def overlap(alternatives: Sequence[Alternative]) -> Optional[Tuple[int, int, str]]:
    """The first two alternatives that overlap, and a value both take, or ``None``."""
    for i, first in enumerate(alternatives):
        for j in range(i + 1, len(alternatives)):
            for a in first.patterns():
                for b in alternatives[j].patterns():
                    if len(a) != len(b):
                        continue
                    shared = [x & y for x, y in zip(a, b, strict=True)]
                    if all(shared):
                        return i, j, "".join(min(s) for s in shared)
    return None
