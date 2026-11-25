# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The plain types of the consumer-query package: what goes in and what comes out.

Standard library only, so that reading the package costs neither sqlglot nor DuckDB. The
checker (:mod:`.check`) and the process runner (:mod:`.runner`) are loaded when one of them is
called, never before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

__all__ = [
    "REFUSAL_CODES",
    "Checked",
    "Refusal",
    "Refused",
    "Table",
    "duckdb_type",
]

#: The codes a refusal carries. Each names a kind of construct, never a value of the text.
REFUSAL_CODES = (
    "unparsable",  # the text does not tokenize or parse
    "several_statements",  # more than one statement, or none
    "not_a_query",  # DDL, DML, PRAGMA, SET, ATTACH, COPY, INSTALL, LOAD, EXPORT, CALL, a command
    "construct",  # an expression class, argument or type that is not on the closed lists
    "function",  # a function that is not on the closed list (table functions, environment, ...)
    "table",  # a table that is not a listed exposeId or a WITH name, or is a string or a path
    "ambiguous_table",  # two tables, or a table and a WITH name, that read as one name
    "placeholder",  # a placeholder: a query text takes no parameters
    "bounds",  # the token, nesting, node or expansion limits
    "recursion",  # the parser or the writer ran out of stack
    "column",  # a column that resolves to none of the listed tables' columns
    "order",  # LIMIT, OFFSET or a window ORDER BY without a total order
    "alias",  # a projection alias read in its own SELECT
    "pattern",  # a LIKE pattern past its length bound
    "canonical",  # the canonical text did not read back as the tree that was checked
    "schema",  # the listed tables themselves cannot be read (a duplicate name, no columns)
    "internal",  # the check stopped on an error nobody named
)


class Refusal(Exception):
    """A text the checker refuses. Internal: :func:`check_text` returns it as :class:`Refused`."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Table:
    """One table a request lists: its columns with their contract types and its unique keys.

    ``columns`` are ``(name, contract type)`` in the contract's order and spelling. ``unique``
    lists each set of column names the contract declares unique for the table; leave it empty
    when they cannot be read, and a ``LIMIT``, ``OFFSET`` or window then needs an ``ORDER BY``
    over every column of the rows instead (stronger is allowed, weaker never).
    """

    columns: Tuple[Tuple[str, str], ...]
    unique: Tuple[Tuple[str, ...], ...] = ()

    @property
    def column_names(self) -> Tuple[str, ...]:
        return tuple(name for name, _ in self.columns)


@dataclass(frozen=True)
class Checked:
    """A text that passed every check.

    ``text`` is the canonical DuckDB text (``sql(dialect="duckdb", comments=False)``); it was
    parsed and checked once more before it was returned, and ``tree`` is the tree of that second
    reading, so the tree checked and the text that would run are one statement.
    """

    kind: str
    #: The canonical text: DuckDB's dialect, over the listed exposeIds and the contract's
    #: column names, comments dropped, every star written out (but ``count(*)``).
    text: str
    #: ``sha256:`` and the lowercase hex digest of the UTF-8 canonical text.
    digest: str
    #: The exposeIds the text reads, sorted.
    tables: Tuple[str, ...]
    #: The ``(exposeId, column)`` pairs the text reads, sorted, in the contract's spelling.
    columns: Tuple[Tuple[str, str], ...]
    #: The names of the columns the rows of the text carry, in order.
    output_columns: Tuple[str, ...]
    sqlglot_version: str
    #: The parsed canonical text (a sqlglot expression). Not part of equality or the repr.
    tree: Any = field(repr=False, compare=False, default=None)


@dataclass(frozen=True)
class Refused:
    """A text the checker refuses: a ``code`` of :data:`REFUSAL_CODES` and a ``reason``.

    The reason names the construct, a table, a column or a function, and the limit passed. It
    never holds the text, a literal of it, or a value of the data.
    """

    kind: str
    code: str
    reason: str


_DECIMAL = re.compile(r"^(?:decimal|numeric|dec)\s*\(\s*(\d{1,2})\s*(?:,\s*(\d{1,2})\s*)?\)$")
_SIMPLE = {
    "string": "VARCHAR",
    "varchar": "VARCHAR",
    "text": "VARCHAR",
    "char": "VARCHAR",
    "uuid": "VARCHAR",
    "byte": "TINYINT",
    "tinyint": "TINYINT",
    "short": "SMALLINT",
    "smallint": "SMALLINT",
    "int": "INTEGER",
    "integer": "INTEGER",
    "long": "BIGINT",
    "bigint": "BIGINT",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
    "float": "DOUBLE",
    "double": "DOUBLE",
    "real": "DOUBLE",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "datetime": "TIMESTAMP",
    "timestamp_ntz": "TIMESTAMP",
    "timestamp_tz": "TIMESTAMP",
}


def duckdb_type(contract_type: str) -> Optional[str]:
    """The DuckDB type the process loads a column of *contract_type* as, or ``None``.

    ``DECIMAL(p, s)`` (also ``numeric``) keeps its precision and scale (at most 38 digits;
    a bare ``decimal`` is ``DECIMAL(18, 3)``); the other names map as the table above says.
    ``None`` means the type is one the process does not load.
    """
    text = contract_type.strip().lower()
    if text in ("decimal", "numeric", "dec"):
        return "DECIMAL(18,3)"
    found = _DECIMAL.match(text)
    if found is not None:
        precision = int(found.group(1))
        scale = int(found.group(2) or 0)
        if 1 <= precision <= 38 and 0 <= scale <= precision:
            return f"DECIMAL({precision},{scale})"
        return None
    return _SIMPLE.get(text)
