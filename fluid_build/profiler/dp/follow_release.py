# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A release under differential privacy whose unit follows its child rows (profile format 1-4-0).

With ``--follow`` the privacy unit is one row of the unit's expose together with the rows of
child tables that follow it (:mod:`~fluid_build.profiler.dp.follows`; PROFILE-SPEC section 3a), and
the release holds the unit's expose and every table the unit reaches. The tables are truncated
before anything is measured, from the seed and from each parent's own children, and then:

* a count table of a table ``T`` (levels, bins, cliques) moves by at most ``m_T`` rows, so its
  squared L2 sensitivity is ``m_T**2`` and its noise is
  :func:`~fluid_build.profiler.dp.release.scale_units` of that, in exact integers;
* a rank correlation of ``T`` has the sensitivity of the registry's, times ``m_T``;
* a relationship's children-per-parent histogram, ungrouped or grouped by a parent column, has
  the sensitivity of PROFILE-SPEC "What a noisy statistic is charged": ``m_P`` when the unit
  reaches the parent and reaches the child through this relationship alone (squared ``m_P**2``),
  and ``2 m_C`` (L1) and squared L2 ``2 m_C**2`` when the parent is fixed and the child is reached.
  Any other case is not released, with the reason;
* a statistic grouped through a link (``groupByLink``) is a count table of the child's table
  (squared ``m_T**2``), released only when the unit follows the relationship or does not reach the
  parent, with the parent column's contract levels as its groups.

A relationship that reaches an entity of a table with a validity window
(RFC-versioned-relationships section 2) counts its children per entity, not per version: the
parents are the distinct entity keys among the target's kept rows, and the target's parent key
repeats by design, so it is not checked for repeated keys. The charge is the same as for a table
whose rows are parents, argued per entity in :func:`plan_follow_release`. It is released only where
an entity cannot span two units (:func:`_entity_units_reason`), and a release whose records put an
entity's versions in two units is refused (``profile_release_entity_units``).

Where the unit follows a relationship the children per parent lie in the integer bins ``0``
to the cap; elsewhere in a fixed public ladder that ends at 10**9, and a count beyond it refuses
the release, naming the relationship only. Every group key is a contract level or a public bin
edge, and the ``sampleSize`` of a noisy statistic is ``{"min": 0}``, as in every release.

The tables the unit does not reach are treated as fixed: the same in every neighbouring data set.
The guarantee protects the unit's rows, not theirs, and the report says so.

Each part is one equal share of the budget (a tenth is spent choosing cliques when a table chooses
them, split over the tables that do), and the document, the report and the record read the one
list of parts (:func:`follow_parts`).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field, replace
from fractions import Fraction
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
)

from fluid_build.policy._common import iter_exposes
from fluid_build.profiler import defaults, document, runner, sources
from fluid_build.profiler._stats import Request, StatisticsBackend
from fluid_build.profiler.dp import bounding, consistency, rank_noise, selection
from fluid_build.profiler.dp.bounding import ROWS, UnitPlan
from fluid_build.profiler.dp.discrete_gaussian import sample_discrete_gaussian
from fluid_build.profiler.dp.follows import FollowPlan, FollowSpec, keep_rows, plan_follows
from fluid_build.profiler.dp.randomness import SeededSource
from fluid_build.profiler.dp.record import ReleaseRecord
from fluid_build.profiler.dp.release import (
    BINS,
    CLIQUE,
    DEFAULT_ROW_FLOOR,
    LEVELS,
    LINK,
    RANK,
    RELATION,
    RULE_NAME,
    WORKLOAD_UNUSED,
    EncodedRows,
    Part,
    PublicStructures,
    ReleasedColumn,
    ReleasedTable,
    ReleasePlan,
    ReleaseRefused,
    _agree,
    _floor_places,
    _mismatch,
    _noise_block,
    _plan_columns,
    _refuse,
    _selected,
    _table_block,
    _table_not_released,
    _TableColumns,
    _version,
    decimal_text,
    place_values,
    public_structures,
    release_parts,
    scale_units,
    selection_share,
    split_budget,
)

__all__ = [
    "CHILDREN_LADDER",
    "FollowRelease",
    "FollowReleasePlan",
    "FollowRows",
    "LABELS_BELOW_P3",
    "LinkStat",
    "RelationStat",
    "TablePlan",
    "build_follow_record",
    "check_follow_record",
    "check_selection_available",
    "describe",
    "draw_follow",
    "encode_follow_rows",
    "measure_counts",
    "truncated",
    "follow_parts",
    "plan_follow_release",
    "unit_block",
]

#: The bins of a relationship's children per parent that the unit does not follow: a fixed public
#: ladder whose last edge is the most children a parent may hold, 10**9.
CHILDREN_LADDER: Tuple[int, ...] = (
    0,
    1,
    2,
    3,
    5,
    10,
    20,
    50,
    100,
    1000,
    10**4,
    10**5,
    10**6,
    10**9,
)

#: The sensitivity labels that need the grade P3 or stronger (ADR 0020, decision 7).
LABELS_BELOW_P3 = frozenset({"pii", "phi", "restricted", "pseudonymized", "tokenized"})

_NOT_NOISY = "format 1-4-0 holds no noisy form of it"
_FIXED = (
    "the unit does not reach this table, so its rows are fixed: its listed columns appear with "
    "their domains only"
)


# ---------------------------------------------------------------------------
# The plan: everything that does not depend on a record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TablePlan:
    """One table the unit reaches: its release plan, scaled by ``m_T``, and the columns read."""

    table: str
    reach: int
    plan: ReleasePlan
    #: The release plan's own request table, ``None`` when the request does not list the table.
    listed: bool


@dataclass(frozen=True)
class RelationStat:
    """A relationship's children per parent, released with noise (ungrouped or by a parent column)."""

    parent: str
    parent_columns: Tuple[str, ...]
    child: str
    child_columns: Tuple[str, ...]
    #: The cap of the followed relationship, ``None`` when the unit does not follow it.
    cap: Optional[int]
    #: The whole-number edges of the bins of children per parent.
    edges: Tuple[int, ...]
    #: The parent column the statistic is grouped by, ``None`` for the ungrouped one.
    group: Optional[ReleasedColumn]
    #: L1 and squared L2 sensitivity to one unit.
    l1: int
    sensitivity: int
    #: The relationship reaches an entity: its parents are the distinct entity keys of the parent
    #: table, one for each entity and not one for each version.
    entity: bool = False

    @property
    def groups(self) -> int:
        return 1 if self.group is None else self.group.size

    @property
    def cells(self) -> int:
        return len(self.edges) - 1

    @property
    def spelled(self) -> str:
        return (
            f"{self.child}({', '.join(self.child_columns)}) to "
            f"{self.parent}({', '.join(self.parent_columns)})"
        )

    @property
    def label(self) -> str:
        base = (
            f"relationship/{self.parent}.{','.join(self.parent_columns)}/"
            f"{self.child}.{','.join(self.child_columns)}"
        )
        return base if self.group is None else f"{base}/by/{self.group.name}"


@dataclass(frozen=True)
class LinkStat:
    """A child column's histogram grouped by a parent column through a relationship."""

    parent: str
    parent_columns: Tuple[str, ...]
    child: str
    child_columns: Tuple[str, ...]
    column: ReleasedColumn
    by: ReleasedColumn
    #: ``m_T`` of the child's table.
    reach: int

    @property
    def sensitivity(self) -> int:
        return self.reach * self.reach

    @property
    def label(self) -> str:
        return (
            f"link/{self.child}.{self.column.name}/by/{self.parent}.{self.by.name}/via/"
            f"{','.join(self.child_columns)}"
        )


@dataclass(frozen=True)
class FollowReleasePlan:
    """What a release of a unit that follows its child rows holds and spends, before a record is read."""

    unit: str
    follows: FollowPlan
    #: The tables the unit reaches, the expose first.
    tables: Tuple[TablePlan, ...]
    relations: Tuple[RelationStat, ...]
    links: Tuple[LinkStat, ...]
    rho: Fraction
    fractional_bits: int
    clique_method: str
    row_floor: Optional[int]
    #: Every table read, with the columns read (each table is read once).
    reads: Tuple[Tuple[str, Tuple[runner.ColumnPlan, ...]], ...]
    #: Tables the profile lists that the unit does not reach, with the columns it lists of each.
    fixed: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...]
    #: The relationships the document names, in order: the followed ones, then the others.
    relationships: Tuple[Tuple[str, Tuple[str, ...], str, Tuple[str, ...]], ...]
    not_released: Tuple[Tuple[str, str], ...]
    warnings: Tuple[str, ...]
    #: The structure columns read besides the released ones: the group and link columns by table,
    #: with their public cells.
    structure: Mapping[Tuple[str, str], ReleasedColumn] = field(default_factory=dict)
    #: The key column tuples read, by table.
    key_sets: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    #: The key column tuples that name a parent row, which must be unique.
    parent_sets: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    #: The entities a statistic of the release counts under a unit that reaches their table:
    #: ``(table, entity columns, parent table of the edge into it, that edge's parent columns, its
    #: child columns on the table)``. The records must put an entity's versions in one unit.
    entity_units: Tuple[
        Tuple[str, Tuple[str, ...], str, Tuple[str, ...], Tuple[str, ...]], ...
    ] = ()

    @property
    def selects(self) -> bool:
        return any(each.plan.selects for each in self.tables)

    @property
    def reach(self) -> Mapping[str, int]:
        return self.follows.reach

    @property
    def read_tables(self) -> Tuple[str, ...]:
        return tuple(table for table, _ in self.reads)

    def table(self, name: str) -> TablePlan:
        return next(each for each in self.tables if each.table == name)


def _unit_plan(unit: str, table: str) -> UnitPlan:
    return UnitPlan(unit, table, ROWS, (), None, 1, 1)


def _declared_relationships(
    contract: Mapping[str, Any],
) -> List[runner.Relationship]:
    """Every relationship the contract declares between two of its exposes, in its order."""
    exposes = {str(each.get("exposeId")): each for each in iter_exposes(dict(contract))}
    found: List[runner.Relationship] = []
    for expose_id, expose in exposes.items():
        for entry in bounding._semantics_list(expose, "relationships"):
            if not isinstance(entry, Mapping) or entry.get("to") not in exposes:
                continue
            child = bounding._column_list(entry.get("fromColumns"))
            parent = bounding._column_list(entry.get("toColumns"))
            if child is None or parent is None or len(child) != len(parent):
                continue
            name = entry.get("name")
            stable = runner._entity_stable(exposes[str(entry["to"])], parent)
            found.append(
                runner.Relationship(
                    expose_id,
                    name if isinstance(name, str) and name else "?",
                    str(entry["to"]),
                    child,
                    parent,
                    entity=stable is not None,
                    stable=stable or frozenset(),
                )
            )
    return found


def _entity_units_reason(
    relationship: runner.Relationship, tree: FollowPlan, m_parent: int
) -> Optional[str]:
    """Why a statistic over the entities of *relationship*'s parent cannot be released, or ``None``.

    An entity of a versioned table is released only when it cannot span two units: the unit does
    not reach the table (``m_T = 0``, its rows are fixed), or reaches it through a followed
    relationship whose child columns on the table are the natural entity or columns a
    ``dependency`` makes stable per entity, so every version of an entity follows the same parent
    row. The unit's own expose is no such table: each of its rows is a unit. The reason is
    decided by the contract and the flags alone.
    """
    if m_parent == 0:
        return None
    edge = tree.edge_into(relationship.parent)
    if edge is not None and set(edge.spec.child_columns) <= relationship.stable:
        return None
    return (
        "an entity's versions could follow different units: follow "
        f"{relationship.parent} by its natural entity or by a column stable per entity"
    )


def _asked_statistics(request: Request, relationship: runner.Relationship) -> Tuple[str, ...]:
    """The statistics the request lists for *relationship*, once each."""
    found: List[str] = []
    for each in request.relationships:
        if (
            each.child.table == relationship.child
            and each.parent.table == relationship.parent
            and each.child.columns == relationship.child_columns
            and each.parent.columns == relationship.parent_columns
        ):
            found.extend(name for name in each.statistics if name not in found)
    return tuple(found)


def _unlisted(contract: Mapping[str, Any], table: str, bits: int) -> _TableColumns:
    """The columns of a table the request does not list: it holds nothing released."""
    expose = next(each for each in iter_exposes(dict(contract)) if each.get("exposeId") == table)
    plan = runner.plan_expose(contract, table)
    return _TableColumns(
        asked=None,
        expose=expose,
        schema=_schema_of(expose),
        expose_plan=plan,
        readable={column.name: column for column in plan.columns},
        skipped=dict(plan.skipped),
        released=(),
        why_not={},
        keys=frozenset(),
        eligible=(),
        selects=False,
        rank_pairs=(),
        rank_left_out=(),
    )


def _schema_of(expose: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    from fluid_build.profiler.dp.release import _schema

    return _schema(expose)


def _key_names(
    contract: Mapping[str, Any],
    request: Request,
    tree: FollowPlan,
    declared: Sequence[runner.Relationship],
) -> Dict[str, FrozenSet[str]]:
    """The key columns of each table that a followed or a requested relationship uses."""
    found: Dict[str, Set[str]] = {}
    for edge in tree.edges:
        found.setdefault(edge.parent, set()).update(edge.spec.parent_columns)
        found.setdefault(edge.child, set()).update(edge.spec.child_columns)
    for each in declared:
        asks = _asks(request, each)
        if asks:
            found.setdefault(each.parent, set()).update(each.parent_columns)
            found.setdefault(each.child, set()).update(each.child_columns)
    return {table: frozenset(names) for table, names in found.items()}


def _asks(request: Request, relationship: runner.Relationship) -> bool:
    from fluid_build.profiler import request as asked_for

    counted, group_by, linked = asked_for.relationship_asks(request, relationship)
    return bool(counted or group_by or linked or _asked_statistics(request, relationship))


def _group_structure(
    contract: Mapping[str, Any],
    backend: StatisticsBackend,
    table: str,
    name: str,
    bits: int,
    *,
    keys: FrozenSet[str],
) -> Tuple[Optional[ReleasedColumn], str]:
    """The public cells of column *name* of *table*, or ``None`` and the reason it has none."""
    expose = next(each for each in iter_exposes(dict(contract)) if each.get("exposeId") == table)
    plan = runner.plan_expose(contract, table)
    column = next((each for each in plan.columns if each.name == name), None)
    if column is None:
        why = dict(plan.skipped).get(name)
        return None, f"{name}: it is a column fluid profile does not read" + (
            f" ({why})" if why else ""
        )
    if name in keys:
        return None, f"{name}: a key of a relationship the release uses"
    known: PublicStructures = public_structures(expose, [column], backend, bits)
    made = known.column(column, _schema_of(expose).get(name, {}), backend)
    if isinstance(made, str):
        return None, f"{name}: {made}"
    return made, ""


def _label_of(contract: Mapping[str, Any], table: str, name: str) -> Optional[str]:
    expose = next(each for each in iter_exposes(dict(contract)) if each.get("exposeId") == table)
    label = _schema_of(expose).get(name, {}).get("sensitivity")
    return label if isinstance(label, str) and label in LABELS_BELOW_P3 else None


def _warning(table: str, column: str, label: str) -> str:
    return (
        f"{table}.{column} carries the sensitivity label {label}, and the unit does not reach "
        f"{table}: its rows are fixed, so a noisy statistic grouped by {column} protects nothing "
        f"about them, and flux-engine counts the column as P1 (below the P3 a {label} column "
        "needs). A run that emits it is refused by the engine's minimum grade."
    )


def plan_follow_release(
    contract: Mapping[str, Any],
    request: Request,
    backend: StatisticsBackend,
    *,
    unit: str,
    rho: Fraction,
    follows: Sequence[FollowSpec],
    row_floor: Optional[int] = None,
    clique_method: str = "aim",
) -> FollowReleasePlan:
    """Plan the release of a unit that follows its child rows. Reads no record.

    Every refusal that does not need a record is made here, before one is read: the tree of
    followed relationships (naming the entry), the unit, what is released, the cells and the
    budget.
    """
    from fluid_build.profiler import request as asked_for

    if clique_method not in selection.METHODS:
        raise _refuse(
            "profile_release_clique_method",
            f"--clique-method is one of {', '.join(selection.METHODS)}",
            "name one of them, or leave it out for aim",
            read=True,
        )
    tree = plan_follows(contract, unit, follows, backend)
    asked_for.check_tables(request, contract)
    if request.table(unit) is None:
        raise _refuse(
            "profile_release_table_not_in_request",
            f"the request does not list the privacy unit {unit!r}",
            "release a table the request lists: "
            + ", ".join(each.expose_id for each in request.tables),
            read=True,
        )
    bits = request.fractional_bits
    declared = _declared_relationships(contract)
    keyed = _key_names(contract, request, tree, declared)
    reach = tree.reach

    # -- the tables the unit reaches, each planned as a table of its own
    columns: Dict[str, _TableColumns] = {}
    for table in tree.order:
        if request.table(table) is None:
            columns[table] = _unlisted(contract, table, bits)
        else:
            columns[table] = _plan_columns(
                contract, request, backend, table, sorted(keyed.get(table, ())), nothing_ok=True
            )
    ranked = any(each.rank_pairs for each in columns.values())
    if row_floor is not None and not ranked:
        raise _refuse(
            "profile_release_row_floor_unused",
            "--row-floor is the floor of a rank correlation, and no rank correlation of a "
            "table the unit reaches is released",
            "leave --row-floor out, or ask for a rank correlation of two released number or "
            "timestamp columns of a table the unit reaches",
            read=True,
        )
    floor = None if not ranked else (DEFAULT_ROW_FLOOR if row_floor is None else row_floor)

    # -- the relationships' statistics
    not_released: List[Tuple[str, str]] = []
    warnings: List[str] = []
    relations: List[RelationStat] = []
    links: List[LinkStat] = []
    structure: Dict[Tuple[str, str], ReleasedColumn] = {}
    relationships: List[Tuple[str, Tuple[str, ...], str, Tuple[str, ...]]] = [
        (edge.parent, edge.spec.parent_columns, edge.child, edge.spec.child_columns)
        for edge in tree.edges
    ]
    used_keys: Dict[str, List[Tuple[str, ...]]] = {}
    parent_sets: List[Tuple[str, Tuple[str, ...]]] = []
    entity_units: List[Tuple[str, Tuple[str, ...], str, Tuple[str, ...], Tuple[str, ...]]] = []
    extra_read: Dict[str, List[str]] = {}
    entities = {
        (each.parent, each.parent_columns, each.child, each.child_columns)
        for each in declared
        if each.entity
    }

    def note_entities(each: runner.Relationship) -> None:
        """A statistic counts the entities of a reached table: its records must put each in one unit."""
        edge = tree.edge_into(each.parent)
        if each.entity and edge is not None:
            item = (
                each.parent,
                each.parent_columns,
                edge.parent,
                edge.spec.parent_columns,
                edge.spec.child_columns,
            )
            if item not in entity_units:
                entity_units.append(item)
                note_keys(edge.parent, edge.spec.parent_columns)
                note_keys(edge.child, edge.spec.child_columns)

    def note_keys(table: str, names: Tuple[str, ...]) -> None:
        if names not in used_keys.setdefault(table, []):
            used_keys[table].append(names)
        extra_read.setdefault(table, []).extend(names)

    for edge in tree.edges:
        note_keys(edge.parent, edge.spec.parent_columns)
        note_keys(edge.child, edge.spec.child_columns)
        if (
            edge.parent,
            edge.spec.parent_columns,
            edge.child,
            edge.spec.child_columns,
        ) not in entities:
            parent_sets.append((edge.parent, edge.spec.parent_columns))  # an entity's key repeats
    for each in declared:
        counted, group_by, linked = asked_for.relationship_asks(request, each)
        stats = _asked_statistics(request, each)
        followed = tree.edge_for(
            (each.parent, each.parent_columns), (each.child, each.child_columns)
        )
        if not (counted or group_by or linked or stats) and followed is None:
            continue
        spelled = (
            f"{each.child}({', '.join(each.child_columns)}) to "
            f"{each.parent}({', '.join(each.parent_columns)})"
        )
        m_parent, m_child = reach.get(each.parent, 0), reach.get(each.child, 0)
        why_units = _entity_units_reason(each, tree, m_parent) if each.entity else None
        for name in stats:
            if name != "marginalHistogram/1":
                not_released.append((f"{name} of {spelled}", _NOT_NOISY))
        if counted:
            others = m_child - (followed.max_children * m_parent if followed is not None else 0)
            if why_units is not None:
                not_released.append((f"children per parent of {spelled}", why_units))
                l1 = squared = 0
            elif m_parent >= 1 and others == 0:
                l1, squared = m_parent, m_parent * m_parent
            elif m_parent == 0 and m_child >= 1:
                l1, squared = 2 * m_child, 2 * m_child * m_child
            else:
                not_released.append(
                    (
                        f"children per parent of {spelled}",
                        _relation_reason(each, m_parent, m_child),
                    )
                )
                l1 = squared = 0
            if squared:
                cap = None if followed is None else followed.max_children
                if (
                    followed is not None
                    and followed.max_children + 1 > selection.MOST_RELEASE_CELLS
                ):
                    # One bin for each count from 0 to the cap; refuse before the edges are built.
                    raise _refuse(
                        "profile_release_too_many_cells",
                        f"--follow {followed.spec.text()} asks for {followed.max_children + 1:,} bins of children "
                        f"per parent, more than the {selection.MOST_RELEASE_CELLS:,} cells a "
                        "release may measure",
                        "lower the cap of that --follow entry, or leave the relationship's "
                        "histogram out of the request",
                    )
                edges = tuple(range(cap + 2)) if cap is not None else CHILDREN_LADDER
                base = RelationStat(
                    each.parent,
                    each.parent_columns,
                    each.child,
                    each.child_columns,
                    cap,
                    edges,
                    None,
                    l1,
                    squared,
                    each.entity,
                )
                relations.append(base)
                note_keys(each.parent, each.parent_columns)
                note_keys(each.child, each.child_columns)
                if each.entity:
                    note_entities(each)
                else:
                    parent_sets.append((each.parent, each.parent_columns))
                for name in group_by:
                    if each.entity and name not in each.stable:
                        not_released.append(
                            (
                                f"children per parent of {spelled} grouped by {each.parent}.{name}",
                                f"{name}: {runner._UNSTABLE}",
                            )
                        )
                        continue
                    made, why = _group_structure(
                        contract,
                        backend,
                        each.parent,
                        name,
                        bits,
                        keys=keyed.get(each.parent, frozenset()),
                    )
                    if made is not None and made.edge_texts:
                        starts = list(made.edge_texts[:-1])
                        if starts != sorted(starts) or len(set(starts)) != len(starts):
                            made, why = None, (
                                f"{name}: its bins' lower edges do not sort as text in bin "
                                "order, which a noisy grouped statistic needs"
                            )
                    if made is None:
                        not_released.append(
                            (
                                f"children per parent of {spelled} grouped by {each.parent}.{name}",
                                why,
                            )
                        )
                        continue
                    relations.append(replace(base, group=made))
                    structure[(each.parent, name)] = made
                    extra_read.setdefault(each.parent, []).append(name)
                    label = _label_of(contract, each.parent, name) if m_parent == 0 else None
                    if label is not None:
                        warnings.append(_warning(each.parent, name, label))
        for column, parent_column in linked:
            what = f"{column} of {each.child} grouped by {each.parent}.{parent_column}"
            if m_child < 1:
                not_released.append(
                    (what, f"the unit does not reach {each.child}, so its rows are fixed")
                )
                continue
            if each.entity and parent_column not in each.stable:
                not_released.append((what, f"{parent_column}: {runner._UNSTABLE}"))
                continue
            if why_units is not None:
                not_released.append((what, why_units))
                continue
            if followed is None and m_parent != 0:
                not_released.append(
                    (
                        what,
                        "a statistic grouped through a link is released only when the unit "
                        "follows the relationship or does not reach the parent: a parent row "
                        "that moves alone would move its children into a group that is never "
                        "written",
                    )
                )
                continue
            child_made, why = _group_structure(
                contract, backend, each.child, column, bits, keys=keyed.get(each.child, frozenset())
            )
            if child_made is not None:
                entry = _schema_of(
                    next(
                        item
                        for item in iter_exposes(dict(contract))
                        if item.get("exposeId") == each.child
                    )
                ).get(column, {})
                if entry.get("required") is not True:
                    child_made, why = None, (
                        f"{column}: the contract does not declare it required: true, and a "
                        "record with no value is refused, never counted"
                    )
            by_made, by_why = _group_structure(
                contract,
                backend,
                each.parent,
                parent_column,
                bits,
                keys=keyed.get(each.parent, frozenset()),
            )
            if by_made is not None and not by_made.categorical:
                by_made, by_why = None, (
                    f"{parent_column}: a group is keyed by the parent column's value, and only the "
                    "levels an enum(...) or a boolean column declares are public"
                )
            if child_made is None or by_made is None:
                not_released.append((what, why or by_why))
                continue
            links.append(
                LinkStat(
                    each.parent,
                    each.parent_columns,
                    each.child,
                    each.child_columns,
                    child_made,
                    by_made,
                    m_child,
                )
            )
            structure[(each.child, column)] = child_made
            structure[(each.parent, parent_column)] = by_made
            extra_read.setdefault(each.child, []).append(column)
            extra_read.setdefault(each.parent, []).append(parent_column)
            note_keys(each.parent, each.parent_columns)
            note_keys(each.child, each.child_columns)
            note_entities(each)
            label = _label_of(contract, each.parent, parent_column) if m_parent == 0 else None
            if label is not None:
                warnings.append(_warning(each.parent, parent_column, label))
    for relation in relations:
        key = (relation.parent, relation.parent_columns, relation.child, relation.child_columns)
        if key not in relationships:
            relationships.append(key)
    for asked_link in request.relationships:
        if not any(
            asked_link.child.table == item.child
            and asked_link.parent.table == item.parent
            and asked_link.child.columns == item.child_columns
            and asked_link.parent.columns == item.parent_columns
            for item in declared
        ):
            not_released.append(
                (
                    f"children per parent of {asked_link.child.table}({', '.join(asked_link.child.columns)}) "
                    f"to {asked_link.parent.table}({', '.join(asked_link.parent.columns)})",
                    "the contract declares no such relationship",
                )
            )
    for fact in request.facts:
        not_released.append((f"fact {fact.name}", _NOT_NOISY))
    not_released.extend(asked_for.unformed(request, workload=WORKLOAD_UNUSED, private=True))

    # -- each reached table's plan, now that its keys and structure columns are known
    tables: List[TablePlan] = []
    for table in tree.order:
        found = columns[table]
        m = reach[table]
        released = found.released
        deltas = tuple(
            (definition, m * backend.rank_sensitivity(definition, floor or 2))
            for definition in ("kendallTau/1", "spearmanRho/1")
            if any(each[0] == definition for each in found.rank_pairs)
        )
        needed = set(each.name for each in released) | set(extra_read.get(table, ()))
        read = tuple(column for column in found.expose_plan.columns if column.name in needed)
        asked = found.asked
        in_request = set() if asked is None else {each.name for each in asked.columns}
        listed = tuple(
            (column.name, column.domain)
            for column in found.expose_plan.columns
            if column.name in in_request
        )
        table_plan = ReleasePlan(
            unit=_unit_plan(unit, table),
            table=table,
            columns=released,
            table_columns=listed,
            other_tables=(),
            read=read,
            not_released=(),
            rho=rho,
            selects=found.selects,
            fractional_bits=bits,
            dimensions=frozenset(
                defaults._dimension_columns(
                    found.expose, [c.name for c in released if c.categorical]
                )
            ),
            eligible=found.eligible,
            rank_pairs=found.rank_pairs,
            row_floor=floor if found.rank_pairs else None,
            rank_deltas=deltas,
            clique_method=clique_method,
            reach=m,
        )
        tables.append(TablePlan(table, m, table_plan, asked is not None))
        if asked is not None:
            not_released.extend(
                _table_not_released(
                    asked, table, frozenset(c.name for c in released), found.why_not, found.skipped
                )
            )
            not_released.extend(found.rank_left_out)
    # the reached tables first, then the others, in the request's order
    fixed: List[Tuple[str, Tuple[Tuple[str, str], ...]]] = []
    for table in sorted((set(request_tables(request)) | set(extra_read)) - set(tree.order)):
        expose_plan = runner.plan_expose(contract, table)
        asked_table = request.table(table)
        wanted = set(extra_read.get(table, ()))
        if asked_table is not None:
            wanted |= {each.name for each in asked_table.columns}
            not_released.append((f"table {table}", _FIXED))
        domains = tuple(
            (column.name, column.domain) for column in expose_plan.columns if column.name in wanted
        )
        if domains:
            fixed.append((table, domains))
    reads: List[Tuple[str, Tuple[runner.ColumnPlan, ...]]] = [
        (each.table, each.plan.read) for each in tables
    ]
    for table in sorted(extra_read):
        if table in tree.order:
            continue
        expose_plan = runner.plan_expose(contract, table)
        names = set(extra_read[table])
        reads.append(
            (table, tuple(column for column in expose_plan.columns if column.name in names))
        )

    chosen = _with_selection_shares(tables, rho)
    plan = FollowReleasePlan(
        unit=unit,
        follows=tree,
        tables=tuple(chosen),
        relations=tuple(relations),
        links=tuple(links),
        rho=rho,
        fractional_bits=bits,
        clique_method=clique_method,
        row_floor=floor,
        reads=tuple(reads),
        fixed=tuple(fixed),
        relationships=tuple(relationships),
        not_released=tuple(not_released),
        warnings=tuple(dict.fromkeys(warnings)),
        structure=structure,
        key_sets=tuple((table, names) for table, sets in used_keys.items() for names in sets),
        parent_sets=tuple(dict.fromkeys(parent_sets)),
        entity_units=tuple(entity_units),
    )
    parts = follow_parts(plan, {})
    if not parts:
        raise _refuse(
            "profile_release_nothing_to_release",
            "nothing the request lists can be released from the tables the unit reaches",
            "list levelHistogram/1 for a required enum column, or marginalHistogram/1 for a "
            "required number, date or timestamp column of a table the unit reaches, or a "
            "relationship's marginalHistogram/1 the unit's reach allows",
            read=True,
        )
    split_budget(rho, plan.selects, len(parts))
    return plan


def request_tables(request: Request) -> List[str]:
    return [each.expose_id for each in request.tables]


def _relation_reason(relationship: runner.Relationship, m_parent: int, m_child: int) -> str:
    if m_parent == 0 and m_child == 0:
        return "the unit reaches neither table, so every row of both is fixed"
    return (
        "the unit reaches the parent table and also reaches the child table by another way, so one "
        "unit's effect on the counts has no bound the profile can state"
    )


def _with_selection_shares(tables: Sequence[TablePlan], rho: Fraction) -> List[TablePlan]:
    """Give each table that chooses cliques its share of the tenth spent choosing them."""
    choosing = [each for each in tables if each.plan.selects]
    if not choosing:
        return list(tables)
    share = _floor_places(rho / 10 / len(choosing), 12)
    try:
        if share <= 0:
            raise ValueError("a share of zero")
        selection.largest_float_not_above(share)
    except ValueError:
        raise _refuse(
            "profile_release_rho_too_small",
            f"--rho {decimal_text(rho)} leaves choosing the cliques of each table a share below "
            "the smallest positive number a float holds, or zero at 12 decimal places",
            "raise --rho, or release fewer tables with cliques",
            read=True,
        ) from None
    return [
        replace(each, plan=replace(each.plan, selection_rho=share)) if each.plan.selects else each
        for each in tables
    ]


def check_selection_available(plan: FollowReleasePlan) -> None:
    """Refuse, before a record is read, a release that chooses cliques without OpenDP."""
    from fluid_build.profiler.dp.release import check_selection_available as check

    for each in plan.tables:
        check(each.plan)


# ---------------------------------------------------------------------------
# The parts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FollowPart:
    """One noisy statistic of the release and the rows whose change it is charged for."""

    table: str
    part: Part
    #: The squared L2 sensitivity of a count table, a relationship or a link; unused for a rank.
    sensitivity: int
    #: The L1 sensitivity of the same; unused for a rank.
    l1: int = 0


def follow_parts(
    plan: FollowReleasePlan, cliques: Mapping[str, Sequence[Sequence[str]]]
) -> Tuple[FollowPart, ...]:
    """Every noisy statistic of the release, once, in document order. The one source of the parts.

    The parts of each table the unit reaches (:func:`~fluid_build.profiler.dp.release.release_parts`
    with the table's cliques), table by table, then each relationship's histogram, then each
    statistic grouped through a link. Every label names its table, so no two parts share a stream.
    """
    parts: List[FollowPart] = []
    for each in plan.tables:
        for part in release_parts(each.plan, cliques.get(each.table, ())):
            parts.append(
                FollowPart(
                    each.table,
                    replace(part, label=f"{each.table}/{part.label}"),
                    each.reach**2,
                    each.reach,
                )
            )
    for relation in plan.relations:
        parts.append(
            FollowPart(
                relation.child,
                Part(
                    RELATION,
                    relation.label,
                    () if relation.group is None else (relation.group.name,),
                    (relation.groups, relation.cells),
                ),
                relation.sensitivity,
                relation.l1,
            )
        )
    for link in plan.links:
        parts.append(
            FollowPart(
                link.child,
                Part(
                    LINK,
                    link.label,
                    (link.column.name, link.by.name),
                    (link.by.size, link.column.size),
                ),
                link.sensitivity,
                link.reach,
            )
        )
    return tuple(parts)


def describe(
    plan: FollowReleasePlan, cliques: Mapping[str, Sequence[Sequence[str]]]
) -> Tuple[Fraction, Tuple[ReleasedTable, ...]]:
    parts = follow_parts(plan, cliques)
    cells = sum(each.part.cells for each in parts)
    if cells > selection.MOST_RELEASE_CELLS:
        raise _refuse(
            "profile_release_too_many_cells",
            "the tables of the release hold more cells than the "
            f"{selection.MOST_RELEASE_CELLS:,} a release may measure",
            "list fewer columns in the request, or follow fewer children per parent",
        )
    labels = [each.part.label for each in parts]
    if len(set(labels)) != len(labels) or any("\x00" in label for label in labels):
        raise _refuse(
            "profile_release_column_name",
            "two parts of the release would draw their noise from one stream, or a name holds a "
            "zero character",
            "rename a column of the contract",
        )
    _, share = split_budget(plan.rho, plan.selects, len(parts))
    tables: List[ReleasedTable] = []
    for each in parts:
        part = each.part
        extra: Dict[str, Any] = {"table": each.table, "sensitivity": each.sensitivity}
        if part.kind == RANK:
            delta = plan.table(each.table).plan.delta(part.definition)
            units = rank_noise.scale_units(share, delta * delta)
            assert plan.row_floor is not None
            extra.update(
                {"definition": part.definition, "row_floor": plan.row_floor, "delta": delta}
            )
        else:
            units = scale_units(share, each.sensitivity)
        tables.append(
            ReleasedTable(
                label=part.label,
                columns=part.columns,
                kind=part.kind,
                sizes=part.sizes,
                scale=decimal_text(Fraction(units, 10**6)),
                scale_units=units,
                share=decimal_text(share),
                **extra,
            )
        )
    return share, tuple(tables)


# ---------------------------------------------------------------------------
# Reading the records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FollowRows:
    """The tables as the release counts them. Nothing here leaves the process."""

    #: Rows read of each table.
    counts: Mapping[str, int]
    #: For each table the unit reaches: the released columns' cells, the rank grids, each row's
    #: content.
    tables: Mapping[str, EncodedRows]
    #: Each row's bytes over every column read, for the priority of truncation.
    contents: Mapping[str, Sequence[bytes]]
    #: The encoded key of each row over a tuple of key columns; ``None``: a null part.
    keys: Mapping[Tuple[str, Tuple[str, ...]], Sequence[Optional[bytes]]]
    #: The cell of each row of a group or link column; ``None``: a null, in no group.
    placed: Mapping[Tuple[str, str], Sequence[Optional[int]]]


def encode_follow_rows(
    plan: FollowReleasePlan,
    read: Mapping[str, sources.SourceColumns],
    backend: StatisticsBackend,
) -> FollowRows:
    """Read each value of each table once and place it; refuse what the release cannot hold.

    A refusal names a table, a column and a row, never a value or how many. Each parent key must
    name one row: a key held by two rows would make the stated sensitivity understate what one
    unit changes. The one exception is the key of a relationship that reaches an entity, which a
    table with a validity window repeats by design: there each entity must follow one parent row
    of the table the unit reaches it through, or the entity would sit in two units.
    """
    exact: Dict[str, Dict[str, List[Any]]] = {}
    counts: Dict[str, int] = {}
    for table, columns in plan.reads:
        expose_plan = runner.ExposePlan(table, columns)
        values = runner._dates_read(expose_plan, read[table]).values
        counts[table] = max((len(column) for column in values.values()), default=0)
        exact[table] = {
            column.name: [
                backend.read_value(column.domain, raw, column=column.name, row_index=index)
                for index, raw in enumerate(values[column.name])
            ]
            for column in columns
        }
    keys: Dict[Tuple[str, Tuple[str, ...]], List[Optional[bytes]]] = {}
    for table, names in plan.key_sets:
        parts = [exact[table][name] for name in names]
        keys[(table, names)] = [
            (
                None
                if any(part[index] is None for part in parts)
                else bounding.encode_values([part[index] for part in parts])
            )
            for index in range(counts[table])
        ]
    for table, names in plan.parent_sets:
        if bounding.repeated_keys(keys[(table, names)])[0]:
            raise ReleaseRefused(
                "profile_release_repeated_key",
                f"a key value of {table!r} ({', '.join(map(repr, names))}) is held by more than "
                "one row, so a row is not one parent: the stated sensitivity needs each parent "
                "key to name one row",
                {"next": "key the relationship by columns that are unique in the parent table"},
            )
    for table, entity, edge_parent, edge_parent_names, edge_child_names in plan.entity_units:
        _one_unit_each(
            table,
            entity,
            keys[(table, entity)],
            keys[(table, edge_child_names)],
            keys[(edge_parent, edge_parent_names)],
        )
    placed: Dict[Tuple[str, str], List[Optional[int]]] = {}
    for (table, name), column in plan.structure.items():
        found = exact[table][name]
        nullable = _released_in(plan, table, name) is None and (table, name) not in _links_of(plan)
        at = [index for index, value in enumerate(found) if value is not None]
        if not nullable and len(at) != len(found):
            index = next(i for i, value in enumerate(found) if value is None)
            raise ReleaseRefused(
                "profile_release_null",
                f"table {table!r} column {name!r} row {index}: no value, and the release needs "
                "one in every row",
                {"next": "fix the records, or the contract's column type, then release"},
            )
        cells = place_values(name, column, [found[i] for i in at], backend, plan.fractional_bits)
        full: List[Optional[int]] = [None] * len(found)
        for index, cell in zip(at, cells, strict=True):
            full[index] = cell
        placed[(table, name)] = full
    encoded: Dict[str, EncodedRows] = {}
    contents: Dict[str, List[bytes]] = {}
    for each in plan.tables:
        table = each.table
        released = {column.name: column for column in each.plan.columns}
        cells_of: Dict[str, List[int]] = {}
        levels: Dict[str, Sequence[str]] = {}
        for name, column in released.items():
            found = exact[table][name]
            for index, value in enumerate(found):
                if value is None:
                    raise ReleaseRefused(
                        "profile_release_null",
                        f"table {table!r} column {name!r} row {index}: no value, and the "
                        "release needs one in every row",
                        {"next": "fix the records, or the contract's column type, then release"},
                    )
            if column.categorical:
                levels[name] = column.levels
            cells_of[name] = place_values(name, column, found, backend, plan.fractional_bits)
        grid: Dict[str, List[int]] = {}
        if each.plan.rank_pairs:
            encode = backend.encoder(plan.fractional_bits)
            for name in dict.fromkeys(n for _, a, b in each.plan.rank_pairs for n in (a, b)):
                grid[name] = [encode(Fraction(value)) for value in exact[table][name]]
        read_names = sorted(exact[table])
        rows_bytes = [
            bounding.encode_values([exact[table][name][index] for name in read_names])
            for index in range(counts[table])
        ]
        contents[table] = rows_bytes
        encoded[table] = EncodedRows(
            cells=cells_of,
            units=[None] * counts[table],
            contents=rows_bytes,
            levels=levels,
            grid=grid,
        )
    return FollowRows(counts=counts, tables=encoded, contents=contents, keys=keys, placed=placed)


def _one_unit_each(
    table: str,
    entity: Tuple[str, ...],
    entities: Sequence[Optional[bytes]],
    follows: Sequence[Optional[bytes]],
    parents: Sequence[Optional[bytes]],
) -> None:
    """Refuse a table whose entity follows more than one parent row of the table above it.

    *entities* and *follows* are each row's entity key and its key into the parent table of the
    edge the unit reaches the table through, *parents* the keys of that table's rows. A row that
    follows no row (a null key, a key no row holds) is dropped by the truncation, so it is none
    of an entity's rows here. An entity whose rows follow two rows would be in two units, and
    removing one unit would change the other's count of it: the stated sensitivity needs one
    unit for each entity. Names the table and the columns, never a value or a row.
    """
    held = {key for key in parents if key is not None}
    seen: Dict[bytes, bytes] = {}
    for key, follow in zip(entities, follows, strict=True):
        if key is None or follow is None or follow not in held:
            continue
        if seen.setdefault(key, follow) != follow:
            raise ReleaseRefused(
                "profile_release_entity_units",
                f"an entity of {table!r} ({', '.join(map(repr, entity))}) has versions that follow "
                "different rows of the table the unit reaches it through, so one entity would be "
                "in more than one unit and the stated sensitivity would not hold",
                {
                    "next": "follow the table by its natural entity, or keep the column stable "
                    "per entity in the data"
                },
            )


def _released_in(plan: FollowReleasePlan, table: str, name: str) -> Optional[ReleasedColumn]:
    each = next((item for item in plan.tables if item.table == table), None)
    if each is None:
        return None
    return next((column for column in each.plan.columns if column.name == name), None)


def _links_of(plan: FollowReleasePlan) -> FrozenSet[Tuple[str, str]]:
    """The ``(table, column)`` of each child column a link statistic counts: never null."""
    return frozenset((link.child, link.column.name) for link in plan.links)


# ---------------------------------------------------------------------------
# The release
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FollowRelease:
    """A drawn release: the document, its digest and what a report may say about it."""

    document: Dict[str, Any]
    canonical: bytes
    digest: str
    #: Every part, in document order.
    tables: Tuple[ReleasedTable, ...]
    #: The cliques released, by table.
    cliques: Mapping[str, Tuple[Tuple[str, ...], ...]]
    #: The cliques the selection chose, by table; ``None`` when no selection ran.
    selected: Optional[Mapping[str, Tuple[Tuple[str, ...], ...]]]
    selection_rho: Fraction
    share: Fraction

    @property
    def measurement_rho(self) -> Fraction:
        return self.share * len(self.tables)


def _children_cells(
    plan: RelationStat, counts: Sequence[int], backend: StatisticsBackend, bits: int
) -> List[int]:
    """The bin each parent's number of children falls in; refuse a count beyond the ladder."""
    if not counts:
        return []
    if plan.cap is not None:
        if max(counts) > plan.cap:  # truncation keeps at most the cap: a defect, not a record
            raise ReleaseRefused(
                "profile_release_defect",
                f"defect: a parent of {plan.spelled} holds more children than the cap after "
                "truncation",
                {"next": "report it with your forge-cli version"},
            )
        return list(counts)
    if max(counts) > plan.edges[-1]:
        raise ReleaseRefused(
            "profile_release_children_beyond_ladder",
            f"a parent of {plan.spelled} holds more than {plan.edges[-1]:,} children, past the "
            "last bin of the public ladder of children per parent",
            {"next": "follow the relationship with a cap, so its bins are 0 to the cap"},
        )
    return backend.bin_cells(
        [Fraction(each) for each in counts], [Fraction(each) for each in plan.edges], bits
    )


def _noisy(counts: Sequence[int], units: int, seed: bytes, label: str) -> Tuple[Fraction, ...]:
    sigma2 = Fraction(units**2, 10**12)
    source = SeededSource(seed, label)
    return tuple(Fraction(count + sample_discrete_gaussian(sigma2, source)) for count in counts)


def _integer_text(value: Fraction) -> str:
    return decimal_text(value)


def _relation_counts(
    plan: FollowReleasePlan,
    relation: RelationStat,
    rows: FollowRows,
    kept: Mapping[str, Sequence[int]],
    backend: StatisticsBackend,
) -> List[int]:
    """The cells of one relationship histogram, group by group, before noise.

    A parent is a row of the parent table, or, where the relationship reaches an entity, each
    distinct non-null entity key among its kept rows. An entity is in the group of its rows'
    value of the grouping column when they all agree, and in none when they do not.
    """
    parents = rows.keys[(relation.parent, relation.parent_columns)]
    children = rows.keys[(relation.child, relation.child_columns)]
    parent_rows: Sequence[int] = (
        kept[relation.parent] if relation.parent in kept else range(rows.counts[relation.parent])
    )
    child_rows: Sequence[int] = (
        kept[relation.child] if relation.child in kept else range(rows.counts[relation.child])
    )
    tally = Counter(key for at in child_rows if (key := children[at]) is not None)
    if relation.entity:
        entities = list(
            dict.fromkeys(key for at in parent_rows if (key := parents[at]) is not None)
        )
        counts = [tally.get(key, 0) for key in entities]
    else:
        counts = [
            tally.get(key, 0) if (key := parents[at]) is not None else 0 for at in parent_rows
        ]
    cells = _children_cells(relation, counts, backend, plan.fractional_bits)
    width = relation.cells
    flat = [0] * (relation.groups * width)
    if relation.group is None:
        for cell in cells:
            flat[cell] += 1
    else:
        placed = rows.placed[(relation.parent, relation.group.name)]
        if relation.entity:
            of_entity: Dict[bytes, Optional[int]] = {}
            for at in parent_rows:
                key = parents[at]
                if key is None:
                    continue
                if key in of_entity and of_entity[key] != placed[at]:
                    of_entity[key] = None
                else:
                    of_entity.setdefault(key, placed[at])
            groups: List[Optional[int]] = [of_entity[key] for key in entities]
        else:
            groups = [placed[at] for at in parent_rows]
        for group, cell in zip(groups, cells, strict=True):
            if group is not None:
                flat[group * width + cell] += 1
    return flat


def _link_counts(link: LinkStat, rows: FollowRows, kept: Mapping[str, Sequence[int]]) -> List[int]:
    """The cells of one statistic grouped through a link, group by group, before noise."""
    parents = rows.keys[(link.parent, link.parent_columns)]
    children = rows.keys[(link.child, link.child_columns)]
    parent_rows: Sequence[int] = (
        kept[link.parent] if link.parent in kept else range(rows.counts[link.parent])
    )
    child_rows: Sequence[int] = kept[link.child]
    by = rows.placed[(link.parent, link.by.name)]
    reached: Dict[bytes, Optional[int]] = {}
    for at in parent_rows:
        key = parents[at]
        if key is None:
            continue
        if key in reached and reached[key] != by[at]:
            reached[key] = None
        else:
            reached.setdefault(key, by[at])
    cells = rows.placed[(link.child, link.column.name)]
    width = link.column.size
    flat = [0] * (link.by.size * width)
    for at in child_rows:
        key = children[at]
        group = None if key is None else reached.get(key)
        cell = cells[at]
        if group is not None and cell is not None:
            flat[group * width + cell] += 1
    return flat


def truncated(plan: FollowReleasePlan, rows: FollowRows, seed: bytes) -> Dict[str, List[int]]:
    """The rows kept of each table the unit reaches, once every followed relationship is truncated."""
    follows = plan.follows
    return keep_rows(
        follows,
        rows.counts,
        rows.contents,
        {edge.child: rows.keys[(edge.parent, edge.spec.parent_columns)] for edge in follows.edges},
        {edge.child: rows.keys[(edge.child, edge.spec.child_columns)] for edge in follows.edges},
        seed,
    )


def draw_follow(
    plan: FollowReleasePlan,
    rows: FollowRows,
    seed: bytes,
    backend: StatisticsBackend,
    *,
    cliques: Optional[Mapping[str, Sequence[Sequence[str]]]] = None,
    select: Optional[Callable[..., Sequence[Sequence[str]]]] = None,
    structure: Optional[Callable[..., Sequence[Sequence[str]]]] = None,
    producer: Optional[str] = None,
    selection_version: Optional[str] = None,
) -> FollowRelease:
    """Truncate, choose the cliques, count, add noise, make each table's counts agree, write.

    With *cliques* (the record's, on a re-derivation) no selection runs.
    """
    kept = truncated(plan, rows, seed)
    chosen: Optional[Dict[str, Tuple[Tuple[str, ...], ...]]] = None
    released: Dict[str, Tuple[Tuple[str, ...], ...]] = {}
    if cliques is not None:
        released = {table: tuple(tuple(c) for c in found) for table, found in cliques.items()}
    else:
        chosen = {}
        for each in plan.tables:
            if not each.plan.selects:
                continue
            picked = _selected(each.plan, rows.tables[each.table], kept[each.table], select)
            chosen[each.table] = picked
            wide = structure if structure is not None else selection.release_structure
            try:
                joined = wide(
                    [column.name for column in each.plan.eligible],
                    [column.size for column in each.plan.eligible],
                    picked,
                )
            except ValueError:
                raise ReleaseRefused(
                    "profile_release_structure_too_large",
                    "the cliques chosen hold more cells than a release may measure "
                    f"({selection.MOST_RELEASE_CELLS:,}). Choosing them spent the selection "
                    "share of this release's budget on these records, and nothing was written",
                    {
                        "next": "list fewer columns in the request, so the cliques chosen hold "
                        "fewer cells; releasing again spends a new loss on the records"
                    },
                ) from None
            released[each.table] = tuple(tuple(c) for c in joined)
    if chosen is not None and not chosen:
        chosen = None
    share, tables = describe(plan, released)
    measured = measure_counts(plan, rows, kept, tables, backend)
    values_of: Dict[str, Tuple[str, ...]] = {}
    for each in plan.tables:
        mine = [item for item in tables if item.table == each.table and item.kind in _TABLE_KINDS]
        counted = [item for item in mine if item.kind != RANK]
        if counted:
            values_of.update(_count_values(counted, measured, seed))
        for item in mine:
            if item.kind == RANK:
                first, second = item.columns
                grid = rows.tables[each.table].grid
                pairs = [(grid[first][i], grid[second][i]) for i in kept[each.table]]
                exact = backend.rank_value(item.definition, pairs, item.row_floor)
                noised = rank_noise.release_value(
                    exact, item.delta, item.scale_units, SeededSource(seed, item.label)
                )
                values_of[item.label] = (decimal_text(noised),)
    for item in tables:
        if item.kind in (RELATION, LINK):
            values_of[item.label] = tuple(
                _integer_text(value)
                for value in _noisy(measured[item.label], item.scale_units, seed, item.label)
            )
    if selection_version is None and cliques is None and plan.selects:
        selection_version = _version("opendp")
    built = _document(plan, tables, values_of, producer, selection_version)
    canonical = document.canonical_bytes(built)
    backend.check_document(canonical.decode("utf-8"))
    return FollowRelease(
        document=built,
        canonical=canonical,
        digest=document.digest_of(canonical),
        tables=tables,
        cliques=released,
        selected=chosen,
        selection_rho=sum((selection_share(each.plan) for each in plan.tables), Fraction(0)),
        share=share,
    )


_TABLE_KINDS = (CLIQUE, LEVELS, BINS, RANK)


def measure_counts(
    plan: FollowReleasePlan,
    rows: FollowRows,
    kept: Mapping[str, Sequence[int]],
    tables: Sequence[ReleasedTable],
    backend: StatisticsBackend,
) -> Dict[str, List[int]]:
    """The exact cells of every count table, relationship histogram and link statistic, by label.

    These are the numbers one unit moves by at most the sensitivity each part is charged; rank
    correlations are not counts and are left out.
    """
    found: Dict[str, List[int]] = {}
    relations = {relation.label: relation for relation in plan.relations}
    links = {link.label: link for link in plan.links}
    for table in tables:
        if table.kind in (CLIQUE, LEVELS, BINS):
            encoded = rows.tables[table.table]
            rows_kept = kept[table.table]
            cell = [0] * len(rows_kept)
            for name, size in zip(table.columns, table.sizes, strict=True):
                index = encoded.cells[name]
                cell = [value * size + index[i] for value, i in zip(cell, rows_kept, strict=True)]
            counts = [0] * table.cells
            for value in cell:
                counts[value] += 1
            found[table.label] = counts
        elif table.kind == RELATION:
            found[table.label] = _relation_counts(plan, relations[table.label], rows, kept, backend)
        elif table.kind == LINK:
            found[table.label] = _link_counts(links[table.label], rows, kept)
    return found


def _count_values(
    counted: Sequence[ReleasedTable], measured: Mapping[str, Sequence[int]], seed: bytes
) -> Dict[str, Tuple[str, ...]]:
    """The noisy counts of one table's count tables, made to agree, as decimal text."""
    sigma2 = Fraction(counted[0].scale_units ** 2, 10**12)
    count_tables: List[consistency.CountTable] = []
    for table in counted:
        source = SeededSource(seed, table.label)
        values = tuple(
            Fraction(count + sample_discrete_gaussian(sigma2, source))
            for count in measured[table.label]
        )
        count_tables.append(
            consistency.CountTable(table.label, table.columns, table.sizes, values, sigma2)
        )
    try:
        forest = consistency.junction_forest(count_tables)
        agreed = consistency.make_consistent(count_tables)
        texts = consistency.to_decimal_text(count_tables, agreed, forest)
    except ValueError as error:
        raise ReleaseRefused(
            "profile_release_not_a_forest",
            f"the tables of the release cannot be made to agree: {error}",
            {"next": "report it with your forge-cli version"},
        ) from None
    _agree(counted, texts)
    return {table.label: cells for table, cells in zip(counted, texts, strict=True)}


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def _ends(table: str, columns: Sequence[str]) -> Dict[str, Any]:
    return {"table": table, "columns": list(columns)}


def _relation_entry(
    relation: RelationStat, item: ReleasedTable, cells: Sequence[str]
) -> Dict[str, Any]:
    width = relation.cells
    bins = [str(edge) for edge in relation.edges]
    entry: Dict[str, Any] = {
        "definition": "marginalHistogram/1",
        "bins": bins,
        "sampleSize": {"min": 0},
        "disclosure": RULE_NAME,
        "noise": _noise_block(item),
    }
    if relation.group is None:
        entry["groups"] = [{"group": "", "status": "F", "noisyCounts": list(cells)}]
        return entry
    column = relation.group
    keys = list(column.levels) if column.categorical else list(column.edge_texts[:-1])
    entry["groups"] = [
        {
            "group": key,
            "status": "F",
            "noisyCounts": list(cells[position * width : (position + 1) * width]),
        }
        for position, key in enumerate(keys)
    ]
    if column.categorical:
        entry["groupBy"] = column.name
    else:
        entry["groupByBins"] = {"column": column.name, "bins": list(column.edge_texts)}
    return entry


def _link_entry(link: LinkStat, item: ReleasedTable, cells: Sequence[str]) -> Dict[str, Any]:
    width = link.column.size
    entry: Dict[str, Any] = {
        "definition": "levelHistogram/1" if link.column.categorical else "marginalHistogram/1",
        "sampleSize": {"min": 0},
        "disclosure": RULE_NAME,
        "noise": _noise_block(item),
        "groupByLink": {
            "parent": _ends(link.parent, link.parent_columns),
            "columns": list(link.child_columns),
            "column": link.by.name,
        },
        "groups": [
            {
                "group": key,
                "status": "F",
                "noisyCounts": list(cells[position * width : (position + 1) * width]),
            }
            for position, key in enumerate(link.by.levels)
        ],
    }
    if link.column.categorical:
        entry["levels"] = list(link.column.levels)
    else:
        entry["bins"] = list(link.column.edge_texts)
    return entry


def _document(
    plan: FollowReleasePlan,
    tables: Sequence[ReleasedTable],
    values_of: Mapping[str, Sequence[str]],
    producer: Optional[str],
    selection_version: Optional[str],
) -> Dict[str, Any]:
    domains: Dict[str, Dict[str, str]] = {}
    for table, columns in plan.reads:
        domains[table] = {column.name: column.domain for column in columns}
    out_tables: Dict[str, Any] = {}
    for each in plan.tables:
        mine = [item for item in tables if item.table == each.table and item.kind in _TABLE_KINDS]
        block = _table_block(each.plan, mine, values_of, selection_version, states_selection=True)
        for name, domain in domains.get(each.table, {}).items():
            block["columns"].setdefault(name, {"domain": domain})
        out_tables[each.table] = block
    by_label = {item.label: item for item in tables}
    for link in plan.links:
        item = by_label[link.label]
        column = out_tables[link.child]["columns"].setdefault(
            link.column.name, {"domain": link.column.domain}
        )
        column.setdefault("statistics", []).append(_link_entry(link, item, values_of[item.label]))
    for name, listed in plan.fixed:
        out_tables[name] = {"columns": {column: {"domain": domain} for column, domain in listed}}
    relationships: List[Dict[str, Any]] = []
    for parent, parent_columns, child, child_columns in plan.relationships:
        statistics = [
            _relation_entry(relation, by_label[relation.label], values_of[relation.label])
            for relation in plan.relations
            if (relation.parent, relation.parent_columns, relation.child, relation.child_columns)
            == (parent, parent_columns, child, child_columns)
        ]
        relationships.append(
            {
                "parent": _ends(parent, parent_columns),
                "child": _ends(child, child_columns),
                "statistics": statistics,
            }
        )
    if producer is None:
        from fluid_build import __version__

        producer = document.producer_uri(__version__)
    return {
        "_producer": producer,
        "_schemaURL": document.SCHEMA_URL_1_4_0,
        "encoding": {"fractionalBits": plan.fractional_bits, "rounding": "floor"},
        "disclosureRules": {RULE_NAME: {}},
        "privacy": {
            "measure": "zeroConcentratedDP",
            "privacyLoss": {"rho": decimal_text(plan.rho)},
            "privacyUnit": {
                "expose": plan.unit,
                "follows": [
                    {
                        "parent": _ends(edge.parent, edge.spec.parent_columns),
                        "child": _ends(edge.child, edge.spec.child_columns),
                        "maxChildren": edge.max_children,
                    }
                    for edge in plan.follows.edges
                ],
            },
            "publicStructure": True,
        },
        "tables": out_tables,
        "relationships": relationships,
    }


# ---------------------------------------------------------------------------
# The release record
# ---------------------------------------------------------------------------


def unit_block(plan: FollowReleasePlan) -> Dict[str, Any]:
    """The unit as the release record states it."""
    return {
        "expose": plan.unit,
        "mode": "follows",
        "follows": [
            {
                "parent": _ends(edge.parent, edge.spec.parent_columns),
                "child": _ends(edge.child, edge.spec.child_columns),
                "maxChildren": edge.max_children,
            }
            for edge in plan.follows.edges
        ],
    }


def _options_block(plan: FollowReleasePlan) -> Dict[str, Any]:
    found: Dict[str, Any] = {}
    if plan.selects and plan.clique_method != "aim":
        found["cliqueMethod"] = plan.clique_method
    if plan.row_floor is not None:
        found["rowFloor"] = plan.row_floor
    return found


def build_follow_record(
    plan: FollowReleasePlan,
    release: FollowRelease,
    seed: bytes,
    *,
    request_bytes: bytes,
    contract_digest: Optional[str],
) -> ReleaseRecord:
    """The record that lets the release be drawn again inside the data owner's boundary."""
    selection_block: Optional[Dict[str, Any]] = None
    if release.selected is not None:
        selection_block = {
            "rho": decimal_text(release.selection_rho),
            "tables": {
                table: {
                    "rho": decimal_text(selection_share(plan.table(table).plan)),
                    "cliques": [list(clique) for clique in found],
                }
                for table, found in release.selected.items()
            },
        }
    return ReleaseRecord(
        seed=seed,
        profile_digest=release.digest,
        request_digest="sha256:" + hashlib.sha256(request_bytes).hexdigest(),
        contract_digest=contract_digest,
        unit=unit_block(plan),
        privacy_loss={"rho": decimal_text(plan.rho)},
        selection=selection_block,
        tables=tuple(table.as_record() for table in release.tables),
        options=_options_block(plan),
        versions={
            "forge-cli": _version("data-product-forge"),
            "flux-stats": _version("flux-stats"),
            "opendp": _version("opendp") if release.selected is not None else None,
        },
    )


def check_follow_record(
    plan: FollowReleasePlan, record: ReleaseRecord
) -> Dict[str, Tuple[Tuple[str, ...], ...]]:
    """The cliques of a record, by table, once the record is shown to state what the plan computes.

    Nothing in the record is trusted: the unit and its follows, the selection shares and every
    part's label, table, columns, scale and share are computed again from the plan, the record's
    rho and its cliques, and each must equal what the record states.
    """
    if dict(record.unit) != unit_block(plan):
        raise _mismatch("its privacy unit differs")
    if dict(record.privacy_loss) != {"rho": decimal_text(plan.rho)}:
        raise _mismatch("its privacy loss differs")
    states = record.selection
    chooses = [each for each in plan.tables if each.plan.selects]
    by_table: Dict[str, Tuple[Tuple[str, ...], ...]] = {}
    if chooses:
        listed = states.get("tables") if isinstance(states, Mapping) else None
        if (
            not isinstance(states, Mapping)
            or set(states) != {"rho", "tables"}
            or states.get("rho")
            != decimal_text(sum((selection_share(each.plan) for each in chooses), Fraction(0)))
            or not isinstance(listed, Mapping)
            or set(listed) != {each.table for each in chooses}
        ):
            raise _mismatch("its selection differs")
        for each in chooses:
            entry = listed[each.table]
            if (
                not isinstance(entry, Mapping)
                or entry.get("rho") != decimal_text(selection_share(each.plan))
                or not isinstance(entry.get("cliques"), list)
            ):
                raise _mismatch("its selection differs")
    elif states is not None:
        raise _mismatch("it states a selection, and this release chooses no cliques")
    if dict(record.options) != _options_block(plan):
        raise _mismatch("its options differ")
    for stated in record.tables:
        columns = stated.get("columns") if isinstance(stated, Mapping) else None
        owner = stated.get("table") if isinstance(stated, Mapping) else None
        if (
            not isinstance(columns, list)
            or not all(isinstance(name, str) for name in columns)
            or not isinstance(owner, str)
        ):
            raise _mismatch("a table's columns are not names")
        if stated.get("kind") in (RANK, RELATION, LINK):
            continue
        if len(columns) >= 2:
            by_table[owner] = (*by_table.get(owner, ()), tuple(columns))
    _, expected = describe(plan, by_table)
    if [dict(item) for item in record.tables] != [table.as_record() for table in expected]:
        raise _mismatch("its tables, scales or shares differ")
    return by_table
