# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The SQL a plugin's maintenance action runs, held to one write on its own table.

A plugin action (``fluid_build.maintenance_actions``) returns statements for the table it is
declared on, such as a GDPR erasure's ``DELETE FROM <table> WHERE customer_id IN (SELECT ...)``.
forge's Spark runner executes them, stamped like every commit it makes, before the table's
compaction, so the rows' files are rewritten and the snapshots that held them expire in the
same run.

Each statement must parse (Spark SQL, through sqlglot) as exactly one ``DELETE``, ``UPDATE`` or
``MERGE`` whose target is the action's own table, named as the runner's catalog holds it
(``fluid.<database>.<table>``); it may read other tables. Nothing else is
written: no DDL, no second statement, no ``${...}`` variable (Spark would substitute it after
this check), and, since Spark and sqlglot read them differently, no comment and no backslash.
What ships is sqlglot's own rendering of the statement it checked; :func:`sql_literal` writes a
plugin's option value as a literal that passes.
"""

from __future__ import annotations

from typing import Any, List, Sequence, Tuple

#: The writes an action's statement may be.
WRITES = ("DELETE", "UPDATE", "MERGE")

#: Characters a statement may not hold: control characters but tab and newline.
_CONTROL = frozenset(chr(c) for c in range(32) if chr(c) not in "\t\n\r") | {chr(127)}


def _target_parts(expression: Any) -> Tuple[str, ...]:
    """The written table's name, part by part (``catalog``, ``db``, ``table``), lower case; a
    part that is itself dotted (`` `fluid.silver`.orders ``) is kept whole, so it never equals
    the three plain parts it spells."""
    from sqlglot import exp

    table = expression.this if not isinstance(expression, exp.Table) else expression
    if not isinstance(table, exp.Table):
        return ()
    return tuple(str(p).lower() for p in (table.catalog, table.db, table.name) if p)


def _parsed(sql: str) -> Tuple[Any, str]:
    """``(the one statement sqlglot reads in sql, "")`` or ``(None, why not)``."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    try:
        parsed = [e for e in sqlglot.parse(sql, read="spark") if e is not None]
    except (SqlglotError, RecursionError):  # ParseError, TokenError, nesting too deep
        return None, "is not SQL Spark reads"
    if len(parsed) != 1:
        return None, "must be exactly one statement"
    (statement,) = parsed
    if not isinstance(statement, (exp.Delete, exp.Update, exp.Merge)):
        return None, f"must be one of {', '.join(WRITES)}"
    return statement, ""


def canonical_statement(sql: Any, table: str) -> Tuple[str, str]:
    """``(the statement as forge ships it, "")`` or ``("", why it may not run as an action on
    table)``. ``table`` is the name the runner reads the table by, catalog first
    (``fluid.silver.orders``): a statement names it so, since an unqualified name is read in
    Spark's current catalog, which may be another.

    What ships is sqlglot's own rendering of the statement it checked, without comments, never
    the text it was given: Spark and sqlglot read some text differently (Spark continues a
    ``--`` comment over a backslash-newline), so the bytes the runner executes must be the ones
    whose target was checked. The rendering is read back and checked again."""
    if not isinstance(sql, str) or not sql.strip():
        return "", "is not a SQL statement"
    if len(sql) > 20_000:
        return "", "is longer than 20,000 characters"
    if "${" in sql:
        return "", "holds a ${...} variable"
    if any(ch in _CONTROL for ch in sql):
        return "", "holds a control character"
    if "--" in sql or "/*" in sql:
        return "", (
            "holds '--' or '/*': a statement carries no comment, not even inside a string "
            "(Spark and sqlglot end some comments in different places)"
        )
    if "\\" in sql:
        return "", (
            "holds a backslash: Spark and sqlglot read some escapes differently, so a literal "
            "is written without one (fluid_build.maintenance.sql_literal)"
        )
    try:
        import sqlglot  # noqa: F401 - a core dependency
    except ImportError:  # pragma: no cover
        return "", "cannot be checked: sqlglot is not installed"
    wanted = tuple(table.lower().split("."))
    statement, why = _parsed(sql)
    if statement is None:
        return "", why
    if _target_parts(statement) != wanted:
        return "", f"must write the action's own table, named {table}"
    shipped = statement.sql(dialect="spark", comments=False)
    again, why = _parsed(shipped)
    unsafe = any(mark in shipped for mark in ("${", "--", "/*", "\\"))
    if again is None or _target_parts(again) != wanted or unsafe:
        return "", "does not read back as the statement it was checked as"
    return shipped, ""


def sql_literal(value: str) -> str:
    """``value`` as a Spark SQL string literal a maintenance statement may carry, for a plugin
    that builds its statement from its options. Spark reads ``''`` as two literals side by side,
    not as an escaped quote, and a backslash escape is read differently by sqlglot, so a value
    holding ``'`` or a backslash cannot be written safely and is refused, as are a control
    character, ``${`` and a comment marker.

    Raises:
        ValueError: ``value`` cannot be written as such a literal (it is not quoted back).
    """
    if not isinstance(value, str):
        raise ValueError("a literal is a string")
    if any(ch in _CONTROL or ch in "\t\n\r" for ch in value):
        raise ValueError("a literal holds no control character or line break")
    for mark in ("'", "\\", "${", "--", "/*"):
        if mark in value:
            raise ValueError(f"a literal holds no {mark!r}")
    return f"'{value}'"


def statement_problem(sql: Any, table: str) -> str:
    """Why ``sql`` may not run as an action on ``table``, or ``""`` (:func:`canonical_statement`)."""
    return canonical_statement(sql, table)[1]


def checked_statements(statements: Any, table: str, where: str) -> List[str]:
    """``statements`` as forge ships them (:func:`canonical_statement`), at least one; raises
    ``ValueError`` naming the first problem by its position (the SQL itself is never quoted)."""
    if isinstance(statements, str) or not isinstance(statements, Sequence):
        raise ValueError(f"{where}: the action's statements are not a list of SQL statements")
    if not statements:
        raise ValueError(
            f"{where}: the action returned no statement. Its statements are compiled into the "
            "plan, so they are the SQL that runs every time, reading the rows it acts on then"
        )
    out: List[str] = []
    for index, sql in enumerate(statements):
        shipped, problem = canonical_statement(sql, table)
        if problem:
            raise ValueError(f"{where}: statement {index} {problem}")
        out.append(shipped)
    return out


__all__ = [
    "WRITES",
    "canonical_statement",
    "checked_statements",
    "sql_literal",
    "statement_problem",
]
