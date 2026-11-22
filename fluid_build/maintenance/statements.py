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
this check).
"""

from __future__ import annotations

from typing import Any, List, Sequence

#: The writes an action's statement may be.
WRITES = ("DELETE", "UPDATE", "MERGE")

#: Characters a statement may not hold: control characters but tab and newline.
_CONTROL = frozenset(chr(c) for c in range(32) if chr(c) not in "\t\n\r") | {chr(127)}


def _target_name(expression: Any) -> str:
    """The written table's name as ``catalog.db.table`` / ``db.table``, lower case."""
    from sqlglot import exp

    table = expression.this if not isinstance(expression, exp.Table) else expression
    if not isinstance(table, exp.Table):
        return ""
    parts = [p for p in (table.catalog, table.db, table.name) if p]
    return ".".join(parts).lower()


def statement_problem(sql: Any, table: str) -> str:
    """Why ``sql`` may not run as an action on ``table``, or ``""``. ``table`` is the name the
    runner reads the table by, catalog first (``fluid.silver.orders``): a statement names it
    so, since an unqualified name is read in Spark's current catalog, which may be another."""
    if not isinstance(sql, str) or not sql.strip():
        return "is not a SQL statement"
    if len(sql) > 20_000:
        return "is longer than 20,000 characters"
    if "${" in sql:
        return "holds a ${...} variable"
    if any(ch in _CONTROL for ch in sql):
        return "holds a control character"
    try:
        import sqlglot
        from sqlglot import exp
        from sqlglot.errors import ParseError
    except ImportError:  # pragma: no cover - sqlglot is a core dependency
        return "cannot be checked: sqlglot is not installed"
    try:
        parsed = [e for e in sqlglot.parse(sql, read="spark") if e is not None]
    except ParseError:
        return "is not SQL Spark reads"
    if len(parsed) != 1:
        return "must be exactly one statement"
    (statement,) = parsed
    if not isinstance(statement, (exp.Delete, exp.Update, exp.Merge)):
        return f"must be one of {', '.join(WRITES)}"
    if _target_name(statement) != table.lower():
        return f"must write the action's own table, named {table}"
    return ""


def checked_statements(statements: Any, table: str, where: str) -> List[str]:
    """``statements`` as a list, each checked; raises ``ValueError`` naming the first problem
    by its position (the SQL itself is never quoted)."""
    if isinstance(statements, str) or not isinstance(statements, Sequence):
        raise ValueError(f"{where}: the action's statements are not a list of SQL statements")
    out: List[str] = []
    for index, sql in enumerate(statements):
        problem = statement_problem(sql, table)
        if problem:
            raise ValueError(f"{where}: statement {index} {problem}")
        out.append(str(sql).strip().rstrip(";"))
    return out


__all__ = ["WRITES", "checked_statements", "statement_problem"]
