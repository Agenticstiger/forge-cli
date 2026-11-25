# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Consumer queries of a profile request: check the text, then run it in a locked process.

A profile request (PROFILE-REQUEST-SPEC 1-1-0, section 6) lists consumer queries
``{name, sql, unit: {table} | {sql}, groupBy}``. Each entry is untrusted text. The profile
checks it again (:func:`check_text`) and runs it under locks (:func:`run_in_child`). This package
is the two parts and nothing of the profile: it reads no request, no contract and no record,
and its reasons name constructs, tables, columns, functions and limits, never a value.

The two parts are adapted from FLUX's ``flux_engine/consumers/`` (Apache-2.0), see ``NOTICE``.
The package costs nothing on ``fluid --help``: importing it loads the standard library only;
sqlglot is loaded by the first :func:`check_text` and DuckDB only inside the child.

Part 1: the check
=================

``check_text(text, *, kind, tables) -> Checked | Refused``

* ``text``: the query text, one statement, in DuckDB's dialect.
* ``kind``: ``"query"`` or ``"view"`` (a ``unit: {sql}``); the checks are the same, the reasons
  say which. Any other value is a ``ValueError``.
* ``tables``: ``{exposeId: Table(columns=((name, contract_type), ...), unique=((col, ...), ...))}``
  for each table the request lists, in the contract's spelling. ``contract_type`` is a contract
  or DuckDB type name (``string``, ``long``, ``decimal(10,2)``, ``date``, ``timestamp``, ...;
  :func:`duckdb_type` is the mapping; an unknown one reads as ``UNKNOWN``). ``unique`` is each set
  of columns the contract declares unique; leave it empty when it cannot be read, and a ``LIMIT``,
  ``OFFSET`` or window then needs an order over every column of the rows.

Returns :class:`Checked`:

* ``text``: the canonical DuckDB text, ``tree.sql(dialect="duckdb", comments=False)`` over the
  listed exposeIds and the contract's column names, every star written out (``count(*)`` stays).
  It was parsed and checked once more before it was returned.
* ``tree``: the parsed canonical text (a sqlglot expression; not compared, not in the repr).
* ``tables``: the exposeIds it reads, sorted. ``columns``: the ``(exposeId, column)`` pairs it
  reads, sorted. ``output_columns``: the names its rows carry, in order (use them to check a
  ``groupBy``). ``digest``: ``sha256:`` of the canonical text. ``sqlglot_version``.

or :class:`Refused` (``kind``, ``code``, ``reason``), never raised. ``code`` is one of
:data:`REFUSAL_CODES`: ``unparsable``, ``several_statements``, ``not_a_query``, ``construct``,
``function``, ``table``, ``ambiguous_table``, ``placeholder``, ``bounds``, ``recursion``,
``column``, ``order``, ``alias``, ``pattern``, ``canonical``, ``schema``, ``internal``. What is
refused (closed lists, :mod:`._allow`): more than one statement; anything but a ``SELECT``, a set
operation of them, or a non-recursive ``WITH`` before one (DDL, DML, ``PRAGMA``, ``SET``,
``ATTACH``, ``COPY``, ``INSTALL``, ``LOAD``, ``EXPORT``, ``CALL``, commands); an expression
class, argument, type or function that is not on the lists (table functions, the clock and
anything that reads the environment, a setting, the catalog, a sequence or a random source); a
table that is not a listed exposeId or a ``WITH`` name before it, a name with a schema part, a
table written as a string or a path; a placeholder; more than 20,000 tokens, 64 nested brackets,
10,000 nodes or 64 levels; a ``RecursionError`` inside sqlglot; a projection alias read in its
own ``SELECT``; a ``LIKE`` pattern past 512 characters; a column in none of the listed tables;
a ``LIMIT``, ``OFFSET`` or window ``ORDER BY`` that is not a total order.

Part 2: the locked child
========================

``run_in_child(tables, statements, *, seconds, memory) -> ChildRun``

* ``tables``: ``{exposeId: [(column, duckdb_type, values), ...]}``. ``duckdb_type`` is
  ``BOOLEAN``, ``TINYINT``, ``SMALLINT``, ``INTEGER``, ``BIGINT``, ``HUGEINT``, ``DOUBLE``,
  ``VARCHAR``, ``DATE``, ``TIMESTAMP`` or ``DECIMAL(p,s)``. ``values``: ints, booleans, text,
  ``Decimal`` or decimal text for a ``DECIMAL`` (loaded exactly), epoch milliseconds (an int)
  for ``DATE`` and ``TIMESTAMP``, ``None`` for NULL. Columns of one table hold as many values as
  it has rows. A table is created with the exposeId as its name, so the canonical text of
  :func:`check_text` runs over it as it stands.
* ``statements``: ``[Statement(id, sql, returns)]`` (or mappings with those keys), one complete
  ``SELECT`` each, for instance ``SELECT count(*) FROM (<canonical text>) AS q``. ``returns``:
  ``"count"`` one row of one whole number; ``"counts"`` the rows of a ``GROUP BY``, each the
  group columns (booleans, whole numbers, text or ``NULL``; a decimal, date or timestamp key
  arrives as its text) and a whole count last; ``"bits"`` one boolean column, one bit per row in
  the order DuckDB returns the rows (fix it in the statement; a ``NULL`` is an error).
* ``seconds`` is each statement's limit and ``memory`` the child's budget in bytes.

Returns :class:`ChildRun` (``results``, ``duckdb_version``, ``limits``, ``environment``,
``restarts``): one :class:`StatementResult` per statement, in order, with ``id``, ``status``,
``reason`` and, when ``status == "ok"``, ``count`` (an int), ``groups`` (a tuple of tuples) or
``bits`` (:class:`Bits`: ``n``, ``packed``, ``bits[i]``, ``to_list()``). ``status`` is one of
:data:`STATUSES`: ``ok``; ``timedOut`` (past ``seconds``, or a child that did not answer and was
ended); ``outOfMemory`` (also when the tables pass the budget while loading: every statement then
takes it); ``error`` (DuckDB or the child refused the statement or the tables: a lock held, a
type, a name, a non-``SELECT``, a ``NULL`` bit, more than 100,000 groups); ``crashed`` (the child
died on it; a fresh child, sent the tables again, runs the rest, and a child that cannot start
leaves the rest ``crashed``). The other statements always run. ``reason`` is a sentence of the
package's own; DuckDB's message never crosses (it can quote a value).

``ValueError`` (naming what is wrong, never a value) is raised for arguments that are not as
described; :class:`ChildUnavailable` (a ``FluidUserError``) when DuckDB is missing, older than
1.5.0 (the sandbox's floor) or does not read back the locks it connected with.

The child is ``sys.executable -I <_child.py>``: the standard library and DuckDB only, no working
directory and no user site on ``sys.path``; an environment of ``PATH`` and ``LC_ALL=C`` only;
every descriptor closed but its two pipes; ``RLIMIT_FSIZE`` 0, ``RLIMIT_CPU`` and (where the
platform enforces it) ``RLIMIT_AS`` before DuckDB is imported, ``RLIMIT_NOFILE`` after it
connects; an in-memory database connected with ``enable_external_access=false``, no
autoload/autoinstall, no community or unsigned extension, no persistent secret,
``python_enable_replacements=false``, ``threads=1``, ``memory_limit``,
``max_temp_directory_size='0B'`` and ``lock_configuration=true``. Data and results cross as
framed JSON, never pickle; each statement is split with DuckDB's ``extract_statements`` and runs
only when that finds one ``SELECT``. This is defense in depth, not an operating-system sandbox:
run a request from someone else inside a container.
"""

from __future__ import annotations

from typing import Mapping

from fluid_build.profiler.consumer_queries._types import (
    REFUSAL_CODES,
    Checked,
    Refused,
    Table,
    duckdb_type,
)
from fluid_build.profiler.consumer_queries.runner import (
    STATUSES,
    Bits,
    ChildRun,
    ChildUnavailable,
    Statement,
    StatementResult,
    run_in_child,
)

__all__ = [
    "REFUSAL_CODES",
    "STATUSES",
    "Bits",
    "Checked",
    "ChildRun",
    "ChildUnavailable",
    "Refused",
    "Statement",
    "StatementResult",
    "Table",
    "check_text",
    "duckdb_type",
    "run_in_child",
]


def check_text(text: str, *, kind: str, tables: Mapping[str, Table]) -> Checked | Refused:
    """Check *text* and return it in canonical form, or refuse it. See the package docstring."""
    from fluid_build.profiler.consumer_queries import check

    return check.check_text(text, kind=kind, tables=tables)
