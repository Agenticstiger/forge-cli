# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Split a FLUID column type into its base name and its parameter list.

The FLUID 0.7.7 type pattern lets an ``enum`` level be a quoted string that
holds any character but a control character, with its own quote doubled:
``enum('Dr.', '>10000', 'O''Brien', 'a, b')``. That is how SQL writes a
string literal (PostgreSQL, MySQL and DuckDB spell their ENUM levels the same
way). A reader that ends the list at the first ``)`` or cuts it at every
comma breaks such a level apart, so the two functions here step over quoted
text and split only at the commas and the closing parenthesis outside it.

The module is a leaf: it imports nothing from ``fluid_build``, so any mapper
can use it without pulling a provider in.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

_QUOTES = ("'", '"')


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
