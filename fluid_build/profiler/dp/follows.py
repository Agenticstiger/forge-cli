# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A privacy unit that follows its child rows: the relationships, the reach and the truncation.

One unit is one row of the unit's expose together with, recursively, the rows of child tables
that reference a row of the unit through a followed relationship (PROFILE-SPEC section 3a,
format 1-4-0). Each followed relationship carries a public cap on its children per parent row,
and the followed relationships form a tree rooted at the expose. The registry's reader checks the
tree and holds ``m_T``, the rows of table ``T`` one unit contributes
(:meth:`~fluid_build.profiler._stats.StatisticsBackend.follow_reach`); nothing here repeats that
check.

:func:`plan_follows` matches each ``--follow`` entry to a relationship the contract declares and
asks the registry for the reach, before any record is read. :func:`keep_rows` then truncates the
tables before anything is measured. For each followed relationship it keeps at most ``c`` children
of each parent row, the ``c`` with the smallest keyed priority
(:func:`~fluid_build.profiler.dp.bounding.bound_rows`), and it reads only that parent's own
children, so the rows kept for a unit depend on the seed and on that unit alone, never on another
unit or on the order of the records. A child dropped takes its descendants with it, and a row of a
followed table that follows no kept row is dropped.

Nothing here prints or records anything that depends on the records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from fluid_build.profiler import _stats, runner
from fluid_build.profiler._stats import FollowEntry, StatisticsBackend
from fluid_build.profiler.dp import bounding
from fluid_build.profiler.dp.release import _refuse

__all__ = [
    "FollowEdge",
    "FollowPlan",
    "FollowSpec",
    "keep_rows",
    "parse_follow",
    "plan_follows",
]

_SPEC = re.compile(r"(?P<parent>[^=]+)=(?P<child>[^=:]+):(?P<cap>[0-9]+)\Z")


@dataclass(frozen=True)
class FollowSpec:
    """One ``--follow PARENT_TABLE.COL[,COL]=CHILD_TABLE.COL[,COL]:MAX``, as the flag spells it."""

    parent_table: str
    parent_columns: Tuple[str, ...]
    child_table: str
    child_columns: Tuple[str, ...]
    max_children: int

    def text(self) -> str:
        return (
            f"{self.parent_table}.{','.join(self.parent_columns)}="
            f"{self.child_table}.{','.join(self.child_columns)}:{self.max_children}"
        )

    def entry(self) -> FollowEntry:
        return FollowEntry(
            self.parent_table,
            self.parent_columns,
            self.child_table,
            self.child_columns,
            self.max_children,
        )


def _end(text: str, side: str) -> Tuple[str, Tuple[str, ...]]:
    table, dot, columns = text.partition(".")
    names = tuple(part.strip() for part in columns.split(","))
    if not table.strip() or not dot or not all(names) or len(set(names)) != len(names):
        raise ValueError(
            f"the {side} of a --follow is TABLE.COL[,COL] with distinct column names, not {text!r}"
        )
    return table.strip(), names


def parse_follow(text: str) -> FollowSpec:
    """Read ``PARENT_TABLE.COL[,COL]=CHILD_TABLE.COL[,COL]:MAX``; raise :class:`ValueError`."""
    found = _SPEC.match(text.strip())
    if found is None:
        raise ValueError(
            f"{text!r} is not PARENT_TABLE.COL[,COL]=CHILD_TABLE.COL[,COL]:MAX, with MAX a whole number"
        )
    parent_table, parent_columns = _end(found["parent"], "parent")
    child_table, child_columns = _end(found["child"], "child")
    if len(parent_columns) != len(child_columns):
        raise ValueError(f"{text!r}: a parent and a child key have the same number of columns")
    cap = int(found["cap"], 10)
    if cap < 1:
        raise ValueError(f"{text!r}: MAX, the cap on children per parent row, is at least 1")
    return FollowSpec(parent_table, parent_columns, child_table, child_columns, cap)


@dataclass(frozen=True)
class FollowEdge:
    """One followed relationship, matched to the relationship the contract declares."""

    spec: FollowSpec
    #: The declared relationship's name.
    name: str

    @property
    def parent(self) -> str:
        return self.spec.parent_table

    @property
    def child(self) -> str:
        return self.spec.child_table

    @property
    def max_children(self) -> int:
        return self.spec.max_children

    @property
    def label(self) -> str:
        """The label of this edge's priorities: it names the relationship, never a position."""
        return f"follow/{self.spec.text()}"


@dataclass(frozen=True)
class FollowPlan:
    """The followed relationships, in an order that puts each parent before its children."""

    unit: str
    edges: Tuple[FollowEdge, ...]
    #: ``m_T`` of every table the unit reaches, the expose included.
    reach: Mapping[str, int]
    #: The tables the unit reaches: the expose first, each parent before its children.
    order: Tuple[str, ...]

    def edge_into(self, table: str) -> Optional[FollowEdge]:
        return next((each for each in self.edges if each.child == table), None)

    def edge_for(
        self, parent: Tuple[str, Tuple[str, ...]], child: Tuple[str, Tuple[str, ...]]
    ) -> Optional[FollowEdge]:
        return next(
            (
                each
                for each in self.edges
                if (each.parent, each.spec.parent_columns) == parent
                and (each.child, each.spec.child_columns) == child
            ),
            None,
        )


def _declared(contract: Mapping[str, Any], spec: FollowSpec) -> Optional[str]:
    """The name of the relationship *spec* matches in the contract, or ``None``."""
    exposes = bounding._expose_map(contract)
    if spec.parent_table not in exposes or spec.child_table not in exposes:
        return None
    for entry in bounding._semantics_list(exposes[spec.child_table], "relationships"):
        if not isinstance(entry, Mapping) or entry.get("to") != spec.parent_table:
            continue
        if (
            bounding._column_list(entry.get("fromColumns")) == spec.child_columns
            and bounding._column_list(entry.get("toColumns")) == spec.parent_columns
        ):
            name = entry.get("name")
            return name if isinstance(name, str) and name else "?"
    return None


def _refused(spec: FollowSpec, why: str) -> Exception:
    return _refuse(
        "profile_release_follow_refused",
        f"--follow {spec.text()}: {why}",
        "follow relationships the contract declares, as a tree rooted at the privacy unit",
        read=True,
    )


def plan_follows(
    contract: Mapping[str, Any],
    unit: str,
    specs: Sequence[FollowSpec],
    backend: StatisticsBackend,
) -> FollowPlan:
    """Match each entry to a declared relationship and have the registry check the tree.

    Refuses, before any record is read and naming the entry, one the contract does not declare, one
    whose keys are columns fluid profile does not read in one domain, and any set that is not a
    tree rooted at *unit* (a table with two paths into it, a parent the unit does not reach, a
    cycle, the expose as a child).
    """
    if unit not in bounding._expose_map(contract):
        raise _refuse(
            "profile_release_unit_unknown",
            f"the privacy unit {unit!r} is not an expose of the contract",
            "name an expose of the contract",
            read=True,
        )
    names: List[str] = []
    for spec in specs:
        name = _declared(contract, spec)
        if name is None:
            raise _refused(
                spec,
                "the contract declares no relationship from "
                f"{spec.child_table}({', '.join(spec.child_columns)}) to "
                f"{spec.parent_table}({', '.join(spec.parent_columns)})",
            )
        names.append(name)
        unread = runner._keys_unread(
            runner.plan_expose(contract, spec.child_table),
            runner.plan_expose(contract, spec.parent_table),
            spec.child_columns,
            spec.parent_columns,
        )
        if unread is not None:
            raise _refused(spec, unread)
    try:
        reach = backend.follow_reach(unit, [each.entry() for each in specs])
    except _stats.FollowRefused as refused:
        match = re.match(r"follows\[(\d+)\]", str(refused))
        spec = specs[int(match[1])] if match and int(match[1]) < len(specs) else specs[0]
        raise _refused(spec, re.sub(r"^follows\[\d+\](\.\w+)?: ", "", str(refused))) from None
    edges = tuple(FollowEdge(spec, name) for spec, name in zip(specs, names, strict=True))
    ordered: List[FollowEdge] = []
    order: List[str] = [unit]
    waiting = list(edges)
    while waiting:
        ready = [each for each in waiting if each.parent in order]
        if not ready:  # the registry accepted a tree, so every parent is reached in turn
            raise _refuse(
                "profile_release_follow_refused",
                "the followed relationships are not a tree rooted at the privacy unit",
                "follow relationships as a tree rooted at the privacy unit",
                read=True,
            )
        for each in ready:
            ordered.append(each)
            order.append(each.child)
            waiting.remove(each)
    return FollowPlan(unit, tuple(ordered), dict(reach), tuple(order))


def keep_rows(
    plan: FollowPlan,
    counts: Mapping[str, int],
    contents: Mapping[str, Sequence[bytes]],
    parent_keys: Mapping[str, Sequence[Optional[bytes]]],
    child_keys: Mapping[str, Sequence[Optional[bytes]]],
    seed: bytes,
) -> Dict[str, List[int]]:
    """The rows kept of each table the unit reaches, ascending, after truncation.

    *counts* are the rows of each table and *contents* each row's bytes (every column read, so two
    rows that differ anywhere differ here). For the edge into table ``C`` (keyed by *C*),
    *parent_keys* is the encoded key of each parent row, and *child_keys* the encoded foreign key of
    each child row; ``None`` is a key with a null part. The expose keeps every row. A child row is a
    candidate only when its key is that of a kept parent row, and of the candidates of one parent
    key at most ``c`` are kept, by keyed priority. The priority of a row depends on the seed, the
    edge, the parent key and the row's own bytes: no other row.
    """
    kept: Dict[str, List[int]] = {plan.unit: list(range(counts[plan.unit]))}
    for edge in plan.edges:
        into = edge.child
        held: Set[bytes] = {
            key for at in kept[edge.parent] if (key := parent_keys[into][at]) is not None
        }
        candidates = [
            at for at, key in enumerate(child_keys[into]) if key is not None and key in held
        ]
        chosen = bounding.bound_rows(
            [child_keys[into][at] for at in candidates],
            [contents[into][at] for at in candidates],
            seed,
            edge.max_children,
            label=edge.label,
        )
        kept[into] = sorted(candidates[each] for each in chosen)
    return kept
