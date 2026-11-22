# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The tables a piece of Spark SQL reads and writes, found with a real SQL parser.

``spark.sql("INSERT INTO retail.orders_daily SELECT ... FROM retail.orders")`` reads one table
and writes another. The text is parsed with sqlglot (the ``spark`` dialect; already a core
dependency) and never executed.

What is a table, and what is not:

* a name in a ``FROM`` or ``JOIN`` is a read, except a CTE of the same statement
  (sqlglot answers a CTE in ``FROM`` with a table node, so the CTE names are collected and left
  out), a name registered as a temporary view and a table function (``range(10)``); a
  ``global_temp`` name and a name the statement caches (``CACHE TABLE v AS SELECT ...``) are views
  the reader of the job drops, and a statement that reads one is not closed;
* ``INSERT INTO|OVERWRITE``, ``CREATE TABLE|VIEW ... AS``, ``MERGE INTO``, ``UPDATE`` and
  ``DELETE FROM`` write the table they name; ``CREATE TEMPORARY VIEW`` registers a temporary
  view and writes nothing;
* ``delta.`/path```, ``parquet.`/path``` and the like are paths, with their format;
* ``USE``, ``ALTER``, ``DROP``, ``REFRESH``, ``MSCK`` and the rest touch no data and are left
  alone.

A statement that does not parse is reported in ``notes`` and contributes nothing: nothing is
guessed from text that did not parse.

Borrowed, not depended on: sqlglot's scope rules for CTEs and the way the Airflow Informatica
provider and Apache Superset skip CTE names (``find_all(exp.Table)`` includes them), and
sqllineage's target/source split for ``INSERT``, ``CREATE ... AS`` and ``MERGE``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

#: SQL text longer than this, or with more statements, is not parsed.
MAX_SQL_CHARS = 200_000
MAX_STATEMENTS = 200

HOLE = "__fluidhole{}__"
_HOLE_RE = re.compile(r"__fluidhole(\d+)__")

#: ``delta.`path```: the schema part is a file format, and the name is the path.
_PATH_FORMATS = frozenset({"delta", "parquet", "orc", "csv", "json", "text", "avro", "iceberg"})


@dataclass(frozen=True)
class SqlName:
    """A table or a path a statement names."""

    kind: str  # "table" | "path"
    #: A table: lower-case ``(catalog?, database?, table)``. A path: the path, as written.
    parts: Tuple[str, ...]
    #: The name as a person reads it; for a name with a hole, the holes show as ``{expr}``.
    shown: str
    #: Non-empty when part of the name is a hole (an f-string field that did not fold).
    why: str = ""
    fmt: Optional[str] = None

    @property
    def resolved(self) -> bool:
        return not self.why


@dataclass(frozen=True)
class SqlWrite:
    name: SqlName
    #: ``append``, ``overwrite``, ``merge``, ``update``, ``delete``, ``create`` or ``view``.
    mode: str
    #: The names the statement that writes it reads (the same objects as in ``SqlFacts.reads``):
    #: a statement takes nothing but what its own ``FROM``, ``JOIN`` and ``USING`` name.
    sources: Tuple[SqlName, ...] = ()


@dataclass
class SqlFacts:
    reads: List[SqlName] = field(default_factory=list)
    writes: List[SqlWrite] = field(default_factory=list)
    #: Temporary views the SQL registers (lower-case).
    temp_views: Set[str] = field(default_factory=set)
    notes: List[str] = field(default_factory=list)


def _holes_text(text: str, holes: Dict[str, str]) -> str:
    def shown(match: "re.Match[str]") -> str:
        inner = holes.get(match.group(0), "?")
        # A placeholder written in the SQL text (``${db}``, ``{db}``, ``%(db)s``) shows as
        # written; the expression of an f-string field is put in braces.
        return inner if inner.startswith(("{", "$", "%")) else "{" + inner + "}"

    return _HOLE_RE.sub(shown, text)


def _name_of(table: Any, holes: Dict[str, str]) -> Optional[SqlName]:
    """A :class:`SqlName` for an sqlglot ``Table``, or ``None`` for a table function."""
    from sqlglot import exp

    if not isinstance(table.this, exp.Identifier) or not table.name:
        return None
    catalog, database, name = table.catalog, table.db, table.name
    why = ""
    if any(_HOLE_RE.search(part) for part in (catalog, database, name)):
        why = "a part of the name is not a constant"
    if database.lower() in _PATH_FORMATS and not catalog:
        shown = _holes_text(name, holes)
        return SqlName("path", (name,), shown, why, fmt=database.lower())
    parts = tuple(p.lower() for p in (catalog, database, name) if p)
    shown = _holes_text(".".join(p for p in (catalog, database, name) if p), holes)
    return SqlName("table", parts, shown, why)


def analyze(sql: str, holes: Optional[Dict[str, str]] = None) -> SqlFacts:
    """The reads and writes of the statements in ``sql``.

    ``holes`` maps a hole token (an f-string field that did not fold, which the caller replaced
    by ``__fluidhole<N>__``) to the expression it stood for, so a name with a hole is reported as
    unresolved with the expression shown.
    """
    facts = SqlFacts()
    holes = holes or {}
    if len(sql) > MAX_SQL_CHARS:
        facts.notes.append(f"SQL of more than {MAX_SQL_CHARS:,} characters was not parsed")
        return facts
    import logging

    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    # sqlglot warns (not raises) about syntax it falls back on; the report says what we need.
    logger = logging.getLogger("sqlglot")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        statements = sqlglot.parse(sql, read="spark")
    except (SqlglotError, RecursionError, ValueError):
        facts.notes.append("SQL that did not parse was skipped")
        return facts
    finally:
        logger.setLevel(previous)
    if len(statements) > MAX_STATEMENTS:
        facts.notes.append(f"only the first {MAX_STATEMENTS} statements of the SQL were read")
        statements = statements[:MAX_STATEMENTS]

    for statement in statements:
        if statement is None:
            continue
        if isinstance(statement, exp.Create):
            _create(statement, holes, facts)
        elif isinstance(statement, (exp.Insert, exp.Merge, exp.Update, exp.Delete)):
            _dml(statement, holes, facts)
        elif isinstance(statement, exp.Cache):
            _cache(statement, holes, facts)
        elif isinstance(statement, (exp.Select, exp.Union, exp.Subquery, exp.With)):
            _reads(statement, None, holes, facts)
    return facts


def _target_node(statement: Any) -> Optional[Any]:
    from sqlglot import exp

    this = statement.this
    if isinstance(this, exp.Schema):  # INSERT INTO t (a, b) ...
        this = this.this
    return this if isinstance(this, exp.Table) else None


def _cte_names(statement: Any) -> Set[str]:
    from sqlglot import exp

    return {cte.alias.lower() for cte in statement.find_all(exp.CTE) if cte.alias}


def _reads(
    statement: Any, target: Optional[Any], holes: Dict[str, str], facts: SqlFacts
) -> List[SqlName]:
    """Add the tables ``statement`` reads to ``facts.reads``, and return them."""
    from sqlglot import exp

    found: List[SqlName] = []
    ctes = _cte_names(statement)
    for table in statement.find_all(exp.Table):
        if table is target:
            continue
        name = _name_of(table, holes)
        if name is None:
            continue
        if name.kind == "table":
            if len(name.parts) == 1 and name.parts[0] in ctes:
                continue
        facts.reads.append(name)
        found.append(name)
    return found


def _dml(statement: Any, holes: Dict[str, str], facts: SqlFacts) -> None:
    from sqlglot import exp

    target = _target_node(statement)
    if target is None:
        facts.notes.append("a statement whose target is not a table was skipped")
        return
    name = _name_of(target, holes)
    sources = _reads(statement, target, holes, facts)
    if name is not None:
        if isinstance(statement, exp.Insert):
            mode = "overwrite" if statement.args.get("overwrite") else "append"
        elif isinstance(statement, exp.Merge):
            mode = "merge"
        elif isinstance(statement, exp.Update):
            mode = "update"
        else:
            mode = "delete"
        facts.writes.append(SqlWrite(name, mode, tuple(sources)))


def _cache(statement: Any, holes: Dict[str, str], facts: SqlFacts) -> None:
    """``CACHE TABLE v AS SELECT ...`` registers a view named ``v`` and writes nothing."""
    target = _target_node(statement)
    if target is None or statement.args.get("expression") is None:
        return
    if target.name:
        facts.temp_views.add(target.name.lower())
    _reads(statement, target, holes, facts)


def _create(statement: Any, holes: Dict[str, str], facts: SqlFacts) -> None:
    from sqlglot import exp

    kind = str(statement.args.get("kind") or "").upper()
    target = _target_node(statement)
    if target is None or kind not in {"TABLE", "VIEW"}:
        return
    properties = statement.args.get("properties")
    temporary = bool(properties) and any(
        isinstance(p, exp.TemporaryProperty) for p in properties.expressions
    )
    if temporary:
        if kind == "VIEW" and target.name:
            facts.temp_views.add(target.name.lower())
        _reads(statement, target, holes, facts)
        return
    if statement.args.get("expression") is None:
        return  # DDL only: no data moves
    name = _name_of(target, holes)
    sources = _reads(statement, target, holes, facts)
    if name is not None:
        facts.writes.append(SqlWrite(name, "view" if kind == "VIEW" else "create", tuple(sources)))
