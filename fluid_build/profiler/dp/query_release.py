# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The noisy rate of a consumer query in a private release (profile format 1-5-0).

A request's consumer query is answered in a private release, with noise and a ``unit``, when it
passes the checks of ADR 0028 and its records can be charged (PROFILE-SPEC section 3a, "A noisy
query"). Each answered query is one part with two cells for each group, the records of its unit
the query does not return and those it does, and the part is a count table like any other of the
release: one record lands in exactly one cell, so a unit that contributes ``m`` records moves at
most ``m`` cells by one and the squared L2 sensitivity is ``m**2``. It has a share of the budget of
its own, draws its noise from the stream of its label ``query/<name>`` and is made to agree with
no other table.

**What a unit is, and what it is charged.** The planner (``query_rates.plan_queries`` with
``noisy=True``) decides what the query counts; this module decides what one unit of privacy
changes of it, from the contract and the request alone, before any record is read:

* a table, or a view that keeps some rows of one table ``T``: ``unit: {"table": T}``, charged
  ``m_T``. ``T`` must be reached by the unit of privacy (the unit's own expose has ``m = 1``, a
  table the unit follows into has ``m_T`` of the follow plan), else the query is named
  :data:`NOT_REACHED`;
* the groups of ``T``'s rows by the columns ``K`` of an entity: ``unit: {"table": T, "by": [K]}``.
  Form (a): ``K`` is the child column of a relationship from a parent ``P`` into ``T`` that the unit
  follows, one group is the rows of one row of ``P`` and a unit removes a group whole, so the charge
  is ``m_P`` (1 when ``P`` is the unit's expose). Form (b): ``K`` is the natural entity of ``T``
  (the ``history.entity`` of the table's ``validityParams`` window) and the release holds ``T``'s
  history, which is where the document states the entity; the charge is ``m_T``, and the records
  must put an entity's versions in one unit, which is checked exactly as a history's is
  (``profile_release_entity_units``) unless ``K`` is the followed child column, where it holds by
  construction. Form (a) is taken when it matches and ``P`` is reached. A unit's own expose has no
  entity form (one unit is one version and can move its entity from one cell to the other), nor
  has a table the unit does not reach, nor a column that is neither; two followed relationships
  into ``T`` on the same columns from parents the unit reaches would add their charges, so such a
  query is named (:func:`parents_of`). A query whose unit has ``by`` carries no ``groupBy``. Under
  form (b) the contract must declare the entity column required, and a record with no value in it
  refuses the release (``profile_release_null``): rows with no entity would be one group across
  units.

Each query that cannot be answered is named with its reason and never a count; the reasons are
decided before any record is read, so ``--strict`` refuses them.

**Counting.** :func:`exact_counts` runs the checked statements of every answered query once, in the
query process of :mod:`~fluid_build.profiler.query_rates`, over the rows the release keeps of each
table (after the bounding of an own-rows release or the truncation per parent of a release that
follows), and reads the exact cells of every group without any cell or set check: a noisy release
suppresses nothing. The columns a query reads that the release does not otherwise read are read for
the queries only. A failure that depends on the records (a statement that fails, a value that does
not load, rows that are not the unit's records) withholds every query of the release: no entry is
written, each is named with :data:`~fluid_build.profiler.query_rates.RUN_FAILED`, the other parts
of the release are unaffected (the parts and their shares were fixed before any record was read)
and the same run draws the same bytes again.

Truncation is per row of the parent table, not per entity: a release that follows keeps at most the
cap of an entity's rows under one parent row, so an entity with more rows than the cap under one
parent row is described by the rows kept, and a query by entity counts the entity so.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import (
    AbstractSet,
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from fluid_build.profiler import query_rates, sources
from fluid_build.profiler._stats import Request, StatisticsBackend
from fluid_build.profiler.dp import bounding
from fluid_build.profiler.dp.release import (
    NO_CELL,
    QUERY,
    RULE_NAME,
    Part,
    ReleasedTable,
    ReleaseRefused,
    _noise_block,
)

__all__ = [
    "ENTITY_MAY_BE_NULL",
    "ENTITY_OF_EXPOSE",
    "FORM_FOLLOWED",
    "FORM_HISTORY",
    "FORM_ROWS",
    "NOT_AN_ENTITY",
    "NOT_REACHED",
    "NO_HISTORY",
    "NoisyQuery",
    "QueryRelease",
    "TWO_PARENTS",
    "admitted",
    "exact_counts",
    "parents_of",
    "plan_noisy_queries",
    "query_entries",
    "query_parts",
    "query_units",
    "withheld",
]

#: How a query's records are charged: a table's rows, the groups of a followed relationship, a
#: history's entities.
FORM_ROWS, FORM_FOLLOWED, FORM_HISTORY = "rows", "followed", "history"

NOT_REACHED = (
    "the unit of privacy does not reach its table, so fluid profile does not answer a query over it"
)
ENTITY_OF_EXPOSE = (
    "its unit groups the rows of the unit's own expose, where one unit is one row and removing it "
    "can move an entity from one cell of the rate to the other"
)
NO_HISTORY = (
    "its entity is the natural entity of its table, which the release states only through the "
    "table's history, and the release holds none (list a history for the table in the request)"
)
NOT_AN_ENTITY = (
    "its entity column is neither the child column of a relationship the unit follows nor the "
    "natural entity of its table"
)
ENTITY_MAY_BE_NULL = (
    "its entity column is the natural entity of its table and the contract does not declare it "
    "required, so the rows with no value would form one group across units"
)
TWO_PARENTS = (
    "its entity column is the child column of followed relationships from more than one parent "
    "the unit reaches, so one unit's rows of every parent could move a group from one cell of the "
    "rate to the other"
)


# ---------------------------------------------------------------------------
# The plan: what is answered, decided before any record is read
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoisyQuery:
    """One answered query: its plan, its unit and what one unit of privacy changes of it."""

    plan: query_rates.QueryPlan
    #: The table whose rows the query counts (its records are those rows, or groups of them).
    table: str
    #: The columns that group the table's rows into records (an entity), ``()`` for the rows.
    by: Tuple[str, ...]
    #: :data:`FORM_ROWS`, :data:`FORM_FOLLOWED` or :data:`FORM_HISTORY`.
    form: str
    #: ``m``: the most records one unit of privacy changes (the L1 sensitivity of its two cells).
    charge: int

    @property
    def label(self) -> str:
        return f"query/{self.plan.name}"

    @property
    def groups(self) -> int:
        return len(self.plan.levels) if self.plan.group_by is not None else 1

    @property
    def cells(self) -> int:
        """Two cells for each group: the records the query does not return, then those it does."""
        return 2 * self.groups

    @property
    def sensitivity(self) -> int:
        """The squared L2 sensitivity to one unit: one record lands in exactly one cell."""
        return self.charge * self.charge

    @property
    def unit(self) -> Dict[str, Any]:
        found: Dict[str, Any] = {"table": self.table}
        if self.by:
            found["by"] = list(self.by)
        return found


@dataclass(frozen=True)
class QueryRelease:
    """The queries of a release: the plan the query process runs, those answered and those named."""

    #: The queries as planned for the query process, the answerable ones decided by this module.
    plans: query_rates.QueryPlans
    #: The answered queries, in the request's order.
    queries: Tuple[NoisyQuery, ...] = ()
    #: Each query not answered, ``(query <name>, reason)``, in the request's order.
    named: Tuple[Tuple[str, str], ...] = ()
    #: The columns the answered queries read, by table (sorted); read for the queries only when
    #: the release does not read them otherwise.
    read: Mapping[str, Tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))


def parents_of(
    edges: Sequence[Any], table: str, by: Sequence[str], reach: Mapping[str, int]
) -> List[str]:
    """The parents the unit reaches of the followed relationships into *table* on columns *by*.

    One entry for each followed edge whose child is *table*, whose child columns are the columns
    *by* (in any order) and whose parent has a charge of at least 1. More than one means one
    unit's rows of every such parent leave together and their charges would add.
    """
    wanted = set(by)
    return [
        each.parent
        for each in edges
        if each.child == table
        and set(each.spec.child_columns) == wanted
        and reach.get(each.parent, 0) >= 1
    ]


def _natural_entity(contract: Mapping[str, Any], table: str) -> Optional[str]:
    """The column the contract's validity window of *table* names as its entity, else ``None``."""
    from fluid_build.forge_datamodel import semantics_builder
    from fluid_build.policy._common import iter_exposes

    for expose in iter_exposes(dict(contract)):
        if expose.get("exposeId") != table:
            continue
        semantics = expose.get("semantics")
        try:
            window = semantics_builder.version_window(
                semantics if isinstance(semantics, Mapping) else {}
            )
        except (KeyError, StopIteration, TypeError, ValueError):
            return None
        return None if window is None else str(window.key)
    return None


def _required(contract: Mapping[str, Any], table: str, column: str) -> bool:
    """Whether the contract declares *column* of *table* ``required: true``."""
    from fluid_build.policy._common import iter_exposes
    from fluid_build.profiler.dp.release import _schema

    for expose in iter_exposes(dict(contract)):
        if expose.get("exposeId") == table:
            return _schema(expose).get(column, {}).get("required") is True
    return False


def _decided(
    contract: Mapping[str, Any],
    plan: query_rates.QueryPlan,
    *,
    unit: str,
    reach: Mapping[str, int],
    edges: Sequence[Any],
    histories: Mapping[str, str],
) -> Union[str, NoisyQuery]:
    """What one planned query is charged, or why it is named. Reads no record."""
    table = str(plan.table)
    held = reach.get(table, 0)
    if held < 1:
        return NOT_REACHED
    entity = plan.entity
    if entity is None:
        return NoisyQuery(plan, table, (), FORM_ROWS, held)
    if table == unit:
        return ENTITY_OF_EXPOSE
    key = (entity.key,)
    parents = parents_of(edges, table, key, reach)
    if len(parents) > 1:
        return TWO_PARENTS
    if parents:
        return NoisyQuery(plan, table, key, FORM_FOLLOWED, reach[parents[0]])
    if _natural_entity(contract, table) != entity.key:
        return NOT_AN_ENTITY
    if histories.get(table) != entity.key:
        return NO_HISTORY
    if not _required(contract, table, entity.key):
        return ENTITY_MAY_BE_NULL
    return NoisyQuery(plan, table, key, FORM_HISTORY, held)


def plan_noisy_queries(
    contract: Mapping[str, Any],
    request: Request,
    backend: StatisticsBackend,
    *,
    unit: str,
    reach: Mapping[str, int],
    edges: Sequence[Any] = (),
    histories: Mapping[str, str] = MappingProxyType({}),
) -> Optional[QueryRelease]:
    """Plan the queries of *request* for a private release, or ``None`` when it lists none.

    *unit* is the unit's expose, *reach* the ``m_T`` of each table the unit reaches, *edges* the
    relationships the unit follows and *histories* the entity column of each table whose history
    the release holds. A query is answered, with the unit it states and what it is charged, or
    named. Reads no record.
    """
    if not request.queries:
        return None
    planned = query_rates.plan_queries(contract, request, backend.enum_levels, noisy=True)
    answered: List[NoisyQuery] = []
    named: List[Tuple[str, str]] = []
    plans: List[query_rates.QueryPlan] = []
    for plan in planned.plans:
        if plan.reason is not None:
            named.append((f"query {plan.name}", plan.reason))
            plans.append(plan)
            continue
        decided = _decided(contract, plan, unit=unit, reach=reach, edges=edges, histories=histories)
        if isinstance(decided, str):
            named.append((f"query {plan.name}", decided))
            plans.append(plan.refused(decided))
            continue
        answered.append(decided)
        plans.append(plan)
    refined = replace(planned, plans=tuple(plans))
    return QueryRelease(refined, tuple(answered), tuple(named), _reads(answered))


def _reads(queries: Iterable[NoisyQuery]) -> Dict[str, Tuple[str, ...]]:
    """The columns the queries and their units read, by table, sorted."""
    found: Dict[str, Dict[str, None]] = {}
    for each in queries:
        for table, column in each.plan.reads:
            found.setdefault(table, {})[column] = None
        if each.plan.group_by is not None:
            found.setdefault(each.table, {})[each.plan.group_by] = None
    return {table: tuple(sorted(columns)) for table, columns in found.items()}


# ---------------------------------------------------------------------------
# The parts
# ---------------------------------------------------------------------------


def query_parts(queries: Optional[QueryRelease]) -> Tuple[Part, ...]:
    """One part for each answered query, in the request's order: two cells for each group."""
    if queries is None:
        return ()
    return tuple(Part(QUERY, each.label, (), (each.cells,)) for each in queries.queries)


def query_units(queries: Optional[QueryRelease]) -> List[Tuple[str, ...]]:
    """The labels of the queries as units of admission, each released whole or not at all."""
    return [] if queries is None else [(each.label,) for each in queries.queries]


def admitted(
    queries: Optional[QueryRelease], gone: AbstractSet[str], prefix: str = ""
) -> Optional[QueryRelease]:
    """The queries that stay when the parts labelled in *gone* (each with *prefix*) are left out."""
    if queries is None:
        return None
    kept = tuple(each for each in queries.queries if f"{prefix}{each.label}" not in gone)
    if len(kept) == len(queries.queries):
        return queries
    return replace(queries, queries=kept, read=_reads(kept))


def withheld(queries: Optional[QueryRelease]) -> Tuple[Tuple[str, str], ...]:
    """Each answered query as named when the records withhold every query of the release."""
    if queries is None:
        return ()
    return tuple((f"query {each.plan.name}", query_rates.RUN_FAILED) for each in queries.queries)


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


def query_content(
    queries: Optional[QueryRelease], table: str, raw: sources.SourceColumns, rows: int
) -> Optional[List[bytes]]:
    """Each row's bytes of the columns the queries read from *table*, for the rows kept to break ties.

    Rows that differ only in a column a query reads must not be told apart by their position, or
    the rows kept (and so the answer) would depend on the order the records were read in: these
    bytes are added to the row's content, which keys the priority. ``None`` when no answered query
    reads *table*, so a release with no query keeps its bytes.
    """
    if queries is None or not queries.queries:
        return None
    names = queries.read.get(table, ())
    if not names:
        return None
    columns = [raw.values[name] for name in names]
    return [
        bounding.encode_values(
            [
                None if each[at] is None else f"{type(each[at]).__name__}:{each[at]}"
                for each in columns
            ]
        )
        for at in range(rows)
    ]


def _subset(
    read: sources.SourceColumns, rows: Sequence[int], names: Iterable[str]
) -> sources.SourceColumns:
    values = {name: [read.values[name][at] for at in rows] for name in names}
    return sources.SourceColumns(description=read.description, row_count=len(rows), values=values)


def _refuse_missing_entities(
    each: NoisyQuery, read: sources.SourceColumns, rows: Sequence[int]
) -> None:
    """Refuse a record with no value in the entity column: it would be a group of several units."""
    (column,) = each.by
    values = read.values[column]
    for at in rows:
        if values[at] is None:
            raise ReleaseRefused(
                "profile_release_null",
                f"table {each.table!r} column {column!r}: no value, and the release needs one in "
                "every row",
                {"next": "fix the records, or the contract's column type, then release"},
            )


def exact_counts(
    queries: Optional[QueryRelease],
    raw: Mapping[str, sources.SourceColumns],
    kept: Mapping[str, Sequence[int]],
    backend: StatisticsBackend,
    *,
    units: Optional[Sequence[Optional[bytes]]] = None,
    cap: Optional[int] = None,
) -> Optional[Dict[str, List[int]]]:
    """The exact cells of each answered query by its label: ``(not returned, returned)`` per group.

    *raw* are the records read of each table and *kept* the rows of each that the release keeps.
    The queries run over the kept rows only. With *units* (the unit of each row read of the table
    of an own-rows release that keeps rows by entity) and *cap*, a unit counts at most *cap* rows in
    one cell, as every other count table of that release does. ``None`` when a failure that
    depends on the records withholds every query.
    """
    if queries is None or not queries.queries:
        return {}
    for each in queries.queries:
        if each.form == FORM_HISTORY:
            _refuse_missing_entities(each, raw[each.table], kept[each.table])
    reads = {
        table: _subset(raw[table], kept[table], names) for table, names in queries.read.items()
    }
    runnable = replace(
        queries.plans, plans=tuple(each.plan for each in queries.queries), noisy=True
    )
    measured = query_rates.measure(runnable, reads, backend, None)
    if measured.failed:
        return None
    found: Dict[str, List[int]] = {}
    for each in queries.queries:
        cells = measured.raw_cells(each.plan.index)
        if cells is None:
            return None
        if each.by == () and units is not None and cap is not None:
            rows = measured.row_cells(each.plan.index)
            assert rows is not None  # a query over a table's rows has a cell for each row
            held = [units[at] for at in kept[each.table]]
            found[each.label] = bounding.capped_counts(
                [unit for unit, cell in zip(held, rows, strict=True) if cell != NO_CELL],
                [cell for cell in rows if cell != NO_CELL],
                each.cells,
                cap,
            )
            continue
        found[each.label] = [value for k, true in cells for value in (k - true, true)]
    return found


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def query_entries(
    queries: Optional[QueryRelease],
    by_label: Mapping[str, ReleasedTable],
    values_of: Mapping[str, Sequence[str]],
) -> List[Dict[str, Any]]:
    """The ``queries`` of the document: one entry for each answered query, in the request's order.

    Nothing is written for a query whose label has no value (the records withheld every query).
    """
    found: List[Dict[str, Any]] = []
    if queries is None:
        return found
    for each in queries.queries:
        if each.label not in values_of:
            continue
        cells = values_of[each.label]
        keys = list(each.plan.levels) if each.plan.group_by is not None else [""]
        statistic: Dict[str, Any] = {
            "definition": "levelHistogram/1",
            "levels": ["false", "true"],
            "sampleSize": {"min": 0},
            "disclosure": RULE_NAME,
            "noise": _noise_block(by_label[each.label]),
            "groups": [
                {"group": key, "status": "F", "noisyCounts": list(cells[at * 2 : at * 2 + 2])}
                for at, key in enumerate(keys)
            ],
        }
        if each.plan.group_by is not None:
            statistic["groupBy"] = each.plan.group_by
        found.append({"name": each.plan.name, "unit": each.unit, "statistics": [statistic]})
    return found
