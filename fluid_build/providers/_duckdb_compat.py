# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Version-compat seam for the DuckDB Python client (1.5.x and 2.x).

forge-cli takes ``duckdb>=1.5.0`` with no ceiling (the floor is the sandbox's,
:data:`fluid_build.providers._duckdb_sandbox.MIN_DUCKDB_VERSION`). DuckDB 2.0
needs Python 3.11, so a Python 3.10 install stays on 1.5.x while a newer one
gets 2.x: the engine runs on both, and what differs lives here.

These 2.0 changes reach the engine (duckdb-python v2.0.0 release notes,
"Upgrading from 1.x"; https://duckdb.org/docs/2.0/clients/python/migration.html):

* A ``con.execute()`` result streams, and owns its connection until it is read
  to the end. ``fetchone()`` on a one-row ``COUNT(*)`` leaves it open, and the
  next relation read, ``register`` or ``create_function`` on that connection
  raises ``ResourceInUseException``. Relations lost ``fetchone()`` and
  ``fetchmany()`` outright. :func:`first_row` reads to the end on both.
* ``con.sql()`` of SQL whose last statement returns no rows (``CREATE TABLE
  ... AS``, an ``INSERT`` without ``RETURNING``) returns a relation where 1.5
  returned ``None``: over the statement's row count, or, after an earlier
  query in the same SQL, over that query's rows. Written out as a contract's
  result, that is a one-row ``Count`` file or stale rows. It also runs the
  statements ahead of one that does not parse, where 1.5 runs none, and a
  relation of SQL that ends in a comment after its ``;`` fails to aggregate,
  limit or write ("syntax error at or near ';'"). :func:`query_relation`
  answers as 1.5 does on both.
* A relation whose rows 2.0 already holds (its SQL ran when the relation was
  made: a ``PIVOT`` without an ``IN`` list, a ``SELECT`` calling
  ``nextval()``, ``... RETURNING``) cannot back a view ("it reads rows held in
  this process"). :func:`relation_view` puts those rows in a table instead.
* A database file 2.0 creates uses storage version 2.0, which no 1.5 process can
  open ("can only read versions between 64 and 68"). :func:`storage_compat_config`
  has 2.x write the format 1.5 itself writes, so a file forge-cli creates is the
  same whichever DuckDB created it.

Nothing here imports duckdb at module level: callers pass what they hold, and
:func:`query_relation` reads the installed version when it runs.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, Tuple

#: Storage version 64, the one DuckDB 1.5 writes by default (its own setting
#: reads ``v0.10.2``, which writes the same version), and which DuckDB 1.0 and
#: later open. Database files forge-cli creates on DuckDB 2.x are written in
#: it, so a 1.5 install (Python 3.10, or a tool pinned below 2) opens them, and
#: a 2.x connection accepts the same SQL on them as 1.5 does (a VARIANT column,
#: which needs storage v1.5.0, is refused on both).
STORAGE_COMPATIBILITY_VERSION = "v1.0.0"


def first_row(result: Any) -> Optional[Tuple[Any, ...]]:
    """The first row of ``result`` (a ``con.execute()`` result or a relation), or ``None``.

    Reads the whole result, so give it a query of one row (``COUNT(*)``,
    ``LIMIT 1``). ``fetchone()`` does not do: on DuckDB 2.0 a result read only
    partway keeps its connection busy, and a relation has no ``fetchone()``.
    """
    rows = result.fetchall()
    return rows[0] if rows else None


def one_row(result: Any) -> Tuple[Any, ...]:
    """The row of a query that always returns exactly one (an aggregate, ``COUNT(*)``).

    :func:`first_row`, for the callers that index the row straight away: an
    aggregate without ``GROUP BY`` returns one row even over no input, so an
    empty result is a bug and raises ``ValueError`` rather than passing ``None``.
    """
    row = first_row(result)
    if row is None:
        raise ValueError("an aggregate query returned no row")
    return row


#: Statement types whose result is a query result, whatever it holds: DuckDB 1.5
#: returns a relation for these even when it is a ``Success`` flag
#: (``CHECKPOINT`` is a ``CALL``). Not ``MULTI``, 2.0's type for a ``PIVOT``
#: with columns it must look up first: a bare ``PIVOT`` returns rows, a
#: ``CREATE TABLE ... AS PIVOT`` does not, so its shape decides.
_QUERY_STATEMENTS = frozenset({"SELECT", "EXPLAIN", "CALL", "PRAGMA"})
#: What 2.0 holds for a statement with no query result: the changed-row count
#: (``INSERT``, ``UPDATE``, ``DELETE``, ``MERGE``, ``COPY``), or a success flag.
_NO_RESULT_SHAPES = frozenset({(("Count",), ("BIGINT",)), (("Success",), ("BOOLEAN",))})


#: What the engine says when SQL meant to produce a result ends in a statement
#: that returns no rows (``query_relation`` answered ``None``).
NO_ROWS_TO_WRITE = (
    "The SQL ends in a statement that returns no rows (CREATE, INSERT, UPDATE, ...), so "
    "there is no result to write. End it with the SELECT of the result: for "
    "CREATE TABLE t AS SELECT ..., write just the SELECT ..."
)


def query_relation(con: Any, sql: str) -> Any:
    """``con.sql(sql)``, or ``None`` when the SQL's last statement returns no rows.

    What DuckDB 1.5 answers itself: a relation for a query (``SELECT``,
    ``CALL``, ``EXPLAIN``, ``PRAGMA``, a ``RETURNING`` clause), ``None`` for a
    statement that only changes things, so on 1.5 this is ``con.sql``. DuckDB
    2.0 returns a relation for both, and for SQL of several statements the
    relation of the last one that returned rows, which need not be the last
    statement. On 2.x every statement but the last therefore runs first, and
    the last one alone decides: a query type keeps its relation, and any other
    statement is judged by the shape 2.0 gives one without a query result (a
    ``Count`` of changed rows, or a ``Success`` flag; every DDL, ``SET``,
    ``ATTACH``, ``LOAD`` and transaction statement returns one of the two).
    Each statement runs once, and SQL that does not parse raises before any of
    it runs, as on 1.5. One case stays apart: a ``RETURNING`` clause whose only
    column is a ``BIGINT`` named ``Count``, or a ``BOOLEAN`` named ``Success``,
    reads as no rows on 2.x.
    """
    import duckdb

    from ._duckdb_sandbox import duckdb_version  # the sandbox imports this module

    if duckdb_version(duckdb) < (2, 0, 0):
        return con.sql(sql)
    # Parses the whole SQL first: a statement that does not parse raises here,
    # before any runs (2.0's con.sql() would run the statements ahead of it).
    statements = con.extract_statements(sql)
    if not statements:  # empty, or only comments
        return con.sql(sql)
    for statement in statements[:-1]:
        con.execute(statement.query)
        # To its end without reading its rows into Python (2.0's complete()):
        # a SELECT ahead of the last statement is run for its effects only.
        con.complete()
    last = statements[-1]
    rel = con.sql(_without_trailing_comments(last.query))
    if last.type.name in _QUERY_STATEMENTS:
        return rel
    shape = (tuple(rel.columns), tuple(str(t) for t in rel.types))
    return None if shape in _NO_RESULT_SHAPES else rel


def _without_trailing_comments(statement: str) -> str:
    """``statement`` without the comments that end it.

    DuckDB 2.0 keeps a statement's text with the relation ``con.sql()`` makes
    and reads it again inside the SQL of ``aggregate``, ``limit`` or
    ``write_csv``, where a comment after the closing ``;`` no longer parses.
    ``duckdb.tokenize`` tells a comment from a ``--`` inside a string, and
    gives each token's offset in bytes.
    """
    import duckdb

    cut = None
    for offset, kind in reversed(duckdb.tokenize(statement)):
        if kind != duckdb.token_type.comment:
            break
        cut = offset
    if cut is None:
        return statement
    return statement.encode("utf-8")[:cut].decode("utf-8").rstrip()


@contextmanager
def relation_view(con: Any, rel: Any, name: str) -> Iterator[str]:
    """``name`` reads ``rel`` while the block runs (a view), for SQL such as ``COPY``.

    DuckDB 2.0 refuses a view over a relation whose rows it already holds (see
    the module docstring); those rows go to a table of that name instead. The
    view or table is dropped when the block ends. ``name`` is an identifier the
    caller owns.
    """
    import duckdb

    try:
        rel.create_view(name, replace=True)
        kind = "VIEW"
    except duckdb.InvalidInputException:
        con.execute(f"DROP TABLE IF EXISTS {name}")
        rel.to_table(name)
        kind = "TABLE"
    try:
        yield name
    finally:
        con.execute(f"DROP {kind} IF EXISTS {name}")


def storage_compat_config(version: Tuple[int, int, int]) -> Dict[str, str]:
    """``duckdb.connect`` config that keeps files DuckDB ``version`` creates readable by 1.5.

    Empty before 2.0, where the client's own default is the format 1.5 writes.
    From 2.0, ``storage_compatibility_version`` set at connect (a ``SET`` once
    the file is open is too late) makes each database file the connection
    creates, ``ATTACH`` included, use that format. A file that exists keeps the
    format it has.
    """
    if version >= (2, 0, 0):
        return {"storage_compatibility_version": STORAGE_COMPATIBILITY_VERSION}
    return {}


__all__ = [
    "NO_ROWS_TO_WRITE",
    "STORAGE_COMPATIBILITY_VERSION",
    "first_row",
    "one_row",
    "query_relation",
    "relation_view",
    "storage_compat_config",
]
