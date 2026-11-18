# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

r"""Linear-time stand-ins for schema patterns that backtrack in Python's ``re``.

Every FLUID schema checks a column ``type`` with one ``pattern``. Up to this change
each of them read a type's arguments with::

    \s*(\(\s*[0-9A-Za-z_,\s'\"-]*\s*\))?\s*$

Python's ``re`` backtracks. Inside the brackets, ``\s*``, a set that holds ``\s``,
and ``\s*`` again can all take the same spaces, so ``decimal(`` followed by n spaces
and no ``)`` tries about n**3 / 6 ways to share them out before it gives up: 2.4 s at
n = 2,000, and minutes for a contract with 10 KB of type text. Without brackets the
``\s*`` before the group and the ``\s*`` after it share the spaces too, so ``int``,
n spaces and any other character take about n**2 / 2 steps. Both are the shape the
OWASP ReDoS guidance warns about: adjacent repetitions that can match the same text.

The released schema files are copied byte for byte by other projects, so their text
stays. The validator runs a pattern that matches exactly the same values in linear
time instead (``schema_manager._compiled_pattern``)::

    (\s*\([0-9A-Za-z_,\s'\"-]*\))?\s*$

It is the same language. Write S for ``\s``, C for the bracket set and G for
``\(C*\)``. Every ``\s`` character is in C, under ``(?i)`` too, and neither bracket is
in S or C (the tests check every code point), so:

* ``S*C*S*`` is ``C*``, because S is part of C;
* ``S*(G)?S*`` is ``S* | S*GS*``, which is ``(S*G)?S*``.

Nothing else in the pattern changes, and the parts that do change hold no anchor and
no lookaround, so a value matches the stand-in exactly when it matches the original.
The 0.7.7 preview, which is not released, now holds the linear text itself.

A linear-time engine such as RE2 or Rust's ``regex`` was the other way out. RE2 has
no lookahead, which the 0.7.7 enum rule uses, and its ``\s`` is ASCII only, which
would change what a type may hold; it would also add a dependency for one pattern.
"""

from __future__ import annotations

from typing import Dict

#: The type names, as every FLUID schema since 0.7.1 spells them.
TYPE_NAMES = (
    r"(string|text|varchar|varchar2|nvarchar|char|nchar|character|clob|int|integer|int2|"
    r"int4|int8|int16|int32|int64|tinyint|smallint|mediumint|bigint|long|longint|serial|"
    r"bigserial|float|float4|float8|float32|float64|double|real|double\s+precision|decimal|"
    r"dec|numeric|number|bignumeric|money|boolean|bool|bit|date|time|datetime|datetime2|"
    r"smalldatetime|timestamp|timestamptz|timestamp_tz|timestamp_ntz|timestamp_ltz|"
    r"timestampntz|timestamp\s+with(out)?\s+time\s+zone|interval|year|variant|object|array|"
    r"struct|map|record|row|json|jsonb|super|binary|varbinary|bytes|blob|bytea|raw|"
    r"geography|geometry|geom|point|uuid|uniqueidentifier|guid|enum|hll)"
)

#: One character of a type's arguments: ``decimal(10, 2)``, ``varchar(255)``.
ARGUMENT_CHARACTER = r"[0-9A-Za-z_,\s'\"-]"

#: A type's arguments as the schemas wrote them: ``\s*`` around the set inside the
#: brackets, and before the brackets. With the ``\s*$`` that follows, it backtracks.
BACKTRACKING_ARGUMENTS = r"\s*(\(\s*" + ARGUMENT_CHARACTER + r"*\s*\))?"

#: The same values: the set already holds the spaces inside the brackets, and the group
#: takes the spaces before them, so no two repetitions next to each other share a space.
LINEAR_ARGUMENTS = r"(\s*\(" + ARGUMENT_CHARACTER + r"*\))?"

#: One level of a 0.7.7 enum: a quoted string with its own quote doubled, or words.
_ENUM_LEVEL = (
    r"(?:'(?:[^'\x00-\x1f\x7f-\x9f]|'')*'"
    r'|"(?:[^"\x00-\x1f\x7f-\x9f]|"")*"'
    r"|[0-9A-Za-z_-]+(?:\s+[0-9A-Za-z_-]+)*)"
)
_ENUM = r"enum\s*\(\s*(?:" + _ENUM_LEVEL + r"\s*(?:,\s*" + _ENUM_LEVEL + r"\s*)*)?\)"


def _released(arguments: str) -> str:
    return r"(?i)^\s*" + TYPE_NAMES + arguments + r"\s*$"


def _preview(arguments: str) -> str:
    return r"(?i)^\s*(?:" + _ENUM + r"|(?!enum\s*\()" + TYPE_NAMES + arguments + r")\s*$"


#: The column type pattern of FLUID 0.7.1 to 0.7.6 and of the 0.7.6 authoring schema.
RELEASED_COLUMN_TYPE = _released(BACKTRACKING_ARGUMENTS)
#: What the validator runs in its place.
RELEASED_COLUMN_TYPE_LINEAR = _released(LINEAR_ARGUMENTS)

#: The 0.7.7 preview's column type pattern: the linear form.
PREVIEW_COLUMN_TYPE = _preview(LINEAR_ARGUMENTS)
#: The 0.7.7 preview's pattern before it was made linear, as a cached or vendored copy
#: of that schema still holds it.
PREVIEW_COLUMN_TYPE_BACKTRACKING = _preview(BACKTRACKING_ARGUMENTS)

#: Each backtracking pattern a bundled schema has shipped, and its linear equivalent.
#: A new entry needs the proofs in tests/test_linear_schema_patterns.py.
LINEAR_EQUIVALENTS: Dict[str, str] = {
    RELEASED_COLUMN_TYPE: RELEASED_COLUMN_TYPE_LINEAR,
    PREVIEW_COLUMN_TYPE_BACKTRACKING: PREVIEW_COLUMN_TYPE,
}


def linear_equivalent(pattern: str) -> str:
    """``pattern``, or the linear-time pattern that matches exactly the same values."""
    return LINEAR_EQUIVALENTS.get(pattern, pattern)
