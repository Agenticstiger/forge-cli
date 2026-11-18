# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Split a FLUID column type into its base name and its parameter list.

The FLUID 0.7.7 type pattern lets an ``enum`` level be a quoted string that
holds any character but a control character, with its own quote doubled:
``enum('Dr.', '>10000', 'O''Brien', 'a, b')``. That is how SQL writes a
string literal (PostgreSQL, MySQL and DuckDB spell their ENUM levels the same
way). A reader that ends the list at the first ``)`` or cuts it at every
comma breaks such a level apart, so the functions here step over quoted
text and split only at the commas and the closing parenthesis outside it.
:func:`read_enum_list` reads a ``validationRules`` enum list the same way
(``RFC-data-rules.md`` section 2.3).

The module is a leaf: it imports nothing from ``fluid_build``, so any mapper
can use it without pulling a provider in.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

_QUOTES = ("'", '"')
#: What a quoted level may not hold: FLUID 0.7.7's ``[^'\x00-\x1f\x7f-\x9f]``.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _scan_quoted(text: str, start: int) -> int:
    """Return the index just past the quoted string that opens at ``start``.

    A doubled quote inside the string is one literal quote. Returns ``-1``
    when the string never closes.
    """
    quote = text[start]
    i = start + 1
    while i < len(text):
        if text[i] == quote:
            if i + 1 < len(text) and text[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return -1


def split_type_params(fluid_type: str) -> Tuple[str, Optional[str]]:
    """Split ``"decimal(18,4)"`` into ``("decimal", "18,4")``.

    ``"enum('a)b', 'c')"`` gives ``("enum", "'a)b', 'c'")``: the ``)`` inside
    the quoted level does not end the list. A quote that never closes is an
    ordinary character, as it was before quoted levels existed, so
    ``"varchar(a'b)"`` still gives ``("varchar", "a'b")``.

    A type with no parameter list gives ``(type, None)``. So does a type whose
    parentheses do not close it: an unclosed list, a nested ``(`` outside
    quotes, or text after the closing ``)``. Callers treat that as an
    unparameterised type they do not know, as they always have.

    Both parts are stripped of surrounding whitespace; the inside of the list
    is returned as written.
    """
    text = fluid_type.strip()
    open_at = text.find("(")
    if open_at < 0:
        return text, None
    i = open_at + 1
    while i < len(text):
        char = text[i]
        if char in _QUOTES:
            end = _scan_quoted(text, i)
            i = i + 1 if end < 0 else end
            continue
        if char == "(":
            return text, None
        if char == ")":
            if text[i + 1 :].strip():
                return text, None
            return text[:open_at].strip(), text[open_at + 1 : i].strip()
        i += 1
    return text, None


def split_param_list(params: str) -> List[str]:
    """Split a parameter list at the commas outside quotes, each part stripped.

    ``"'a, b', 'c'"`` gives ``["'a, b'", "'c'"]`` and ``"18, 4"`` gives
    ``["18", "4"]``. Empty parts are kept (``"a,,b"`` gives three parts), so a
    caller decides what an empty part means. A quote that never closes is an
    ordinary character: ``"a'b, c"`` gives ``["a'b", "c"]``.
    """
    parts: List[str] = []
    start = 0
    i = 0
    while i < len(params):
        char = params[i]
        if char in _QUOTES:
            end = _scan_quoted(params, i)
            i = i + 1 if end < 0 else end
            continue
        if char == ",":
            parts.append(params[start:i].strip())
            start = i + 1
        i += 1
    parts.append(params[start:].strip())
    return parts


def _skip_spaces(text: str, i: int) -> int:
    while i < len(text) and text[i].isspace():
        i += 1
    return i


def _quoted_value(text: str, start: int, separators: str) -> Optional[Tuple[str, int]]:
    """The quoted string that opens at ``start`` and the index the next value
    starts at, or ``None`` when no quoted value opens there: no quote, one that
    never closes, a control character inside, or text after the closing quote."""
    if start >= len(text) or text[start] not in _QUOTES:
        return None
    end = _scan_quoted(text, start)
    if end < 0:
        return None
    after = _skip_spaces(text, end)
    body = text[start + 1 : end - 1]
    if (after < len(text) and text[after] not in separators) or _CONTROL.search(body):
        return None
    quote = text[start]
    return body.replace(quote + quote, quote), after + 1


def read_enum_list(text: str, separators: str = ",") -> List[str]:
    """The values an enum list names, each value ended by a character of ``separators``.

    A value that opens with a quote is a quoted string when the quote closes
    and only spaces follow it before the next separator or the end: its value
    is the text between the quotes, each doubled quote read as one, as SQL and
    FLUID 0.7.7's enum levels write it. ``'a, b', 'O''Brien'`` gives
    ``["a, b", "O'Brien"]``. The value is kept as written, so ``' a'`` keeps
    its space and ``''`` is the empty value. A quoted string may not hold a
    control character, as in the 0.7.7 type pattern.

    Every other value is its text up to the next separator, stripped, and is
    left out when empty, as a plain split reads it. A quote inside such a
    value is an ordinary character: ``O'Brien, D'Arcy`` gives
    ``["O'Brien", "D'Arcy"]``, and ``'a'b, c`` gives ``["'a'b", "c"]``. A list
    in which no value opens with a quote reads as a split at every separator,
    each part stripped and the empty parts left out.

    The scan is linear in the length of ``text``.
    """
    values: List[str] = []
    i = 0
    while i <= len(text):
        start = _skip_spaces(text, i)
        quoted = _quoted_value(text, start, separators)
        if quoted is not None:
            value, i = quoted
            values.append(value)
            continue
        stop = start
        while stop < len(text) and text[stop] not in separators:
            stop += 1
        value = text[start:stop].strip()
        if value:
            values.append(value)
        i = stop + 1
    return values


def write_enum_list(values: Sequence[str]) -> str:
    """``values`` as one comma-separated list, for a message. When one of them
    holds a comma, opens with a quote, is empty or has a space at an end, each
    is written as a quoted string (``'a, b', 'c'``), so the list reads one way:
    :func:`read_enum_list` reads it back to ``values``, unless a value it had
    to quote holds a control character."""
    if any(
        "," in value or value[:1] in _QUOTES or value != value.strip() or not value
        for value in values
    ):
        return ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return ", ".join(values)
