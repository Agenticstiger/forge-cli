# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The rate of each consumer query a profile request lists: how many of its unit's records it keeps.

A request query ``{name, sql, unit, groupBy}`` (PROFILE-REQUEST-SPEC section 6) is answered with
a ``queries`` entry of the same name (PROFILE-SPEC section 5): one ``levelHistogram/1`` over
``["false", "true"]``, where ``k`` is the number of records of the unit and the ``true`` count the
number of rows the query returns, per level of the ``groupBy`` column when it has one. The
unit is a table of the request (its rows) or a view (the rows of the unit's SQL).

**What is answered is decided before any record is read** (:func:`plan_queries`), so that
``--strict`` refuses the rest. A query is answered when

1. the query, and a view unit, pass the checker (:mod:`fluid_build.profiler.consumer_queries`)
   against the request's tables, typed by the contract;
2. it keeps some of its unit's records and changes none: an outer ``SELECT`` whose projection
   holds only columns, from exactly the unit relation (the table; for a view, the one ``WITH``
   entry whose body is the unit's SQL), with at most one ``WHERE``, and nothing else (no join,
   ``DISTINCT``, ``GROUP BY``, ``HAVING``, ``QUALIFY``, ``ORDER BY``, ``LIMIT``, ``OFFSET``,
   window, aggregate, set operation or subquery in the ``WHERE``). The unit's view itself may
   be any query the checker accepts, aggregating and joining ones included;
3. its ``groupBy``, when it has one, names a column of a table unit's records that the query
   also returns under its own name and whose type declares its levels (an ``enum(...)`` or a
   boolean): the groups are those levels, whatever the records hold;
4. every column it reads has a type the query process loads;
5. it falls inside the public bounds: at most :data:`MOST_QUERIES` queries in a request,
   :data:`MOST_PER_UNIT` over one unit, :data:`MOST_SET_PARTS` parts in one table's set check
   (a grouped query adds one per level of its column, and the first adds one more per level),
   :data:`MOST_GROUPED_PER_COLUMN` grouped queries per column, :data:`MOST_GROUP_LEVELS` levels,
   and :data:`MOST_TEXT_TOKENS` tokens of query and view text in the whole request. The budget is
   the sum of the tokenizer's own counts and is spent in the order the request lists the
   queries; a decision therefore never depends on the machine, and the queries past the budget
   are named as not checked.

A private release answers none (:data:`fluid_build.profiler.request.NO_QUERIES_PRIVATE`).

**No request text reaches DuckDB.** What runs is a statement this module builds from the
checked tree of a query (its ``WHERE`` condition, qualifiers rewritten to the table or to the
unit's view), renders with sqlglot and passes through the checker again; the checker's canonical
text of that statement is what the process runs, never the text of the request. Each query also
runs ``SELECT count(*) FROM (<its own checked text>)``, and when the count differs from the one
derived from the unit's records the query is named ("its rows are not its unit's records") and
nothing is published for it.

**Measuring.** The columns the queries and their units read are read even when the request does
not list them, for the queries only. A *population* is the records a unit counts: the rows of a
table (one statement per query returns one bit per row, in the order the rows were read, so each
row's state lines up with the facts' states of the same table), or the rows of one view (one
statement returns the count of every combination of the queries' outcomes).

**Disclosure, as a set.** A query's two cells (``false = k - true`` and ``true``) pass the cell
checks as a fact's do, and either cell withheld withholds the group. The queries of one
population are then checked together (:func:`fluid_build.profiler.disclosure.suppress_differencing`
over the exact counts of every combination of their outcomes), because two queries that differ
by one literal reveal one record by differencing. For a table, the facts of that table and its
queries are one call. Each query is a factor of its own with both outcomes allowed (its text is
not read for links), the conservative side: every empty combination then counts as a zero cell
where zeros are disclosive. For a grouped query the groups of a column are one linked factor
with the group sizes: each group is the query ``query AND column = level`` and the group's size
is a published count, so the queries and groups of one column are checked against each other and
against the table's other queries and facts. The records whose grouping value is null are one
more group, never written, which counts only when it holds a record. A group the set withholds
is written as a fact withheld by it (``D``); a group that is the only way to a withheld group's
size withholds the others the same way, so some groups of a query may be published and others
withheld. Nothing is published that depends on a query's text.

Nothing printed, logged or written names a count of records: a reason names a query, a table, a
column or a bound.
"""

from __future__ import annotations

import datetime
import itertools
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from fluid_build.policy._common import iter_exposes
from fluid_build.profiler import consumer_queries, defaults, disclosure
from fluid_build.profiler._stats import Request, RequestQuery, StatisticsBackend
from fluid_build.profiler.consumer_queries import Checked, Refused, Table

__all__ = [
    "MEMORY_BYTES",
    "MOST_GROUPED_PER_COLUMN",
    "MOST_GROUP_LEVELS",
    "MOST_PER_UNIT",
    "MOST_QUERIES",
    "MOST_SET_PARTS",
    "MOST_TEXT_TOKENS",
    "NOT_A_LEVEL",
    "NOT_LOADED",
    "OUT_OF_MEMORY",
    "PROCESS_STOPPED",
    "RECORD_REASONS",
    "REFUSED_BY_PROCESS",
    "ROWS_NOT_RECORDS",
    "STATEMENT_SECONDS",
    "TIMED_OUT",
    "Measured",
    "QueryPlan",
    "QueryPlans",
    "QueryResult",
    "measure",
    "plan_queries",
    "report_lines",
]

#: The most queries a request lists (the request format's own bound).
MOST_QUERIES = 1024
#: The most queries answered over one unit (one table's rows, or one view).
MOST_PER_UNIT = 64
#: The most tokens of query and view text a request may hold, in all, for the profile to check.
MOST_TEXT_TOKENS = 200_000
#: The most levels a grouping column may have, and the most grouped queries per column.
MOST_GROUP_LEVELS = 32
MOST_GROUPED_PER_COLUMN = 4
#: The most parts of one table's set check that queries add.
MOST_SET_PARTS = 256
#: The time each statement may run, and the memory the query process may hold.
STATEMENT_SECONDS = 60
MEMORY_BYTES = 2 << 30

#: Why a query is not answered once the records are read: only they decide these, so ``--strict``
#: never refuses on them.
NOT_LOADED = "a value of a column the query reads cannot be loaded as the column's type"
TIMED_OUT = "the query process passed its time limit"
OUT_OF_MEMORY = "the query process passed its memory limit"
REFUSED_BY_PROCESS = "the query process refused the statement built from it"
PROCESS_STOPPED = "the query process stopped before it answered"
ROWS_NOT_RECORDS = "its rows are not its unit's records"
NOT_A_LEVEL = "a value of its groupBy column is not a level the contract declares"
RECORD_REASONS = frozenset(
    {
        NOT_LOADED,
        TIMED_OUT,
        OUT_OF_MEMORY,
        REFUSED_BY_PROCESS,
        PROCESS_STOPPED,
        ROWS_NOT_RECORDS,
        NOT_A_LEVEL,
    }
)

_KEPT = "fluid profile answers a query that keeps some of its unit's records: "
_LEVELS = ["false", "true"]
#: A record's outcome under a query: the query does not return it, or returns it.
_OUT, _IN = 1, 2
_OUTCOMES = (_OUT, _IN)
#: The states of a part of a table's set: a fact may also not cover a record.
_ALL_STATES = (0, _OUT, _IN)

Combination = Tuple[int, ...]


@dataclass(frozen=True)
class QueryResult:
    """What the profile says about one query, for the summary: never a count of records.

    ``published`` says some of its groups (or its one group) are published as rounded counts;
    ``groups_published`` is ``(published, groups)`` for a grouped query and ``None`` otherwise;
    ``code`` is the ``CL_CONF_STATUS`` code of the check that withheld it, ``None`` when published.
    """

    name: str
    published: bool
    code: Optional[str] = None
    groups_published: Optional[Tuple[int, int]] = None

    @property
    def whole(self) -> bool:
        return self.published and (
            self.groups_published is None or self.groups_published[0] == self.groups_published[1]
        )


def report_lines(results: Sequence[QueryResult], asked: int) -> List[str]:
    """The summary of the queries a request lists: ``Queries: N of M published whole, K in part.``"""
    if not asked:
        return []
    whole = sum(1 for each in results if each.whole)
    part = sum(1 for each in results if each.published and not each.whole)
    line = f"Queries: {whole} of {asked} published whole"
    return [f"{line}, {part} in part." if part else f"{line}."]


# ---------------------------------------------------------------------------------------------
# Planning, before any record is read
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryPlan:
    """What one request query needs, decided from the request and the contract alone.

    ``reason`` is set when the query is not answered; the other fields then hold what was found
    before the refusal and are not used.
    """

    index: int
    name: str
    reason: Optional[str] = None
    table: Optional[str] = None
    #: The canonical text of the unit's view, for a unit given as SQL.
    view: Optional[str] = None
    #: The canonical text of the query (run only inside the count that checks the rows).
    text: str = field(default="", repr=False)
    group_by: Optional[str] = None
    levels: Tuple[str, ...] = ()
    #: The ``(exposeId, column)`` pairs the query and its unit read.
    reads: Tuple[Tuple[str, str], ...] = ()
    #: The checked ``WHERE`` condition, qualifiers still the query's own (``None``: no condition).
    where: Any = field(default=None, compare=False, repr=False)
    alias: str = ""
    #: The statements built from the checked tree, as the checker wrote them again.
    bits_sql: str = field(default="", repr=False)
    verify_sql: str = field(default="", repr=False)

    @property
    def unit(self) -> Tuple[str, str]:
        return ("table", self.table or "") if self.view is None else ("view", self.view)

    def refused(self, reason: str) -> "QueryPlan":
        return QueryPlan(self.index, self.name, reason=reason)


@dataclass(frozen=True)
class QueryPlans:
    """Every request query, planned, in the request's order."""

    plans: Tuple[QueryPlan, ...] = ()
    #: The request's tables as the checker reads them, and each column's contract type.
    schema: Mapping[str, Table] = field(default_factory=dict)
    #: The column that numbers a table's rows in the query process, by table.
    row_columns: Mapping[str, str] = field(default_factory=dict)

    def answerable(self) -> List[QueryPlan]:
        return [each for each in self.plans if each.reason is None]

    def unanswered(self) -> List[Tuple[str, str]]:
        """Each query decided not answered before any record is read, with why."""
        return [(f"query {each.name}", each.reason) for each in self.plans if each.reason]

    def columns_of(self, table: str) -> List[str]:
        """The columns of *table* the answerable queries and their units read, in order."""
        found: Dict[str, None] = {}
        for plan in self.answerable():
            for name, column in plan.reads:
                if name == table:
                    found[column] = None
        return list(found)

    def types_of(self, table: str) -> Mapping[str, str]:
        return {name: kind for name, kind in self.schema[table].columns}


class _NotKept(Exception):
    """A query that does not keep some of its unit's records; the message says what it found."""


def plan_queries(
    contract: Mapping[str, Any],
    request: Request,
    enum_levels: Callable[[str], Sequence[str]],
) -> QueryPlans:
    """Plan each query of *request* against *contract*: answered, or named with why not.

    Reads no record. *enum_levels* reads the levels an ``enum(...)`` type declares (the
    registry's reader, :meth:`~fluid_build.profiler._stats.StatisticsBackend.enum_levels`).
    """
    if not request.queries:
        return QueryPlans()
    schema = _schema_of(contract, request)
    rows = {table: _row_column(columns) for table, columns in schema.items()}
    planner = _Planner(schema, rows, enum_levels)
    plans = tuple(planner.plan(index, each) for index, each in enumerate(request.queries))
    plans = _statements_of_views(plans, schema)
    return QueryPlans(plans, schema, rows)


def _schema_of(contract: Mapping[str, Any], request: Request) -> Dict[str, Table]:
    exposes = {
        str(item.get("exposeId")): item
        for item in iter_exposes(dict(contract))
        if item.get("exposeId")
    }
    found: Dict[str, Table] = {}
    for each in request.tables:
        body = exposes.get(each.expose_id, {}).get("contract")
        entries = body.get("schema") if isinstance(body, Mapping) else None
        columns = tuple(
            (str(entry["name"]), str(entry.get("type") or ""))
            for entry in (entries if isinstance(entries, list) else ())
            if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
        )
        found[each.expose_id] = Table(columns=columns)
    return found


def _row_column(table: Table) -> str:
    taken = {name.lower() for name in table.column_names}
    name = "__row"
    while name in taken:
        name += "_"
    return name


def _tokens(text: str) -> int:
    """The tokenizer's own count of *text* (what the checker bounds), 0 for a text it cannot read."""
    try:
        from sqlglot.dialects.dialect import Dialect

        return len(Dialect.get_or_raise("duckdb").tokenize(text))
    except Exception:  # a text the tokenizer cannot read is refused by the checker, cheaply
        return 0


def _blank(value: Any) -> bool:
    return value is None or value is False or (isinstance(value, (list, tuple, dict)) and not value)


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class _Planner:
    def __init__(
        self,
        schema: Mapping[str, Table],
        rows: Mapping[str, str],
        enum_levels: Callable[[str], Sequence[str]],
    ) -> None:
        self._schema = schema
        self._rows = rows
        self._enum_levels = enum_levels
        self._spent = 0
        self._exhausted = False
        self._counted: Set[str] = set()
        self._views: Dict[str, Union[Checked, Refused]] = {}
        self._per_unit: Dict[Tuple[str, str], int] = {}
        self._parts: Dict[str, int] = {}
        self._grouped: Dict[Tuple[str, str], int] = {}

    # -- one query ---------------------------------------------------------------------------

    def plan(self, index: int, query: RequestQuery) -> QueryPlan:
        blank = QueryPlan(index, query.name)
        if index >= MOST_QUERIES:
            return blank.refused(f"past the {MOST_QUERIES:,} queries a request may list")
        why = self._budget(query)
        if why is not None:
            return blank.refused(why)
        checked = consumer_queries.check_text(query.sql, kind="query", tables=self._schema)
        if isinstance(checked, Refused):
            return blank.refused(f"the check refuses the query: {checked.reason}")
        view: Optional[Checked] = None
        if query.unit_sql is not None:
            found = self._view(query.unit_sql)
            if isinstance(found, Refused):
                return blank.refused(f"the check refuses the unit's view: {found.reason}")
            view = found
        elif query.table not in self._schema:
            return blank.refused("its unit's table is not one the request lists")
        try:
            where, alias = _kept(
                checked, table=query.table, view=None if view is None else view.text
            )
        except _NotKept as found_not_kept:
            return blank.refused(_KEPT + str(found_not_kept))
        reads = tuple(sorted(set(checked.columns)))
        levels: Tuple[str, ...] = ()
        if query.group_by is not None:
            grouped = self._group(query, checked, query.table or "")
            if isinstance(grouped, str):
                return blank.refused(grouped)
            levels = grouped
        why = self._loadable(reads)
        if why is not None:
            return blank.refused(why)
        plan = QueryPlan(
            index,
            query.name,
            table=query.table,
            view=None if view is None else view.text,
            text=checked.text,
            group_by=query.group_by,
            levels=levels,
            reads=reads,
            where=where,
            alias=alias,
        )
        why = self._room(plan)
        if why is not None:
            return blank.refused(why)
        built = self._statements(plan)
        if isinstance(built, str):
            return blank.refused(built)
        return replace(plan, **built)

    # -- the public budget -------------------------------------------------------------------

    def _budget(self, query: RequestQuery) -> Optional[str]:
        if self._exhausted:
            return self._over()
        cost = _tokens(query.sql)
        if query.unit_sql is not None and query.unit_sql not in self._counted:
            cost += _tokens(query.unit_sql)
        if self._spent + cost > MOST_TEXT_TOKENS:
            self._exhausted = True
            return self._over()
        self._spent += cost
        if query.unit_sql is not None:
            self._counted.add(query.unit_sql)
        return None

    @staticmethod
    def _over() -> str:
        return (
            f"not checked: the request's query and view texts pass the {MOST_TEXT_TOKENS:,} "
            "tokens fluid profile checks, counted in the order the request lists the queries"
        )

    def _view(self, text: str) -> Union[Checked, Refused]:
        if text not in self._views:
            self._views[text] = consumer_queries.check_text(text, kind="view", tables=self._schema)
        return self._views[text]

    # -- groupBy -----------------------------------------------------------------------------

    def _group(
        self, query: RequestQuery, checked: Checked, table: str
    ) -> Union[str, Tuple[str, ...]]:
        name = str(query.group_by)
        if query.table is None:
            return "fluid profile groups a query only over a table unit's records"
        kind = self.types(table).get(name)
        if kind is None:
            return f"its groupBy {name} is not a column of {table}"
        levels = _levels_of(kind, self._enum_levels)
        if not levels:
            return f"its groupBy {name} is a column whose type declares no levels"
        if len(levels) > MOST_GROUP_LEVELS:
            return (
                f"its groupBy {name} has more than the {MOST_GROUP_LEVELS} levels a grouping holds"
            )
        if not _returns_plainly(checked, name):
            return (
                f"its groupBy {name} is not a column its rows return under the name "
                f"the {table} column has"
            )
        return levels

    def types(self, table: str) -> Mapping[str, str]:
        return {name: kind for name, kind in self._schema[table].columns}

    # -- types, room, statements -------------------------------------------------------------

    def _loadable(self, reads: Sequence[Tuple[str, str]]) -> Optional[str]:
        for table, column in reads:
            kind = self.types(table).get(column, "")
            if _duck(kind) is None:
                return (
                    f"it reads column {column} of {table}, whose type the query process "
                    "does not load"
                )
        return None

    def _room(self, plan: QueryPlan) -> Optional[str]:
        unit = plan.unit
        if self._per_unit.get(unit, 0) >= MOST_PER_UNIT:
            return f"past the {MOST_PER_UNIT} queries over one unit that fluid profile answers"
        table = plan.table
        parts = 1
        if plan.group_by is not None and table is not None:
            column = (table, plan.group_by)
            if self._grouped.get(column, 0) >= MOST_GROUPED_PER_COLUMN:
                return (
                    f"past the {MOST_GROUPED_PER_COLUMN} grouped queries per column "
                    "that fluid profile answers"
                )
            parts = len(plan.levels) * (2 if self._grouped.get(column, 0) == 0 else 1)
        if table is not None and self._parts.get(table, 0) + parts > MOST_SET_PARTS:
            return f"past the {MOST_SET_PARTS} parts one table's set check holds"
        self._per_unit[unit] = self._per_unit.get(unit, 0) + 1
        if table is not None:
            self._parts[table] = self._parts.get(table, 0) + parts
            if plan.group_by is not None:
                column = (table, plan.group_by)
                self._grouped[column] = self._grouped.get(column, 0) + 1
        return None

    def _statements(self, plan: QueryPlan) -> Union[str, Dict[str, Any]]:
        verify = _checked_statement(
            f"SELECT count(*) FROM ({plan.text}) AS {_ident('q')}", self._schema
        )
        if isinstance(verify, Refused):
            return (
                f"the statement that counts its rows is refused when checked again: {verify.reason}"
            )
        found: Dict[str, Any] = {"verify_sql": verify.text}
        if plan.table is not None:
            row = self._rows[plan.table]
            tables = {**self._schema, plan.table: _with_row(self._schema[plan.table], row)}
            condition = _condition(plan.where, plan.alias, plan.table)
            bits = _checked_statement(
                f"SELECT COALESCE(({condition}), FALSE) FROM {_ident(plan.table)} AS "
                f"{_ident(plan.table)} ORDER BY {_ident(plan.table)}.{_ident(row)}",
                tables,
            )
            if isinstance(bits, Refused):
                return f"the statement that reads its outcomes is refused when checked again: {bits.reason}"
            found["bits_sql"] = bits.text
        return found


def _duck(contract_type: str) -> Optional[str]:
    """The DuckDB type a column of *contract_type* is loaded as; an ``enum(...)`` is text."""
    if re.match(r"\s*enum\s*\(", contract_type, re.IGNORECASE):
        return "VARCHAR"
    return consumer_queries.duckdb_type(contract_type)


def _with_row(table: Table, column: str) -> Table:
    return Table(columns=(*table.columns, (column, "long")), unique=table.unique)


def _checked_statement(text: str, tables: Mapping[str, Table]) -> Union[Checked, Refused]:
    return consumer_queries.check_text(text, kind="query", tables=tables)


def _condition(where: Any, alias: str, qualifier: str) -> str:
    """The checked ``WHERE`` condition written over the relation named *qualifier*; ``TRUE`` if none."""
    if where is None:
        return "TRUE"
    from sqlglot import exp

    rebuilt = where.copy()
    for column in rebuilt.find_all(exp.Column):
        column.set("table", exp.to_identifier(qualifier, quoted=True))
    return str(rebuilt.sql(dialect="duckdb", comments=False))


def _statements_of_views(
    plans: Sequence[QueryPlan], schema: Mapping[str, Table]
) -> Tuple[QueryPlan, ...]:
    """Check, for each view, the one statement that counts its rows by their outcomes."""
    refused: Dict[int, str] = {}
    for view, members in _by_view(plans).items():
        built = view_statement(view, members, schema)
        if isinstance(built, str):
            refused.update({each.index: built for each in members})
    return tuple(
        each.refused(refused[each.index]) if each.index in refused else each for each in plans
    )


def _by_view(plans: Sequence[QueryPlan]) -> Dict[str, List[QueryPlan]]:
    found: Dict[str, List[QueryPlan]] = {}
    for each in plans:
        if each.reason is None and each.view is not None:
            found.setdefault(each.view, []).append(each)
    return found


def view_statement(
    view: str, members: Sequence[QueryPlan], schema: Mapping[str, Table]
) -> Union[str, Checked]:
    """The statement that counts a view's rows under every combination of *members*' outcomes.

    One ``GROUP BY`` over each query's condition (as a boolean, a missing one is true), built from
    the checked trees and checked again. Returns the checked statement or why it is refused.
    """
    flags = ", ".join(
        f"COALESCE(({_condition(each.where, each.alias, 'u')}), FALSE)" for each in members
    )
    keys = ", ".join(str(position) for position in range(1, len(members) + 1))
    built = _checked_statement(
        f"SELECT {flags}, count(*) FROM ({view}) AS {_ident('u')} GROUP BY {keys}", schema
    )
    if isinstance(built, Refused):
        return f"the statement that counts the unit's view is refused when checked again: {built.reason}"
    return built


# -- the shape of a query that keeps some of its unit's records ---------------------------------

_KEPT_ARGS = frozenset({"expressions", "from_", "where", "with_"})
_WHAT: Mapping[str, str] = {
    "joins": "it joins another relation",
    "group": "it groups its rows",
    "having": "it filters groups",
    "qualify": "it filters by a window",
    "order": "it orders its rows",
    "limit": "it limits its rows",
    "offset": "it skips rows",
    "distinct": "it takes distinct rows",
    "windows": "it names a window",
    "laterals": "it uses a lateral",
    "pivots": "it pivots",
    "sample": "it samples its rows",
    "into": "it writes its rows",
    "kind": "it is not a plain SELECT",
}


def _kept(checked: Checked, *, table: Optional[str], view: Optional[str]) -> Tuple[Any, str]:
    """The ``WHERE`` condition of *checked*, and the alias it reads its relation under.

    Raises :class:`_NotKept` with what it found when the query is not an outer ``SELECT`` of
    columns from exactly its unit, with at most one ``WHERE``.
    """
    from sqlglot import exp

    tree = checked.tree
    if not isinstance(tree, exp.Select):
        raise _NotKept("it is a set operation or another kind of statement, not one plain query")
    for key, value in tree.args.items():
        if key not in _KEPT_ARGS and not _blank(value):
            raise _NotKept(_WHAT.get(key, f"it uses {key}"))
    relation = _relation(tree, view)
    if table is not None and relation != table:
        raise _NotKept("it reads a relation that is not its unit's table")
    from_ = tree.args.get("from_")
    source = from_.this if isinstance(from_, exp.From) else None
    if not isinstance(source, exp.Table):
        raise _NotKept("it reads a relation that is not a table")
    for key, value in source.args.items():
        if key not in ("this", "alias") and not _blank(value):
            raise _NotKept(f"its FROM holds {key}, not only its unit")
    alias = str(source.alias or source.name)
    for item in tree.args.get("expressions") or []:
        column = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(column, exp.Column):
            raise _NotKept("its projection holds an expression that is not a column")
        if str(column.table) != alias:
            raise _NotKept("its projection reads a column of another relation")
    where = tree.args.get("where")
    if where is None:
        return None, alias
    condition = where.this
    for node in condition.walk():
        if isinstance(node, (exp.Subquery, exp.Select, exp.Window, exp.AggFunc, exp.Star)):
            raise _NotKept("its WHERE holds a subquery, an aggregate or a window")
        if isinstance(node, exp.Column) and str(node.table) != alias:
            raise _NotKept("its WHERE reads a column of another relation")
    return condition, alias


def _relation(tree: Any, view: Optional[str]) -> str:
    """The name of the relation the query reads: its table, or the one ``WITH`` entry of its view."""
    from sqlglot import exp

    from_ = tree.args.get("from_")
    source = from_.this if isinstance(from_, exp.From) else None
    if not isinstance(source, exp.Table):
        raise _NotKept("it reads a relation that is not a table")
    with_ = tree.args.get("with_")
    if view is None:
        if not _blank(with_):
            raise _NotKept("it has a WITH clause but its unit is a table")
        return str(source.name)
    entries = with_.expressions if isinstance(with_, exp.With) else []
    if len(entries) != 1 or with_.args.get("recursive"):
        raise _NotKept("its WITH does not hold exactly one entry, the unit's view")
    entry = entries[0]
    alias = entry.args.get("alias")
    if alias is not None and not _blank(alias.args.get("columns")):
        raise _NotKept("its WITH entry renames the columns of the unit's view")
    if str(entry.alias) != str(source.name):
        raise _NotKept("it does not read its WITH entry")
    if str(entry.this.sql(dialect="duckdb", comments=False)) != view:
        raise _NotKept("its WITH entry is not the unit's view")
    return str(source.name)


def _returns_plainly(checked: Checked, name: str) -> bool:
    """Whether a column of the unit's table named *name* is among the query's rows as itself."""
    from sqlglot import exp

    tree = checked.tree
    for item in tree.args.get("expressions") or []:
        column = item.this if isinstance(item, exp.Alias) else item
        written = item.alias if isinstance(item, exp.Alias) else column.name
        if isinstance(column, exp.Column) and str(column.name) == name and str(written) == name:
            return True
    return False


def _levels_of(contract_type: str, enum_levels: Callable[[str], Sequence[str]]) -> Tuple[str, ...]:
    """The levels a column's type declares, sorted: a boolean's two, an ``enum(...)``'s list."""
    text = re.sub(r"\s+", " ", contract_type.strip().lower())
    if re.sub(r"\s*\(.*\)$", "", text) in defaults.BOOLEAN_TYPES:
        return tuple(defaults.BOOLEAN_LEVELS)
    if re.match(r"\s*enum\s*\(", contract_type, re.IGNORECASE):
        try:
            return tuple(sorted(set(enum_levels(contract_type))))
        except ValueError:  # the registry's grammar refuses the list: no levels it can name
            return ()
    return ()


# ---------------------------------------------------------------------------------------------
# Loading the records into the query process
# ---------------------------------------------------------------------------------------------

_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
_MILLIS_PER_DAY = 86_400_000
_BOUNDS: Mapping[str, Tuple[int, int]] = {
    "TINYINT": (-(2**7), 2**7 - 1),
    "SMALLINT": (-(2**15), 2**15 - 1),
    "INTEGER": (-(2**31), 2**31 - 1),
    "BIGINT": (-(2**63), 2**63 - 1),
    "HUGEINT": (-(2**127), 2**127 - 1),
}
_WHOLE = re.compile(r"[+-]?[0-9]+")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _loaded(duck: str, values: Sequence[Any]) -> Optional[List[Any]]:
    """*values* as the query process takes a column of the DuckDB type *duck*, or ``None``.

    ``None`` when any value cannot be read exactly as that type (never says which). A value of
    the wrong class, a number that does not fit, a decimal with more digits than its scale and a
    time with less than a millisecond are not rounded or cut: the column is not loaded.
    """
    read = _READERS.get(duck.split("(")[0])
    if read is None:
        return None
    found: List[Any] = []
    try:
        for value in values:
            found.append(None if value is None else read(duck, value))
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None
    return found


def _text(duck: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError
    return value


def _boolean(duck: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ValueError


def _whole(duck: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and _WHOLE.fullmatch(value):
        number = int(value)
    elif isinstance(value, Decimal) and value == value.to_integral_value():
        number = int(value)
    else:
        raise ValueError
    low, high = _BOUNDS[duck]
    if not low <= number <= high:
        raise ValueError
    return number


def _decimal(duck: str, value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
        raise ValueError
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    precision, scale = (int(each) for each in re.findall(r"[0-9]+", duck))
    if not number.is_finite() or number != number.quantize(Decimal(1).scaleb(-scale)):
        raise ValueError
    if abs(number) >= Decimal(10) ** (precision - scale):
        raise ValueError
    return number


def _double(duck: str, value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, (int, float, Decimal)):
        return value
    if isinstance(value, str):
        return Decimal(value)
    raise ValueError


def _instant(text: str) -> int:
    """Epoch milliseconds of an RFC 3339 time with an offset; refuses what has less than 1 ms."""
    if not isinstance(text, str):
        raise ValueError
    spelled = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    moment = datetime.datetime.fromisoformat(spelled)
    if moment.tzinfo is None or moment.microsecond % 1000:
        raise ValueError
    delta = moment - _EPOCH
    return delta.days * _MILLIS_PER_DAY + delta.seconds * 1000 + delta.microseconds // 1000


def _timestamp(duck: str, value: Any) -> int:
    return _instant(value)


def _day(duck: str, value: Any) -> int:
    if isinstance(value, str) and _DAY.fullmatch(value):
        return (
            datetime.date.fromisoformat(value) - datetime.date(1970, 1, 1)
        ).days * _MILLIS_PER_DAY
    millis = _instant(value)  # a day read from a Parquet file or a database is its midnight in UTC
    if millis % _MILLIS_PER_DAY:
        raise ValueError
    return millis


_READERS: Mapping[str, Callable[[str, Any], Any]] = {
    "VARCHAR": _text,
    "BOOLEAN": _boolean,
    "TINYINT": _whole,
    "SMALLINT": _whole,
    "INTEGER": _whole,
    "BIGINT": _whole,
    "HUGEINT": _whole,
    "DECIMAL": _decimal,
    "DOUBLE": _double,
    "DATE": _day,
    "TIMESTAMP": _timestamp,
}


# ---------------------------------------------------------------------------------------------
# Measuring: one run of the query process for every answerable query
# ---------------------------------------------------------------------------------------------


@dataclass
class _Member:
    """One answered query over a table: each record's outcome, and its groups."""

    plan: QueryPlan
    n: int
    outcomes: bytes
    true: int
    #: The level index of each record's grouping value (``None``: no group), grouped queries only.
    assigned: Optional[List[Optional[int]]] = None
    #: ``(records, records the query returns)`` of each level, grouped queries only.
    sizes: Optional[List[Tuple[int, int]]] = None


@dataclass
class _Own:
    """The cell checks of one query's groups, then what the set check added."""

    #: ``None``: published; else the verdict that withholds the group.
    verdicts: List[Optional[disclosure.Verdict]]


@dataclass
class _ViewPopulation:
    members: List[QueryPlan]
    cells: Dict[Combination, int]
    n: int
    trues: List[int]


class Measured:
    """The answers of one run, and what the set checks and the document need of them."""

    def __init__(self, plans: QueryPlans) -> None:
        self.plans = plans
        self.failed: Dict[int, str] = {}
        self.tables: Dict[str, List[_Member]] = {}
        self.views: Dict[str, _ViewPopulation] = {}
        self._own: Dict[int, _Own] = {}
        self._members: Dict[int, _Member] = {}
        self._population: Dict[int, int] = {}
        self._checked: Set[str] = set()

    def has_table(self, table: str) -> bool:
        return bool(self.tables.get(table))

    # -- the cell checks of every group -----------------------------------------------------

    def check_cells(self, rule: disclosure.DisclosureRule) -> None:
        """Pass each group's two cells through the cell checks, before any set check."""
        for members in self.tables.values():
            for each in members:
                self._members[each.plan.index] = each
                if each.sizes is None:
                    cells = [[each.n - each.true, each.true]]
                else:
                    cells = [[k - true, true] for k, true in each.sizes]
                self._own[each.plan.index] = _Own([_cell_verdict(pair, rule) for pair in cells])
                self._population[each.plan.index] = each.n
        for view in self.views.values():
            for position, plan in enumerate(view.members):
                pair = [view.n - view.trues[position], view.trues[position]]
                self._own[plan.index] = _Own([_cell_verdict(pair, rule)])
                self._population[plan.index] = view.n

    # -- views: one set per view -------------------------------------------------------------

    def check_views(self, rule: disclosure.DisclosureRule) -> None:
        for view in self.views.values():
            members = view.members
            passed = [
                at for at, plan in enumerate(members) if self._own[plan.index].verdicts[0] is None
            ]

            def factors(order: Sequence[int]) -> disclosure.Factors:
                return [((at,), frozenset({(_OUT,), (_IN,)})) for at in order]

            withheld = disclosure.suppress_differencing(
                view.cells, passed, rule, factors, _OUTCOMES
            )
            for at in withheld:
                self._own[members[at].index].verdicts[0] = _SET_VERDICT

    # -- a table: its queries and its facts in one set --------------------------------------

    def check_table(
        self,
        table: str,
        facts: Sequence[str],
        fact_states: Mapping[str, bytes],
        fact_passed: Set[str],
        together: Any,
        rule: disclosure.DisclosureRule,
    ) -> Set[str]:
        """Check *table*'s queries and *facts* as one set; return the facts to withhold.

        *together* is what the facts' texts allow (:func:`fluid_build.profiler.facts.states_together`).
        The queries' groups the set withholds are recorded for :meth:`entries`.
        """
        self._checked.add(table)
        layout = _Layout(self.tables.get(table, []), self._own, facts, fact_states, fact_passed)
        if not layout.width:
            return set()
        cells = Counter(zip(*layout.states, strict=True))
        dead: Set[int] = set()

        def factors(order: Sequence[int]) -> disclosure.Factors:
            return layout.factors(order, together)

        while True:
            candidates = [at for at in range(layout.width) if at not in dead]
            found = set(
                disclosure.suppress_differencing(cells, candidates, rule, factors, _ALL_STATES)
            )
            new = (found | layout.dependants(dead | found)) - dead
            if not new:
                break
            dead |= new
        for at in dead:
            if at in layout.query_of:
                index, group = layout.query_of[at]
                if self._own[index].verdicts[group] is None:
                    self._own[index].verdicts[group] = _SET_VERDICT
        return {layout.fact_of[at] for at in dead if at in layout.fact_of}

    # -- the entries -------------------------------------------------------------------------

    def entries(
        self, rule: disclosure.DisclosureRule, tally: "Counter[str]"
    ) -> Tuple[List[Dict[str, Any]], Tuple[QueryResult, ...], List[Tuple[str, str]]]:
        """The ``queries`` entries, what became of every query listed, and those not answered."""
        for table in self.tables:
            if table not in self._checked:
                self.check_table(table, (), {}, set(), None, rule)
        written: List[Dict[str, Any]] = []
        results: List[QueryResult] = []
        left: List[Tuple[str, str]] = []
        for plan in self.plans.plans:
            why = plan.reason or self.failed.get(plan.index)
            if why is not None:
                left.append((f"query {plan.name}", why))
                results.append(QueryResult(plan.name, False))
                continue
            if plan.index not in self._own:
                continue
            entry, result = self._entry(plan, rule, tally)
            written.append(entry)
            results.append(result)
        return written, tuple(results), left

    def _entry(
        self, plan: QueryPlan, rule: disclosure.DisclosureRule, tally: "Counter[str]"
    ) -> Tuple[Dict[str, Any], QueryResult]:
        verdicts = self._own[plan.index].verdicts
        n = self._population[plan.index]
        member = self._members.get(plan.index)
        if member is not None and member.sizes is not None:
            keys = plan.levels
            pairs = member.sizes
        else:
            keys = ("",)
            pairs = [(n, self._true_of(plan))]
        groups: List[Dict[str, Any]] = []
        for key, (k, true), verdict in zip(keys, pairs, verdicts, strict=True):
            if verdict is None:
                tally[disclosure.FREE] += 1
                groups.append(
                    {
                        "group": key,
                        "status": disclosure.FREE,
                        "k": disclosure.round_count(k, rule),
                        "levelCounts": [
                            disclosure.round_count(k - true, rule),
                            disclosure.round_count(true, rule),
                        ],
                    }
                )
            else:
                tally[verdict.status] += 1
                groups.append({"group": key, "status": disclosure.WITHHELD})
        statistic: Dict[str, Any] = {
            "definition": "levelHistogram/1",
            "sampleSize": disclosure.sample_size(n, rule),
            "disclosure": rule.name,
            "levels": list(_LEVELS),
            "groups": groups,
        }
        if plan.group_by is not None:
            statistic["groupBy"] = plan.group_by
        shown = sum(1 for each in verdicts if each is None)
        code = next((each.status for each in verdicts if each is not None), None)
        result = QueryResult(
            plan.name,
            shown > 0,
            None if shown else code,
            (shown, len(verdicts)) if plan.group_by is not None else None,
        )
        return {"name": plan.name, "statistics": [statistic]}, result

    def _true_of(self, plan: QueryPlan) -> int:
        member = self._members.get(plan.index)
        if member is not None:
            return member.true
        for view in self.views.values():
            for position, each in enumerate(view.members):
                if each.index == plan.index:
                    return view.trues[position]
        raise KeyError(plan.index)


#: What a set check withholds a group with: the code facts the set withholds are written with.
_SET_VERDICT = disclosure.Verdict(
    disclosure.SECONDARY,
    "with the other queries and facts of its unit it would give back a small cell",
)


def _cell_verdict(
    counts: Sequence[int], rule: disclosure.DisclosureRule
) -> Optional[disclosure.Verdict]:
    """The verdict of a group's two cells: the first one withheld, ``None`` when both pass."""
    for count in counts:
        verdict = disclosure.check_count(count, rule)
        if verdict.published:
            verdict = disclosure.check_rounding(count, rule)
        if not verdict.published:
            return verdict
    return None


class _Layout:
    """The positions of one table's set check: facts, ungrouped queries, and each column's groups.

    A position is a part whose outcome the exact counts are formed over: a fact (states
    ``0``..``2``), an ungrouped query (outcomes ``1``, ``2``), the size of a group of a column,
    and the group of a grouped query (the query's outcome among the group's records, else ``1``).
    Only parts that passed their own checks are laid out; a part left out is merged, as the set
    check merges a part it withholds.
    """

    def __init__(
        self,
        members: Sequence[_Member],
        own: Mapping[int, _Own],
        facts: Sequence[str],
        fact_states: Mapping[str, bytes],
        fact_passed: Set[str],
    ) -> None:
        self.states: List[bytes] = []
        self.fact_of: Dict[int, str] = {}
        self.query_of: Dict[int, Tuple[int, int]] = {}
        self._fact_at: Dict[str, int] = {}
        self._single: List[Tuple[int, int]] = []
        #: For each grouped column: its parts, with the conceptual rule of which combinations occur.
        self._columns: List[_Column] = []
        for name in facts:
            if name in fact_passed:
                at = len(self.states)
                self.states.append(fact_states[name])
                self.fact_of[at] = name
                self._fact_at[name] = at
        by_column: Dict[str, List[_Member]] = {}
        for each in members:
            verdicts = own[each.plan.index].verdicts
            if each.plan.group_by is None:
                if verdicts[0] is None:
                    at = len(self.states)
                    self.states.append(each.outcomes)
                    self.query_of[at] = (each.plan.index, 0)
                    self._single.append((at, each.plan.index))
            elif any(verdict is None for verdict in verdicts):
                by_column.setdefault(each.plan.group_by, []).append(each)
        for column, grouped in by_column.items():
            self._columns.append(self._add_column(column, grouped, own))
        self.width = len(self.states)

    def _add_column(
        self, column: str, grouped: Sequence[_Member], own: Mapping[int, _Own]
    ) -> "_Column":
        first = grouped[0]
        assigned = first.assigned or []
        levels = len(first.plan.levels)
        alive = [[verdict is None for verdict in own[each.plan.index].verdicts] for each in grouped]
        sized = [at for at in range(levels) if any(row[at] for row in alive)]
        parts: List[Tuple[str, int, int]] = []  # ("P", level, -1) or ("R", level, query slot)
        for level in sized:
            parts.append(("P", level, -1))
        for slot, row in enumerate(alive):
            for level in sized:
                if row[level]:
                    parts.append(("R", level, slot))
        positions: List[int] = []
        for kind, level, slot in parts:
            at = len(self.states)
            positions.append(at)
            if kind == "P":
                self.states.append(bytes(_IN if g == level else _OUT for g in assigned))
            else:
                outcomes = grouped[slot].outcomes
                self.states.append(
                    bytes(
                        _IN if g == level and out == _IN else _OUT
                        for g, out in zip(assigned, outcomes, strict=True)
                    )
                )
                self.query_of[at] = (grouped[slot].plan.index, level)
        kept = set(sized)
        outside = any(g is None or g not in kept for g in assigned)
        return _Column(parts, positions, outside)

    def factors(self, order: Sequence[int], together: Any) -> disclosure.Factors:
        shown = set(order)
        found: List[Tuple[Tuple[int, ...], FrozenSet[Combination]]] = []
        names = [name for name, at in self._fact_at.items() if at in shown]
        if names:
            for members, allowed in together.factors(names):
                found.append((tuple(self._fact_at[each] for each in members), allowed))
        for at, _ in self._single:
            if at in shown:
                found.append(((at,), frozenset({(_OUT,), (_IN,)})))
        for column in self._columns:
            kept = [at for at in column.positions if at in shown]
            if kept:
                found.append((tuple(kept), column.allowed_over(kept)))
        return found

    def dependants(self, dead: Set[int]) -> Set[int]:
        """The parts that go with *dead*: a group whose size is withheld, a size no group shows."""
        more: Set[int] = set()
        for column in self._columns:
            more |= column.dependants(dead)
        return more - dead


@dataclass
class _Column:
    """The parts of one grouping column, and which combinations of them can occur together."""

    parts: List[Tuple[str, int, int]]
    positions: List[int]
    #: Whether some record is in none of the groups laid out (no value, or a group left out).
    outside: bool

    def allowed_over(self, kept: Sequence[int]) -> FrozenSet[Combination]:
        """The combinations of the *kept* parts a record can take: it is in one group or none."""
        chosen = [self.positions.index(at) for at in kept]
        slots = sorted({slot for kind, _, slot in self.parts if kind == "R"})
        levels = sorted({level for _, level, _ in self.parts})
        combos: Set[Combination] = set()
        for group in [*([-1] if self.outside else []), *levels]:
            for outcome in itertools.product(_OUTCOMES, repeat=len(slots)):
                row = []
                for kind, level, slot in self.parts:
                    if group != level:
                        row.append(_OUT)
                    elif kind == "P":
                        row.append(_IN)
                    else:
                        row.append(outcome[slots.index(slot)])
                combos.add(tuple(row[at] for at in chosen))
        return frozenset(combos)

    def dependants(self, dead: Set[int]) -> Set[int]:
        """The parts that go with *dead*: a group whose size is gone, a size no group shows."""
        more: Set[int] = set()
        by_level: Dict[int, List[int]] = {}
        size_at: Dict[int, int] = {}
        for (kind, level, _), at in zip(self.parts, self.positions, strict=True):
            if kind == "P":
                size_at[level] = at
            else:
                by_level.setdefault(level, []).append(at)
        for level, size in size_at.items():
            holders = by_level.get(level, [])
            if size in dead:
                more |= {at for at in holders if at not in dead}
            elif holders and all(at in dead for at in holders):
                more.add(size)
        return more


# -- running ------------------------------------------------------------------------------------


def measure(
    plans: QueryPlans,
    reads: Mapping[str, Any],
    backend: StatisticsBackend,
    rule: disclosure.DisclosureRule,
) -> Measured:
    """Run every answerable query over the records in one query process and keep what it counted.

    *reads* are the tables' records (``fluid_build.profiler.sources.SourceColumns``), which hold
    every column the queries read. Each group's cells are checked and each view's queries are
    checked as a set; a table's queries wait for :meth:`Measured.check_table`, which also takes
    the table's facts. Raises :class:`consumer_queries.ChildUnavailable` when the process cannot
    run here. A query whose records or statements fail is not answered and is named with the
    reason, which names no value.
    """
    done = Measured(plans)
    live = plans.answerable()
    if not live:
        return done
    loaded = _load(live, plans, reads, done)
    live = [each for each in live if each.index not in done.failed]
    assigned = _assign(live, reads, backend, done)
    live = [each for each in live if each.index not in done.failed]
    built: Dict[str, Checked] = {}
    for view, members in _by_view(live).items():
        statement = view_statement(view, members, plans.schema)
        if isinstance(statement, str):
            for each in members:
                done.failed[each.index] = REFUSED_BY_PROCESS
        else:
            built[view] = statement
    live = [each for each in live if each.index not in done.failed]
    if not live:
        return done
    wanted = _by_view(live)
    tables, statements = _send(live, wanted, built, plans, loaded, reads)
    try:
        run = consumer_queries.run_in_child(
            tables, statements, seconds=STATEMENT_SECONDS, memory=MEMORY_BYTES
        )
    except ValueError:  # an argument the process refuses: nothing is run, nothing is guessed
        for each in live:
            done.failed[each.index] = NOT_LOADED
        return done
    by_id = {each.id: each for each in run.results}
    for plan in live:
        if plan.view is None:
            _table_answer(plan, by_id, reads, assigned.get(plan.index), done)
    for number, (view, members) in enumerate(wanted.items()):
        _view_answer(view, members, by_id, f"v{number}", done)
    done.check_cells(rule)
    done.check_views(rule)
    return done


def _load(
    live: Sequence[QueryPlan], plans: QueryPlans, reads: Mapping[str, Any], done: Measured
) -> Dict[Tuple[str, str], List[Any]]:
    loaded: Dict[Tuple[str, str], Optional[List[Any]]] = {}
    for plan in live:
        for table, column in plan.reads:
            if (table, column) not in loaded:
                duck = _duck(plans.types_of(table)[column]) or ""
                loaded[(table, column)] = _loaded(duck, reads[table].values[column])
    for plan in live:
        if any(loaded[pair] is None for pair in plan.reads):
            done.failed[plan.index] = NOT_LOADED
    return {pair: values for pair, values in loaded.items() if values is not None}


def _assign(
    live: Sequence[QueryPlan], reads: Mapping[str, Any], backend: StatisticsBackend, done: Measured
) -> Dict[int, List[Optional[int]]]:
    """Each grouped query's level index per record, or the query named when a value is no level."""
    found: Dict[int, List[Optional[int]]] = {}
    cache: Dict[Tuple[str, str], Optional[List[Optional[str]]]] = {}
    for plan in live:
        if plan.group_by is None or plan.table is None:
            continue
        key = (plan.table, plan.group_by)
        if key not in cache:
            try:
                cache[key] = [
                    backend.read_value("category", value, column=plan.group_by, row_index=row)
                    for row, value in enumerate(reads[plan.table].values[plan.group_by])
                ]
            except ValueError:
                cache[key] = None
        read = cache[key]
        index = {level: at for at, level in enumerate(plan.levels)}
        if read is None or any(value is not None and value not in index for value in read):
            done.failed[plan.index] = NOT_A_LEVEL
            continue
        found[plan.index] = [None if value is None else index[value] for value in read]
    return found


def _send(
    live: Sequence[QueryPlan],
    wanted: Mapping[str, List[QueryPlan]],
    built: Mapping[str, Checked],
    plans: QueryPlans,
    loaded: Mapping[Tuple[str, str], List[Any]],
    reads: Mapping[str, Any],
) -> Tuple[Dict[str, List[Tuple[str, str, Sequence[Any]]]], List[Any]]:
    """The tables the query process loads and the statements it runs, all checked statements."""
    from fluid_build.profiler.consumer_queries import Statement

    needed: Dict[str, Dict[str, None]] = {}
    rowed: Set[str] = set()
    for plan in live:
        for table, column in plan.reads:
            needed.setdefault(table, {})[column] = None
        if plan.view is None and plan.table is not None:
            rowed.add(plan.table)
    tables: Dict[str, List[Tuple[str, str, Sequence[Any]]]] = {}
    for table, columns in needed.items():
        entries: List[Tuple[str, str, Sequence[Any]]] = [
            (
                column,
                _duck(plans.types_of(table)[column]) or "",
                loaded[(table, column)],
            )
            for column in columns
        ]
        if table in rowed:
            entries.append(
                (plans.row_columns[table], "BIGINT", list(range(reads[table].row_count)))
            )
        tables[table] = entries
    statements: List[Any] = []
    for plan in live:
        statements.append(Statement(f"c{plan.index}", plan.verify_sql, "count"))
        if plan.view is None:
            statements.append(Statement(f"b{plan.index}", plan.bits_sql, "bits"))
    for number, view in enumerate(wanted):
        statements.append(Statement(f"v{number}", built[view].text, "counts"))
    return tables, statements


_STATUS_REASON = {
    "timedOut": TIMED_OUT,
    "outOfMemory": OUT_OF_MEMORY,
    "error": REFUSED_BY_PROCESS,
    "crashed": PROCESS_STOPPED,
}


def _reason_of(result: Any) -> str:
    return _STATUS_REASON.get(str(result.status), REFUSED_BY_PROCESS)


def _table_answer(
    plan: QueryPlan,
    by_id: Mapping[str, Any],
    reads: Mapping[str, Any],
    assigned: Optional[List[Optional[int]]],
    done: Measured,
) -> None:
    bits = by_id.get(f"b{plan.index}")
    count = by_id.get(f"c{plan.index}")
    if bits is None or bits.status != "ok" or bits.bits is None:
        done.failed[plan.index] = _reason_of(bits) if bits is not None else PROCESS_STOPPED
        return
    if count is None or count.status != "ok":
        done.failed[plan.index] = _reason_of(count) if count is not None else PROCESS_STOPPED
        return
    n = reads[str(plan.table)].row_count
    flags = bits.bits.to_list()
    outcomes = bytes(_IN if each else _OUT for each in flags)
    true = outcomes.count(_IN)
    if len(flags) != n or count.count != true:
        done.failed[plan.index] = ROWS_NOT_RECORDS
        return
    sizes: Optional[List[Tuple[int, int]]] = None
    if assigned is not None:
        records = [0] * len(plan.levels)
        returned = [0] * len(plan.levels)
        for group, outcome in zip(assigned, outcomes, strict=True):
            if group is not None:
                records[group] += 1
                returned[group] += 1 if outcome == _IN else 0
        sizes = list(zip(records, returned, strict=True))
    member = _Member(plan, n, outcomes, true, assigned, sizes)
    done.tables.setdefault(str(plan.table), []).append(member)


def _view_answer(
    view: str,
    members: Sequence[QueryPlan],
    by_id: Mapping[str, Any],
    identifier: str,
    done: Measured,
) -> None:
    result = by_id.get(identifier)
    if result is None or result.status != "ok" or result.groups is None:
        reason = _reason_of(result) if result is not None else PROCESS_STOPPED
        for plan in members:
            done.failed[plan.index] = reason
        return
    cells: Dict[Combination, int] = {}
    trues = [0] * len(members)
    for row in result.groups:
        flags, total = row[:-1], int(row[-1])
        key = tuple(_IN if flag else _OUT for flag in flags)
        cells[key] = cells.get(key, 0) + total
        for at, outcome in enumerate(key):
            if outcome == _IN:
                trues[at] += total
    bad = False
    for at, plan in enumerate(members):
        count = by_id.get(f"c{plan.index}")
        if count is None or count.status != "ok":
            done.failed[plan.index] = _reason_of(count) if count is not None else PROCESS_STOPPED
            bad = True
        elif count.count != trues[at]:
            done.failed[plan.index] = ROWS_NOT_RECORDS
            bad = True
    if bad:
        keep = [(at, plan) for at, plan in enumerate(members) if plan.index not in done.failed]
        if not keep:
            return
        # the combinations of the queries that stay are those summed over the ones that went
        positions = [at for at, _ in keep]
        merged: Dict[Combination, int] = {}
        for key, total in cells.items():
            small = tuple(key[at] for at in positions)
            merged[small] = merged.get(small, 0) + total
        cells = merged
        members = [plan for _, plan in keep]
        trues = [trues[at] for at in positions]
    done.views[view] = _ViewPopulation(list(members), cells, sum(cells.values()), trues)
