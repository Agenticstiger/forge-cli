# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The rate of each consumer query a profile request lists: how many of its unit's records it keeps.

A request query ``{name, sql, unit, groupBy}`` (PROFILE-REQUEST-SPEC section 6) is answered with
a ``queries`` entry of the same name (PROFILE-SPEC section 5): one ``levelHistogram/1`` over
``["false", "true"]``, where ``k`` is the number of records of the unit and the ``true`` count the
number of rows the query returns, per level of the ``groupBy`` column when it has one. The
unit is a table of the request (its rows) or a view that keeps some of one table's rows
(``SELECT <columns> FROM <table> [WHERE <condition>]``): the records counted are then the table's,
and the view's condition is one more factor of that table's set check. Any other view (a join,
a reshaping) is named before any read, and so is an aggregate unless it counts entities.

**Entity units.** A unit may aggregate when it groups one table's rows by an entity they belong
to: ``SELECT K [AS ..], <aggregates of t's columns> [AS ..] FROM t [AS a] [WHERE c] GROUP BY K
[HAVING h]``, with ``K`` the child column of one declared ``many_to_one`` relationship of ``t``
(one column a side). The aggregates are ``count``, ``sum``, ``avg``, ``min`` and ``max`` of one
column; no nested aggregate, window, ``DISTINCT``, join, subquery, set operation, ``ORDER BY`` or
``LIMIT``. ``c`` passes the fence the other conditions pass, and ``h`` passes it too, reading each
aggregate as a column of the type it returns (:func:`_aggregate_type`); a query over the unit
passes it over the unit's output columns, typed the same way. A table's natural entity, and a
column that also keys the table's per-entity statistics, are refused: those publish counts of
the same entities. The unit's records are the entities with at least one row passing ``c`` and
``h``, and the population is every entity:

* ``K`` is the child column of a relationship the profile counts: the parents, each a record, with
  the bin of the children-per-parent histogram each is in, and, beside them, each group of the
  table's rows whose key is null or names no parent (one record each, in the bin "no parent").
  The bins whose cell the histogram withholds, and "no parent", are a part of the set check (a
  published cell is one state, as a column's published histogram is not crossed with a fact), so
  a query cannot give a withheld cell back;
* otherwise: the distinct values of ``K`` among the table's rows.

One ``counts`` statement per population gives every combination of "in the unit" and each query's
outcome; the entities' set is checked apart from the table's own rows.

**What is answered is decided before any record is read** (:func:`plan_queries`), so that
``--strict`` refuses the rest. A query is answered when

1. the query, and a view unit, pass the checker (:mod:`fluid_build.profiler.consumer_queries`)
   against the request's tables, typed by the contract;
2. it keeps some of its unit's records and changes none: an outer ``SELECT`` whose projection
   holds only columns, from exactly the unit relation (the table; for a view, the one ``WITH``
   entry whose body is the unit's SQL), with at most one ``WHERE``, and nothing else (no join,
   ``DISTINCT``, ``GROUP BY``, ``HAVING``, ``QUALIFY``, ``ORDER BY``, ``LIMIT``, ``OFFSET``,
   window, aggregate, set operation or subquery in the ``WHERE``). A view unit must itself be
   such a filter of one request table (:data:`NOT_A_FILTER_VIEW`);
3. its ``groupBy``, when it has one, names a column of a table unit's records that the query
   also returns under its own name and whose type declares its levels (an ``enum(...)`` or a
   boolean): the groups are those levels, whatever the records hold;
4. every column it reads has a type the query process loads;
5. it falls inside the public bounds: at most :data:`MOST_QUERIES` queries in a request,
   :data:`MOST_PER_UNIT` over one unit, :data:`MOST_SET_PARTS` parts in one table's set check
   (a grouped query adds one per level of its column, and the first adds one more per level),
   :data:`MOST_GROUPED_PER_COLUMN` grouped queries per column, :data:`MOST_GROUP_LEVELS` levels,
   as many units and queries over one table's entities as keep the one statement that counts
   their combinations (the bin factor's states times two to the units and queries) inside the
   query process's group and reply limits (:func:`_counts_combinations`; the rest are named
   :data:`ENTITIES_PAST_BOUND`), and :data:`MOST_TEXT_TOKENS` tokens of query and view text in
   the whole request. The budget is
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
row's state lines up with the facts' states of the same table), or the rows a view unit keeps,
which are the same table's rows, so the unit's condition is read as one more state of each row.

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

**One way per table.** A run counts one table's records one way: as its rows (a fact asked of the
table, a table unit, a view unit) or as its entities by one column (an entity unit). A fact
decides first, then the first query in the request's order that is answered; a query whose unit
counts the table another way is refused before any read with :data:`COUNTED_ANOTHER_WAY`. The
rows and the entities of a table are two populations, checked as two sets, and where every
entity holds one row they are the same records: a query over the rows and one over the
entities would differ by one record that no set check sees.

**Errors that depend on a record.** A condition that could raise an error depending on a row's
value (a cast, arithmetic, a ``CASE``, an unlisted function) is refused before any read with
:data:`CAN_FAIL`, by the fence and function allowlist the MCP gateway applies to a filtered
caller (:func:`fluid_build.output_ports.mcp.query_compiler.refuse_conditions_that_can_fail`).
As a backstop, any failure that still depends on the records withholds every query of the run
with :data:`RUN_FAILED`, so a run says at most one bit (that some statement failed), never which
query or which record.

**What is left.** A query is checked as a set with the table's facts and the other queries and
units over that table, not with a column's level histogram, ``nullCount`` or row count beside
it (facts have the same limit), and an entity unit's population is not checked with its table's
own statistics, which count the same records where every entity holds one row. A counted entity
population is checked apart from the parent table's own facts and queries: two sets over the
same rows, never crossed with each other, so a query over the parent's rows and one over its
entities are not differenced together. A reader can difference such counts, and the rounding
hides the difference only when neither count lies at a rounding edge. Where the parent table
repeats a key (records that break its declared key), an entity is counted once for each parent
row that holds its key, and so is its unit's ``k``. The cost is bounded and public: at most
:data:`MOST_QUERIES` queries, each statement limited in time and memory, run in a locked
process. A request from a source that is not trusted belongs in a container.

Nothing printed, logged or written names a count of records: a reason names a query, a table, a
column or a bound.
"""

from __future__ import annotations

import datetime
import itertools
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Context, Decimal, InvalidOperation, localcontext
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
    "CAN_FAIL",
    "COUNTED_ANOTHER_WAY",
    "ENTITIES_PAST_BOUND",
    "ENTITY_ONLY",
    "HISTORY_ENTITY",
    "PER_ENTITY_KEY",
    "CountedLink",
    "EntityUnit",
    "MEMORY_BYTES",
    "MOST_GROUPED_PER_COLUMN",
    "MOST_GROUP_LEVELS",
    "MOST_PER_UNIT",
    "MOST_QUERIES",
    "MOST_SET_PARTS",
    "MOST_TEXT_TOKENS",
    "NOT_A_FILTER_VIEW",
    "RECORD_REASONS",
    "RUN_FAILED",
    "STATEMENT_SECONDS",
    "SUM_CAN_OVERFLOW",
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
RUN_FAILED = (
    "a statement of this request's queries failed on the records, so no query of it is answered"
)
RECORD_REASONS = frozenset({RUN_FAILED})
NOT_A_FILTER_VIEW = (
    "a unit that aggregates, joins or reshapes rows is not one fluid profile can check with "
    "its table: name the table, or a view that keeps some of one table's rows"
)

ENTITY_ONLY = (
    "a unit that aggregates is answered only when it groups one table's rows by an entity "
    "they belong to"
)
HISTORY_ENTITY = (
    "a unit that groups a versioned table by its natural entity counts the entities whose "
    "versions per entity the table's history publishes, so fluid profile does not answer it"
)
PER_ENTITY_KEY = (
    "its entity column also keys the per-entity statistics of its table, which count the same "
    "entities, so fluid profile does not answer a unit that groups by it"
)
ENTITY_KEY_TYPE = (
    "its entity column has a type whose values fluid profile does not match as entities: "
    "use a text, boolean, whole-number or decimal key"
)
ENTITY_PARENT_TYPE = (
    "its entity column and the key of its relationship's parent are not read as one type"
)

SUM_CAN_OVERFLOW = (
    "its unit sums or averages a column whose total could exceed what the query process adds "
    "up exactly: use a column of at most 18 digits, or count, min or max"
)
ENTITIES_PAST_BOUND = (
    "not checked: one table's entities hold more units and queries than one set check can count"
)
CAN_FAIL = (
    "a condition of its query or its unit's view could raise an error that depends on a row's "
    "value, so fluid profile does not run it: compare columns with constants of their own type, "
    "joined by AND, OR and NOT"
)
COUNTED_ANOTHER_WAY = (
    "a fact or an earlier query counts the same table's records another way (as its rows, or as "
    "its entities by another column); two sets over one table's records could give a record "
    "back by differencing, so fluid profile counts a table's records one way per run"
)
#: How a unit counts its table's records: as the rows, or as the entities of one column.
_ROWS: Tuple[str, ...] = ("rows",)
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
class EntityUnit:
    """A unit that groups one table's rows by an entity they belong to.

    ``key`` is the table's column the rows are grouped by and ``out_key`` the name the unit
    returns it under; ``outputs`` are the unit's columns with the contract type each has for the
    fence. ``parent`` is the ``(table, key column)`` of the declared ``many_to_one`` relationship
    ``key`` is the child column of.
    """

    table: str
    key: str
    out_key: str
    outputs: Tuple[Tuple[str, str], ...]
    parent: Optional[Tuple[str, str]] = None


@dataclass(frozen=True)
class CountedLink:
    """A relationship whose children per parent the profile counts, as a population needs it.

    ``bins`` is the bin of each parent row (the bins of the children-per-parent histogram), in the
    order the parent's rows were read; ``shown`` says which bins' cells the histogram publishes.
    Held in memory for the one run, never written.
    """

    child: str
    parent: str
    child_column: str
    parent_column: str
    bins: Tuple[int, ...] = field(repr=False)
    shown: Tuple[bool, ...] = ()


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
    #: The canonical text of the unit's view, for a unit given as SQL. The view keeps some of
    #: ``table``'s rows: the records counted are the table's, the view's condition one more
    #: factor of the table's set check.
    view: Optional[str] = None
    #: The canonical text of the query (run only inside the count that checks the rows).
    text: str = field(default="", repr=False)
    group_by: Optional[str] = None
    levels: Tuple[str, ...] = ()
    #: The ``(exposeId, column)`` pairs the query and its unit read.
    reads: Tuple[Tuple[str, str], ...] = ()
    #: The checked ``WHERE`` condition over the table's columns, a view unit's condition and the
    #: query's together (``None``: no condition).
    where: Any = field(default=None, compare=False, repr=False)
    #: A view unit's own condition over the table's columns (``None``: it keeps every row).
    unit_where: Any = field(default=None, compare=False, repr=False)
    alias: str = ""
    #: The statements built from the checked tree, as the checker wrote them again.
    bits_sql: str = field(default="", repr=False)
    verify_sql: str = field(default="", repr=False)
    #: A view unit: the statement that reads which rows its condition keeps, and the one that
    #: counts the rows of its checked text.
    unit_bits_sql: str = field(default="", repr=False)
    unit_verify_sql: str = field(default="", repr=False)
    #: A unit that groups its table's rows by an entity: its records are entities, not rows.
    entity: Optional[EntityUnit] = None

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

    def needs_links(self) -> bool:
        """Whether an answerable query counts entities that may be a counted relationship's parents."""
        return any(each.entity is not None and each.entity.parent for each in self.answerable())

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
    planner = _Planner(
        schema, rows, enum_levels, contract, facts=[fact.table for fact in request.facts]
    )
    plans = tuple(planner.plan(index, each) for index, each in enumerate(request.queries))
    return QueryPlans(plans, schema, rows)


def _way_of(plan: QueryPlan) -> Tuple[str, ...]:
    """How *plan*'s unit counts its table's records: the rows, or the entities of one column."""
    return _ROWS if plan.entity is None else ("entities", plan.entity.key)


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
        contract: Mapping[str, Any],
        facts: Sequence[str] = (),
    ) -> None:
        self._schema = schema
        self._rows = rows
        self._enum_levels = enum_levels
        self._exposes: Dict[str, Mapping[str, Any]] = {
            str(item.get("exposeId")): item
            for item in iter_exposes(dict(contract))
            if item.get("exposeId")
        }
        self._entity_parts: Dict[Tuple[str, str], int] = {}
        self._entity_units: Dict[Tuple[str, str], Set[str]] = {}
        self._entity_queries: Dict[Tuple[str, str], int] = {}
        self._spent = 0
        self._exhausted = False
        self._counted: Set[str] = set()
        self._views: Dict[str, Union[Checked, Refused]] = {}
        self._per_unit: Dict[Tuple[str, str], int] = {}
        self._parts: Dict[str, int] = {}
        self._grouped: Dict[Tuple[str, str], int] = {}
        self._unit_parts: Set[Tuple[str, str]] = set()
        #: How each table's records are counted this run: a fact counts its rows, and the first
        #: answered query over a table counts them as its unit does.
        self._ways: Dict[str, Tuple[str, ...]] = {table: _ROWS for table in facts}

    # -- one query ---------------------------------------------------------------------------

    def plan(self, index: int, query: RequestQuery) -> QueryPlan:
        planned = self._plan(index, query)
        if planned.reason is None and planned.table is not None:
            self._ways.setdefault(planned.table, _way_of(planned))
        return planned

    def _plan(self, index: int, query: RequestQuery) -> QueryPlan:
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
        table = query.table
        unit_where: Any = None
        if query.unit_sql is not None:
            found = self._view(query.unit_sql)
            if isinstance(found, Refused):
                return blank.refused(f"the check refuses the unit's view: {found.reason}")
            view = found
            kept = _filter_view(view, self._schema)
            if kept is None:
                counts_entities = self._entity_unit(view)
                if counts_entities is None:
                    return blank.refused(NOT_A_FILTER_VIEW)
                if isinstance(counts_entities, str):
                    return blank.refused(counts_entities)
                return self._plan_entity(index, query, checked, view, counts_entities)
            table, unit_where, names = kept
        elif query.table not in self._schema:
            return blank.refused("its unit's table is not one the request lists")
        try:
            where, alias = _kept(
                checked, table=query.table, view=None if view is None else view.text
            )
        except _NotKept as found_not_kept:
            return blank.refused(_KEPT + str(found_not_kept))
        if view is not None:
            where = _folded(unit_where, where, names)
            if where is False:
                return blank.refused(_KEPT + "it reads a column its unit's view does not return")
            # the statements that count the view's rows and the query's rows read what they name
            reads = _reads_of(where, table or "", self._schema, (*checked.columns, *view.columns))
            alias = table or ""
        else:
            reads = tuple(sorted(set(checked.columns)))
        if not self._cannot_fail(where, alias, table or ""):
            return blank.refused(CAN_FAIL)
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
            table=table,
            view=None if view is None else view.text,
            text=checked.text,
            group_by=query.group_by,
            levels=levels,
            reads=reads,
            where=where,
            unit_where=unit_where,
            alias=alias,
        )
        why = self._room(plan)
        if why is not None:
            return blank.refused(why)
        built = self._statements(plan)
        if isinstance(built, str):
            return blank.refused(built)
        return replace(plan, **built)

    def _cannot_fail(self, where: Any, alias: str, table: str) -> bool:
        """Whether the condition passes the gateway's fence for conditions that can fail on a row.

        The fence and its function allowlist are the ones a filtered caller's statement passes
        (:func:`fluid_build.output_ports.mcp.query_compiler.refuse_conditions_that_can_fail`),
        applied to the one condition the query and its unit's view keep rows by, typed by the
        contract. A column the contract types as an ``enum(...)`` is loaded as text.
        """
        return self._fenced(where, table, self._schema[table].columns)

    @staticmethod
    def _fenced(where: Any, relation: str, columns: Sequence[Tuple[str, str]]) -> bool:
        """Whether *where*, read over *relation* whose *columns* are ``(name, contract type)``, passes."""
        if where is None:
            return True
        from fluid_build.output_ports.mcp import query_compiler

        types: Dict[str, Optional[Any]] = {}
        for name, kind in columns:
            if re.match(r"\s*enum\s*\(", kind, re.IGNORECASE):
                types[name.casefold()] = query_compiler.ColumnType("string")
        sql = (
            f"SELECT 1 FROM {_ident(relation)} AS {_ident(relation)} "
            f"WHERE {_condition(where, relation, relation)}"
        )
        expose = {"contract": {"schema": [{"name": n, "type": k} for n, k in columns]}}
        try:
            query_compiler.refuse_conditions_that_can_fail(sql, expose, relation, "duckdb", types)
        except query_compiler.QueryValidationError:
            return False
        return True

    # -- a unit that groups one table's rows by an entity -----------------------------------

    def _entity_unit(self, view: Checked) -> Union[None, str, EntityUnit]:
        """A view that groups one table's rows by an entity they belong to, else why not.

        ``None`` for a view that does not aggregate at all (it is a view that is not a filter).
        The shape is ``SELECT K [AS ..], <aggregates of the table's columns> [AS ..] FROM t
        [WHERE c] GROUP BY K [HAVING h]`` and nothing else: no join, subquery, window,
        ``DISTINCT``, set operation, ``ORDER BY`` or ``LIMIT``. ``c`` and ``h`` pass the fence
        the other conditions pass, ``h`` over the typed outputs of the aggregates it reads.
        """
        from sqlglot import exp

        from fluid_build.output_ports.mcp import query_compiler

        tree = view.tree
        if not isinstance(tree, exp.Select):
            return None
        if tree.args.get("group") is None and not any(
            isinstance(node, exp.AggFunc) for node in tree.walk()
        ):
            return None
        for key, value in tree.args.items():
            if key not in _ENTITY_ARGS and not _blank(value):
                return ENTITY_ONLY
        allowed = (*_entity_nodes(), *query_compiler._condition_nodes())
        for node in tree.walk():
            if not isinstance(node, allowed):
                return ENTITY_ONLY
            if isinstance(node, exp.Star) and not isinstance(node.parent, exp.Count):
                return ENTITY_ONLY
        from_ = tree.args.get("from_")
        source = from_.this if isinstance(from_, exp.From) else None
        if not isinstance(source, exp.Table) or str(source.name) not in self._schema:
            return ENTITY_ONLY
        if any(k not in ("this", "alias") and not _blank(v) for k, v in source.args.items()):
            return ENTITY_ONLY
        table, alias = str(source.name), str(source.alias or source.name)
        group = tree.args.get("group")
        if not isinstance(group, exp.Group):
            return ENTITY_ONLY
        if any(k != "expressions" and not _blank(v) for k, v in group.args.items()):
            return ENTITY_ONLY
        grouped = group.expressions
        if len(grouped) != 1 or not isinstance(grouped[0], exp.Column):
            return ENTITY_ONLY
        if str(grouped[0].table) != alias:
            return ENTITY_ONLY
        key = str(grouped[0].name)
        columns = self.types(table)
        if key not in columns:
            return ENTITY_ONLY
        parent = self._entity_of(table, key)
        if isinstance(parent, str):
            return parent
        why = self._entity_key(table, key, parent)
        if why is not None:
            return why
        outputs, out_key = self._outputs(tree, view, alias, key, columns)
        if out_key is None:
            return ENTITY_ONLY
        if any(_sum_may_overflow(node, columns) for node in tree.find_all(exp.Sum, exp.Avg)):
            return SUM_CAN_OVERFLOW
        where = tree.args.get("where")
        if where is not None:
            if any(isinstance(node, exp.AggFunc) for node in where.walk()):
                return ENTITY_ONLY
            if not self._cannot_fail(where.this, alias, table):
                return CAN_FAIL
        having = tree.args.get("having")
        if having is not None:
            found = self._having_fenced(having.this, alias, key, columns)
            if found is not None:
                return found
        return EntityUnit(table, key, out_key, tuple(outputs), parent)

    def _entity_of(self, table: str, key: str) -> Union[str, Optional[Tuple[str, str]]]:
        """The ``(parent, key column)`` of the one many_to_one relationship *key* is the child of.

        A reason when *key* is no entity of *table*: its natural entity (the table's history
        publishes that population's versions per entity), the key of per-entity statistics, or
        a column no single declared many_to_one relationship hangs on. ``None`` when the
        relationship's parent is not a table of the request.
        """
        from fluid_build.forge_datamodel import semantics_builder
        from fluid_build.profiler import per_entity

        expose = self._exposes.get(table, {})
        semantics = expose.get("semantics")
        semantics = semantics if isinstance(semantics, Mapping) else {}
        try:
            window = semantics_builder.version_window(semantics)
        except (KeyError, StopIteration, TypeError, ValueError):
            return HISTORY_ENTITY  # a window that cannot be read is not shown not to be the key's
        if window is not None and window.key == key:
            return HISTORY_ENTITY
        links = [
            raw
            for raw in (semantics.get("relationships") or [])
            if isinstance(raw, Mapping) and _names(raw.get("fromColumns")) == (key,)
        ]
        if len(links) != 1 or links[0].get("cardinality") != "many_to_one":
            return ENTITY_ONLY
        if any(source == (key,) for source, _ in per_entity.dependencies(expose)):
            return PER_ENTITY_KEY
        target, to_columns = links[0].get("to"), _names(links[0].get("toColumns"))
        if len(to_columns) != 1 or not isinstance(target, str):
            return ENTITY_ONLY
        return (target, to_columns[0]) if target in self._schema else None

    def _entity_key(self, table: str, key: str, parent: Optional[Tuple[str, str]]) -> Optional[str]:
        """Why the entity column cannot be matched as entities, or ``None``."""
        duck = _duck(self.types(table)[key])
        if duck is None or duck.split("(")[0] not in _ENTITY_KEYS:
            return ENTITY_KEY_TYPE
        if parent is not None:
            theirs = self.types(parent[0]).get(parent[1])
            if theirs is None or _duck(theirs) != duck:
                return ENTITY_PARENT_TYPE
        return None

    def _outputs(
        self, tree: Any, view: Checked, alias: str, key: str, columns: Mapping[str, str]
    ) -> Tuple[List[Tuple[str, str]], Optional[str]]:
        """The unit's output columns with their contract types, and the name the key is returned as."""
        from sqlglot import exp

        items = tree.args.get("expressions") or []
        outputs: List[Tuple[str, str]] = []
        out_key: Optional[str] = None
        if len(items) != len(view.output_columns):
            return [], None
        for item, name in zip(items, view.output_columns, strict=True):
            inner = item.this if isinstance(item, exp.Alias) else item
            if isinstance(inner, exp.Column):
                if str(inner.table) != alias or str(inner.name) != key:
                    return [], None
                out_key = out_key or name
                outputs.append((name, columns[key]))
                continue
            kind = _aggregate_type(inner, alias, columns)
            if kind is None:
                return [], None
            outputs.append((name, kind))
        if len({name for name, _ in outputs}) != len(outputs):
            return [], None
        return outputs, out_key

    def _having_fenced(
        self, condition: Any, alias: str, key: str, columns: Mapping[str, str]
    ) -> Optional[str]:
        """Why a ``HAVING`` condition is refused, or ``None``: it reads only typed aggregates.

        Each aggregate of the condition is read as a column of the type it returns, and the
        entity column as itself, so the one fence the other conditions pass decides it.
        """
        from sqlglot import exp

        rebuilt = condition.copy()
        typed: List[Tuple[str, str]] = [(key, columns[key])]
        for found in list(rebuilt.find_all(exp.AggFunc)):
            kind = _aggregate_type(found, alias, columns)
            if kind is None:
                return ENTITY_ONLY
            name = f"__a{len(typed)}"
            typed.append((name, kind))
            found.replace(exp.column(name))
        for column in rebuilt.find_all(exp.Column):
            if not str(column.name).startswith("__a") and (
                str(column.name) != key or str(column.table) not in ("", alias)
            ):
                return ENTITY_ONLY
        return None if self._fenced(rebuilt, "having", typed) else CAN_FAIL

    def _plan_entity(
        self, index: int, query: RequestQuery, checked: Checked, view: Checked, unit: EntityUnit
    ) -> QueryPlan:
        """Plan a query over an entity unit: a filter of the unit's output rows."""
        blank = QueryPlan(index, query.name)
        if query.group_by is not None:
            return blank.refused("fluid profile groups a query only over a table unit's records")
        try:
            where, alias = _kept(checked, table=None, view=view.text)
        except _NotKept as found_not_kept:
            return blank.refused(_KEPT + str(found_not_kept))
        if not self._fenced(where, alias, unit.outputs):
            return blank.refused(CAN_FAIL)
        pairs = {*view.columns, (unit.table, unit.key)}
        if unit.parent is not None:
            pairs.add(unit.parent)
        reads = tuple(sorted(pairs))
        why = self._loadable(reads)
        if why is not None:
            return blank.refused(why)
        plan = QueryPlan(
            index,
            query.name,
            table=unit.table,
            view=view.text,
            text=checked.text,
            reads=reads,
            where=where,
            alias=alias,
            entity=unit,
        )
        why = self._room(plan)
        if why is not None:
            return blank.refused(why)
        verify = _checked_statement(
            f"SELECT count(*) FROM ({plan.text}) AS {_ident('q')}", self._schema
        )
        counted = _checked_statement(
            f"SELECT count(*) FROM ({view.text}) AS {_ident('q')}", self._schema
        )
        if isinstance(verify, Refused) or isinstance(counted, Refused):
            again = verify if isinstance(verify, Refused) else counted
            assert isinstance(again, Refused)
            return blank.refused(
                f"a statement that counts its rows is refused when checked again: {again.reason}"
            )
        return replace(plan, verify_sql=verify.text, unit_verify_sql=counted.text)

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
        way = _way_of(plan)
        if self._ways.get(plan.table or "", way) != way:
            return COUNTED_ANOTHER_WAY
        unit = plan.unit
        if self._per_unit.get(unit, 0) >= MOST_PER_UNIT:
            return f"past the {MOST_PER_UNIT} queries over one unit that fluid profile answers"
        table = plan.table
        parts = 1
        unit_part = (table or "", plan.view or "")
        if plan.view is not None and unit_part not in self._unit_parts:
            parts += 1  # the view's own condition is one more factor of the table's set
        if plan.entity is not None:
            # the entities of a table are another population than its rows: a set of their own
            population = (plan.entity.table, plan.entity.key)
            if self._entity_parts.get(population, 0) + parts > MOST_SET_PARTS:
                return f"past the {MOST_SET_PARTS} parts one table's set check holds"
            held = self._entity_units.setdefault(population, set())
            wider = len(held | {plan.view or ""}) + self._entity_queries.get(population, 0) + 1
            if not _counts_combinations(plan.entity.parent is not None, wider):
                return ENTITIES_PAST_BOUND
            held.add(plan.view or "")
            self._entity_queries[population] = self._entity_queries.get(population, 0) + 1
            self._per_unit[unit] = self._per_unit.get(unit, 0) + 1
            self._unit_parts.add(unit_part)
            self._entity_parts[population] = self._entity_parts.get(population, 0) + parts
            return None
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
        if plan.view is not None:
            self._unit_parts.add(unit_part)
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
        table = str(plan.table)
        row = self._rows[table]
        tables = {**self._schema, table: _with_row(self._schema[table], row)}

        def outcomes(where: Any) -> Union[str, Checked]:
            condition = _condition(where, plan.alias, table)
            built = _checked_statement(
                f"SELECT COALESCE(({condition}), FALSE) FROM {_ident(table)} AS "
                f"{_ident(table)} ORDER BY {_ident(table)}.{_ident(row)}",
                tables,
            )
            if isinstance(built, Refused):
                return (
                    "the statement that reads its outcomes is refused when checked again: "
                    + built.reason
                )
            return built

        bits = outcomes(plan.where)
        if isinstance(bits, str):
            return bits
        found["bits_sql"] = bits.text
        if plan.view is not None:
            counted = _checked_statement(
                f"SELECT count(*) FROM ({plan.view}) AS {_ident('q')}", self._schema
            )
            if isinstance(counted, Refused):
                return (
                    "the statement that counts its unit's rows is refused when checked again: "
                    + counted.reason
                )
            found["unit_verify_sql"] = counted.text
            if plan.unit_where is not None:
                unit_bits = outcomes(plan.unit_where)
                if isinstance(unit_bits, str):
                    return unit_bits
                found["unit_bits_sql"] = unit_bits.text
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


def _filter_view(
    view: Checked, schema: Mapping[str, Table]
) -> Optional[Tuple[str, Any, Dict[str, str]]]:
    """A view that keeps some rows of one request table, as ``(table, condition, names)``.

    The view is ``SELECT <columns> FROM <table> [WHERE <condition>]`` and nothing else
    (:func:`_kept`'s shape, over a table). *names* maps each column name the view returns to the
    table's column it is. ``None`` for any other view: one that joins, aggregates or reshapes
    rows, which is not a filter of its table.
    """
    from sqlglot import exp

    tree = view.tree
    from_ = tree.args.get("from_") if isinstance(tree, exp.Select) else None
    source = from_.this if isinstance(from_, exp.From) else None
    if not isinstance(source, exp.Table) or str(source.name) not in schema:
        return None
    table = str(source.name)
    try:
        condition, _ = _kept(view, table=table, view=None)
    except _NotKept:
        return None
    names: Dict[str, str] = {}
    for item in tree.args.get("expressions") or []:
        column = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(column, exp.Column):
            return None
        written = str(item.alias) if isinstance(item, exp.Alias) else str(column.name)
        if written in names:
            return None
        names[written] = str(column.name)
    return table, condition, names


_ENTITY_ARGS = frozenset({"expressions", "from_", "where", "group", "having"})
#: The duckdb types an entity column may have: ones whose equality is the entity's own.
_ENTITY_KEYS = frozenset(
    {"VARCHAR", "BOOLEAN", "DECIMAL", "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT"}
)


def _entity_nodes() -> Tuple[type, ...]:
    """What an entity unit's own structure is made of, beside the nodes of a condition."""
    from sqlglot import exp

    return (
        exp.Select,
        exp.From,
        exp.Table,
        exp.TableAlias,
        exp.Where,
        exp.Group,
        exp.Having,
        exp.Alias,
        exp.Count,
        exp.Sum,
        exp.Avg,
        exp.Min,
        exp.Max,
        exp.Star,
    )


def _names(value: Any) -> Tuple[str, ...]:
    return tuple(each for each in value if isinstance(each, str)) if isinstance(value, list) else ()


def _aggregate_type(node: Any, alias: str, columns: Mapping[str, str]) -> Optional[str]:
    """The contract type an aggregate of one column returns, as the fence compares it.

    ``count`` is a whole number, ``sum`` an exact number (or a double for a double), ``avg`` a
    double, ``min`` and ``max`` the column's own type; ``None`` for an aggregate or an argument
    that cannot be typed this way (an expression, a column of a type that does not add up).
    """
    from sqlglot import exp

    if not isinstance(node, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
        return None
    argument = node.this
    if isinstance(node, exp.Count) and isinstance(argument, exp.Star):
        return "long"
    # a column inside HAVING is left unqualified by the check: the one table it reads is the unit's
    if not isinstance(argument, exp.Column) or str(argument.table) not in ("", alias):
        return None
    kind = columns.get(str(argument.name))
    duck = _duck(kind) if kind is not None else None
    if kind is None or duck is None:
        return None
    base = duck.split("(")[0]
    if isinstance(node, exp.Count):
        return "long"
    if isinstance(node, (exp.Min, exp.Max)):
        return kind
    if base == "DOUBLE":
        return "double"
    if isinstance(node, exp.Avg):
        return "double" if base in _BOUNDS or base == "DECIMAL" else None
    if base in _BOUNDS:
        return "decimal(38,0)"
    if base == "DECIMAL":
        return f"decimal(38,{duck.split(',')[1].rstrip(')')})"
    return None


#: The most digits of a decimal whose sum DuckDB keeps in 64 bits per value: past it a value is a
#: 128-bit integer and the sum of two can overflow it, an error that depends on the records.
_EXACT_DECIMAL_DIGITS = 18


def _sum_may_overflow(node: Any, columns: Mapping[str, str]) -> bool:
    """Whether a ``sum`` or ``avg`` over a column could raise an overflow that depends on rows.

    A decimal of more than 18 digits is summed in 128 bits and overflows when a few of its values
    are large; a whole number overflows the 128-bit sum only when its type's largest value times
    the most rows the process holds reaches ``2 ** 127``. A column that cannot be typed is left to
    the caller's own typing, which refuses it.
    """
    kind = columns.get(str(getattr(node.this, "name", "")))
    duck = _duck(kind) if kind is not None else None
    if duck is None:
        return False
    base = duck.split("(")[0]
    if base == "DECIMAL":
        return int(re.findall(r"[0-9]+", duck)[0]) > _EXACT_DECIMAL_DIGITS
    if base in _BOUNDS:
        return _BOUNDS[base][1] * consumer_queries.MAX_BITS >= 2**127
    return False


def _folded(unit_where: Any, where: Any, names: Mapping[str, str]) -> Any:
    """A view unit's condition and a query's over it, as one condition over the table.

    The query reads the view's columns, each rewritten to the table column it is. ``False`` when
    the query reads a column the view does not return; ``None`` when neither has a condition.
    """
    from sqlglot import exp

    parts: List[Any] = []
    if unit_where is not None:
        parts.append(exp.Paren(this=unit_where.copy()))
    if where is not None:
        rewritten = where.copy()
        for column in list(rewritten.find_all(exp.Column)):
            base = names.get(str(column.name))
            if base is None:
                return False
            column.set("this", exp.to_identifier(base, quoted=True))
        parts.append(exp.Paren(this=rewritten))
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else exp.And(this=parts[0], expression=parts[1])


def _reads_of(
    where: Any,
    table: str,
    schema: Mapping[str, Table],
    named: Sequence[Tuple[str, str]],
) -> Tuple[Tuple[str, str], ...]:
    """The columns of *table* a view unit's statements read: the condition's and those *named*.

    One column of it when they read none, so the table is always read.
    """
    from sqlglot import exp

    found = {str(each.name) for each in where.find_all(exp.Column)} if where is not None else set()
    found |= {column for name, column in named if name == table}
    if not found:
        found = set(schema[table].column_names[:1])
    return tuple((table, name) for name in sorted(found))


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
    # a context of its own: the default one rounds to 28 digits, and a column is up to 38 wide
    with localcontext(Context(prec=max(precision, 38) + 2, traps=[InvalidOperation])):
        if not number.is_finite() or number != number.quantize(Decimal(1).scaleb(-scale)):
            raise ValueError
        if abs(number) >= Decimal(1).scaleb(precision - scale):
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
    #: The table's rows.
    n: int
    #: Each row's outcome under the query (a view unit's condition and the query's together).
    outcomes: bytes
    true: int
    #: The records the query's unit counts: the table's rows, or the rows a view unit keeps.
    size: int = 0
    #: Each row's outcome under a view unit's own condition (``None``: a table, or a view that
    #: keeps every row).
    unit: Optional[bytes] = None
    #: The level index of each record's grouping value (``None``: no group), grouped queries only.
    assigned: Optional[List[Optional[int]]] = None
    #: ``(records, records the query returns)`` of each level, grouped queries only.
    sizes: Optional[List[Tuple[int, int]]] = None


@dataclass
class _Own:
    """The cell checks of one query's groups, then what the set check added."""

    #: ``None``: published; else the verdict that withholds the group.
    verdicts: List[Optional[disclosure.Verdict]]


class Measured:
    """The answers of one run, and what the set checks and the document need of them."""

    def __init__(self, plans: QueryPlans) -> None:
        self.plans = plans
        self.failed: Dict[int, str] = {}
        self.tables: Dict[str, List[_Member]] = {}
        self._own: Dict[int, _Own] = {}
        self._members: Dict[int, _Member] = {}
        self._population: Dict[int, int] = {}
        self._checked: Set[str] = set()
        #: The entities of the tables the entity units group, one population each.
        self.populations: List[_Population] = []
        self._entity_true: Dict[int, int] = {}
        self._populations_checked = False

    def has_table(self, table: str) -> bool:
        return bool(self.tables.get(table))

    # -- the cell checks of every group -----------------------------------------------------

    def check_cells(self, rule: disclosure.DisclosureRule) -> None:
        """Pass each group's two cells through the cell checks, before any set check."""
        for members in self.tables.values():
            for each in members:
                self._members[each.plan.index] = each
                if each.sizes is None:
                    cells = [[each.size - each.true, each.true]]
                else:
                    cells = [[k - true, true] for k, true in each.sizes]
                self._own[each.plan.index] = _Own([_cell_verdict(pair, rule) for pair in cells])
                self._population[each.plan.index] = each.size
        for population in self.populations:
            for entity in population.members:
                index = entity.plan.index
                pair = [entity.k - entity.true, entity.true]
                self._own[index] = _Own([_cell_verdict(pair, rule)])
                self._population[index] = entity.k
                self._entity_true[index] = entity.true

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
        self._check_populations(rule)
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
        if plan.index in self._entity_true:
            return self._entity_true[plan.index]
        return self._members[plan.index].true

    # -- a population of entities: its units' and its queries' outcomes, in one set -------------

    def _check_populations(self, rule: disclosure.DisclosureRule) -> None:
        """Check each population of entities as a set, withholding the queries that give a cell back.

        The population is the parents of a counted relationship, or the distinct values of the
        entity column. The parts are whether an entity is in each unit, each query's outcome and,
        when the relationship's histogram withholds some bins, the bin of the entity's children
        count; the table's own rows are another population and are never in this set.
        """
        if self._populations_checked:
            return
        self._populations_checked = True
        for population in self.populations:
            layout = _EntityLayout(population, self._own)
            if not layout.width:
                continue
            cells = layout.project(population.cells)
            dead: Set[int] = set()
            while True:
                candidates = [at for at in range(layout.width) if at not in dead]
                found = set(
                    disclosure.suppress_differencing(
                        cells, candidates, rule, layout.factors, _ENTITY_STATES
                    )
                )
                if layout.bin_at in found:
                    # a published bin cannot be merged: the entities' parts are all withheld
                    dead = set(range(layout.width))
                    break
                new = (found | layout.dependants(dead | found)) - dead
                if not new:
                    break
                dead |= new
            for at in dead:
                index = layout.query_of.get(at)
                if index is not None and self._own[index].verdicts[0] is None:
                    self._own[index].verdicts[0] = _SET_VERDICT


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


@dataclass
class _EntityMember:
    """One answered query over an entity unit: the entities its unit holds and the query keeps."""

    plan: QueryPlan
    k: int
    true: int


@dataclass
class _Population:
    """The entities of one table's rows (or of a counted relationship's parents) and what is known.

    A combination holds, in order, whether the entity is in each unit, each query's outcome and,
    when the relationship's histogram withholds some bin, the entity's bin state; ``cells`` maps
    each combination some entity holds to the exact number that do.
    """

    members: List[_EntityMember]
    unit_at: List[int]
    query_at: List[int]
    bin_at: Optional[int]
    bin_states: FrozenSet[int]
    cells: Dict[Combination, int]


#: The bins of the children-per-parent histogram (runner.CHILDREN_EDGES has one edge more).
_BINS = 7
#: A bin of the children-per-parent histogram is one state when its cell is published and its own
#: state when the cell is withheld: the withheld cells are what a query must not give back. A
#: group of the unit that matches no parent (a null or unknown key) is a record of its own, in
#: the bin ``_BINS``, "no parent", which has a state of its own as well.
_BIN_PUBLISHED = 3
_ENTITY_STATES = tuple(range(_BIN_PUBLISHED + 9))


def _bin_state(bin_index: int, shown: Sequence[bool]) -> int:
    published = bin_index < len(shown) and shown[bin_index]
    return _BIN_PUBLISHED if published else _BIN_PUBLISHED + 1 + bin_index


class _EntityLayout:
    """The positions of one population's set check: each unit, its queries, and the bin.

    Only queries that passed their own cell checks are laid out; a part left out is merged, as the
    set check merges a part it withholds. A unit's position is the entity being in it, and a
    record kept by none of its queries when it is not.
    """

    def __init__(self, population: _Population, own: Mapping[int, _Own]) -> None:
        self.query_of: Dict[int, int] = {}
        self.units: List[Tuple[int, List[int]]] = []
        self.bin_at: Optional[int] = None
        kept: List[int] = []
        by_unit: Dict[int, List[Tuple[int, int]]] = {}
        for entity, unit_at, query_at in zip(
            population.members, population.unit_at, population.query_at, strict=True
        ):
            if own[entity.plan.index].verdicts[0] is None:
                by_unit.setdefault(unit_at, []).append((query_at, entity.plan.index))
        for unit_at, queries in by_unit.items():
            here = len(kept)
            kept.append(unit_at)
            positions: List[int] = []
            for query_at, index in queries:
                self.query_of[len(kept)] = index
                positions.append(len(kept))
                kept.append(query_at)
            self.units.append((here, positions))
        if population.bin_at is not None and kept:
            self.bin_at = len(kept)
            kept.append(population.bin_at)
        self._kept = kept
        self.width = len(kept)
        self._bin_allowed: FrozenSet[Combination] = frozenset(
            (state,) for state in population.bin_states
        )

    def project(self, cells: Mapping[Combination, int]) -> "Counter[Combination]":
        projected: Counter[Combination] = Counter()
        for combination, count in cells.items():
            projected[tuple(combination[at] for at in self._kept)] += count
        return projected

    def factors(self, order: Sequence[int]) -> disclosure.Factors:
        shown = set(order)
        found: List[Tuple[Tuple[int, ...], FrozenSet[Combination]]] = []
        for unit_at, queries in self.units:
            if unit_at in shown:
                kept = [unit_at, *(at for at in queries if at in shown)]
                found.append((tuple(kept), _inside(len(kept) - 1)))
            else:
                found.extend(((at,), frozenset({(_OUT,), (_IN,)})) for at in queries if at in shown)
        if self.bin_at is not None and self.bin_at in shown:
            found.append(((self.bin_at,), self._bin_allowed))
        return found

    def dependants(self, dead: Set[int]) -> Set[int]:
        """The parts that go with *dead*: a unit's queries, and a unit none of its queries show."""
        more: Set[int] = set()
        for unit_at, queries in self.units:
            if unit_at in dead:
                more |= set(queries)
            elif all(at in dead for at in queries):
                more.add(unit_at)
        return more - dead


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
        #: A view unit's own condition (its position) with the positions of its queries.
        self._units: Dict[str, Tuple[int, List[int]]] = {}
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
                    held = self._unit_of(each)
                    at = len(self.states)
                    self.states.append(each.outcomes)
                    self.query_of[at] = (each.plan.index, 0)
                    if held is None:
                        self._single.append((at, each.plan.index))
                    else:
                        held.append(at)
            elif any(verdict is None for verdict in verdicts):
                by_column.setdefault(each.plan.group_by, []).append(each)
        for column, grouped in by_column.items():
            self._columns.append(self._add_column(column, grouped, own))
        self.width = len(self.states)

    def _unit_of(self, member: _Member) -> Optional[List[int]]:
        """The positions of the queries over *member*'s view unit, which gets a position once."""
        if member.unit is None or member.plan.view is None:
            return None
        key = member.plan.view
        if key not in self._units:
            self._units[key] = (len(self.states), [])
            self.states.append(member.unit)
        return self._units[key][1]

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
        for unit_at, queries in self._units.values():
            if unit_at in shown:
                kept = [unit_at, *(at for at in queries if at in shown)]
                found.append((tuple(kept), _inside(len(kept) - 1)))
            else:
                found.extend(((at,), frozenset({(_OUT,), (_IN,)})) for at in queries if at in shown)
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
        for unit_at, queries in self._units.values():
            if unit_at in dead:
                more |= set(queries)
            elif all(at in dead for at in queries):
                more.add(unit_at)
        return more - dead


def _inside(queries: int) -> FrozenSet[Combination]:
    """The combinations of a view unit's condition and the conditions of *queries* over it.

    A record the unit's condition does not keep is kept by none of the queries over it.
    """
    outside = (_OUT,) * (queries + 1)
    inside = {(_IN, *each) for each in itertools.product(_OUTCOMES, repeat=queries)}
    return frozenset({outside, *inside})


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
    links: Sequence[CountedLink] = (),
) -> Measured:
    """Run every answerable query over the records in one query process and keep what it counted.

    Any failure that depends on the records (a statement that errors, a time-out, out of memory,
    a crash, a value that does not load, a grouping value that is not a level, rows that are not
    the unit's records) withholds every query of the run, each named with :data:`RUN_FAILED`: an
    answer for the others would say, one bit per query, that a chosen record made that statement
    fail. The run says at most one bit, that some statement failed, never which.

    *links* are the relationships the profile counts, for the queries over a unit that groups
    by the child column of one: their parents are that unit's records.
    """
    done = _measure(plans, reads, backend, rule, links)
    if not done.failed:
        return done
    stopped = Measured(plans)
    stopped.failed = {each.index: RUN_FAILED for each in plans.answerable()}
    return stopped


def _measure(
    plans: QueryPlans,
    reads: Mapping[str, Any],
    backend: StatisticsBackend,
    rule: disclosure.DisclosureRule,
    links: Sequence[CountedLink] = (),
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
    if not live:
        return done
    tables, statements = _send(live, plans, loaded, reads)
    populations = _populations([each for each in live if each.entity is not None], plans, links)
    for population in populations:
        _add_population(population, plans, reads, tables, statements, done)
    try:
        run = consumer_queries.run_in_child(
            tables, statements, seconds=STATEMENT_SECONDS, memory=MEMORY_BYTES
        )
    except ValueError:  # an argument the process refuses: nothing is run, nothing is guessed
        for each in live:
            done.failed[each.index] = RUN_FAILED
        return done
    by_id = {each.id: each for each in run.results}
    for plan in live:
        if plan.entity is None:
            _table_answer(plan, by_id, reads, assigned.get(plan.index), done)
    for population in populations:
        _population_answer(population, by_id, reads, done)
    done.check_cells(rule)
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
            done.failed[plan.index] = RUN_FAILED
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
            done.failed[plan.index] = RUN_FAILED
            continue
        found[plan.index] = [None if value is None else index[value] for value in read]
    return found


def _send(
    live: Sequence[QueryPlan],
    plans: QueryPlans,
    loaded: Mapping[Tuple[str, str], List[Any]],
    reads: Mapping[str, Any],
) -> Tuple[Dict[str, List[Tuple[str, str, Sequence[Any]]]], List[Any]]:
    """The tables the query process loads and the statements it runs, all checked statements."""
    from fluid_build.profiler.consumer_queries import Statement

    needed: Dict[str, Dict[str, None]] = {}
    for plan in live:
        for table, column in plan.reads:
            needed.setdefault(table, {})[column] = None
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
        entries.append((plans.row_columns[table], "BIGINT", list(range(reads[table].row_count))))
        tables[table] = entries
    statements: List[Any] = []
    for plan in live:
        statements.append(Statement(f"c{plan.index}", plan.verify_sql, "count"))
        if plan.entity is not None:
            statements.append(Statement(f"k{plan.index}", plan.unit_verify_sql, "count"))
            continue
        statements.append(Statement(f"b{plan.index}", plan.bits_sql, "bits"))
        if plan.view is not None:
            statements.append(Statement(f"k{plan.index}", plan.unit_verify_sql, "count"))
            if plan.unit_bits_sql:
                statements.append(Statement(f"u{plan.index}", plan.unit_bits_sql, "bits"))
    return tables, statements


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
        done.failed[plan.index] = RUN_FAILED
        return
    if count is None or count.status != "ok":
        done.failed[plan.index] = RUN_FAILED
        return
    n = reads[str(plan.table)].row_count
    flags = bits.bits.to_list()
    outcomes = bytes(_IN if each else _OUT for each in flags)
    true = outcomes.count(_IN)
    if len(flags) != n or count.count != true:
        done.failed[plan.index] = RUN_FAILED
        return
    size = n
    unit: Optional[bytes] = None
    if plan.view is not None:
        kept = by_id.get(f"k{plan.index}")
        if kept is None or kept.status != "ok":
            done.failed[plan.index] = RUN_FAILED
            return
        size = kept.count or 0
        if plan.unit_where is not None:
            held = by_id.get(f"u{plan.index}")
            if held is None or held.status != "ok" or held.bits is None:
                done.failed[plan.index] = RUN_FAILED
                return
            unit_flags = held.bits.to_list()
            unit = bytes(_IN if each else _OUT for each in unit_flags)
            if len(unit_flags) != n or unit.count(_IN) != size:
                done.failed[plan.index] = RUN_FAILED
                return
        elif size != n:
            done.failed[plan.index] = RUN_FAILED
            return
        if any(each == _IN and not held_in for each, held_in in _pairs(outcomes, unit)):
            done.failed[plan.index] = RUN_FAILED
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
    member = _Member(plan, n, outcomes, true, size, unit, assigned, sizes)
    done.tables.setdefault(str(plan.table), []).append(member)


def _pairs(outcomes: bytes, unit: Optional[bytes]) -> List[Tuple[int, bool]]:
    """Each row's outcome under a query with whether its view unit keeps the row."""
    if unit is None:
        return [(each, True) for each in outcomes]
    return [(each, held == _IN) for each, held in zip(outcomes, unit, strict=True)]


# -- the entities a unit counts -----------------------------------------------------------------


@dataclass
class _PopPlan:
    """The queries over the units that group one table by one entity column, run together."""

    n: int
    table: str
    key: str
    plans: List[QueryPlan]
    link: Optional[CountedLink]
    parent: Optional[Tuple[str, str]]
    units: List[str] = field(default_factory=list)
    bin_column: str = ""
    sql: str = field(default="", repr=False)
    size_sql: str = field(default="", repr=False)
    #: Whether the bin of the entity's children count is one more part of the set.
    binned: bool = False
    #: Whether two parent rows hold one key, so one group may be the record of several parents.
    repeated: bool = False


def _populations(
    live: Sequence[QueryPlan], plans: QueryPlans, links: Sequence[CountedLink]
) -> List[_PopPlan]:
    """The populations of *live* entity queries: one for each table and entity column."""
    grouped: Dict[Tuple[str, str], List[QueryPlan]] = {}
    for plan in live:
        assert plan.entity is not None
        grouped.setdefault((plan.entity.table, plan.entity.key), []).append(plan)
    found: List[_PopPlan] = []
    for n, ((table, key), members) in enumerate(grouped.items()):
        unit = members[0].entity
        assert unit is not None
        link = None
        if unit.parent is not None:
            wanted = (table, unit.parent[0], key, unit.parent[1])
            link = next(
                (
                    each
                    for each in links
                    if (each.child, each.parent, each.child_column, each.parent_column) == wanted
                ),
                None,
            )
        found.append(
            _PopPlan(n, table, key, members, link, unit.parent if link is not None else None)
        )
    return found


def _counts_combinations(parented: bool, parts: int) -> bool:
    """Whether a population's one ``counts`` statement stays inside the query process's limits.

    It returns one row per combination present, and a combination is the bin factor's state
    (the children-per-parent bins and "no parent": :data:`_BINS` + 1 of them when the entity
    column has a parent, else one) and whether each unit and each query holds the entity: at
    most ``states * 2 ** parts`` rows, public structure only, whatever the records hold. The
    process refuses a result of more than ``MAX_GROUPS`` rows, which would make one record decide
    whether the run is answered. A row is at most ``6 * parts + 24`` bytes of JSON (a boolean is
    at most six, the bin two, the count at most twenty); the bound also keeps the largest
    allowed reply (at most ``MAX_GROUPS`` rows) under a quarter of ``REPLY_LIMIT``. With the
    16 parts the group cap allows that is 120 bytes a row, about 12 MB against 64 MiB.
    """
    rows = ((_BINS + 1) if parented else 1) << parts
    return (
        rows <= consumer_queries.MAX_GROUPS
        and rows * (6 * parts + 24) * 4 <= consumer_queries.REPLY_LIMIT
    )


def _unused(name: str, taken: Set[str]) -> str:
    while name in taken:
        name += "_"
    return name


def _sentinel(duck: str) -> str:
    """A constant of the entity column's type: a null is told from it by its own flag."""
    base = duck.split("(")[0]
    return "''" if base == "VARCHAR" else "FALSE" if base == "BOOLEAN" else "0"


def _population_sql(population: _PopPlan, plans: QueryPlans) -> Union[str, Tuple[str, str]]:
    """The checked statement that counts every combination of the population, and the size one.

    The statement is built from the checked text of each unit and the checked tree of each
    query's condition, then checked once more; a refusal is the reason it is not run.
    """
    members = population.plans
    units: List[str] = []
    first: Dict[str, QueryPlan] = {}
    for plan in members:
        text = str(plan.view)
        if text not in first:
            units.append(text)
            first[text] = plan
    population.units = units
    taken = {name for plan in members for name, _ in (plan.entity.outputs if plan.entity else ())}
    mark = _unused("__in", taken)
    kv, kn = _unused("__kv", taken), _unused("__kn", taken)
    tables = dict(plans.schema)
    table, key = population.table, population.key
    counted = population.link is not None and population.parent is not None
    duck = _duck(plans.types_of(table)[key]) or "VARCHAR"
    if counted:
        assert population.parent is not None
        parent, parent_key = population.parent
        schema = plans.schema[parent]
        population.bin_column = _unused("__bin", {*schema.column_names, plans.row_columns[parent]})
        tables[parent] = Table(columns=(*schema.columns, (population.bin_column, "long")))

    def wrapper(at: int) -> str:
        unit = first[units[at]].entity
        assert unit is not None
        columns = ", ".join(f'"w".{_ident(name)} AS {_ident(name)}' for name, _ in unit.outputs)
        held = f'"w".{_ident(unit.out_key)}'
        extra = (
            f", TRUE AS {_ident(mark)}, COALESCE({held}, {_sentinel(duck)}) AS {_ident(kv)}, "
            f"({held} IS NULL) AS {_ident(kn)}"
        )
        return f'(SELECT {columns}{extra} FROM ({units[at]}) AS "w") AS "v{at}"'

    column = f"{_ident(table)}.{_ident(key)}"
    if counted:
        # The records are the parents and, beside them, each group of the table's rows whose key
        # is null or names no parent (one record each, in the bin of "no parent"): every group
        # of every unit is then a record of the population, whatever the unit's condition keeps.
        parent_column = f'"p".{_ident(parent_key)}'
        entities = (
            f'(SELECT "p".{_ident(population.bin_column)} AS "b", '
            f'COALESCE({parent_column}, {_sentinel(duck)}) AS "kv", ({parent_column} IS NULL) '
            f'AS "kn", TRUE AS "po" FROM {_ident(parent)} AS "p" UNION ALL '
            f'SELECT {_BINS} AS "b", COALESCE({column}, {_sentinel(duck)}) AS "kv", '
            f'({column} IS NULL) AS "kn", FALSE AS "po" FROM {_ident(table)} AS {_ident(table)} '
            f'LEFT JOIN {_ident(parent)} AS "q" ON "q".{_ident(parent_key)} = {column} '
            f'WHERE "q".{_ident(parent_key)} IS NULL GROUP BY {column})'
        )
        head = ['"e"."b" AS "b"']
    else:
        entities = (
            f'(SELECT {column} AS "k", COALESCE({column}, {_sentinel(duck)}) AS "kv", '
            f'({column} IS NULL) AS "kn" FROM {_ident(table)} AS {_ident(table)} '
            f"GROUP BY {column})"
        )
        head = []
    base = f'{entities} AS "e"'
    joins: List[str] = []
    selects = list(head)
    for at, text in enumerate(units):
        unit = first[text].entity
        assert unit is not None
        on = f'"e"."kv" = "v{at}".{_ident(kv)} AND "e"."kn" = "v{at}".{_ident(kn)}'
        if counted:
            on += ' AND NOT ("e"."po" AND "e"."kn")'  # a parent with no key matches no group
        joins.append(f"LEFT JOIN {wrapper(at)} ON {on}")
        selects.append(f'COALESCE("v{at}".{_ident(mark)}, FALSE) AS "u{at}"')
    for at, plan in enumerate(members):
        unit_at = units.index(str(plan.view))
        condition = _condition(plan.where, plan.alias, f"v{unit_at}")
        selects.append(f'COALESCE("v{unit_at}".{_ident(mark)} AND ({condition}), FALSE) AS "o{at}"')
    names = [*(["b"] if counted else []), *(f"u{at}" for at in range(len(units)))]
    names += [f"o{at}" for at in range(len(members))]
    listed = ", ".join(f'"x".{_ident(name)}' for name in names)
    text = (
        f"SELECT {listed}, COUNT(*) FROM (SELECT {', '.join(selects)} FROM {base} "
        f'{" ".join(joins)}) AS "x" GROUP BY {listed}'
    )
    checked = _checked_statement(text, tables)
    if isinstance(checked, Refused):
        return f"the statement that counts its entities is refused when checked again: {checked.reason}"
    sized = _checked_statement(f'SELECT COUNT(*) FROM {entities} AS "z"', tables)
    if isinstance(sized, Refused):
        return (
            f"the statement that counts its entities is refused when checked again: {sized.reason}"
        )
    size = sized.text
    return checked.text, size


def _add_population(
    population: _PopPlan,
    plans: QueryPlans,
    reads: Mapping[str, Any],
    tables: Dict[str, List[Tuple[str, str, Sequence[Any]]]],
    statements: List[Any],
    done: "Measured",
) -> None:
    """Add what *population* runs to the tables and statements of the process, or fail its queries."""
    from fluid_build.profiler.consumer_queries import Statement

    built = _population_sql(population, plans)
    link = population.link
    stale = (
        link is not None
        and population.parent is not None
        and len(link.bins) != reads[population.parent[0]].row_count
    )
    if isinstance(built, str) or stale:
        for plan in population.plans:
            done.failed[plan.index] = RUN_FAILED
        population.plans = []
        return
    population.sql, population.size_sql = built
    if link is not None and population.parent is not None:
        parent, parent_key = population.parent
        loaded = _loaded(
            _duck(plans.types_of(parent)[parent_key]) or "", reads[parent].values[parent_key]
        )
        held = [value for value in loaded or () if value is not None]
        population.repeated = loaded is None or len(set(held)) != len(held)
        tables[population.parent[0]].append((population.bin_column, "BIGINT", list(link.bins)))
        population.binned = not all(_shown_of(link))
    statements.append(Statement(f"e{population.n}", population.sql, "counts"))
    statements.append(Statement(f"s{population.n}", population.size_sql, "count"))


def _shown_of(link: CountedLink) -> Tuple[bool, ...]:
    """Which of the histogram's bins publish their cell; a bin it has no word on is withheld."""
    return tuple(link.shown[at] if at < len(link.shown) else False for at in range(_BINS))


def _population_answer(
    population: _PopPlan, by_id: Mapping[str, Any], reads: Mapping[str, Any], done: "Measured"
) -> None:
    """Read a population's counts and check them against the rows each statement counted.

    Anything that does not add up fails every query of the population (and so of the run): the
    units' rows must be the entities, the queries' rows those the combinations keep. A counted
    relationship's parents leave out the groups with no parent (a null or unknown key), which the
    rows may hold, so there the rows may only be as many as the entities or more.
    """
    members = population.plans
    if not members:
        return

    def fail() -> None:
        for plan in members:
            done.failed[plan.index] = RUN_FAILED

    result = by_id.get(f"e{population.n}")
    if result is None or result.status != "ok" or result.groups is None:
        fail()
        return
    counted = population.link is not None and population.parent is not None
    sized = by_id.get(f"s{population.n}")
    if sized is None or sized.status != "ok" or sized.count is None:
        fail()
        return
    size = sized.count
    link = population.link
    units, queries = len(population.units), len(members)
    offset = 1 if counted else 0
    binned = population.binned
    cells: Dict[Combination, int] = {}
    in_unit = [0] * units
    kept = [0] * queries
    seen: Set[int] = set()
    total = 0
    for row in result.groups:
        *keys, count = row
        if len(keys) != offset + units + queries or not isinstance(count, int) or count < 0:
            fail()
            return
        flags = keys[offset:]
        if any(not isinstance(each, bool) for each in flags):
            fail()
            return
        state: Tuple[int, ...] = ()
        if counted:
            assert link is not None
            at = keys[0]
            if isinstance(at, bool) or not isinstance(at, int) or not 0 <= at <= _BINS:
                fail()
                return
            seen.add(at)
            state = (_bin_state(at, link.shown),) if binned else ()
        combination = (*(_IN if each else _OUT for each in flags), *state)
        cells[combination] = cells.get(combination, 0) + count
        total += count
        for at, each in enumerate(flags[:units]):
            in_unit[at] += count if each else 0
        for at, each in enumerate(flags[units:]):
            kept[at] += count if each else 0
    if total != size:
        fail()
        return
    members_found: List[_EntityMember] = []
    unit_at: List[int] = []
    for at, plan in enumerate(members):
        unit = population.units.index(str(plan.view))
        rows_unit, rows_query = by_id.get(f"k{plan.index}"), by_id.get(f"c{plan.index}")
        if rows_unit is None or rows_unit.status != "ok" or rows_unit.count is None:
            fail()
            return
        if rows_query is None or rows_query.status != "ok" or rows_query.count is None:
            fail()
            return
        # Every group of the unit is a record of the population by construction (a parent, or a
        # group that names no parent), so the unit's rows and the query's are the records counted;
        # only a parent key that repeats makes one group the record of several parents. A
        # mismatch is an error of this module's own, not of the records.
        if population.repeated:
            exact = rows_unit.count <= in_unit[unit] and rows_query.count <= kept[at]
        else:
            exact = rows_unit.count == in_unit[unit] and rows_query.count == kept[at]
        if not exact:
            fail()
            return
        members_found.append(_EntityMember(plan, in_unit[unit], kept[at]))
        unit_at.append(unit)
    states: Set[int] = set()
    if binned:
        assert link is not None
        states = {_bin_state(at, link.shown) for at in range(_BINS + 1)}
    done.populations.append(
        _Population(
            members_found,
            unit_at,
            [units + at for at in range(queries)],
            units + queries if binned else None,
            frozenset(states),
            cells,
        )
    )
